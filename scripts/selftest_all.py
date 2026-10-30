#!/usr/bin/env python3
"""跑全量自测：`scripts/test_*.py` + 所有支持 `--selftest` 的守卫。

## 为什么要有这条命令（2026-10-31 实测事故）

改了 `gen_versions.floor_bumps_map` 的取值（加 `org` 字段）之后，本地自测清单
只覆盖 `verify_*.py` / `check_*.py` / `scan_*.py`，**漏了 `scripts/test_*.py`**
⇒ `test_sync_matrix.py` 构造的假 args 没有 `org` 字段 ⇒ `AttributeError`
⇒ CI 的 `Scripts` job 红了一轮，本地却全绿。

根因不是「漏跑了一个脚本」，而是**清单靠人记** ⇒ 清单必然漂移。
⇒ 清单本身要机器可执行：一条命令跑全部，退出码如实反映失败。

## 用法

    python3 scripts/selftest_all.py          # 跑全量，失败则退出码非 0
    python3 scripts/selftest_all.py --list   # 只列出要跑哪些，不执行

## 边界

本脚本**不判任何业务口径**——它只是聚合器（跑别人的自测并汇总退出码）。
口径判据住在各自守卫里，本脚本不复制、不改写。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).resolve().name


def collect() -> tuple[list[tuple[Path, list[str]]], list[Path]]:
    """→ (普通运行的自测脚本, 用 --selftest 运行的守卫脚本)。"""
    plain: list[tuple[Path, list[str]]] = []
    with_selftest: list[Path] = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name == SELF:
            continue
        if path.name.startswith("test_"):
            plain.append((path, []))
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "selftest" in src:
            with_selftest.append(path)
    return plain, with_selftest


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 matrix-tools 的全量自测")
    ap.add_argument("--list", action="store_true", help="只列出要跑哪些，不执行")
    args = ap.parse_args()

    plain, with_selftest = collect()
    plan = plain + [(p, ["--selftest"]) for p in with_selftest]
    if args.list:
        for path, extra in plan:
            print("%s %s" % (path.name, " ".join(extra)))
        return 0

    failed: list[tuple[Path, list[str], str]] = []
    for path, extra in plan:
        cmd = [sys.executable, str(path)] + extra
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)})
        if proc.returncode != 0:
            tail = (proc.stdout or "") + (proc.stderr or "")
            failed.append((path, extra, tail.strip()[-2000:]))
            print("  ✗ rc=%d  %s" % (proc.returncode, path.name))
        else:
            print("  ✓ rc=0  %s" % path.name)

    print("─── 全量自测：%d 个通过 / %d 个失败 ───" % (len(plan) - len(failed), len(failed)))
    if failed:
        for path, extra, tail in failed:
            print("\n=== %s %s ===" % (path.name, " ".join(extra)))
            print(tail)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
