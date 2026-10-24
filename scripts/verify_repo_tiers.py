#!/usr/bin/env python3
"""各模块仓 `AGENTS.md` / `README.md` 声明的层位 vs 矩阵单一数据源 `modules.json`。

## 守的不变量

**每个模块仓的 `AGENTS.md` 与 `README.md` 声明的层位，必须与 `matrix-tools/modules.json` 一致。**

这两个文件都是仓级的第一入口（人读 + agent 读）。它们写错层位，读者就会按错的层位
去判断依赖方向——而分层是矩阵的硬不变量（tier N 不得依赖 tier > N）。

## 为什么 `README.md` 也要守（实测 2026-10-24）

`AGENTS.md` 那一侧的漂移被本守卫抓住并刷正了（queue/obs/skillkit/mcpkit/rag 5 个仓，
见下节），但**同一批仓的 `README.md` 至今仍写着重定位之前的旧层位**——
上一轮只修了 `AGENTS.md`，因为本守卫当时**只读 `AGENTS.md`**。

实测同一形态的第二个入口：

| 仓 | `README.md` 声明 | `modules.json` |
|---|---|---|
| `queue` | tier 0（基础层） | **tier 2** |
| `obs` / `skillkit` / `mcpkit` / `rag` | tier 1（能力层，依赖 tier0） | **tier 2** |

⇒ **同一个仓的两个第一入口互相矛盾**（`AGENTS.md` 说 tier 2、`README.md` 说 tier 1），
比单个入口写错更糟：读者读到哪个就信哪个。

所以本守卫对两份文档各认一套「自述层位」形态（见 `DOC_PROFILES`），
判据、判定档位、`--write` 的契约完全共用——**不新造第二套守卫**。

同仓的 `verify_repo_dep_claims.py` 本来就读两个文件（`DOC_FILES = ("AGENTS.md", "README.md")`），
覆盖面不一致本身就是一条缺口。

## 为什么这条缺口此前没人守

实测 2026-10-24：`queue` / `obs` / `skillkit` / `mcpkit` / `rag` **5 个仓**的
`AGENTS.md` 写的都是**重定位之前**的旧层位（queue 写 tier 0，其余 4 个写 tier 1），
而 `modules.json` 里它们全是 tier 2。

三个本该抓住它的机制**逐个失效**：

1. `verify_repo_docs.py` **在 CI 里跑**，但它是**报告器**（`continue-on-error`，
   恒不失败）——它比对的是整文件 diff，而 13 个仓的 `AGENTS.md` 都有大量手写段落
   （仓库版本是更丰富的那个），差异噪音太大 ⇒ 恒红 ⇒ 与「红了就加豁免」同命。
2. `verify_profile_tiers.py` 只守 org profile（`.github/profile/README.md`）的**一处生成区**，
   不覆盖各仓 `AGENTS.md`。
3. `gen_docs.py` 本可修好它，但它的合并模型要求目标文件带
   `<!-- oagentkit:generated:begin -->` 标记，而这 13 个仓的 `AGENTS.md`
   **一个都没有该标记** ⇒ 生成器一律「跳过：无生成区标记」⇒ **重定位之后
   再没有任何自动路径能更新这些文件**。

⇒ 结果是：profile 侧当年漂过的那同一批仓，在仓内文档这一侧**一直留到现在**。

## 判据：认「自述层位」，不认「提及层位」

判据只管**本仓自述**的层位。`AGENTS.md`：

1. **规范行**：第 5 行的 `矩阵分层：**tier N（层名…）**` ——
   层位**数字**与**层名**都要与数据源一致；
2. **正文自述行**：`本仓是 tier N` / `本仓 tier N`（以及紧跟其后的层名，如
   「本仓是 tier 0 基础层」）—— 数字与层名都要一致。

`README.md`（同一套判据，两种形态）：

1. **定位声明行**（规范行）：首部的 `oagentkit 模块矩阵 **tier N（层名…）**` ——
   `guardx` 是同义变体 `oagentkit 模块矩阵的 **tier 0 安全层**`（同一正则可认）；
   层位**数字**与**层名**都要与数据源一致；
2. **`## 定位` 自述行**：`**tier N** — 层名…`（12 个仓有；`guardx` 无此形态 ⇒
   **认出来就核、认不出不强制**——它是可选形态，不是必备形态）。

**不判**的是「跨仓提及」：像 runtime 的「procx 被 tier1 的 mcpkit 使用」这种
**谈论别的仓**的句子不在判据内（那是另一条不变量：提及的仓层位是否对）。

层名不是硬编码的：权威值从 `modules.json` 的 `tiers` 映射取
（`"2": "集成层：依赖 tier0/tier1 + 重第三方地板（…）"` → 层名 `集成层`）；
「哪些词算层名」也由同一张映射推出（各层名之集），所以「tier 2 却写（基础层）」
判红，而口语里的「底层」不会被误当成层名。

### 三档判定（硬规则 3：判不了 ≠ 没问题）

| 情形 | 判定 | 为什么 |
|---|---|---|
| 数字或层名与数据源不符 | **红** | 确认违规 |
| `AGENTS.md` 不存在 / 认不出任何自述层位 | **黄** | 判不了：措辞可能变了 ⇒ 守卫对该仓**失去覆盖** |
| 全部一致 | 绿 | — |

⚠️ 黄灯在本守卫里是**危险信号**而不是「小事」：它意味着「这条仓级文档已不再被机器核对」。
所以 CI 接线用 `--fail-on-yellow`（把黄灯升级为失败）——本守卫每次都扫全矩阵，
「安静地失去覆盖」正是本仓反复出现的失效形态。

## 为什么不用「生成区标记」

加 `<!-- oagentkit:generated:begin -->` 标记是 org 对 profile 的既有做法，
但对**这 13 个仓的 `AGENTS.md`**不适用：

- 标记会把标记行带进 diff（验收要求「diff 只含层位行」）；
- 守卫若以「有没有标记」判定「这仓有没有声明」，就得给**全部 13 个仓**加标记；
- `gen_docs.py` 的整文件覆盖模型在手写丰富的 `AGENTS.md` 上风险高
  （这正是 `verify_repo_docs` 只做报告器、恒不失败的原因）。

所以本守卫**直接解析自述层位**，并提供 `--write`：**只改层位数字与层名**，
其余段落逐字不动（自测里有一条断言专门守这条）。

⚠️ `--write` 的已知边界：它**不重写依赖语义措辞**。例如 tier 0 专属的
「任何矩阵内模块都不得被本仓依赖」在改成 tier 2 之后语义已经不成立 ——
`--write` 会把数字改对，但**那句措辞仍需人读一遍**。改层位时同句的语义
（依赖方向、层名、专属措辞）要一起改。

## 用法

    python3 scripts/verify_repo_tiers.py --root .            # 校验（CI 口径）
    python3 scripts/verify_repo_tiers.py --root . --write    # 按数据源改写层位行
    python3 scripts/verify_repo_tiers.py --selftest          # 离线自测（带反证样本）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402

DEFAULT_MATRIX = "matrix-tools/modules.json"

# ── 规范行：`矩阵分层：**tier N（层名…）**`（粗体标记可有可无、位置可有可无）──
HEADER_RE = re.compile(r"矩阵分层[：:]\s*\**\s*tier\s*(\d+)")

# ── 正文自述行：`本仓是 tier N` / `本仓 tier N` / `本仓为 tier N` ──
SELF_RE = re.compile(r"本仓(?:是|为|属于)?\s*tier\s*(\d+)")

# ── 紧跟层位数字的层名（`（基础层）` / ` 基础层` / ` **基础层**`）──
#
# ⚠️ 用 `match(text, pos)` **锚在层位数字之后**：层名必须**紧邻**那个数字
# （中间只允许空白 / 粗体星号 / 括号，最多 4 个字符）。
# 若用 `search`，同一行后面出现的「集成层」会被误当成这个自述的层名
# ——「本仓是 tier 2，依赖 tier 1 的 alpha 集成层」就会误判。
LABEL_RE = re.compile(r"[\s\*（(]{0,4}([一-鿿]{2,4}层)")

# ── `README.md` 的两种自述形态 ──
#
# 定位声明行（规范行，13 仓全覆盖）：
#   `… —— oagentkit 模块矩阵 **tier 2**（集成层，依赖 tier0/tier1 + 重第三方地板）。`
#   `guardx` 的变体：`oagentkit 模块矩阵的 **tier 0 安全层**。`
# ⇒ `模块矩阵` 与 `的` 可选，同一个正则覆盖两种写法。
README_HEADER_RE = re.compile(r"oagentkit 模块矩阵(?:的)?\s*\**\s*tier\s*(\d+)")

# `## 定位` 小节的自述行（12 仓有；guardx 没有 ⇒ 可选形态）：
#   `**tier 2** — 集成层，依赖 tier0/tier1 + 重第三方地板。`
#   `**tier 0，依赖方向的最底层。** 矩阵内没有任何模块可以反向依赖本仓，`
README_SELF_RE = re.compile(r"^\*\*tier\s*(\d+)")

# README 的层名模式：共享的 `LABEL_RE` 锚得太紧（层位数字后最多 4 个字符），
# 认不出 `## 定位` 自述行里的层名 —— 那里层位数字与层名之间隔着一个破折号：
#   `**tier 2** — 集成层，依赖 tier0/tier1 + 重第三方地板。`
# （`**` + 空格 + `—` + 空格 = 5 个字符 ⇒ 超了共享模式 4 字符的窗口。）
#
# 放宽到 6 个字符并允许破折号后，实测 13 仓 README 抽出的层名全部合法：
# 编排层 / 集成层 / 能力层 / 基础层 / guardx 的「安全层」；
# `queue`（`**tier 0**，零矩阵内依赖…`）与 `textx`（`**tier 0，依赖方向的最底层。**`）
# 抽不出层名 —— 这是对的：它们那两行**本来就没有**层名，不该硬认一个。
README_LABEL_RE = re.compile(r"[\s\*（(—–\-]{0,6}([一-鿿]{2,4}层)")


@dataclass(frozen=True)
class DocProfile:
    """一个仓级文档的「自述层位」形态。

    两份文档各认各的形态，但**判据、判定档位、`--write` 契约完全共用**——
    覆盖面不一致本身就是缺口（同仓的 `verify_repo_dep_claims.py` 就读两个文件）。
    """

    filename: str          # 相对仓根，如 "README.md"
    header_re: re.Pattern  # 规范行（定位声明行）
    self_re: re.Pattern    # 正文自述行
    label_re: re.Pattern   # 层名模式（紧跟在层位数字之后）
    hint: str = ""         # 「认不出声明」时给人的写法提示


# 文档档位表：两份文档各认各的形态，判据完全共用。
#
# 「认不出声明」的口径对两份文档一致：**两种形态一个都没认出来**才算认不出
# （`guardx` 只有定位声明行、没有 `## 定位` 自述行 ⇒ 照样算认出来了）。
DOC_PROFILES = (
    DocProfile("AGENTS.md", HEADER_RE, SELF_RE, LABEL_RE,
               "规范行写法：> 矩阵分层：**tier N（层名…）**；"
               "正文自述写法：本仓是 tier N"),
    DocProfile("README.md", README_HEADER_RE, README_SELF_RE, README_LABEL_RE,
               "定位声明行写法：… —— oagentkit 模块矩阵 **tier N（层名…）**；"
               "`## 定位` 自述行写法：**tier N** — 层名…"),
)


@dataclass
class Decl:
    """一个仓在一份文档里的层位声明。"""

    repo: str
    header_tier: int | None = None
    header_label: str | None = None
    header_line: int = 0
    # (层位数字, 层名或 None, 行号)
    self_decls: list[tuple[int, str | None, int]] = field(default_factory=list)

    @property
    def recognized(self) -> bool:
        return self.header_tier is not None or bool(self.self_decls)


def layer_labels(tiers_map: dict) -> dict[int, str]:
    """从 `modules.json` 的 `tiers` 映射取「层位 → 层名」。

    层名**不硬编码**：它由数据源说了算（`"2": "集成层：…"` → 集成层）。
    """
    out: dict[int, str] = {}
    for key, raw in (tiers_map or {}).items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            tier = int(key)
        except (TypeError, ValueError):
            continue
        head = raw.split("：", 1)[0].split(":", 1)[0].strip()
        if head:
            out[tier] = head
    return out


def parse_decl(repo: str, text: str, prof: DocProfile) -> Decl:
    """从一份仓级文档的全文里认出本仓的自述层位。"""
    d = Decl(repo=repo)
    for idx, line in enumerate(text.splitlines(), start=1):
        m = prof.header_re.search(line)
        if m and d.header_tier is None:
            d.header_tier = int(m.group(1))
            d.header_line = idx
            lm = prof.label_re.match(line, m.end(1))
            if lm:
                d.header_label = lm.group(1)
        for sm in prof.self_re.finditer(line):
            lm = prof.label_re.match(line, sm.end(1))
            d.self_decls.append(
                (int(sm.group(1)), lm.group(1) if lm else None, idx))
    return d


def _label_conflict(candidate: str | None, want: str | None,
                    known: set[str]) -> bool:
    """候选层名是否与权威层名冲突。

    只有「候选本身是个层名（出自数据源那张映射）」才判冲突 ——
    口语里的「底层」「上层」不是层位声明，不该误报。
    """
    if candidate is None or want is None:
        return False
    if candidate not in known:
        return False
    return candidate != want


def _mismatches(d: Decl, tier: int, want_label: str | None,
                known: set[str]) -> list[str]:
    """列出该仓与数据源不符的每一处声明。"""
    out: list[str] = []
    if d.header_tier is not None:
        if d.header_tier != tier:
            out.append(f"第 {d.header_line} 行规范行声明 tier {d.header_tier}"
                       f"（数据源 tier {tier}）")
        if _label_conflict(d.header_label, want_label, known):
            out.append(f"第 {d.header_line} 行层名写「{d.header_label}」"
                       f"（数据源层名「{want_label}」）")
    for stier, slabel, sline in d.self_decls:
        if stier != tier:
            out.append(f"第 {sline} 行自述「本仓… tier {stier}」"
                       f"（数据源 tier {tier}）")
        elif _label_conflict(slabel, want_label, known):
            out.append(f"第 {sline} 行层名写「{slabel}」（数据源层名「{want_label}」）")
    return out


def _rewrite_label(text: str, start: int, want_label: str | None,
                   known: set[str], label_re: re.Pattern) -> str:
    """把**紧跟在 start 之后**的层名改成权威层名。

    只改**确实是个层名**（出自数据源那张映射）的那一个词——
    口语里的「底层」「上层」不动，别的行的层名也不动。
    """
    if not want_label:
        return text
    m = label_re.match(text, start)
    if m and m.group(1) in known and m.group(1) != want_label:
        return text[:m.start(1)] + want_label + text[m.end(1):]
    return text


def rewrite_text(text: str, tier: int, want_label: str | None,
                 known: set[str], prof: DocProfile) -> str:
    """把自述层位改成 (tier, 层名)——**只动层位数字与层名，其余逐字不动**。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        m = prof.header_re.search(line)
        if m:
            new = line[:m.start(1)] + str(tier) + line[m.end(1):]
            m2 = prof.header_re.search(new)
            new = _rewrite_label(new, m2.end(1), want_label, known, prof.label_re)
            out.append(new)
            continue
        # 正文自述行：一遍扫过去，边拼边改（下标始终指向原串，不会错位）
        out_pieces: list[str] = []
        pos = 0
        for sm in prof.self_re.finditer(line):
            out_pieces.append(line[pos:sm.start(1)])
            out_pieces.append(str(tier))
            pos = sm.end(1)
            lm = prof.label_re.match(line, pos)
            if lm and lm.group(1) in known and want_label and lm.group(1) != want_label:
                out_pieces.append(line[pos:lm.start(1)])
                out_pieces.append(want_label)
                pos = lm.end(1)
        out_pieces.append(line[pos:])
        out.append("".join(out_pieces))
    return "".join(out)


