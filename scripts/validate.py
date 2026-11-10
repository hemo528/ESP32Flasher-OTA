#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固件商店索引与固件包一致性校验（CI 与本地共用，纯标准库）。

用法：
    python scripts/validate.py            # 在仓库任意位置执行均可
    python scripts/validate.py --quiet    # 只打印错误与汇总

校验内容：
  * firmware.json 是合法 JSON，schema == fwstore/1；
  * mirrors 非空且都以 / 结尾；
  * 每个 firmware 必填字段齐全、id 唯一；
  * latest == versions[0].version，版本号形如 x.y.z，版本唯一且从新到旧；
  * versions[].url 是相对路径、指向的文件存在，size / sha256 与整包文件一致；
  * 每个 .fwpack 能被解析：manifest format 以 fwpack/ 开头、files 非空、
    offset 合法且不重复、包内文件存在且 sha256 与 manifest 一致；
  * firmware/ 下没有索引未登记的孤儿包（警告）；
  * 包内 manifest 的 id / version 与索引条目一致。

退出码：0 = 全部通过（可有警告），1 = 有错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "firmware.json")
FIRMWARE_DIR = os.path.join(ROOT, "firmware")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CHIP_RE = re.compile(r"^[a-z0-9]+$")

errors: list[str] = []
warnings: list[str] = []
checks = 0


# ---------------------------------------------------------------- 输出辅助

def ok(msg: str) -> None:
    global checks
    checks += 1
    print(f"  \u2713 {msg}")


def bad(msg: str) -> None:
    global checks
    checks += 1
    errors.append(msg)
    print(f"  \u2717 {msg}")


def warn(msg: str) -> None:
    global checks
    warnings.append(msg)
    print(f"  \u26a0 {msg}")


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- 工具函数

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_tuple(ver: str) -> tuple[int, ...]:
    return tuple(int(x) for x in ver.split("."))


def parse_offset(value) -> int:
    """与 fwpack.py 一致：按十六进制解析（"0x10000" / "10000" 都当 hex）。"""
    return int(str(value), 16)


def parse_flash_size(text: str):
    """把 "16MB" / "4MB" / "8MB" / "16M" 解析成字节数；解析不了返回 None。"""
    m = re.match(r"^\s*(\d+)\s*([KMG]?)B?\s*$", str(text), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).upper()
    mul = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[unit]
    return n * mul


def resolve_member(names: list[str], base: str, rel_path: str):
    """按 fwpack.py 的规则在 ZIP 里找到 rel_path 对应的成员名。"""
    member = base + rel_path
    if member in names:
        return member
    alt = next((n for n in names if n.endswith("/" + rel_path) or n == rel_path), None)
    return alt


