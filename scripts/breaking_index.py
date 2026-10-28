#!/usr/bin/env python3
"""从各模块仓的 CHANGELOG.md 抽取破坏性变更条目（breakingIndex 的**唯一来源**）。

## 为什么要有这个

`versions.json` 的 `breakingIndex` 曾整段是空的（13 个模块全为 `[]`），
而各仓 CHANGELOG 里**明确标着** BREAKING 变更。矩阵说「无破坏性变更」、
CHANGELOG 说「import 路径已变」——入口与事实不一致，下游只能**先构建断裂**、
再去翻 llms.txt 才知道怎么改。

更糟的一类：矩阵自己的 `packages[].importPath` 登记的是变更**前**的旧路径。
机器按它写 import，直接编译不过——而 breakingIndex 若为空，连「这里变过」都查不到。

## 判定「破坏性」的五条依据（任一命中即算）

1. 小节标题带 conventional commit 的叹号（`### 重构(llm)!：…`）；
2. 小节标题里出现 `Breaking` / `BREAKING`；
3. 小节正文里有 `BREAKING CHANGE:` / `BREAKING：` / `BREAKING:` 行；
4. **同一个包名出现在 ≥2 个不同模块下**（包在模块之间搬家）。

第 3 条是最可靠的一条：它由 commit body 里的 conventional commit trailer 渲染而来，
与发版时写的语义同源。另两条是补充——有些破坏性变更的 commit 没写 trailer，
但标题带了 `!`（发版时用 `!` 标了破坏性）。

第 5 条是 2026-10-28 实测后补的：中文发布说明最常写的「行首声明」
（`破坏性变更（…）：…`）不落在前四条依据的任何一条上 ⇒ 抽取器收不到 ⇒
矩阵对下游说「无破坏性变更」（实测 mcpkit v0.9.0）。只认**行首**（句中散文是
事实陈述，不是声明），排除否定形态（`破坏性变更：无`）与核对记录
（`破坏性变更核对（…）`）。

第 4 条是 2026-10-20 实测后补的：有一类破坏性变更**只写在散文里、没有显式标记**——
`llm v0.12.0` 把 `github.com/oagentkit/obs/obsx` 迁到 `github.com/oagentkit/llm/obsx`，
import 路径真的变了，但版本段里既没有 `BREAKING`、标题也没有 `!`，
按前三条判定会整个漏掉 → 矩阵对下游说「无破坏性变更」→ 下游照矩阵升版直接构建断裂。
判据是「同一个包 basename 出现在 ≥2 个不同模块前缀下」，比「出现两个路径」严：
`queue v0.4.0` 的 `github.com/oagentkit/queue` 与 `github.com/oagentkit/queue/pglease`
是同仓两个包，不是搬家，不能误判。

**独立 oracle**：`scripts/verify_breaking_oracle.py` 用另一套判据（版本段级聚合、
大小写不敏感的宽口径标记、跨模块搬家）重新对账，判据与本文件不相干——
本文件漏收时那条门禁必须能报出来。

## 边界

- **只认 `## [vX.Y.Z]` 形式的版本段**。CHANGELOG 里可能出现 `## 1. 某某` 这种
  commit body 里的 markdown 标题——它不是版本段，不能当版本边界切。
- 版本段内没有 `###` 小节时，整段正文当作一个小节处理。
- `detail` 存小节全文（迁移说明在里面——breakingIndex 的价值不在「告知会坏」，
  在「告知怎么修」）。

用法:
    from breaking_index import extract
    extract(open("CHANGELOG.md", encoding="utf-8").read())
"""

from __future__ import annotations

import argparse

import re

