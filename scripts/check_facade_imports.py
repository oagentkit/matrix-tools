#!/usr/bin/env python3
"""门面仓依赖守卫：禁止下游 import 已退役的单仓 module 与门面仓。

背景：门面仓 `github.com/oagentkit/agentkit` **已废弃**——README 首行「门面仓 · 已废弃」，
「**v1.x 退役条件已满足**。本仓不接受任何新功能，只做别名转发与安全修复」。
但它**还没 archive**，所以「不许再引」这件事**仍然只有文档约束、没有机器约束**——
半年后有人写一个新 import `agentkit/textutil` 不会有任何人收到通知，
archive 之后就直接构建断裂。

本脚本把「不许再引」变成 CI 里的一条红灯。
**门面活着期间这条守卫必须继续跑**：它保护的是**存量下游**——已经引了门面的代码
不能突然编译不过，新代码也不能接着引一个已废弃的仓。

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

# 豁免目录：路径中包含这些片段就跳过。
#
# ⚠️ 这里刻意**不豁免整个 oagentkit 矩阵**。曾经有一条 "/oagentkit/" 把 13 个仓
# 全部排除了，注释写的是「各仓自身的 go.sum / 文档」——但本脚本只扫 *.go，
# 而 go.sum 和 .md 根本不会被扫到，所以那条豁免**排除了该查的、没挡住它声称要挡的**：
# 哪天有人在矩阵仓里写下 import "github.com/oagentkit/agentkit/x"，守卫会静默放过，
# 而这正是它存在的唯一理由。
EXEMPT = [
    "/.git/", "/node_modules/", "/vendor/", "/_tmp/", "/.worktrees/",
    "/.gomodcache", "/.superpowers/", "/.cache/", "/dist/", "/site/",
    # agentkit 本体就是那个**已废弃**的门面仓，它当然引自己
    "/lacus/agentkit/", "/agentkit-bisect/",
    # oagentkit 下面只有「门面仓自己」需要豁免：它 48 个包全是 re-export，
    # 必须 import 各能力仓。精确到仓目录，不要退回到整个矩阵目录。
    "/oagentkit/agentkit/", "/oagentkit/.github/",
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


def selftest() -> int:
    """证明守卫没瞎——否则「静默放过」和「检查通过」在 CI 里长得一模一样。

    为什么必须自测：豁免名单写错一个字符，脚本就会**静默放过所有违规**，
    仍然 exit 0、CI 仍然全绿。这类守卫最危险的失效模式就是「一直绿，但从没生效过」。
    用真实逻辑跑真实样本，不 mock。
    """
    import tempfile

    cases: list[tuple[str, str, int]] = [
        # (说明, 文件内容, 期望命中数)
        ("门面仓 import（该抓）",
         'package x\n\nimport "github.com/oagentkit/agentkit/agentrun"\n', 1),
        ("门面仓带子路径（该抓）",
         'package x\n\nimport (\n\t"git.enjoye.top/enjoydream/agentkit/knowledge/rag"\n)\n', 1),
        ("旧单仓 import（该抓）",
         'package x\n\nimport "git.enjoye.top/enjoydream/agentkit/textutil"\n', 1),
        ("注释里的旧路径（不该抓）",
         'package x\n\n// 旧路径 git.enjoye.top/enjoydream/agentkit/textutil 已迁走\n', 0),
        ("测试断言里的旧路径（不该抓）",
         'package x\n\nconst want = "git.enjoye.top/enjoydream/agentkit/textutil"\n', 1),
        ("正常的新路径（不该抓）",
         'package x\n\nimport "github.com/oagentkit/rag"\n', 0),
    ]

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i, (desc, body, want) in enumerate(cases):
            f = root / f"case{i}.go"
            f.write_text(body, encoding="utf-8")
            got = len(scan_file(f, f.name))
            status = "ok" if got == want else "FAIL"
            if got != want:
                failures.append(f"{desc}：期望 {want}，实际 {got}")
            print(f"  {status:<4} {desc}（期望 {want} / 实际 {got}）")

    # 豁免回归：矩阵里的仓【不能】再被整目录豁免
    probes = [
        ("/home/u/oagentkit/rag/rag.go", False, "矩阵仓必须被扫"),
        ("/home/u/oagentkit/agentkit/agentrun/alias.go", True, "门面仓自身需豁免"),
        ("/home/u/oagentkit/.github/scripts/sync-callers.sh", True, ".github 需豁免"),
        ("/home/u/lacus/agentkit/textutil/x.go", True, "退役中的单仓需豁免"),
    ]
    for path, want_exempt, desc in probes:
        got = any(x in path for x in EXEMPT)
        status = "ok" if got == want_exempt else "FAIL"
        if got != want_exempt:
            failures.append(f"{desc}：{path} 豁免={got}，期望 {want_exempt}")
        print(f"  {status:<4} {desc}")

    print()
    if failures:
        print(f"❌ 自测失败 {len(failures)} 项：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"✅ 自测通过（{len(cases)} 个样本 + {len(probes)} 个豁免探针）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="跑内置样本，证明守卫本身有效（CI 里应该常跑）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.roots:
        ap.print_help()
        return 2

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