def judge(root: Path, matrix_path: Path, write: bool = False) -> Report:
    """判定各仓层位声明与数据源是否一致。"""
    rep = Report(title="各仓 README.md / AGENTS.md 层位声明")
    if not matrix_path.is_file():
        rep.red("modules.json", f"找不到矩阵数据源：{matrix_path}",
                "路径相对矩阵根目录；确认 CI 布局里有 matrix-tools/modules.json")
        return rep
    try:
        data = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rep.red("modules.json", f"读不了矩阵数据源 {matrix_path}：{exc}")
        return rep

    modules = data.get("modules") or []
    if not modules:
        rep.red("modules.json", "数据源里没有 modules")
        return rep

    label_map = layer_labels(data.get("tiers") or {})
    known_labels = set(label_map.values())

    checked = 0
    for m in modules:
        repo = str(m.get("repo", "")).strip()
        if not repo:
            continue
        try:
            tier = int(m["tier"])
        except (KeyError, TypeError, ValueError):
            rep.yellow(repo, "数据源里这一条没有可用的 tier 字段",
                       "modules.json 的 modules[].tier 必须是整数")
            continue
        want_label = label_map.get(tier)
        if want_label is None:
            rep.yellow(repo, f"数据源的 tiers 里没有 tier {tier} 的层名",
                       "modules.json 的 tiers 映射要覆盖本仓的层位")

        for prof in DOC_PROFILES:
            # AGENTS.md 的报告 subject 保持仓名（历史口径），README 带上文件名
            subject = repo if prof.filename == "AGENTS.md" else f"{repo}/{prof.filename}"
            path = root / repo / prof.filename
            if not path.is_file():
                rep.yellow(subject, f"没有 {repo}/{prof.filename} —— 该仓的层位声明无从核对",
                           "模块仓应当有 " + prof.filename
                           + "（仓级第一入口）；补上并按下面的写法声明层位："
                           + prof.hint)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                rep.yellow(subject, f"读不了 {repo}/{prof.filename}：{exc}")
                continue

            d = parse_decl(repo, text, prof)
            if not d.recognized:
                rep.yellow(subject,
                           f"认不出本仓在 {prof.filename} 里的层位声明"
                           "（既没有规范行，也没有自述行）",
                           prof.hint + " —— 认不出 ⇒ 这条仓不再被机器核对")
                continue

            bad = _mismatches(d, tier, want_label, known_labels)
            if not bad:
                checked += 1
                continue

            if write:
                new_text = rewrite_text(text, tier, want_label, known_labels, prof)
                if new_text != text:
                    path.write_text(new_text, encoding="utf-8")
                rep.yellow(subject,
                           "已按数据源改写层位数字/层名：" + "；".join(bad),
                           "⚠️ --write 只改层位数字与层名：**同句的依赖语义措辞仍需人读一遍**")
                continue

            rep.red(subject,
                    f"{prof.filename} 声明的层位与 modules.json 不一致：" + "；".join(bad),
                    f"modules.json 说本仓是 tier {tier}"
                    + (f"（{want_label}）" if want_label else "")
                    + f"；改 {prof.filename} 的层位行与自述行"
                    + "（--write 可改数字与层名，语义措辞需手改）")

    rep.note(f"逐仓比对 {len(modules)} 个模块仓的 "
             + " + ".join(p.filename for p in DOC_PROFILES)
             + f" 层位声明；一致 {checked} 处")
    return rep


