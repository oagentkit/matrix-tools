#!/usr/bin/env python3
"""门面仓依赖守卫：禁止下游 import 已退役的单仓 module 与门面仓。

背景：门面仓 `github.com/oagentkit/agentkit` 的退役计划是「v1.x 后转 archive」。
但退役这件事**只有文档约束、没有机器约束**——半年后有人写一个新 import
`agentkit/textutil` 不会有任何人收到通知，archive 之后就直接构建断裂。

本脚本把「不许再引」变成 CI 里的一条红灯。

用法:
    check_facade_imports.py --roots ~/my_project/lacus/argus ~/my_project/lacus/heimdallr
    check_facade_imports.py --roots ~/my_project          # 全盘扫描（自动排除豁免目录）

退出码: 0 = 干净；1 = 发现违规 import。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 被禁止的 import 前缀
FORBIDDEN = [
    ("git.enjoye.top/enjoydream/agentkit", "旧单仓 module（已拆分为 github.com/oagentkit/*）"),
    ("github.com/oagentkit/agentkit", "门面仓（v1.x 后退役，新代码不要引）"),
]

# 豁免目录：路径中包含这些片段就跳过
EXEMPT = [
    "/.git/", "/node_modules/", "/vendor/", "/_tmp/", "/.worktrees/",
    "/.gomodcache", "/.superpowers/", "/.cache/", "/dist/", "/site/",
    # agentkit 本体就是要退役的那一坨，它当然引自己
    "/lacus/agentkit/", "/agentkit-bisect/",
    # oagentkit 各仓自身的 go.sum / 文档
    "/oagentkit/",
]

SKIP_DIRS = {".git", "node_modules", "vendor", "_tmp", ".worktrees",
             ".gomodcache", ".superpowers", ".cache", "dist", "site"}


def scan_file(p: Path, rel: str) -> list[tuple[str, int, str]]:
    try:
        text = p.read_text("utf-8", errors="ignore")
    except Exception:
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for prefix, why in FORBIDDEN:
            # 只看真正的 import 行（含引号的路径），避免误伤注释/文档
            if re.search(rf'"{re.escape(prefix)}(/[a-zA-Z0-9_.\-/]*)?"', line):
                if line.lstrip().startswith("//"):
                    continue
                hits.append((rel, i, why))
                break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    total_files = 0
    violations: list[tuple[str, int, str]] = []

    for root_s in args.roots:
        root = Path(root_s).expanduser().resolve()
        if not root.exists():
            print(f"⚠️  路径不存在，跳过: {root}")
            continue

        walker = [root] if root.is_file() else root.rglob("*.go")
        for p in walker:
            ps = str(p)
            if any(x in ps for x in EXEMPT):
                continue
            if p.suffix != ".go":
                continue
            rel = str(p.relative_to(root)) if root in p.parents else p.name
            total_files += 1
            violations.extend(scan_file(p, rel))

    print(f"扫描 .go 文件: {total_files}")
    for prefix, why in FORBIDDEN:
        print(f"  禁止前缀: {prefix}  —— {why}")

    if not violations:
        print("✅ 未发现门面仓/旧单仓 import")
        return 0

    print(f"\n❌ 发现 {len(violations)} 处违规 import：\n")
    for rel, line, why in violations[:60]:
        print(f"  {rel}:{line}  —— {why}")
    if len(violations) > 60:
        print(f"  …… 另有 {len(violations) - 60} 处")
    print("\n修法：  python3 matrix-tools/scripts/rewrite_imports.py --org oagentkit "
          "--root <仓目录> --check      # 先看要改什么\n"
          "        python3 matrix-tools/scripts/rewrite_imports.py --org oagentkit "
          "--root <仓目录> --apply      # 再写盘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