# 版本段：`## [v0.9.0] — 2026-10-19`（破折号可能是 em dash / 连字符 / 无日期）
VERSION_HEADING = re.compile(
    r"^##\s*\[(?P<version>v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?)\]\s*(?:[—–-]\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)

# 小节标题：`### 重构(llm)!：包上移至 module 根`
SUB_HEADING = re.compile(r"^###\s+(?P<title>.+?)\s*$")

# 破坏性 trailer 行：`BREAKING CHANGE: …` / `BREAKING：…` / `BREAKING: …`
BREAKING_LINE = re.compile(r"^\s*BREAKING(?:\s+CHANGE)?\s*[：:]\s*(?P<text>.*)$")

# 行首的破坏性**声明**（中文发布说明的常见写法）：`破坏性变更（…）：…`。
#
# 为什么需要它：第 3 条依据认的是**英文 trailer**（`BREAKING CHANGE:`），
# 而中文发布说明最常写的是「行首声明」——它不落在四条依据的任何一条上 ⇒
# 抽取器收不到 ⇒ 矩阵对下游说「无破坏性变更」（实测 mcpkit v0.9.0：
# 段里明确写着「破坏式变更（0.x 阶段 minor 版）：…」，而 breakingIndex 是空的）。
#
# 为什么只认**行首**：句中的「…对下游是隐式破坏性变更」是**事实陈述**，不是声明
# ——独立 oracle 早已因此收窄过自己那条（见其文件头注释）。行首声明与那种
# 句中散文正好分得开。
#
# 为什么短语后必须跟标点（`：` / `（`）或行尾：否则「破坏性变更**核对**（均无影响）」
# 这种**核对记录**会被当成声明（实测样本：guardx v0.5.0 的段里就有一行）。
#
# 为什么排除否定形态：「破坏性变更：无」说的是「这版**没有**破坏性变更」，
# 把它算成破坏性就是对下游说谎。
BREAKING_DECL_LINE = re.compile(
    r"^\s{0,3}(?:[-*+>]\s*)?(?:\*\*)?(?:破坏性变更|破坏式变更)(?:\*\*)?\s*(?:[（(：:]|$)"
)
# 声明后面的否定词（去掉前导标点后比对）。
DECL_NEGATION = re.compile(r"^(?:无|没有|不|非|暂无)")

# 标题里的叹号（conventional commit 的破坏性标记）：`重构(llm)!：`
BANG_IN_TITLE = re.compile(r"[a-zA-Z]!|!\s*[：:]")

# 标题里的 Breaking 字样：**标题本身就是 Breaking 段**才算
# （`### Breaking：…`）。散文里提到 breaking（`### 修复：修 breaking change 的说明`）
# 不算——否则每条「解释破坏性变更」的修复说明都会被误判成破坏性变更。
BREAKING_IN_TITLE = re.compile(r"^breaking\b", re.IGNORECASE)

# 一方模块的包路径：github.com/oagentkit/<模块>/<包>
OAGENT_PATH = re.compile(r"github\.com/oagentkit/([A-Za-z0-9_.\-/]+)")


def _moved_packages(body: list[str]) -> dict[str, list[str]]:
    """同一个包 basename 出现在 ≥2 个不同模块前缀下 → 该包在模块之间搬家。

    比「出现两个路径」严：同仓两个包（queue 与 queue/pglease）不是搬家。
    """
    by: dict[str, set[str]] = {}
    for line in body:
        for m in OAGENT_PATH.finditer(line):
            path = m.group(1).rstrip(".,，。；;）)")
            parts = path.split("/")
            if len(parts) < 2:
                continue
            by.setdefault(parts[-1], set()).add(parts[0])
    return {k: sorted(v) for k, v in by.items() if len(v) >= 2}


def _sections(text: str) -> list[dict]:
    """把 CHANGELOG 切成版本段。

    只认 `## [vX.Y.Z]` 形式的标题；其余 `##`（commit body 里的 markdown 标题）
    留在正文里，不当版本边界。
    """
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = VERSION_HEADING.match(line)
        if m:
            cur = {
                "version": m.group("version"),
                "date": m.group("date") or "",
                "lines": [],
            }
            out.append(cur)
            continue
        if cur is not None:
            cur["lines"].append(line)
    return out


def _subsections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """版本段正文 → [(小节标题, 小节正文行)]。

    没有 `###` 小节时，整段正文作为一个标题为空的小节。
    """
    subs: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    seen = False
    for line in lines:
        m = SUB_HEADING.match(line)
        if m:
            if seen or body:
                subs.append((title, body))
            title = m.group("title")
            body = []
            seen = True
            continue
        body.append(line)
    if seen or body:
        subs.append((title, body))
    return subs


def _is_breaking(title: str, body: list[str]) -> tuple[bool, str]:
    """→ (是否破坏性, trailer 摘要)"""
    if BANG_IN_TITLE.search(title) or BREAKING_IN_TITLE.search(title):
        return True, ""
    for line in body:
        m = BREAKING_LINE.match(line)
        if m:
            # trailer 之后的缩进续行也算摘要的一部分
            # （runtime 的 BREAKING：… 后跟着两行缩进给出迁移写法）
            summary = [m.group("text").rstrip()]
            return True, "\n".join(summary).strip()
    # 第 4 条依据：包在模块之间搬家（散文形态，无显式 trailer）
    if _moved_packages(body):
        return True, ""
    # 第 5 条依据：正文里**行首**的破坏性声明（中文发布说明的常见写法）
    if _decl_line(body):
        return True, ""
    return False, ""


def _decl_line(body: list[str]) -> str:
    """正文里行首的破坏性声明行（没有则空串）。

    否定形态（`破坏性变更：无`）不算——它说的是「这版没有破坏性变更」。
    """
    for line in body:
        m = BREAKING_DECL_LINE.match(line)
        if not m:
            continue
        rest = line[m.end():].lstrip("：:（( \t")
        if DECL_NEGATION.match(rest):
            continue
        return line.strip()
    return ""


def _summary_with_continuation(body: list[str]) -> str:
    """BREAKING 行 + 它所属的段落。

    续行规则：直到空行、列表项（"- "）或新小节标题为止。
    **不看缩进**——trailer 的句子可能跨行且第二行不缩进
    （实测 agent v0.8.0：`BREAKING CHANGE: … import 路径为` 换行接着写
    `github.com/oagentkit/llm）。`）。只看缩进会把句子截在半路，
    而「机器读到的就是错的」正是本 issue 要治的病。
    """
    out: list[str] = []
    started = False
    for line in body:
        m = BREAKING_LINE.match(line)
        if m and not started:
            started = True
            out.append(m.group("text").rstrip())
            continue
        if started:
            s = line.strip()
            if not s or s.startswith("- ") or s.startswith("#"):
                break
            out.append(line.rstrip())
    if not out:
        # 没有 trailer 行时：行首的破坏性声明就是摘要（它是发版人自己写的
        # 「这版破坏了 + 怎么改」——机器读者要的就是这句）。
        return _decl_line(body)
    return "\n".join(out).strip()


def extract(text: str) -> list[dict]:
    """CHANGELOG 全文 → 破坏性变更条目列表（新 → 旧）。"""
    entries: list[dict] = []
    for sec in _sections(text):
        for title, body in _subsections(sec["lines"]):
            breaking, _ = _is_breaking(title, body)
            if not breaking:
                continue
            summary = _summary_with_continuation(body)
            detail = "\n".join(body).strip()
            # 没有 trailer 行时（标题带 `!` 或散文形态的搬家），摘要退回标题——
            # 空摘要对机器读者等于「有变更但说不清是什么」。
            if not summary:
                moved = _moved_packages(body)
                if moved:
                    summary = "包在模块之间搬家：" + ", ".join(
                        f"{k} → {'/'.join(v)}" for k, v in sorted(moved.items()))
                else:
                    summary = title
            entries.append({
                "version": sec["version"],
                "date": sec["date"],
                "title": title,
                "summary": summary,
                "detail": detail,
            })
    return entries


# ---------------------------------------------------------------- 自测

_FIXTURE = """# demo CHANGELOG

## [v0.9.0] — 2026-10-19

### 重构(llm)!：包上移至 module 根，import 路径不再重复末段

BREAKING CHANGE: import 路径 github.com/oagentkit/llm/llm → github.com/oagentkit/llm。
包名 llm 恰好等于仓名 llm。

## [v0.8.0] — 2026-10-19

- 杂务(deps)：依赖升级

## [v0.5.0] — 2026-10-19

### 重构：移除本仓的 breaker 实现，迁到 oekit/concurrency/breaker

三态状态机全部改用上游。
BREAKING：github.com/oagentkit/runtime/breaker 不再存在。迁移方
  import github.com/oagentkit/runtime/breaker
    -> github.com/oekit/concurrency/breaker
注意语义差异。

## [v0.4.0] — 2026-10-19

### Breaking：配置格式变更

老的配置格式不再支持。

## [v0.3.0] — 2026-10-19

## 1. 缺两个 re-export 包

这不是版本段，是 commit body 里的 markdown 标题。
"""


def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    entries = extract(_FIXTURE)
    check("抽出 3 条", len(entries), 3)
    if len(entries) == 3:
        check("v0.9.0 版本号", entries[0]["version"], "v0.9.0")
        check("v0.9.0 日期", entries[0]["date"], "2026-10-19")
        check("v0.9.0 标题", entries[0]["title"], "重构(llm)!：包上移至 module 根，import 路径不再重复末段")
        check("v0.9.0 摘要含同段续行", entries[0]["summary"],
              "import 路径 github.com/oagentkit/llm/llm → github.com/oagentkit/llm。\n"
              "包名 llm 恰好等于仓名 llm。")
        # runtime 形态：标题无叹号，靠正文 trailer 判定
        check("v0.5.0 靠 trailer 命中", entries[1]["version"], "v0.5.0")
        check("v0.5.0 摘要含续行", entries[1]["summary"],
              "github.com/oagentkit/runtime/breaker 不再存在。迁移方\n"
              "  import github.com/oagentkit/runtime/breaker\n"
              "    -> github.com/oekit/concurrency/breaker\n"
              "注意语义差异。")
        # 标题里的 Breaking 字样
        check("v0.4.0 靠标题命中", entries[2]["version"], "v0.4.0")
        # 非版本段（## 1. …）不得被当版本边界
        check("非版本段被忽略", [e["version"] for e in entries],
              ["v0.9.0", "v0.5.0", "v0.4.0"])

    # 负向样本：普通小节不得被误判成破坏性
    plain = extract("## [v1.0.0] — 2026-01-01\n\n### 修复(deps)：升级依赖\n\n依赖升级。\n")
    check("普通小节不误判", len(plain), 0)

    # 负向样本：正文里提到 "breaking" 但不是 trailer 行
    mention = extract("## [v1.0.0] — 2026-01-01\n\n### 修复：修 breaking change 的说明\n\n正文提到 breaking 但没写 trailer。\n")
    check("仅提及不误判", len(mention), 0)

    # 负向样本：空 CHANGELOG
    check("空文件", extract(""), [])
    check("无版本段", extract("# demo\n\n随便写点什么\n"), [])

    # 第 4 条依据：散文形态的「包在模块之间搬家」——没有 BREAKING、没有 `!`
    moved = extract(
        "## [v0.12.0] — 2026-10-20\n\n"
        "### 新增(obsx)：接收 obs/obsx\n\n"
        "自 github.com/oagentkit/obs/obsx 迁入。import 路径从\n"
        "github.com/oagentkit/obs/obsx 变为 github.com/oagentkit/llm/obsx。\n"
    )
    check("散文搬家被收", len(moved), 1)
    if moved:
        check("搬家摘要", "搬家" in moved[0]["summary"], True)
    # 负向样本：同仓两个包不是搬家
    same_repo = extract(
        "## [v0.4.0] — 2026-10-20\n\n"
        "### 新增：queue 仓建立\n\n"
        "收编 2 个包：\n- github.com/oagentkit/queue\n- github.com/oagentkit/queue/pglease\n"
    )
    check("同仓两包不误判", len(same_repo), 0)

    # 第 5 条依据：正文里**行首**的破坏性声明（中文发布说明的常见写法）
    decl = extract(
        "## [v0.9.0] — 2026-10-28\n\n"
        "### 新增(mcp)：工具暴露名带 server 命名空间\n\n"
        "破坏式变更（0.x 阶段 minor 版）：模型侧看到的工具名变成 `server.原始名`。\n"
        "历史 checkpoint 里的 tool_call 名字与新名对不上。\n"
    )
    check("行首声明被收", len(decl), 1)
    if decl:
        check("行首声明的摘要就是那句声明",
              decl[0]["summary"],
              "破坏式变更（0.x 阶段 minor 版）：模型侧看到的工具名变成 `server.原始名`。")
    # 负向样本：句中的散文是**事实陈述**，不是声明
    prose = extract(
        "## [v0.5.0] — 2026-10-28\n\n"
        "### 杂务(deps)：依赖地板追平\n\n"
        "地板抬高对下游是隐式破坏性变更：不跑 go mod tidy 就红。\n"
    )
    check("句中散文不误判", len(prose), 0)
    # 负向样本：否定形态说的是「这版没有破坏性变更」
    negated = extract(
        "## [v0.5.0] — 2026-10-28\n\n"
        "### 杂务：版本例行发布\n\n"
        "破坏性变更：无\n"
    )
    check("否定形态不误判", len(negated), 0)
    # 负向样本：核对记录不是声明
    checklist = extract(
        "## [v0.5.0] — 2026-10-28\n\n"
        "### 杂务：版本例行发布\n\n"
        "破坏性变更核对（均无影响）：逐条看过。\n"
    )
    check("核对记录不误判", len(checklist), 0)

    if fails:
        print("❌ breaking_index 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ breaking_index 自测通过（五条判定依据 + 负向样本 + 非版本段边界）")
    return 0


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(description="从 CHANGELOG 抽取破坏性变更条目")
    # --selftest 是「默认就跑自测」的显式写法（与其他守卫脚本的参数习惯一致）
    _ap.add_argument("--selftest", action="store_true", help="离线自测（默认行为）")
    _ap.parse_args()
    raise SystemExit(selftest())
