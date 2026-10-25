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
  - 排除 Markdown 链接 URL（`https://github.com/...`）—— 那是网页地址不是 import。
    判据是**按匹配位置**判的：落在 URL 里的 token 不算包引用
    （行级豁免仍在，但它有个洞：行里出现 "import" 一词时整行豁免会失效 ⇒
    `https://github.com/oagentkit/llm/issues/47` 这类**网页地址**会被误报成陈旧包引用，
    实测 2026-10-26 命中过）
  - 排除「已退役」章节里**有意记录**的历史路径（用 --allow-retired 标注）
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

# 只匹配两段形式：oagentkit/<repo>/<pkg>。单段（oagentkit/agentkit）是仓名不是包。
PKG_RE = re.compile(r"github\.com/oagentkit/([a-z0-9][a-z0-9-]*/[a-z][a-z0-9-]*)")
URL_RE = re.compile(r"https?://")
# URL 的**跨度**：用来判「这个匹配是不是落在 URL 里」。
URL_SPAN_RE = re.compile(r"https?://\S+")

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


def in_url_span(line: str, start: int) -> bool:
    """该位置的匹配是否落在**URL 里面**。

    为什么要按「匹配位置」判，而不是按整行判：
    行级豁免（「这一行有 URL 就整行跳过」）有个洞 ——
    只要行里还出现 "import" 这个词，整行豁免就失效，于是
    `https://github.com/oagentkit/llm/issues/47` 这种**网页地址**
    会被当成包引用报成「陈旧引用」（实测 2026-10-26：站点新增的 issue 链链接，
    描述里带 "import" 一词 ⇒ 两处误报）。

    而守卫自己的判据写得很清楚：「URL 是网页地址，不是 import」⇒
    URL 里的 token 本来就不该算包引用。行级豁免**保持原样**
    （避免把同一行里 URL 之外的路径也放过），只把「落在 URL 内」的匹配摘掉。
    """
    for m in URL_SPAN_RE.finditer(line):
        if m.start() <= start < m.end():
            return True
    return False


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
                # URL 里的 token 是网页地址，不是 import 路径 ⇒ 不算陈旧引用
                if in_url_span(line, m.start()):
                    continue
                full = f"github.com/oagentkit/{m.group(1)}"
                if full in valid:
                    continue
                if in_retired_section(lines, i):
                    continue
                hits.append((str(md.relative_to(root)), i + 1, full, line.strip()[:120]))
    return hits