# ---------------------------------------------------------------------------
# 自测（硬规则 2：必须带**反证样本**——故意造坏的被检对象必须被报出来）
# ---------------------------------------------------------------------------

def _mk_matrix(root: Path) -> Path:
    (root / "matrix-tools").mkdir(parents=True, exist_ok=True)
    path = root / "matrix-tools" / "modules.json"
    path.write_text(json.dumps({
        "tiers": {
            "0": "基础层：零重量级第三方地板",
            "2": "集成层：依赖 tier0/tier1 + 重第三方地板",
        },
        "modules": [
            {"repo": "alpha", "tier": 0},
            {"repo": "beta", "tier": 2},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _mk_agents(root: Path, repo: str, body: str) -> Path:
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    p = d / "AGENTS.md"
    p.write_text(body, encoding="utf-8")
    return p


def _mk_readme(root: Path, repo: str, body: str) -> Path:
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    p = d / "README.md"
    p.write_text(body, encoding="utf-8")
    return p


_GOOD_ALPHA = """# alpha

> 矩阵分层：**tier 0（基础层）**

- 依赖严格自底向上。**本仓是 tier 0**，任何矩阵内模块都不得被本仓依赖。

手写段落，必须逐字保留。
"""

_GOOD_BETA = """# beta

> 矩阵分层：**tier 2**（集成层，依赖 tier0/tier1 + 重第三方地板）

- 依赖严格自底向上。本仓是 tier 2，只允许依赖 tier ≤ 2 的仓。
  ⚠️ 两者都是 tier 0，本仓 tier 2 —— 依赖方向合法。

手写段落，必须逐字保留。
"""

# README 侧的「一致样本」：定位声明行 + `## 定位` 自述行。
# ⚠️ 定位声明行是 README 的**必备**形态（13 仓全覆盖）；
# `## 定位` 自述行是**可选**形态（`guardx` 没有）⇒ 守卫对它是「认出来就核」。
_GOOD_README_ALPHA = """# alpha

alpha 是什么 —— oagentkit 模块矩阵 **tier 0**（基础层）。

## 定位

**tier 0** — 基础层。

手写段落，必须逐字保留。
"""

_GOOD_README_BETA = """# beta

beta 是什么 —— oagentkit 模块矩阵 **tier 2**（集成层，依赖 tier0/tier1 + 重第三方地板）。

## 定位

**tier 2** — 集成层，依赖 tier0/tier1 + 重第三方地板。

手写段落，必须逐字保留。
"""

# `guardx` 形态的样本：只有定位声明行（且层名是仓自定的「安全层」），
# 没有 `## 定位` 自述行 ⇒ 守卫必须照样认出来、且不因层名不是数据源层名而误报。
_GOOD_README_GUARDX = """# guardx

guardx 是什么 —— oagentkit 模块矩阵的 **tier 0 安全层**。与矩阵其余仓的关系：

手写段落，必须逐字保留。
"""


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    def reds(rep) -> set[str]:
        return {f.subject for f in rep.reds}

    def yellows(rep) -> set[str]:
        return {f.subject for f in rep.yellows}

    def all_subjects(rep) -> set[str]:
        return reds(rep) | yellows(rep)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        matrix = _mk_matrix(root)
        _mk_agents(root, "alpha", _GOOD_ALPHA)
        _mk_agents(root, "beta", _GOOD_BETA)
        _mk_readme(root, "alpha", _GOOD_README_ALPHA)
        _mk_readme(root, "beta", _GOOD_README_BETA)

        # ① 全部一致 ⇒ 无红
        rep = judge(root, matrix)
        check("一致样本不判红", reds(rep), set())

        # ② 反证：层位数字被改回旧值 ⇒ 必须判红
        _mk_agents(root, "beta", _GOOD_BETA.replace("tier 2", "tier 1"))
        rep = judge(root, matrix)
        check("层位数字错 → 判红", "beta" in reds(rep), True)

        # ③ 反证：层名错（数字对、层名是别的层的）⇒ 必须判红
        _mk_agents(root, "beta", _GOOD_BETA.replace("（集成层，", "（基础层，"))
        rep = judge(root, matrix)
        check("层名错 → 判红", "beta" in reds(rep), True)

        # ④ 反证：**只刷规范行、正文自述行没刷** ⇒ 仍必须判红
        _mk_agents(root, "beta",
                   _GOOD_BETA.replace("> 矩阵分层：**tier 2**", "> 矩阵分层：**tier 1**"))
        rep = judge(root, matrix)
        check("规范行错 → 判红", "beta" in reds(rep), True)
        _mk_agents(root, "beta",
                   _GOOD_BETA.replace("本仓 tier 2", "本仓 tier 1"))
        rep = judge(root, matrix)
        check("正文自述行错 → 判红", "beta" in reds(rep), True)

        # ⑤ 认不出声明 ⇒ 黄（既不是红，也不是绿）
        _mk_agents(root, "beta", "# beta\n\n这里没有任何层位声明。\n")
        rep = judge(root, matrix)
        check("认不出声明 → 判黄", "beta" in yellows(rep), True)
        check("认不出声明 → 不判红", "beta" in reds(rep), False)

        # ⑥ AGENTS.md 缺失 ⇒ 黄
        (root / "beta" / "AGENTS.md").unlink()
        rep = judge(root, matrix)
        check("缺 AGENTS.md → 判黄", "beta" in yellows(rep), True)
        check("缺 AGENTS.md → 不判红", "beta" in reds(rep), False)
        _mk_agents(root, "beta", _GOOD_BETA)

        # ⑦ 跨仓提及**不误报**（只认自述，不认提及）
        _mk_agents(root, "beta",
                   _GOOD_BETA.replace("本仓是 tier 2", "本仓是 tier 2，依赖 tier 1 的 alpha"))
        rep = judge(root, matrix)
        check("跨仓提及不误报", reds(rep), set())

        # ⑦b 口语里的「底层」不是层名 ⇒ 不误报
        _mk_agents(root, "beta",
                   _GOOD_BETA.replace("本仓是 tier 2", "本仓是 tier 2，位于矩阵底层"))
        rep = judge(root, matrix)
        check("口语「底层」不误报", reds(rep), set())

        # ⑧ --write：改数字与层名，且**手写段落逐字不动**
        bad = _GOOD_BETA.replace("tier 2", "tier 1").replace("（集成层，", "（基础层，")
        p = _mk_agents(root, "beta", bad)
        rep = judge(root, matrix, write=True)
        check("--write 后判红消失", "beta" in reds(rep), False)
        after = p.read_text(encoding="utf-8")
        check("--write 保留手写段落", "手写段落，必须逐字保留。" in after, True)
        check("--write 改了规范行", "矩阵分层：**tier 2" in after, True)
        check("--write 改了层名", "（集成层，" in after, True)
        check("--write 改了正文自述", "本仓是 tier 2" in after, True)
        rep = judge(root, matrix)
        check("--write 后复核不判红", reds(rep), set())

        # ── README 档位的反证样本 ────────────────────────────────
        #
        # README 与 AGENTS.md 是同一份不变量在两个第一入口上的两面，
        # 所以 README 档位**必须**有等价的反证样本 —— 否则「覆盖面扩大了」
        # 只是多认了几行文本，没有多抓一类缺陷。

        # ⑧b 反证：README 定位声明行的层位数字错 ⇒ 必须判红
        _mk_readme(root, "beta",
                   _GOOD_README_BETA.replace("模块矩阵 **tier 2**",
                                             "模块矩阵 **tier 1**"))
        rep = judge(root, matrix)
        check("README 定位声明行数字错 → 判红", "beta/README.md" in reds(rep), True)

        # ⑧c 反证：README `## 定位` 自述行的层位数字错 ⇒ 必须判红
        _mk_readme(root, "beta",
                   _GOOD_README_BETA.replace("**tier 2** — 集成层",
                                             "**tier 1** — 能力层"))
        rep = judge(root, matrix)
        check("README 定位自述行数字错 → 判红", "beta/README.md" in reds(rep), True)

        # ⑧d 反证：README 层名错（数字对、层名是别的层的）⇒ 必须判红
        _mk_readme(root, "beta",
                   _GOOD_README_BETA.replace("（集成层，", "（基础层，"))
        rep = judge(root, matrix)
        check("README 层名错 → 判红", "beta/README.md" in reds(rep), True)

        # ⑧e 反证：README 两种形态都认不出 ⇒ 判黄（不是红，也不是绿）
        _mk_readme(root, "beta", "# beta\n\n这里没有任何层位声明。\n")
        rep = judge(root, matrix)
        check("README 认不出声明 → 判黄", "beta/README.md" in yellows(rep), True)
        check("README 认不出声明 → 不判红", "beta/README.md" in reds(rep), False)

        # ⑧f README 缺文件 ⇒ 判黄
        (root / "beta" / "README.md").unlink()
        rep = judge(root, matrix)
        check("缺 README.md → 判黄", "beta/README.md" in yellows(rep), True)
        check("缺 README.md → 不判红", "beta/README.md" in reds(rep), False)
        _mk_readme(root, "beta", _GOOD_README_BETA)

        # ⑧g README 里**谈论别的仓**的层位 ⇒ 不误报
        _mk_readme(root, "beta",
                   _GOOD_README_BETA.replace(
                       "## 定位",
                       "本仓依赖 tier 0 的 alpha 与 tier 1 的 gamma。\n\n## 定位"))
        rep = judge(root, matrix)
        check("README 跨仓提及不误报", reds(rep), set())

        # ⑧h README --write：数字与层名都改，手写段落逐字不动
        bad_readme = (_GOOD_README_BETA
                      .replace("模块矩阵 **tier 2**", "模块矩阵 **tier 1**")
                      .replace("（集成层，", "（基础层，")
                      .replace("**tier 2** — 集成层", "**tier 1** — 基础层"))
        pr = _mk_readme(root, "beta", bad_readme)
        rep = judge(root, matrix, write=True)
        check("--write 后 README 判红消失", "beta/README.md" in reds(rep), False)
        after = pr.read_text(encoding="utf-8")
        check("--write README 保留手写段落", "手写段落，必须逐字保留。" in after, True)
        check("--write README 改了定位声明行",
              "模块矩阵 **tier 2**" in after, True)
        check("--write README 改了定位声明行的层名",
              "（集成层，" in after, True)
        check("--write README 改了定位自述行",
              "**tier 2** — 集成层" in after, True)
        rep = judge(root, matrix)
        check("--write README 后复核不判红", reds(rep), set())

        # ⑧i `guardx` 形态：只有定位声明行、层名是仓自定的「安全层」
        #     （不在数据源的层名映射里）⇒ 既不判红也不判黄
        _mk_readme(root, "alpha", _GOOD_README_GUARDX)
        _mk_agents(root, "alpha", _GOOD_ALPHA)
        rep = judge(root, matrix)
        check("仓自定层名不判红", "alpha/README.md" in reds(rep), False)
        check("仓自定层名不判黄", "alpha/README.md" in yellows(rep), False)
        _mk_readme(root, "alpha", _GOOD_README_ALPHA)

        # ⑨ 判据本身坏掉时必须被发现（判红，而不是静默放行）：
        #    数据源缺该层位的层名 ⇒ 判黄（判不了），不是绿
        m2 = json.loads(matrix.read_text(encoding="utf-8"))
        m2["tiers"].pop("2")
        matrix.write_text(json.dumps(m2, ensure_ascii=False), encoding="utf-8")
        rep = judge(root, matrix)
        check("数据源缺层名 → 判黄", "beta" in yellows(rep), True)

    if fails:
        print("❌ verify_repo_tiers 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_repo_tiers 自测通过"
          "（一致→绿 / 层位数字错→红 / 层名错→红 / 自述行错→红 / "
          "认不出声明→黄 / 缺 AGENTS.md→黄 / 跨仓提及不误报 / 口语「底层」不误报 / "
          "--write 只改层位行 / "
          "README 定位声明行错→红 / README 定位自述行错→红 / README 层名错→红 / "
          "README 认不出→黄 / 缺 README.md→黄 / README 跨仓提及不误报 / "
          "仓自定层名不误报 / --write 改 README 层位行）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="各仓 README.md / AGENTS.md 层位声明 vs modules.json")
    ap.add_argument("--root", default=".", help="矩阵根目录（各仓的父目录）")
    ap.add_argument("--matrix", default=None,
                    help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--write", action="store_true",
                    help="按数据源改写层位数字与层名（默认只校验）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--json-out", default=None, help="把 JSON 报告写到该文件")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也判失败")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    matrix_path = Path(args.matrix) if args.matrix else root / DEFAULT_MATRIX
    rep = judge(root, matrix_path, write=args.write)
    code = emit(rep, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if args.json_out:
        Path(args.json_out).write_text(rep.render_json(), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
