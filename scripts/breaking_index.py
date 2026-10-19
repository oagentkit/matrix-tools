#!/usr/bin/env python3
"""从各模块仓的 CHANGELOG.md 抽取破坏性变更条目（breakingIndex 的**唯一来源**）。

## 为什么要有这个

`versions.json` 的 `breakingIndex` 曾整段是空的（13 个模块全为 `[]`），
而各仓 CHANGELOG 里**明确标着** BREAKING 变更。矩阵说「无破坏性变更」、
CHANGELOG 说「import 路径已变」——入口与事实不一致，下游只能**先构建断裂**、
再去翻 llms.txt 才知道怎么改。

更糟的一类：矩阵自己的 `packages[].importPath` 登记的是变更**前**的旧路径。
机器按它写 import，直接编译不过——而 breakingIndex 若为空，连「这里变过」都查不到。

## 判定「破坏性」的三条依据（任一命中即算）

1. 小节标题带 conventional commit 的叹号（`### 重构(llm)!：…`）；
2. 小节标题里出现 `Breaking` / `BREAKING`；
3. 小节正文里有 `BREAKING CHANGE:` / `BREAKING：` / `BREAKING:` 行。

第 3 条是最可靠的一条：它由 commit body 里的 conventional commit trailer 渲染而来，
与发版时写的语义同源。另两条是补充——有些破坏性变更的 commit 没写 trailer，
但标题带了 `!`（发版时用 `!` 标了破坏性）。

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

# 标题里的叹号（conventional commit 的破坏性标记）：`重构(llm)!：`
BANG_IN_TITLE = re.compile(r"[a-zA-Z]!|!\s*[：:]")

# 标题里的 Breaking 字样：**标题本身就是 Breaking 段**才算
# （`### Breaking：…`）。散文里提到 breaking（`### 修复：修 breaking change 的说明`）
# 不算——否则每条「解释破坏性变更」的修复说明都会被误判成破坏性变更。
BREAKING_IN_TITLE = re.compile(r"^breaking\b", re.IGNORECASE)


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
    return False, ""


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
            # 没有 trailer 行时（标题带 `!` 的那种），摘要退回标题——
            # 空摘要对机器读者等于「有变更但说不清是什么」。
            if not summary:
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

    if fails:
        print("❌ breaking_index 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ breaking_index 自测通过（三种判定依据 + 负向样本 + 非版本段边界）")
    return 0


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(description="从 CHANGELOG 抽取破坏性变更条目")
    # --selftest 是「默认就跑自测」的显式写法（与其他守卫脚本的参数习惯一致）
    _ap.add_argument("--selftest", action="store_true", help="离线自测（默认行为）")
    _ap.parse_args()
    raise SystemExit(selftest())