def check_pack(pack_path: str, fw: dict, ver: dict) -> None:
    """深度校验一个 .fwpack：可解析、内部文件存在、sha256 与 manifest 一致。"""
    rel = os.path.relpath(pack_path, ROOT).replace("\\", "/")
    if not zipfile.is_zipfile(pack_path):
        bad(f"{rel}：不是有效的 ZIP（.fwpack 应为 ZIP 格式）")
        return

    try:
        with zipfile.ZipFile(pack_path) as z:
            names = z.namelist()
            mf_name = next((n for n in names if n.endswith("manifest.json")), None)
            if not mf_name:
                bad(f"{rel}：包内没有 manifest.json")
                return
            try:
                manifest = json.loads(z.read(mf_name).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                bad(f"{rel}：manifest.json 解析失败：{e}")
                return

            fmt = str(manifest.get("format", ""))
            if not fmt.startswith("fwpack/"):
                bad(f"{rel}：manifest format={fmt!r}，应以 fwpack/ 开头")
                return
            ok(f"{rel}：manifest format={fmt}")

            files = manifest.get("files") or []
            if not isinstance(files, list) or not files:
                bad(f"{rel}：manifest.files 为空")
                return

            # manifest 元信息与索引条目交叉核对
            mf_id = str(manifest.get("id", ""))
            mf_ver = str(manifest.get("version", ""))
            if mf_id != fw["id"]:
                bad(f"{rel}：manifest.id={mf_id!r} 与索引 id={fw['id']!r} 不一致")
            else:
                ok(f"{rel}：manifest.id={mf_id} 与索引一致")
            if mf_ver != ver["version"]:
                bad(f"{rel}：manifest.version={mf_ver!r} 与索引 version={ver['version']!r} 不一致")
            else:
                ok(f"{rel}：manifest.version={mf_ver} 与索引一致")
            if str(manifest.get("chip", "")) != fw.get("chip"):
                warn(f"{rel}：manifest.chip={manifest.get('chip')!r} 与索引 chip={fw.get('chip')!r} 不一致")
            if str(manifest.get("name", "")) != fw.get("name"):
                warn(f"{rel}：manifest.name 与索引 name 不一致（索引应取自包内 manifest）")
            if str(manifest.get("builtAt", "")) != str(ver.get("builtAt", "")):
                warn(f"{rel}：manifest.builtAt 与索引 builtAt 不一致")

            # flash 容量（用于 offset 越界检查）
            flash = manifest.get("flash") or {}
            flash_size = parse_flash_size(flash.get("size", "16MB"))
            if flash_size is None:
                warn(f"{rel}：无法解析 flash.size={flash.get('size')!r}，跳过 offset 越界检查")

            base = mf_name[: -len("manifest.json")]
            seen_offsets: dict[int, str] = {}
            spans: list[tuple[int, int, str]] = []
            n_files = 0
            for i, item in enumerate(files):
                if not isinstance(item, dict):
                    bad(f"{rel}：files[{i}] 不是对象")
                    continue
                path = item.get("path")
                if not path:
                    bad(f"{rel}：files[{i}] 缺少 path")
                    continue
                try:
                    offset = parse_offset(item.get("offset"))
                except (TypeError, ValueError):
                    bad(f"{rel}：{path} 的 offset={item.get('offset')!r} 无效")
                    continue
                if offset < 0:
                    bad(f"{rel}：{path} 的 offset 为负数")
                    continue
                if offset in seen_offsets:
                    bad(f"{rel}：offset 重复（{seen_offsets[offset]} 与 {path} 都是 0x{offset:X}）")
                else:
                    seen_offsets[offset] = path

                member = resolve_member(names, base, path)
                if member is None:
                    bad(f"{rel}：包内缺少文件 {path}")
                    continue
                data = z.read(member)
                n_files += 1
                expect_sha = str(item.get("sha256", "")).lower()
                if not expect_sha:
                    warn(f"{rel}：{path} 未记录 sha256")
                elif not SHA256_RE.match(expect_sha):
                    bad(f"{rel}：{path} 的 sha256 不是 64 位小写十六进制")
                elif hashlib.sha256(data).hexdigest() != expect_sha:
                    bad(f"{rel}：{path} 内容 sha256 与 manifest 不一致")
                else:
                    ok(f"{rel}：{path} ({len(data)} B) sha256 校验通过")

                recorded = item.get("size")
                if recorded is not None and int(recorded) != len(data):
                    warn(f"{rel}：{path} 记录 size={recorded}，实际 {len(data)}（size 仅文档用途）")

                if flash_size is not None and offset + len(data) > flash_size:
                    bad(f"{rel}：{path} 写入区间 0x{offset:X}+{len(data)} 超出 flash 容量 {flash.get('size')}")
                spans.append((offset, offset + len(data), path))

            if n_files != len(files):
                bad(f"{rel}：有 {len(files) - n_files} 个文件未能读取")
            else:
                ok(f"{rel}：{n_files} 个文件全部存在且可读")

            # 索引里的 flash / files 必须与包内 manifest 完全一致（工具详情面板直接用这两项）
            mf_flash = manifest.get("flash") or {}
            expected_flash = {
                "mode": str(mf_flash.get("mode", "dio")),
                "freq": str(mf_flash.get("freq", "80m")),
                "size": str(mf_flash.get("size", "16MB")),
            }
            idx_flash = ver.get("flash")
            if not isinstance(idx_flash, dict):
                bad(f"{rel}：索引条目缺少 flash（应为 {expected_flash}）")
            else:
                diffs = [
                    f"{k}={idx_flash.get(k)!r}≠{v!r}"
                    for k, v in expected_flash.items()
                    if str(idx_flash.get(k, "")) != v
                ]
                if diffs:
                    bad(f"{rel}：索引 flash 与 manifest 不一致：{', '.join(diffs)}")
                else:
                    ok(f"{rel}：索引 flash 与 manifest 一致"
                       f"（{expected_flash['mode']}/{expected_flash['freq']}/{expected_flash['size']}）")

            mf_files = []
            for item in files:
                mf_files.append({
                    "offset": parse_offset(item.get("offset")),
                    "path": item.get("path"),
                    "size": int(item.get("size", 0)),
                    "role": str(item.get("role", "app")),
                })
            mf_files.sort(key=lambda x: x["offset"])

            idx_files = ver.get("files")
            if not isinstance(idx_files, list) or not idx_files:
                bad(f"{rel}：索引条目缺少 files（应拷贝 manifest.files）")
            else:
                idx_norm = []
                idx_ok = True
                for i, item in enumerate(idx_files):
                    if not isinstance(item, dict):
                        bad(f"{rel}：索引 files[{i}] 不是对象")
                        idx_ok = False
                        continue
                    try:
                        off = parse_offset(item.get("offset"))
                    except (TypeError, ValueError):
                        bad(f"{rel}：索引 files[{i}] 的 offset={item.get('offset')!r} 无效")
                        idx_ok = False
                        continue
                    try:
                        size = int(item.get("size"))
                    except (TypeError, ValueError):
                        bad(f"{rel}：索引 files[{i}] 的 size={item.get('size')!r} 不是整数")
                        idx_ok = False
                        continue
                    idx_norm.append({
                        "offset": off,
                        "path": item.get("path"),
                        "size": size,
                        "role": str(item.get("role", "")),
                    })
                if idx_ok:
                    offsets = [f["offset"] for f in idx_norm]
                    if offsets != sorted(offsets):
                        bad(f"{rel}：索引 files 未按 offset 升序排列")
                    elif len(idx_norm) != len(mf_files):
                        bad(f"{rel}：索引 files 数量 {len(idx_norm)} ≠ manifest {len(mf_files)}")
                    else:
                        mismatch = []
                        for a, b in zip(idx_norm, mf_files):
                            if (a["offset"] != b["offset"] or a["path"] != b["path"]
                                    or a["size"] != b["size"] or a["role"] != b["role"]):
                                mismatch.append(
                                    f"0x{a['offset']:X}/{a['path']} vs 0x{b['offset']:X}/{b['path']}")
                        if mismatch:
                            bad(f"{rel}：索引 files 与 manifest 不一致：{'; '.join(mismatch)}")
                        else:
                            ok(f"{rel}：索引 files 与 manifest 完全一致（{len(mf_files)} 项，按 offset 升序）")

            # 区间重叠检查（重叠会互相覆盖，属于打包错误）
            spans.sort()
            for (a_start, a_end, a_name), (b_start, b_end, b_name) in zip(spans, spans[1:]):
                if b_start < a_end:
                    warn(f"{rel}：{a_name} 与 {b_name} 的写入区间重叠")
    except (zipfile.BadZipFile, OSError) as e:
        bad(f"{rel}：读取失败：{e}")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="校验 firmware.json 与 firmware/ 下固件包的一致性")
    parser.add_argument("--quiet", action="store_true", help="只打印错误/警告与汇总")
    args = parser.parse_args()

    print("固件商店索引校验")
    print(f"仓库根目录：{ROOT}")
    print(f"索引文件：{INDEX_PATH}")

    if not os.path.isfile(INDEX_PATH):
        print(f"\n\u2717 找不到索引文件：{INDEX_PATH}")
        return 1
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            index = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"\n\u2717 firmware.json 不是合法 JSON：{e}")
        return 1
    if not isinstance(index, dict):
        print("\n\u2717 firmware.json 顶层应为对象")
        return 1

    # ---- 索引头
    section("索引头")
    if index.get("schema") == "fwstore/1":
        ok("schema = fwstore/1")
    else:
        bad(f"schema={index.get('schema')!r}，应为 'fwstore/1'")

    mirrors = index.get("mirrors")
    if not isinstance(mirrors, list) or not mirrors:
        bad("mirrors 缺失或为空")
    else:
        bad_mirrors = [m for m in mirrors if not (isinstance(m, str) and m.startswith(("https://", "http://")) and m.endswith("/"))]
        if bad_mirrors:
            bad(f"mirrors 中有 {len(bad_mirrors)} 个不是以 / 结尾的 http(s) 前缀：{bad_mirrors}")
        else:
            ok(f"mirrors {len(mirrors)} 个，均为以 / 结尾的 http(s) 前缀")

    homepage = index.get("homepage")
    if isinstance(homepage, str) and homepage.startswith("https://"):
        ok(f"homepage = {homepage}")
    else:
        warn("homepage 缺失或不是 https:// 地址")

    generated = index.get("generatedAt")
    if isinstance(generated, str) and ISO_UTC_RE.match(generated):
        ok(f"generatedAt = {generated}")
    else:
        warn(f"generatedAt={generated!r} 不是 ISO8601 UTC 格式（如 2026-11-10T06:00:00Z）")

    # ---- 固件条目
    firmwares = index.get("firmwares")
    if not isinstance(firmwares, list) or not firmwares:
        print("\n\u2717 firmwares 缺失或为空")
        return 1
    section(f"固件条目（{len(firmwares)} 个）")

    ids: dict[str, int] = {}
    indexed_urls: set[str] = set()

    for idx, fw in enumerate(firmwares):
        if not isinstance(fw, dict):
            bad(f"firmwares[{idx}] 不是对象")
            continue
        fw_id = str(fw.get("id", "") or f"<firmwares[{idx}]>")
        print(f"\n  ({idx + 1}/{len(firmwares)}) {fw_id}")

        # 必填字段
        missing = [k for k in ("id", "name", "tag", "chip", "desc", "latest", "versions") if k not in fw]
        empty = [k for k in ("id", "name", "latest") if not str(fw.get(k, "")).strip()]
        if missing:
            bad(f"{fw_id}：缺少必填字段 {missing}")
        if empty:
            bad(f"{fw_id}：字段为空 {empty}")
        if not missing and not empty:
            ok(f"{fw_id}：必填字段齐全（id/name/tag/chip/desc/latest/versions）")
        if fw_id in ids:
            bad(f"{fw_id}：id 重复（第 {ids[fw_id] + 1} 与第 {idx + 1} 条）")
        else:
            ids[fw_id] = idx
        if not CHIP_RE.match(str(fw.get("chip", ""))):
            warn(f"{fw_id}：chip={fw.get('chip')!r} 不是常见的芯片标识（如 esp32s3）")

        versions = fw.get("versions")
        if not isinstance(versions, list) or not versions:
            bad(f"{fw_id}：versions 缺失或为空")
            continue
        if not isinstance(versions[0], dict):
            bad(f"{fw_id}：versions[0] 不是对象")
            continue
        if fw.get("latest") != versions[0].get("version"):
            bad(f"{fw_id}：latest={fw.get('latest')!r} != versions[0].version={versions[0].get('version')!r}")
        else:
            ok(f"{fw_id}：latest={fw.get('latest')} == versions[0].version")

        seen_versions: set[str] = set()
        prev_tuple = None
        for vi, ver in enumerate(versions):
            if not isinstance(ver, dict):
                bad(f"{fw_id}：versions[{vi}] 不是对象")
                continue
            v = str(ver.get("version", ""))
            tag = f"{fw_id} v{v or '?'}"
            if not VERSION_RE.match(v):
                bad(f"{tag}：版本号不是 x.y.z 形式")
                continue
            if v in seen_versions:
                bad(f"{tag}：同一 id 下版本号重复")
                continue
            seen_versions.add(v)

            vt = version_tuple(v)
            if prev_tuple is not None and not (vt < prev_tuple):
                bad(f"{tag}：versions 未按版本从新到旧排列")
            prev_tuple = vt

            vmissing = [k for k in ("version", "url", "size", "sha256", "builtAt", "changelog", "flash", "files") if k not in ver]
            if vmissing:
                bad(f"{tag}：缺少必填字段 {vmissing}")
                continue

            min_flasher = ver.get("minFlasher")
            if min_flasher is not None and not VERSION_RE.match(str(min_flasher)):
                bad(f"{tag}：minFlasher={min_flasher!r} 不是 x.y.z 形式")

            built_at = str(ver.get("builtAt", ""))
            if not ISO_UTC_RE.match(built_at):
                warn(f"{tag}：builtAt={built_at!r} 不是 ISO8601 UTC 格式")
            if not str(ver.get("changelog", "")).strip():
                bad(f"{tag}：changelog 为空")

            url = str(ver.get("url", ""))
            if not url:
                bad(f"{tag}：url 为空")
                continue
            if "://" in url:
                bad(f"{tag}：url={url!r} 应为相对路径（不是绝对 URL）")
                continue
            norm_url = url.replace("\\", "/")
            if norm_url.startswith("/") or ".." in norm_url.split("/"):
                bad(f"{tag}：url={url!r} 必须是仓库内相对路径")
                continue
            if norm_url in indexed_urls:
                bad(f"{tag}：url={url!r} 被多个条目引用")
            indexed_urls.add(norm_url)

            expected_url = f"firmware/{fw_id}/{fw_id}-v{v}.fwpack"
            if norm_url != expected_url:
                warn(f"{tag}：url={norm_url!r}，建议用规范路径 {expected_url!r}")

            pack_path = os.path.join(ROOT, norm_url.replace("/", os.sep))
            if not os.path.isfile(pack_path):
                bad(f"{tag}：url 指向的文件不存在：{norm_url}")
                continue

            actual_size = os.path.getsize(pack_path)
            try:
                recorded_size = int(ver.get("size"))
            except (TypeError, ValueError):
                bad(f"{tag}：size={ver.get('size')!r} 不是整数")
                recorded_size = None
            if recorded_size is not None:
                if recorded_size != actual_size:
                    bad(f"{tag}：size 不符（索引 {recorded_size}，实际 {actual_size}）")
                else:
                    ok(f"{tag}：size={actual_size} 与实际一致")

            recorded_sha = str(ver.get("sha256", "")).lower()
            if not SHA256_RE.match(recorded_sha):
                bad(f"{tag}：sha256 不是 64 位小写十六进制")
            else:
                actual_sha = sha256_file(pack_path)
                if actual_sha != recorded_sha:
                    bad(f"{tag}：sha256 不符（索引 {recorded_sha[:12]}…，实际 {actual_sha[:12]}…）")
                else:
                    ok(f"{tag}：sha256={actual_sha[:16]}… 与整包一致")

            # 包内部深度校验
            check_pack(pack_path, fw, ver)

    # ---- 孤儿包
    section("孤儿包检查")
    orphans: list[str] = []
    if os.path.isdir(FIRMWARE_DIR):
        for dirpath, _dirnames, filenames in os.walk(FIRMWARE_DIR):
            for name in filenames:
                if not name.lower().endswith(".fwpack"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT).replace("\\", "/")
                if rel not in indexed_urls:
                    orphans.append(rel)
    if orphans:
        for rel in sorted(orphans):
            warn(f"firmware/ 下存在索引未登记的包：{rel}")
    else:
        ok("firmware/ 下没有未登记的 .fwpack")

    # ---- 汇总
    print("\n" + "=" * 60)
    if errors:
        print(f"结果：\u2717 失败 —— 通过 {checks - len(errors) - len(warnings)} 项，"
              f"警告 {len(warnings)} 项，错误 {len(errors)} 项")
        print("\n错误列表：")
        for i, e in enumerate(errors, 1):
            print(f"  {i}. {e}")
        return 1
    print(f"结果：\u2713 通过 —— {checks} 项检查全部通过，警告 {len(warnings)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
