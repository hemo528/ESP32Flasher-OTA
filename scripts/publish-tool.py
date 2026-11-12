#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布刷机工具自身更新（tool 块）到固件商店仓库。

用法（在仓库根目录或任意位置执行均可）：
    python scripts/publish-tool.py -Exe <FirmwareFlasher.exe 路径> -Version 1.3.0 \
        -Changelog "本次更新内容"

流程：
  1. 计算 exe 的 size / sha256；
  2. 复制到 tool/FirmwareFlasher-<version>.exe；
  3. 更新 firmware.json 顶层 tool 块（latest + versions，版本从新到旧）；
  4. 只保留最近 N 个版本（默认 2），删除多余 exe；
  5. 本地校验（scripts/validate.py）；
  6. git add/commit/push（-DryRun 时只打印）。

约束：
  * 版本号必须 x.y.z，且必须大于当前 tool.latest（-Force 允许同版本覆盖重发）；
  * 同一版本、同样 sha256 时直接跳过（幂等，便于重跑）；
  * 索引里其它块（firmwares 等）原样保留。

退出码：0 = 成功或幂等跳过；1 = 失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "firmware.json")
TOOL_DIR = os.path.join(ROOT, "tool")

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def version_tuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(v).strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fail(msg: str) -> int:
    print("\n\u2717 " + msg)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="发布刷机工具自身更新到固件商店索引")
    parser.add_argument("-Exe", required=True, help="FirmwareFlasher.exe 路径")
    parser.add_argument("-Version", required=True, help="工具版本号，x.y.z")
    parser.add_argument("-Changelog", required=True, help="更新说明（必填）")
    parser.add_argument("-KeepVersions", type=int, default=2, help="保留最近几个版本（默认 2）")
    parser.add_argument("-DryRun", action="store_true", help="只打印将要做的操作")
    parser.add_argument("-Force", action="store_true", help="允许同版本覆盖重发")
    args = parser.parse_args()

    exe = os.path.abspath(args.Exe)
    version = str(args.Version).strip()
    keep = max(1, int(args.KeepVersions))

    if not os.path.isfile(exe):
        return fail("找不到 exe：%s" % exe)
    if not exe.lower().endswith(".exe"):
        return fail("不是 .exe 文件：%s" % exe)
    if not VERSION_RE.match(version):
        return fail("版本号 %r 不是 x.y.z 形式" % version)
    if not str(args.Changelog).strip():
        return fail("changelog 不能为空")

    if not os.path.isfile(INDEX_PATH):
        return fail("找不到索引：%s" % INDEX_PATH)
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            index = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return fail("firmware.json 解析失败：%s" % e)
    if not isinstance(index, dict) or index.get("schema") != "fwstore/1":
        return fail("firmware.json 的 schema 不是 fwstore/1")

    size = os.path.getsize(exe)
    sha = sha256_file(exe)

    tool = index.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    versions = [v for v in (tool.get("versions") or []) if isinstance(v, dict)]
    cur_latest = str(tool.get("latest") or "")
    if cur_latest and not VERSION_RE.match(cur_latest):
        cur_latest = ""

    rel = "tool/FirmwareFlasher-%s.exe" % version
    dst = os.path.join(TOOL_DIR, "FirmwareFlasher-%s.exe" % version)

    # 幂等：同版本且 sha/size 一致，直接跳过
    for v in versions:
        if str(v.get("version")) == version:
            same = (str(v.get("sha256", "")).lower() == sha and int(v.get("size") or -1) == size
                    and os.path.isfile(dst))
            if same and not args.Force:
                print("\u2713 版本 %s 已存在且 sha256/size 一致，跳过发布（幂等）" % version)
                return 0
            if not args.Force:
                return fail("版本 %s 已存在但内容不同（sha256 变化），需加 -Force 覆盖重发" % version)

    if cur_latest and version_tuple(version) < version_tuple(cur_latest) and not args.Force:
        return fail("版本回退：%s 低于当前 latest=%s，拒绝发布（-Force 可强制）" % (version, cur_latest))

    entry = {
        "version": version,
        "url": rel,
        "size": size,
        "sha256": sha,
        "builtAt": now_utc(),
        "changelog": str(args.Changelog).strip(),
    }

    print("刷机工具更新发布")
    print("  仓库：%s" % ROOT)
    print("  源文件：%s（%d B）" % (exe, size))
    print("  sha256：%s" % sha)
    print("  版本：%s -> %s" % (cur_latest or "（无）", version))
    if args.DryRun:
        print("\n[DryRun] 将复制到：%s" % rel)
        print("[DryRun] 将更新 %s 的 tool 块（保留最近 %d 版）" % (INDEX_PATH, keep))
        print("[DryRun] 将执行 git add/commit/push")
        return 0

    os.makedirs(TOOL_DIR, exist_ok=True)
    if os.path.abspath(dst) != exe:
        shutil.copy2(exe, dst)
    print("[复制] %s（%d B）" % (rel, size))

    # 新版本插到最前，按版本号从新到旧排序并裁剪
    versions = [v for v in versions if str(v.get("version")) != version]
    versions.append(entry)
    versions.sort(key=lambda v: version_tuple(str(v.get("version", ""))), reverse=True)
    dropped = versions[keep:]
    versions = versions[:keep]

    for v in dropped:
        old_rel = str(v.get("url") or "")
        old_path = os.path.join(ROOT, old_rel.replace("/", os.sep))
        if old_rel and os.path.isfile(old_path):
            os.remove(old_path)
            print("[删除] %s（超出保留 %d 版）" % (old_rel, keep))

    index["tool"] = {"latest": versions[0]["version"], "versions": versions}
    index["generatedAt"] = now_utc()

    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(index, ensure_ascii=False, indent=4) + "\n")
    os.replace(tmp, INDEX_PATH)
    print("[写入] firmware.json：tool.latest=%s，共 %d 个版本" % (versions[0]["version"], len(versions)))

    print("\n本地校验：")
    rc = subprocess.call([sys.executable, os.path.join(ROOT, "scripts", "validate.py")])
    if rc != 0:
        return fail("validate.py 未通过，已停止（未提交）。修好后再跑一次。")

    def run(cmd: list[str]) -> int:
        print("\n$ " + " ".join(cmd))
        return subprocess.call(cmd, cwd=ROOT)

    if run(["git", "add", "-A"]) != 0:
        return fail("git add 失败")
    if run(["git", "commit", "-m", "tool: 发布 v%s" % version]) != 0:
        return fail("git commit 失败（可能没有改动）")
    if run(["git", "push"]) != 0:
        return fail("git push 失败（检查 SSH remote 是否可用）")
    print("\n\u2713 已发布刷机工具 v%s（%s，%d B）" % (version, sha[:16], size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
