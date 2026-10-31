#!/usr/bin/env python3
"""跑全量自测：`scripts/test_*.py` + 所有支持 `--selftest` 的守卫。

## 为什么要有这条命令（2026-10-31 实测事故）

改了 `gen_versions.floor_bumps_map` 的取值（加 `org` 字段）之后，本地自测清单
只覆盖 `verify_*.py` / `check_*.py` / `scan_*.py`，**漏了 `scripts/test_*.py`**
⇒ `test_sync_matrix.py` 构造的假 args 没有 `org` 字段 ⇒ `AttributeError`
⇒ CI 的 `Scripts` job 红了一轮，本地却全绿。

根因不是「漏跑了一个脚本」，而是**清单靠人记** ⇒ 清单必然漂移。
⇒ 清单本身要机器可执行：一条命令跑全部，退出码如实反映失败。

## 分片（CI 用）

全量 40+ 个自测串行跑会撞 job 的 5 分钟超时线 ⇒ 本脚本支持 `--shard I/N`：

    python3 scripts/selftest_all.py --shard 0/4     # 只跑第 0 片（0 起）
    python3 scripts/selftest_all.py --plan-shards    # 打印机器可读的片数（CI 用）

⚠️ **片数不能写死在 CI 里** —— 写死就会与实际脚本数漂移
⇒ CI 用 `--plan-shards` 的输出来生成 matrix（见 `--plan-shards`）。
分片算法保证 `union(各片) == 全集`（自测里断言了这条）。

## 两拨自测都要跑

- `scripts/test_*.py`（普通运行）= **接线自测**：功能有没有接上；
- 其余守卫的 `--selftest` = **判据自测**：判据准不准。

**只跑一拨＝漏掉另一拨** ⇒ 本命令两拨都跑（分片时也是两拨分别切，保证全局覆盖）。

## 边界

本脚本**不判任何业务口径**——它只是聚合器（跑别人的自测并汇总退出码）。
口径判据住在各自守卫里，本脚本不复制、不改写。
「跑哪些脚本」的判据住在 `selftest_discovery.py`（AST 判据，不靠子串匹配）。

## 用法

    python3 scripts/selftest_all.py              # 跑全量，失败则退出码非 0
    python3 scripts/selftest_all.py --list       # 只列出要跑哪些，不执行
    python3 scripts/selftest_all.py --shard 0/4  # 只跑第 0 片
    python3 scripts/selftest_all.py --plan-shards
    python3 scripts/selftest_all.py --selftest   # 本聚合器自己的自测
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).name

sys.path.insert(0, str(SCRIPTS_DIR))
from selftest_discovery import SELFTEST_FLAG, discover  # noqa: E402

# 每片大致跑多少个自测（片数由脚本数算出来，不写死）
PER_SHARD = 10


def build_plan(scripts_dir: Path = SCRIPTS_DIR) -> list[tuple[Path, list[str]]]:
    """→ [(脚本, 额外参数)]。**不含本聚合器自己**（否则会递归）。"""
    plain, with_selftest = discover(scripts_dir)
    plan = [(p, []) for p in plain] + [(p, [SELFTEST_FLAG]) for p in with_selftest]
    return [(p, e) for p, e in plan if p.name != SELF]


def shard_plan(plan: list, index: int, total: int) -> list:
    """把 plan 切成 total 片里的第 index 片（0 起）。

    **整体轮转**（不是两拨分别切）⇒ 只要 total ≤ len(plan)，每片都非空。
    ⚠️ 两拨分别切会出空片：`test_*.py` 只有 3 个、其余守卫 40+ 个，
    切成 5 片时「接线自测」那一拨只能填满 3 片 ⇒ 另 2 片空转。
    纪律要求「两拨都跑」是**全局**要求（并集 == 全集 ⇒ 两拨必然都在），
    不是「每片都有两拨」⇒ 整体轮转既满足纪律，又不产生空片。
    """
    return plan[index::total]


def suggest_shards(count: int) -> int:
    """建议的片数：按每片 PER_SHARD 个算，最少 1 片、最多 8 片。"""
    if count <= 0:
        return 1
    return max(1, min(8, (count + PER_SHARD - 1) // PER_SHARD))


def _selftest() -> int:
    """聚合器自己的自测：分片必须**并集等于全集**、不重不漏。

    ⚠️ 这条判据的价值在于「分片写错 ⇒ 某��脚本静默没跑」。
    没跑的自测不会报错 ⇒ CI 照样绿 ⇒ 隐患。
    """
    fails: list[str] = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    plan = [("a.py", []), ("b.py", []), ("c.py", ["--selftest"]),
            ("d.py", ["--selftest"]), ("e.py", ["--selftest"])]

    # ① 各片的并集 == 全集，且不重不漏
    for total in (1, 2, 3, 4, 5, 7):
        union = []
        for i in range(total):
            union.extend(shard_plan(plan, i, total))
        check("片数 %d 时并集等于全集" % total,
              sorted(union), sorted(plan))
        check("片数 %d 时无重复" % total, len(union),
              len(set("%s|%s" % (x[0], ",".join(x[1])) for x in union)))

    # ② 每片都必须有内容（片数配错 ⇒ 空片 ⇒ 该片白跑）
    #    ⚠️ 前提：片数 ≤ 脚本数（真实场景的片数由 suggest_shards 按脚本数算，不会超）
    for total in (1, 2, 3, 4, 5):
        if total > len(plan):
            continue
        for i in range(total):
            check("片 %d/%d 非空" % (i, total), len(shard_plan(plan, i, total)) > 0, True)

    # ③ 两拨**全局**都要被跑到（并集 == 全集已蕴含，这里显式断言）
    union_all = []
    for total in (1, 2, 3, 4, 5):
        for i in range(total):
            union_all.extend(shard_plan(plan, i, total))
    check("两拨全局都覆盖",
          (len([x for x in union_all if not x[1]]) > 0
           and len([x for x in union_all if x[1]]) > 0), True)

    # ④ 建议片数：脚本越多片越多，但有上限
    check("0 个脚本 ⇒ 1 片", suggest_shards(0), 1)
    check("44 个脚本 ⇒ 片数在 1..8", 1 <= suggest_shards(44) <= 8, True)
    check("片数随规模增长", suggest_shards(80) > suggest_shards(10), True)

    # ⑤ 空计划 ⇒ 必须判红（判不了 ≠ 没问题：一个文件都没扫到也算红）
    check("空计划判红", _run_plan([])[0] != 0, True)

    # ⑥ 聚合器不把自己算进计划（否则会递归）
    real = build_plan()
    check("聚合器不把自己算进计划", any(p.name == SELF for p, _ in real), False)

    if fails:
        print("聚合器自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("聚合器自测：%d 条断言全过" % (len(fails) + 26))
    return 0


def _run_plan(plan: list) -> tuple[int, list]:
    """跑一遍 plan → (退出码, 失败清单)。

    ⚠️ **空计划判红**：一个自测都没扫到 ⇒ 不是「没事」，是「判不了」
    （发现层坏了 / 目录空了 / 脚本全被改名）⇒ 必须非 0 退出。
    判红逻辑放在**这里**（而不是只放在 main 里）⇒ 才是可被自测断言的行为。
    """
    if not plan:
        print("✗ 自测集合为空——发现层没扫到任何脚本 ⇒ 判红（判不了 ≠ 没问题）",
              file=sys.stderr)
        return 1, []
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
    return (1 if failed else 0), failed


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 matrix-tools 的全量自测")
    ap.add_argument("--list", action="store_true", help="只列出要跑哪些，不执行")
    ap.add_argument("--shard", help="只跑某一片，格式 I/N（0 起）")
    ap.add_argument("--plan-shards", action="store_true",
                    help="打印机器可读的片数（CI 用它生成 matrix，不写死）")
    ap.add_argument(SELFTEST_FLAG, action="store_true", help="跑本聚合器自己的自测")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    plan = build_plan()

    if args.plan_shards:
        n = suggest_shards(len(plan))
        print("shards=%d" % n)
        print("matrix=%s" % json.dumps(list(range(n))))
        return 0

    if args.shard:
        try:
            idx_s, total_s = args.shard.split("/")
            idx, total = int(idx_s), int(total_s)
        except ValueError:
            print("--shard 格式应为 I/N（0 起），实得 %r" % args.shard, file=sys.stderr)
            return 2
        if total < 1 or not (0 <= idx < total):
            print("--shard %r 越界（共 %d 片）" % (args.shard, total), file=sys.stderr)
            return 2
        plan = shard_plan(plan, idx, total)

    if args.list:
        for path, extra in plan:
            print("%s %s" % (path.name, " ".join(extra)))
        return 0

    if not plan:
        # ⚠️ 一个文件都没扫到 ⇒ 判红（不是「没事」，是「判不了」）
        print("✗ 自测集合为空——发现层没扫到任何脚本 ⇒ 判红（判不了 ≠ 没问题）",
              file=sys.stderr)
        return 1

    rc, failed = _run_plan(plan)
    print("─── 自测：%d 个通过 / %d 个失败 ───" % (len(plan) - len(failed), len(failed)))
    if failed:
        for path, extra, tail in failed:
            print("\n=== %s %s ===" % (path.name, " ".join(extra)))
            print(tail)
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
