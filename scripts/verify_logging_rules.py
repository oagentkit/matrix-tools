#!/usr/bin/env python3
"""loggingRule ↔ 代码交叉校验：modules.json 的日志规则不是装饰性文案。

背景（2026-11-07，agentkit#139 实测）：
    modules.json 是机器可读唯一事实源，但 rag 条目的 loggingRule 写着
    「本仓不引入日志依赖」（从 tier0 模板复制未改），而 rag/local.go 里
    就有 `logx.Delegating("agentkit-rag")`——规则与代码各说各话，且无人发现。
    同时 queue/llm 等仓的规则明文要求组件名 `oagentkit-<module>`，代码里
    却残留旧 org 名 `agentkit-leader` / `agentkit-worker`。

    这类漂移靠人读两边对不上——正是门禁该扫的「看起来合法但已经错了」。

判定（两条，都对着 modules.json 的声明面）：
  1. loggingRule 声明「不引入日志依赖」的仓：代码里出现 `oekit/logx`
     import 即红（规则过时或代码越界，总要有一边改）。
  2. loggingRule 给出组件名样例 `logx.Delegating("oagentkit-<x>")` 的仓：
     代码里所有 `logx.Delegating("...")` / `logx.New(...)` 组件名必须
     以 `oagentkit-` 开头，否则红（旧 org 名 / 随手命名的组件名都会让
     下游按规则做的日志过滤/路由失配）。

用法:
    verify_logging_rules.py --root <各仓父目录> [--matrix modules.json]

退出码: 0 = 一致；1 = 有漂移。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# modules.json 里声明的组件名样例：logx.Delegating("oagentkit-<x>")
RULE_COMPONENT_RE = re.compile(r"logx\.Delegating\(\"(oagentkit-[a-z0-9-]+)\"\)")
# 「不引入日志依赖」声明（加粗形态也算）
NO_LOG_RE = re.compile(r"不引入日志依赖")
# 代码里的 logx import（直接依赖；// indirect 的 go.mod 行不在 .go 扫描面）
LOGX_IMPORT_RE = re.compile(r"\"github\.com/oekit/logx")
# 代码里的组件 logger 构造（Delegating / New 两种形态）
COMPONENT_CALL_RE = re.compile(r"logx\.(?:Delegating|New)\(\s*\"([^\"]+)\"")

# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
SMOKE_ARGS = ["--selftest"]


def load_matrix(matrix_path: Path) -> dict:
    with matrix_path.open(encoding="utf-8") as f:
        return json.load(f)


def rule_face(rule: str | None) -> tuple[bool, str | None]:
    """把 loggingRule 文本折叠成判定面：(声明不引入日志, 声明的组件名样例)。"""
    if not rule:
        return False, None
    declared = RULE_COMPONENT_RE.search(rule)
    return bool(NO_LOG_RE.search(rule)), declared.group(1) if declared else None


def scan_repo(repo_dir: Path) -> tuple[list[str], list[str]]:
    """返回 (logx import 命中文件, 代码里的组件名列表)。"""
    imports: list[str] = []
    components: list[str] = []
    for go in repo_dir.rglob("*.go"):
        if "vendor" in go.parts:
            continue
        try:
            text = go.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(go.relative_to(repo_dir))
        if LOGX_IMPORT_RE.search(text):
            imports.append(rel)
        for m in COMPONENT_CALL_RE.finditer(text):
            components.append(m.group(1))
    return imports, components


def check(root: Path, matrix: dict) -> list[str]:
    findings: list[str] = []
    modules = [x for x in matrix.get("modules", []) if isinstance(x, dict)]
    for mod in modules:
        repo = mod.get("repo")
        if not repo:
            continue
        repo_dir = root / repo
        if not repo_dir.is_dir():
            continue  # 本地缺仓静默跳过（与 bump_dep_floors 同口径）
        no_log, _declared = rule_face(mod.get("loggingRule"))
        imports, components = scan_repo(repo_dir)
        if no_log and imports:
            findings.append(
                f"{repo}: loggingRule 声明「不引入日志依赖」，但代码 import oekit/logx："
                + "、".join(sorted(imports)[:5])
            )
        bad = sorted({c for c in components if not c.startswith("oagentkit-")})
        if bad:
            findings.append(
                f"{repo}: 组件 logger 名不以 oagentkit- 开头（loggingRule 要求 oagentkit-<module>）："
                + "、".join(bad)
            )
    return findings


def selftest() -> int:
    import tempfile

    ok = True

    def check_case(name: str, got, want) -> None:
        nonlocal ok
        status = "ok" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{status}] {name}: got={got!r} want={want!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 仓 A：声明不用日志但代码用了（agentkit#139 的 rag 形态）
        (root / "a").mkdir()
        (root / "a" / "x.go").write_text('import "github.com/oekit/logx"\n')
        # 仓 B：组件名旧 org 名
        (root / "b").mkdir()
        (root / "b" / "y.go").write_text('var l = logx.Delegating("agentkit-b")\n')
        # 仓 C：完全合规
        (root / "c").mkdir()
        (root / "c" / "z.go").write_text('var l = logx.Delegating("oagentkit-c")\n')
        matrix = {
            "modules": [
                {"repo": "a", "loggingRule": "**本仓不引入日志依赖。**"},
                {"repo": "b", "loggingRule": '- 日志走 `oekit/logx`，组件 logger 用 `logx.Delegating("oagentkit-b")`。'},
                {"repo": "c", "loggingRule": '- 日志走 `oekit/logx`，组件 logger 用 `logx.Delegating("oagentkit-c")`。'},
            ]
        }
        findings = check(root, matrix)
        check_case("命中数（a 越界 + b 旧名）", len(findings), 2)
        check_case("a 越界被点名", any(x.startswith("a:") and "logx" in x for x in findings), True)
        check_case("b 旧名被点名", any(x.startswith("b:") and "agentkit-b" in x for x in findings), True)
        check_case("c 零命中", any(x.startswith("c:") for x in findings), False)
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="loggingRule ↔ 代码交叉校验")
    ap.add_argument("--root", default=None, help="各仓父目录（--selftest 时可省）")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--selftest", action="store_true", help="只跑自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    matrix_path = Path(args.matrix) if args.matrix else root / "matrix-tools" / "modules.json"
    if not matrix_path.is_file():
        print(f"❌ 找不到 modules.json：{matrix_path}")
        return 1
    findings = check(root, load_matrix(matrix_path))
    if findings:
        print(f"❌ loggingRule 与代码漂移（{len(findings)} 处）：")
        for f in findings:
            print(f"  - {f}")
        print("修法：要么改代码对齐规则（组件名 oagentkit-<module> / 撤掉越界 import），")
        print("      要么改 modules.json 的 loggingRule 使其符合真实架构（如 rag 实际走 logx）。")
        return 1
    print("✅ loggingRule 与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