def selftest() -> int:
    """反证自测：拿**故意造坏**的文档跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西
    （matrix-tools/AGENTS.md 硬规则 2）。所以样本必须覆盖两类方向：

      ① **该报的必报**（反证）
         - 裸写死包路径（无豁免）        → 必须报出
         - 死包出现在当前指引正文里       → 必须报出
      ② **不该报的不报**（不误报样本）
         - 合法包引用                     → 不报
         - 迁移箭头（旧 → 新）             → 不报（保留旧路径是正确写法）
         - 「已退役」章节内的历史路径       → 不报
         - 宣布退役的引用块               → 不报
         - Markdown 链接 URL              → 不报
         - CHANGELOG.md                   → 不扫（那是历史记录）

    误报是这类扫描的主要风险（脚本 docstring 原话），所以不误报样本
    与反证样本同等重要。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        valid = {"github.com/oagentkit/alpha/core",
                 "github.com/oagentkit/beta/core"}

        def write(rel: str, text: str):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

        # ① 裸写死包路径 → 必须报出
        write("alpha/README.md",
              "能力归属：`github.com/oagentkit/alpha/ghost` 负责熔断。\n")
        # ① 死包在当前指引正文里 → 必须报出
        write("alpha/AGENTS.md",
              "## 能力归属\n\n- 熔断：`github.com/oagentkit/alpha/breaker`\n")
        # ② 合法包 → 不报
        write("beta/README.md",
              "能力归属：`github.com/oagentkit/beta/core` 负责文本。\n")
        # ② 迁移箭头（旧 → 新，右侧合法）→ 不报
        write("beta/AGENTS.md",
              "`audit` / `bashguard` 已迁出：`github.com/oagentkit/beta/audit` → "
              "`github.com/oagentkit/alpha/core`\n")
        # ② 「已退役」章节内的历史路径 → 不报
        write("gamma/README.md",
              "# gamma\n\n## 已退役的包\n\n"
              "### `breaker` —— 三态熔断器\n\n"
              "    import \"github.com/oagentkit/alpha/breaker\"\n")
        # ② 宣布退役的引用块 → 不报
        # 形态取自真实文件 guardx/AGENTS.md:30-32（注意「已**退役**」被粗体打断，
        # 真正生效的是同一块里未被打断的「已冻结」/「不再有后续版本」——
        # 这就是为什么反证样本必须照抄真实形态，不能自己编一句）。
        write("gamma/AGENTS.md",
              "> ⚠️ `github.com/oagentkit/alpha/audit` 这两条 import 路径\n"
              "> 已**退役**（它们曾短暂以嵌套 module 形式存在，tag `audit/v0.1.0`\n"
              "> 仍在，但那条线**已冻结**，不再有后续版本）。新代码请用上面表里的新路径。\n")
        # ② Markdown 链接 URL → 不报
        write("delta/README.md",
              "详见 <https://github.com/oagentkit/alpha/deadlink>。\n")
        # ② URL + 行内出现 "import" 一词 → 仍不报（网页地址不是 import 路径）
        #   实测 2026-10-26 的误报形态：issue 链链接那一行，描述里带 "import" 一词，
        #   行级豁免（「这行有 URL 就整行跳过」）因此失效，URL 里的 llm/issues/47
        #   被当成陈旧包引用报了出来 —— 而它是一个**网页地址**。
        write("epsilon/README.md",
              "需求：llm/llm 扁平化后缺兼容子包 —— 下游需整仓改 import 路径，"
              "见 [`#47`](https://github.com/oagentkit/llm/issues/47)。\n")
        # ① 同一行既有 URL 又有裸死包 import → 必报
        #   反证：URL 豁免只能摘掉「落在 URL 内」的匹配，不能把整行放过 ——
        #   否则这一行就成了一条藏死包路径的免死金牌。
        write("zeta/AGENTS.md",
              'import "github.com/oagentkit/alpha/breaker" —— 见 '
              "https://github.com/oagentkit/alpha/breaker/issues/1\n")
        # ② CHANGELOG.md → 不扫
        write("alpha/CHANGELOG.md",
              "BREAKING：`github.com/oagentkit/alpha/breaker` 不再存在。\n")

        hits = scan(root, valid, verbose=False)
        found = {full for _, _, full, _ in hits}
        paths = {p for p, _, _, _ in hits}

        # ① 反证：死包必须被报出
        check("① 裸写死包被报出",
              "github.com/oagentkit/alpha/ghost" in found, True)
        check("① 正文里的死包被报出",
              "github.com/oagentkit/alpha/breaker" in found, True)
        # ② 不误报
        check("② 合法包不误报",
              "github.com/oagentkit/beta/core" in found, False)
        check("② 迁移箭头不误报",
              "github.com/oagentkit/beta/audit" in found, False)
        check("② 已退役章节不误报",
              "github.com/oagentkit/alpha/breaker" in
              {full for p, _, full, _ in hits if p.startswith("gamma/")}, False)
        check("② 退役引用块不误报",
              "github.com/oagentkit/alpha/audit" in found, False)
        check("② URL 不误报",
              "github.com/oagentkit/alpha/deadlink" in found, False)
        # ② URL + "import" 一词 → 不误报（实测 2026-10-26 的误报形态）
        check("② URL+import 词不误报",
              "github.com/oagentkit/llm/issues" in found, False)
        # ① 同一行的裸死包 import → 仍必报（URL 豁免不得放过整行）
        check("① 同行的裸死包仍报出",
              "github.com/oagentkit/alpha/breaker" in found, True)
        check("② CHANGELOG.md 不扫",
              any(p == "alpha/CHANGELOG.md" for p in paths), False)
        # 命中总数：恰好三条死包（alpha/ghost、alpha/AGENTS 的 breaker、zeta 的裸 import）
        check("命中总数恰好 3", len(hits), 3)

    if fails:
        print("❌ scan_stale_refs 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ scan_stale_refs 自测通过"
          "（死包必报；迁移箭头 / 退役章节 / 退役引用块 / URL / CHANGELOG 不误报）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="各仓工作目录的父目录")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认自动定位）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root:
        print("❌ 缺少 --root（或用 --selftest）")
        return 1

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
