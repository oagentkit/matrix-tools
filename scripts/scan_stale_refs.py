#!/usr/bin/env python3
"""扫描文档里的陈旧包引用：指向已不存在包的 import 路径。

背景（2026-10-18 实测，两轮）：
    `runtime/breaker` 退役后，`gen_llms_txt.py` 里的「暂不可删」结论被修掉了，
    但 `textx/AGENTS.md` 和 `guardx/AGENTS.md` 的「能力归属表」仍在把熔断
    归给 `github.com/oagentkit/runtime/breaker` —— 那个包已经不存在了。

    而这些 AGENTS.md 恰恰是 AI Agent 读得最多的文件。AI 照着它去 import
    会拿到 module not found。

    更麻烦的是 `guardx/AGENTS.md` 写的是「并发/流控/熔断/事件总线 →
    runtime」，语法上完全合法（runtime 仓还在），靠人读是看不出错的。

    所以这条门禁的价值不是「扫出格式问题」，是**扫出「看起来合法但已经错了」
    的结论** —— 人工 review 恰恰最容易漏这一类。

判定：文档里出现的 `github.com/oagentkit/<repo>/<pkg>` 形式的**包引用**，
比对 modules.json 登记的真实包集合。不在集合里 = 陈旧。

用法:
    scan_stale_refs.py --root <各仓父目录>            # 扫描
    scan_stale_refs.py --root <父目录> --verbose     # 打印命中行
    scan_stale_refs.py --root <父目录> --matrix modules.json

退出码: 0 = 无陈旧引用；1 = 有。

⚠️ **误报是这类扫描的主要风险**，所以刻意保守：
  - 只认 `<repo>/<pkg>` 两段形式，不认单段（单段是仓名不是包）
  - 排除 Markdown 链接 URL（`https://github.com/...`）—— 那是网页地址不是 import
  - 排除「已退役」章节里**有意记录**的历史路径（用 --allow-retired 标注）
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 只匹配两段形式：oagentkit/<repo>/<pkg>。单段（oagentkit/agentkit）是仓名不是包。
PKG_RE = re.compile(r"github\.com/oagentkit/([a-z0-9][a-z0-9-]*/[a-z][a-z0-9-]*)")
URL_RE = re.compile(r"https?://")

# 基础设施仓：不在 modules.json 里，但确实存在
INFRA = {".github", "matrix-tools", "agentkit"}


def load_valid(matrix: Path) -> tuple[set[str], set[str]]:
    """返回 (合法包引用集合, 仓库名集合)。"""
    data = json.loads(matrix.read_text(encoding="utf-8"))
    valid: set[str] = set()
    repos: set[str] = set()
    for m in data["modules"]:
        repos.add(m["repo"])
        for p in m["packages"]:
            valid.add(f"github.com/oagentkit/{m['repo']}/{p.rsplit('/', 1)[-1]}")
    repos |= INFRA
    return valid, repos


def in_retired_section(lines: list[str], idx: int) -> bool:
    """判断该行是否落在「已退役 / 历史」章节里。

    有意记录旧路径是**正确做法**（MIGRATION.md 就该写清 breaker 迁到哪了），
    把这些当错误报出来会逼人删掉有用的历史记录。

    必须向上遍历**整个标题链**，不能只看最近的那个：
        ## 已退役的包
        ### `breaker` —— 三态熔断器     ← 最近标题不含关键词
            import "…/runtime/breaker"   ← 但它在「已退役」章节内
    只看最近标题会把这行误报成陈旧引用。
    """
    for i in range(idx, max(-1, idx - 60), -1):
        m = re.match(r"^(#{1,6})\s+(.*)", lines[i])
        if m and any(k in m.group(2) for k in ("退役", "历史", "迁移对照")):
            return True
    return False


# 「已退役」类标记：出现在引用块里说明该块在**宣布**路径退役，而不是推荐使用。
RETIRED_MARKERS = ("已退役", "已迁出", "已冻结", "已迁移", "迁至", "不再有后续版本")
# 迁移映射箭头：旧路径 → 新路径。
MIGRATION_ARROWS = ("→", "->")


def has_migration_arrow(line: str, valid: set[str]) -> bool:
    """判断该行是否是「旧路径 → 新路径」的迁移映射。

    实测（2026-10-20）：站点 docs/modules/guardx.md:47 写着
        **`audit` / `bashguard` 已迁出**（v0.7.0）：`…/guardx/audit` → `…/obs/audit`
    这一行**必须**保留旧路径——删掉它，下游就不知道该改成什么。把它报成陈旧引用，
    等于逼人删掉最好的迁移说明。

    豁免条件要求箭头的**右侧是合法包**：`→ ` 后面跟着一个当前存在的包，
    证明这行是在做映射，而不是在推荐一个死包。裸写一个死包路径（无箭头）仍然照报。
    """
    for sep in MIGRATION_ARROWS:
        if sep not in line:
            continue
        right = line.split(sep, 1)[1]
        for m in PKG_RE.finditer(right):
            if f"github.com/oagentkit/{m.group(1)}" in valid:
                return True
    return False


def in_retired_blockquote(lines: list[str], idx: int) -> bool:
    """判断该行是否落在「宣布路径已退役」的引用块里。

    实测（2026-10-20）：guardx/AGENTS.md 与 guardx/README.md 各有一处
        > ⚠️ `github.com/oagentkit/guardx/audit` 与 `…/guardx/bashguard` 这两条 import 路径
        > 已**退役**（…）……新代码请用上面表里的新路径。
    这是**正确的迁移警告**：点名死路径是为了让读者避开它。`in_retired_section`
    只认「专门的退役章节」，抓不到这种行内引用块。

    判据：命中行所在的**连续引用块**（连续的 `>` 行）里出现退役标记。
    注意这不会放过真正的陈旧推荐——推荐用某个死包时不会写「已退役」。
    """
    def is_quote(s: str) -> bool:
        return s.lstrip().startswith(">")

    if idx >= len(lines) or not is_quote(lines[idx]):
        return False
    start = idx
    while start > 0 and is_quote(lines[start - 1]):
        start -= 1
    end = idx
    while end < len(lines) - 1 and is_quote(lines[end + 1]):
        end += 1
    block = "\n".join(lines[start:end + 1])
    return any(k in block for k in RETIRED_MARKERS)


def scan(root: Path, valid: set[str], verbose: bool) -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    for md in sorted(root.rglob("*.md")):
        parts = md.parts
        if ".git" in parts or "__pycache__" in parts:
            continue
        # CHANGELOG.md 是**历史记录**，不是当前指引。
        # 它按定义会记录已经退役的包路径——那正是 changelog 该干的事。
        # 用「当前归属是否正确」的标准去扫它，是分类错误：
        # 实测 runtime/CHANGELOG.md 里的
        #   "BREAKING：github.com/oagentkit/runtime/breaker 不再存在。迁移方
        #    import github.com/oagentkit/runtime/breaker"
        # 恰恰是**正确且必要**的迁移说明，却会被报成陈旧引用。
        # （那个标题写的是「迁到」，也不含豁免关键词「迁移对照」——
        #   靠加关键词兜是脆的：changelog 的标题措辞不受控。）
        # 本守卫的靶子是「AI Agent 读得最多的文件」——AGENTS.md / README.md，
        # 那是给 Agent 照着 import 的**当前**指引。
        if md.name == "CHANGELOG.md":
            continue
        lines = md.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines):
            # 文档链接 URL 是网页地址，不是 import 路径
            if URL_RE.search(line) and "import" not in line:
                continue
            if line.lstrip().startswith(("<!--", "|---")):
                continue
            # 行内迁移映射：「旧路径 → 新路径」——保留旧路径是迁移说明的正确写法
            if has_migration_arrow(line, valid):
                continue
            # 引用块里的退役警告：点名死路径是为了让读者避开它
            if in_retired_blockquote(lines, i):
                continue
            for m in PKG_RE.finditer(line):
                full = f"github.com/oagentkit/{m.group(1)}"
                if full in valid:
                    continue
                if in_retired_section(lines, i):
                    continue
                hits.append((str(md.relative_to(root)), i + 1, full, line.strip()[:120]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="各仓工作目录的父目录")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认自动定位）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    matrix = Path(args.matrix) if args.matrix else here.parent / "modules.json"
    if not matrix.is_file():
        print(f"❌ 找不到 modules.json: {matrix}")
        return 1

    root = Path(args.root).expanduser().resolve()
    valid, _ = load_valid(matrix)

    print(f"矩阵包数: {len(valid)}  扫描根目录: {root}")
    hits = scan(root, valid, args.verbose)

    if not hits:
        print("✅ 未发现指向已不存在包的文档引用")
        return 0

    print(f"\n❌ 发现 {len(hits)} 处陈旧包引用（该包已不在 modules.json 中）：\n")
    for path, line, full, text in hits:
        print(f"  {path}:{line}  →  {full}")
        if args.verbose:
            print(f"      {text}")
    print("\n这些是 AI Agent 读得最多的文件里的**错误归属**：照着 import 会 module not found。")
    print("修法：把归属改到真实存在的包；若是有意记录的历史路径，"
          "把它挪到标题含「退役 / 历史 / 迁移」的章节下。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
