#!/usr/bin/env python3
"""刷机包格式 fwpack/1 —— 读写实现。

一个刷机包 = 一份 manifest.json + 若干 bin 文件，有两种承载形式：

  1. 单文件  xxx.fwpack        ZIP 封装（分发用：不会漏文件、可校验完整性）
  2. 文件夹  xxx/              manifest.json + bin（开发用：改一个 bin 立刻能试）

manifest 结构（两种形式完全相同）：

    {
      "format":  "fwpack/1",
      "id":      "rlcd-screen",          # 唯一标识，工具内部用
      "name":    "RLCD 桌面信息屏",
      "version": "1.2.0",
      "desc":    "八页面 · 真实接口数据",
      "tag":     "桌面屏",                # 可选，列表里的小标签
      "chip":    "esp32s3",              # 目标芯片
      "builtAt": "2026-11-01T12:00:00Z",
      "flash":   { "mode": "dio", "freq": "80m", "size": "16MB" },
      "files": [
        { "offset": "0x0",     "path": "bootloader.bin",
          "sha256": "a3f2...", "role": "bootloader" },
        ...
      ]
    }

`files` 必须按 offset 升序；`sha256` 用于刷写前校验，防止拿到损坏的包。
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

FORMAT = "fwpack/1"
PACK_EXT = ".fwpack"

# 各角色在 flash 里的标准偏移（ESP-IDF 默认布局）
ROLE_OFFSETS = {
    "bootloader": 0x0,
    "partition": 0x8000,
    "app": 0x10000,
}


class PackError(Exception):
    """刷机包损坏或格式不符。"""


@dataclass
class PackFile:
    offset: int
    path: str
    size: int
    sha256: str
    role: str = "app"
    # 只有从目录/ZIP 载入后才填充
    data: Optional[bytes] = field(default=None, repr=False)

    @property
    def hex_offset(self) -> str:
        return "0x%06X" % self.offset


@dataclass
class FirmwarePack:
    id: str
    name: str
    version: str
    chip: str
    files: List[PackFile]
    desc: str = ""
    tag: str = ""
    built_at: str = ""
    flash_mode: str = "dio"
    flash_freq: str = "80m"
    flash_size: str = "16MB"
    # 来源：ZIP 路径或目录路径
    source: str = ""

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def display(self) -> str:
        v = f" v{self.version}" if self.version else ""
        return f"{self.name}{v}"

    def to_manifest(self) -> dict:
        return {
            "format": FORMAT,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "desc": self.desc,
            "tag": self.tag,
            "chip": self.chip,
            "builtAt": self.built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "flash": {
                "mode": self.flash_mode,
                "freq": self.flash_freq,
                "size": self.flash_size,
            },
            "files": [
                {
                    "offset": f.hex_offset,
                    "path": f.path,
                    "size": f.size,
                    "sha256": f.sha256,
                    "role": f.role,
                }
                for f in self.files
            ],
        }


# ---------------------------------------------------------------- 读取

def _parse_manifest(raw: str, source: str) -> FirmwarePack:
    try:
        m = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PackError(f"manifest.json 不是合法 JSON：{e}") from e

    fmt = m.get("format", "")
    if not fmt.startswith("fwpack/"):
        raise PackError(f"不是刷机包（format={fmt!r}，应为 {FORMAT!r}）")

    files = []
    for item in m.get("files") or []:
        try:
            off = int(str(item["offset"]), 16)
        except (KeyError, ValueError):
            raise PackError(f"文件条目的 offset 无效：{item!r}")
        files.append(PackFile(
            offset=off,
            path=item["path"],
            size=int(item.get("size", 0)),
            sha256=item.get("sha256", ""),
            role=item.get("role", "app"),
        ))
    if not files:
        raise PackError("manifest 里没有任何文件")

    files.sort(key=lambda f: f.offset)

    flash = m.get("flash") or {}
    return FirmwarePack(
        id=m.get("id") or os.path.splitext(os.path.basename(source))[0],
        name=m.get("name") or "未命名固件",
        version=m.get("version", ""),
        chip=m.get("chip", "esp32s3"),
        files=files,
        desc=m.get("desc", ""),
        tag=m.get("tag", ""),
        built_at=m.get("builtAt", ""),
        flash_mode=flash.get("mode", "dio"),
        flash_freq=flash.get("freq", "80m"),
        flash_size=flash.get("size", "16MB"),
        source=source,
    )


def load_pack(path: str) -> FirmwarePack:
    """从 .fwpack（ZIP）或含 manifest.json 的目录读入一个刷机包（含文件内容）。"""
    if os.path.isdir(path):
        mf = os.path.join(path, "manifest.json")
        if not os.path.exists(mf):
            raise PackError(f"目录里没有 manifest.json：{path}")
        with open(mf, encoding="utf-8") as f:
            pack = _parse_manifest(f.read(), path)
        for pf in pack.files:
            fp = os.path.join(path, pf.path)
            if not os.path.exists(fp):
                raise PackError(f"缺少文件 {pf.path}")
            with open(fp, "rb") as f:
                pf.data = f.read()
            pf.size = len(pf.data)
        return pack

    if not zipfile.is_zipfile(path):
        raise PackError(f"不是有效的刷机包（既不是目录也不是 ZIP）：{path}")

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        mf = next((n for n in names if n.endswith("manifest.json")), None)
        if not mf:
            raise PackError("刷机包里没有 manifest.json")
        pack = _parse_manifest(z.read(mf).decode("utf-8"), path)
        base = mf[: -len("manifest.json")]  # 允许清单放在子目录里
        for pf in pack.files:
            member = base + pf.path
            if member not in names:
                # 兼容只写文件名、不带目录的包
                alt = next((n for n in names if n.endswith("/" + pf.path) or n == pf.path), None)
                if not alt:
                    raise PackError(f"刷机包里缺少 {pf.path}")
                member = alt
            pf.data = z.read(member)
            pf.size = len(pf.data)
    return pack


def scan_library(directory: str) -> List[FirmwarePack]:
    """扫描一个目录，找出所有可用的刷机包（.fwpack 文件 + 含 manifest 的子目录）。

    只看清单不解包文件内容，所以即使目录里有几十个包也能秒开。
    """
    out: List[FirmwarePack] = []
    if not os.path.isdir(directory):
        return out

    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        try:
            if os.path.isfile(p) and name.lower().endswith(PACK_EXT):
                with zipfile.ZipFile(p) as z:
                    mf = next((n for n in z.namelist() if n.endswith("manifest.json")), None)
                    if not mf:
                        continue
                    pack = _parse_manifest(z.read(mf).decode("utf-8"), p)
                    # 用包里记录的大小，避免为了显示而解压
                    for pf in pack.files:
                        pf.data = None
                    out.append(pack)
            elif os.path.isdir(p):
                mf = os.path.join(p, "manifest.json")
                if os.path.exists(mf):
                    with open(mf, encoding="utf-8") as f:
                        out.append(_parse_manifest(f.read(), p))
        except (PackError, zipfile.BadZipFile, OSError):
            # 单个包坏了不影响其它包列出
            continue

    # 同一个固件可能既有 .fwpack 又有同名目录（make_pack 两种都产出）。
    # 单文件包是分发用的权威版本，优先保留它，避免列表里出现重复条目。
    seen = {}
    for p in out:
        key = (p.id, p.version)
        prev = seen.get(key)
        if prev is None:
            seen[key] = p
            continue
        prev_is_zip = prev.source.lower().endswith(PACK_EXT)
        cur_is_zip = p.source.lower().endswith(PACK_EXT)
        if cur_is_zip and not prev_is_zip:
            seen[key] = p

    merged = sorted(seen.values(), key=lambda x: (x.name, x.version))
    return merged


# ---------------------------------------------------------------- 写入

def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_pack(pack: FirmwarePack) -> List[str]:
    """校验每个文件的 sha256。返回问题列表（空 = 全部通过）。"""
    problems = []
    for pf in pack.files:
        if pf.data is None:
            problems.append(f"{pf.path}：未载入")
            continue
        if pf.size and len(pf.data) != pf.size:
            problems.append(f"{pf.path}：大小不符（期望 {pf.size}，实际 {len(pf.data)}）")
        if pf.sha256 and sha256_of(pf.data) != pf.sha256:
            problems.append(f"{pf.path}：校验和不符")
    return problems


def save_pack(pack: FirmwarePack, out_path: str) -> str:
    """把已载入文件内容的刷机包写成单文件 .fwpack。"""
    for pf in pack.files:
        if pf.data is None:
            raise PackError(f"{pf.path} 没有内容，无法打包")
        pf.size = len(pf.data)
        pf.sha256 = sha256_of(pf.data)

    manifest = json.dumps(pack.to_manifest(), ensure_ascii=False, indent=2)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", manifest)
        for pf in pack.files:
            z.writestr(pf.path, pf.data)
    return out_path


def build_from_esptool_json(json_path: str, pack_id: str, name: str,
                            version: str = "", desc: str = "", tag: str = "") -> FirmwarePack:
    """从 ESP-IDF 的 flasher_args.json 生成刷机包（免手工填偏移）。"""
    build_dir = os.path.dirname(os.path.abspath(json_path))
    with open(json_path, encoding="utf-8") as f:
        fa = json.load(f)

    fs = fa.get("flash_settings") or {}
    files = []
    for off_str, rel in (fa.get("flash_files") or {}).items():
        src = os.path.join(build_dir, rel)
        if not os.path.exists(src):
            raise PackError(f"缺少构建产物：{rel}")
        with open(src, "rb") as f:
            data = f.read()
        base = os.path.basename(rel)
        role = ("bootloader" if "bootloader" in base
                else "partition" if "partition" in base else "app")
        files.append(PackFile(
            offset=int(str(off_str), 16), path=base,
            size=len(data), sha256=sha256_of(data), role=role, data=data,
        ))
    files.sort(key=lambda x: x.offset)

    return FirmwarePack(
        id=pack_id, name=name, version=version, desc=desc, tag=tag,
        chip=(fa.get("extra_esptool_args") or {}).get("chip", "esp32s3"),
        files=files,
        flash_mode=fs.get("flash_mode", "dio"),
        flash_freq=fs.get("flash_freq", "80m"),
        flash_size=fs.get("flash_size", "16MB"),
        built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
