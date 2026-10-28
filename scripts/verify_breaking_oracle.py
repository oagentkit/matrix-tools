#!/usr/bin/env python3
"""breakingIndex 对账门禁（**独立 oracle**）—— 不复用 breaking_index.py 的任何判据。

## 为什么要有第二条对账线（2026-10-20 实测）

`verify_breaking_index.py` 的第 85 行是：

    actual = breaking_index.extract(changelog_text)

它调用的是**生成器** `breaking_index.py` 的同一套解析函数。于是：

    生成器漏收  ⟺  门禁也漏收  ⟹  门禁全绿，而矩阵对下游说「无破坏性变更」

oekit 侧实测过这一形态（解析器漏收「顶格独立成行的 `**BREAKING**:` 标记行」，
两边一起漏、门禁全绿而矩阵说谎）。oagentkit 侧已实测到另一形态：
**破坏性变更只用散文描述、没有显式标记行**时，生成器收不到——
`llm v0.12.0` 把 `github.com/oagentkit/obs/obsx` 迁到 `github.com/oagentkit/llm/obsx`，
import 路径真的变了，但版本段里没有 `BREAKING`、标题也没有 `!`，矩阵因此漏登记。

## 这条门禁的独立性从哪来

判据与 `breaking_index.py` **完全解耦**，不是同一套代码换个写法：

| 维度 | breaking_index.py（生成器） | 本脚本（独立 oracle） |
|---|---|---|
| 版本边界 | 只认 `## [vX.Y.Z]`（**要求方括号**） | 任何 `^## ` 标题含 vX.Y.Z（方括号可选） |
| 判定粒度 | **小节**级（`###` 切分） | **版本段**级（整段聚合） |
| 显式标记 | `^\s*BREAKING(\s+CHANGE)?\s*[：:]`（**大小写敏感、行首**） | 大小写不敏感、容忍 `**` 粗体、项目符号前缀、⚠️ |
| 路径迁移 | **完全不看** | 同包名跨 ≥2 个模块 → 判为搬家（破坏性） |
| 标题叹号 | `[a-zA-Z]!` | 同样识别（conventional commit 公开约定） |

共享的只有「conventional commit 的 `!` 是破坏性标记」这条**公开约定**，
实现是各写各的——门禁的价值不在于「用不同的词表」，而在于**实现路径不相干**：
生成器走「小节 + 行首 trailer」，本脚本走「版本段 + 宽口径信号」。

## 验收 oracle 有效性（反证）

`--selftest` 里带了一组**生成器至今收不到**的样本：

- 顶格独立成行的 `**BREAKING**: …`（粗体标记）
- 项目符号里的 `BREAKING CHANGE: …`
- 小写 `breaking change: …`
- 散文描述的 import 路径搬家（无任何显式标记；生成器已在本轮补上这条，
  但 oracle 必须**独立**收得到，不依赖生成器的修复）

自测会拿生成器跑同一份样本，确认它对前三种形态仍然漏收 → oracle 收得到、
生成器收不到 = 两者不在同一处盲区，oracle 不是假通过。

## 判定语义

- CHANGELOG 说有破坏性、矩阵没有 → **红**（矩阵漏收，对下游说谎）
- 矩阵有、CHANGELOG 没有 → **红**（矩阵条目过期或被手改）
- CHANGELOG **取不到** → **红**（判不了 ≠ 没有；静默放过是这类门禁最危险的失效形态）
- 一个仓都没检查到 → **红**（拒绝「全绿但什么都没查」）

用法:
    verify_breaking_oracle.py --root /path/to/oagentkit   # 读本地各仓 CHANGELOG
    verify_breaking_oracle.py --org oagentkit             # 联网读各仓 CHANGELOG
    verify_breaking_oracle.py --selftest                  # 离线自测

退出码: 0 = 一致；1 = 漂移 / 判不了。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_VERSIONS = HERE.parent / "versions.json"
DEFAULT_MATRIX = HERE.parent / "modules.json"

# --------------------------------------------------------------------------
# 判据（独立实现，不 import breaking_index）
# --------------------------------------------------------------------------

VER = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
VERSION_LINE = re.compile(r"^##(?!#)\s")
SUB_LINE = re.compile(r"^###(?!#)\s")

# 信号 A：显式破坏性标记（宽口径）
EXPLICIT: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\*\*\s*breaking\b", re.I), "粗体 **BREAKING**"),
    # 「BREAKING: / BREAKING CHANGE: / BREAKING-CHANGE:」——**必须锚到行首**。
    #
    # 为什么收窄（实测 2026-10-21）：不锚行首时，它会把**散文里描述**「隐式破坏性」
    # 的句子当成标记行：
    #   「地板抬高对下游是隐式 breaking：不跑 go mod tidy 就红」
    # 这句话描述的是「地板追平对下游是隐式破坏性变更」——**事实陈述，不是标记**。
    # 而矩阵对「什么算破坏性变更」有既定口径：breakingIndex 记的是
    # **import 路径 / API 断裂**（机器按它写 import 直接编译不过），
    # 「地板抬高」不在其中——11 个仓的整段历史里没有一次地板追平进过索引。
    #
    # 收窄后仍能收得到本 oracle 的核心盲区形态（都在行首）：
    #   **BREAKING**: …        （粗体，顶格独立成行）
    #   - BREAKING CHANGE: …   （项目符号）
    #   breaking change: …     （小写，行首）
    #   ⚠️ BREAKING CHANGE: … （⚠️ + 关键词同现，由下面第 4 条收）
    #
    # 反过来说：**散文形态的 API 断裂仍由信号 B（包搬家，结构性）兜住**——
    # llm v0.12.0 那类「import 路径真的变了但没有标记行」的形态靠的是结构判定，
    # 不依赖这条措辞匹配。收窄不损失该类召回。
    (re.compile(r"(?im)^[\s>*\-•]*\**\s*breaking\b[\s\-_]*(?:change|changes)?\**\s*[:：]"),
     "行首 BREAKING: / BREAKING CHANGE: / BREAKING-CHANGE:"),
    # 「BREAKING CHANGE 短语」——**要求短语后紧跟冒号**（标记形态）。
    #
    # 为什么收窄（实测 2026-10-25）：不要求冒号时，它会把**转述别的版本**的
    # 破坏性变更当成「本版是破坏性变更」。实测样本在 llm v0.18.0 的版本段里：
    #
    #   「v0.9.0（2026-10-19）把根包上移到 module 根，路径改成 github.com/oagentkit/llm，
    #     并标了 BREAKING CHANGE。」
    #
    # 这一版是**纯新增**（llm/llm 兼容层），**不破坏**任何东西；句子里描述的是
    # v0.9.0 那次包上移。而矩阵的 breakingIndex 记的是 **import 路径 / API 断裂**
    #（机器按它写 import 直接编译不过）——把纯新增版登记成破坏性变更 = 对下游说谎。
    #
    # 收窄后仍保留 oracle 的「宽口径」本意：**行中**的标记行照收
    #（「本版是 BREAKING CHANGE：因为 import 路径变了」照收），
    # 只把「句中转述别版」这一类形态排除掉。冒号是 Conventional Commits 对
    # BREAKING CHANGE 的规范形态要求，不是本仓自创的词表。
    #
    # 召回未降的实测（14 仓全量对拍）：收窄前被标记、收窄后不再被标记的版本
    # **只有 llm v0.18.0 这一个**；矩阵已登记的破坏性变更版本**一个都没丢**
    # （收窄只减不增，减掉的正是那一条转述）。
    (re.compile(r"\bbreaking\b[\s\-_]*(?:change|changes)\b[ \t]*[:：]", re.I),
     "BREAKING CHANGE 短语（后接冒号 = 标记形态）"),
    # ⚠️ 必须**同一行内**还有英文 breaking 关键词才算——裸 ⚠ 不算。
    #
    # 为什么收窄（实测 2026-10-20）：本 org 的 CHANGELOG 里 ⚠️ 是通用的「注意」标记，
    # 不是破坏性标记：
    #   guardx v0.6.0 段：「⚠️ 嵌套 module 的两个 tag（audit/v0.1.0 / bashguard/v0.1.0）保留不动」
    #   agentkit v1.8.14 段：「⚠️ 这条改动是被矩阵的符号级转发完整性门禁逼出来的」
    # 两处都不是破坏性变更。按「任何 ⚠ = 破坏性」判，agentkit v1.8.14 会被误报成
    # 破坏性变更并把 CI 卡住——门禁一旦开始报假警报，就会被人当成噪声忽略，
    # 那比漏报更糟。
    #
    # 只认英文 breaking（不认中文「破坏/不兼容」）：本 oracle 的其它显式信号
    # （BREAKING: / BREAKING CHANGE / 粗体 **BREAKING**）也全是英文口径，
    # 混进中文关键词会让「不是破坏性变更」这类否定句被误判。
    (re.compile(r"⚠[^\n]{0,120}\bbreaking\b|\bbreaking\b[^\n]{0,120}⚠", re.I),
     "⚠ + BREAKING 同现"),
]

# 中文发布说明的**行首**破坏性声明（`破坏性变更（…）：…`）。
#
# 为什么需要：抽取器认的是**英文 trailer**（`BREAKING CHANGE:`），中文说明最常写的
# 是行首声明 ⇒ 两边都收不到 ⇒ 矩阵对下游说「无破坏性变更」（实测 mcpkit v0.9.0：
# 段里写着「破坏式变更（0.x 阶段 minor 版）：…」，而 breakingIndex 是空的）。
#
# 为什么只认**行首**：句中的「…对下游是隐式破坏性变更」是**事实陈述**，不是声明
# ——本 oracle 早已因此收窄过自己那条（见 EXPLICIT 里的注释）。行首声明与那种
# 句中散文正好分得开。
#
# 为什么短语后必须跟标点：`破坏性变更核对（均无影响）` 是**核对记录**，不是声明
# （实测样本：guardx v0.5.0 的段里就有一行）。
#
# 实现与抽取器**各写各的**（那边剥标点后比对否定词，这里把标点折进正则）——
# 两边共用一个判定函数就是共同盲区。
CN_DECL = re.compile(
    r"^\s{0,3}(?:[-*+>]\s*)?(?:\*\*)?(?:破坏性变更|破坏式变更)(?:\*\*)?\s*[（(：:]"
)
# 声明后面的否定词（去掉前导标点与空白后比对）。
CN_DECL_NEG = re.compile(r"^(?:无|没有|不|非|暂无)")


def cn_declared(line: str) -> bool:
    """该行是否是**行首**的破坏性声明（中文形态）。"""
    m = CN_DECL.match(line)
    if not m:
        return False
    tail = line[m.end():].lstrip("：:（( \t")
    if not tail:
        return False            # 光有短语、没有内容 ⇒ 不是声明
    return not CN_DECL_NEG.match(tail)   # 否定形态（「破坏性变更：无」）不算


# 信号 B：同一个包出现在 ≥2 个不同模块下 → 包在模块间搬家
OAGENT_PATH = re.compile(r"github\.com/oagentkit/([A-Za-z0-9_.\-/]+)")


def version_key(v: str) -> tuple:
    m = VER.search(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def split_sections(text: str) -> list[dict]:
    """CHANGELOG → 版本段列表。边界 = 任何 `^## ` 标题含 vX.Y.Z（方括号可选）。"""
    out: list[dict] = []
    cur: dict | None = None
    for i, line in enumerate(text.splitlines(), 1):
        if VERSION_LINE.match(line):
            m = VER.search(line)
            if m:
                dm = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                cur = {
                    "version": f"v{m.group(1)}.{m.group(2)}.{m.group(3)}",
                    "date": dm.group(1) if dm else "",
                    "heading": line.strip(),
                    "lineno": i,
                    "lines": [],
                }
                out.append(cur)
                continue
        if cur is not None:
            cur["lines"].append((i, line))
    return out


def moved_packages(text: str) -> dict[str, list[str]]:
    """同一个包名（basename）出现在 ≥2 个不同模块下 → 搬家。"""
    by: dict[str, set[str]] = {}
    for m in OAGENT_PATH.finditer(text):
        path = m.group(1).rstrip(".,，。；;）)")
        parts = path.split("/")
        if len(parts) < 2:
            continue
        by.setdefault(parts[-1], set()).add(parts[0])
    return {k: sorted(v) for k, v in by.items() if len(v) >= 2}


def signals_for(sec: dict) -> list[dict]:
    """该版本段的破坏性信号。"""
    sig: list[dict] = []
    for i, line in sec["lines"]:
        for pat, label in EXPLICIT:
            if pat.search(line):
                sig.append({"kind": "explicit", "detail": label,
                            "lineno": i, "line": line.strip()[:160]})
                break
        if cn_declared(line):
            sig.append({"kind": "explicit", "detail": "行首的破坏性变更声明（中文）",
                        "lineno": i, "line": line.strip()[:160]})
        if SUB_LINE.match(line):
            h = line.strip()
            if re.match(r"^###\s*breaking\b", h, re.I):
                sig.append({"kind": "explicit", "detail": "小节标题以 Breaking 开头",
                            "lineno": i, "line": h[:160]})
            elif re.search(r"[a-zA-Z]\s*!", h) or re.search(r"!\s*[:：]", h):
                sig.append({"kind": "bang-title", "detail": "小节标题带 conventional 破坏性叹号",
                            "lineno": i, "line": h[:160]})
    body = "\n".join(l for _, l in sec["lines"])
    moved = moved_packages(body)
    if moved:
        sig.append({"kind": "path-migration",
                    "detail": "同包名跨模块搬家：" +
                              ", ".join(f"{k}→{'/'.join(v)}" for k, v in sorted(moved.items())),
                    "lineno": sec["lineno"], "line": sec["heading"][:160]})
    return sig


def extract(text: str) -> list[dict]:
    """CHANGELOG 全文 → [{version, date, signals:[...]}]（只保留有信号的版本段）。"""
    out = []
    for sec in split_sections(text):
        sig = signals_for(sec)
        if sig:
            out.append({"version": sec["version"], "date": sec["date"], "signals": sig})
    return out


def flagged_versions(text: str) -> set[str]:
    return {e["version"] for e in extract(text)}


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_changelog(root: str | None, org: str | None, repo: str) -> tuple[str | None, str]:
    """→ (正文, 状态)。状态 ∈ {ok, missing, unreadable}。"""
    if root:
        p = Path(root) / repo / "CHANGELOG.md"
        if p.is_file():
            return p.read_text(encoding="utf-8"), "ok"
        return None, "missing"
    text = repo_meta.fetch_file(org, repo, "CHANGELOG.md")
    if text is None:
        return None, "unreadable"
    return text, "ok"


def matrix_entries_for(versions: dict, repo: str) -> list[dict]:
    for m in versions.get("modules", []):
        if m["repo"] == repo:
            return m.get("breakingIndex", [])
    for f in versions.get("facades", []):
        if f["repo"] == repo:
            return f.get("breakingIndex", [])
    return []


def check_one(repo: str, matrix_entries: list[dict], text: str | None,
              status: str) -> list[str]:
    """→ 问题描述列表（空 = 一致）"""
    problems: list[str] = []
    if status == "unreadable":
        return [f"{repo}: CHANGELOG 取不到（判不了，不能当成「没有破坏性变更」）"]
    if status == "missing":
        if matrix_entries:
            problems.append(f"{repo}: 仓里没有 CHANGELOG.md，矩阵却登记了 "
                            f"{len(matrix_entries)} 条 breakingIndex")
        return problems

    have = flagged_versions(text)
    want = {e.get("version") for e in matrix_entries if e.get("version")}

    for v in sorted(have - want, key=version_key):
        detail = ""
        for e in extract(text):
            if e["version"] == v and e["signals"]:
                detail = e["signals"][0]["detail"]
                break
        problems.append(f"{repo} {v}: 独立 oracle 判为破坏性变更，矩阵 breakingIndex 里没有"
                        f"（{detail}）")
    for v in sorted(want - have, key=version_key):
        problems.append(f"{repo} {v}: 矩阵 breakingIndex 登记了，独立 oracle 在 CHANGELOG "
                        f"里找不到破坏性信号——矩阵条目过期？")
    return problems


def collect_repos(versions: dict) -> list[str]:
    repos = [m["repo"] for m in versions.get("modules", [])]
    repos += [f["repo"] for f in versions.get("facades", [])]
    return repos


def main() -> int:
    ap = argparse.ArgumentParser(description="breakingIndex 独立 oracle 对账门禁")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--org", help="联网读各仓 CHANGELOG")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root（本地各仓目录的父目录）或 --org（联网读）")
        return 1

    versions = load_json(Path(args.versions))
    org = args.org or load_json(Path(args.matrix)).get("defaultOrg")

    problems: list[str] = []
    checked = 0
    for repo in collect_repos(versions):
        text, status = read_changelog(args.root, org, repo)
        problems.extend(check_one(repo, matrix_entries_for(versions, repo), text, status))
        checked += 1

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    if problems:
        print(f"❌ 独立 oracle 对账失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n注意：本门禁的判据与 breaking_index.py 相互独立，"
              "两者结论不一致时以本门禁为准（生成器可能漏收散文形态的破坏性变更）。")
        return 1

    n = sum(len(matrix_entries_for(versions, r)) for r in collect_repos(versions))
    print(f"✅ 独立 oracle 与 {checked} 个仓的 CHANGELOG 一致"
          f"（共 {n} 条破坏性变更登记在册）")
    return 0


# ---------------------------------------------------------------- 自测

_BLINDSPOT_FIXTURE = """# demo CHANGELOG

## [v0.12.0] — 2026-10-20

### 新增(obsx)：接收 obs/obsx

自 github.com/oagentkit/obs/obsx 迁入。import 路径从
github.com/oagentkit/obs/obsx 变为 github.com/oagentkit/llm/obsx。

## [v0.11.0] — 2026-10-20

### 修复：某处

**BREAKING**: 顶格独立成行的粗体 BREAKING 标记。

- BREAKING CHANGE: 项目符号里的 BREAKING CHANGE

### 修复：另一处

breaking change: 小写的 breaking change 标记。

## [v0.10.0] — 2026-10-20

### 修复：无破坏性变更的普通小节

只是普通修复。

## [v0.9.0] — 2026-10-20

### 修复：带⚠️注意标记但不是破坏性变更

⚠️ 这条改动是被某个门禁逼出来的。

## [v0.8.0] — 2026-10-21

### 修复：⚠️ 与破坏性标记同现

⚠️ BREAKING CHANGE: 这一行同时有警告标记和破坏性关键词。

## [v0.7.0] — 2026-10-21

### 杂务(deps)：依赖地板追平

地板抬高对下游是隐式 breaking：不跑 go mod tidy 就红，报 updates to go.mod needed。

这**不是**破坏性变更标记行——是散文里描述「地板追平对下游是隐式破坏性变更」
的事实陈述。矩阵的 breakingIndex 记的是 import 路径 / API 断裂，
地板追平不在其中（11 个仓的整段历史里没有一次地板追平进过索引）。

## [v0.6.0] — 2026-10-25

### 新增(x)：转述别的版本的破坏性变更

v0.9.0（2026-10-19）把根包上移到 module 根，路径改成 github.com/oagentkit/llm，
并标了 BREAKING CHANGE。

本版是**纯新增**（补兼容层），不破坏任何东西——上面那句描述的是 v0.9.0 那次上移。

## [v0.5.0] — 2026-10-25

### 重构(y)：行中的破坏性标记（后接冒号）

本版是 BREAKING CHANGE：因为 import 路径变了。

生成器只认**行首**的标记行 ⇒ 收不到这一条；oracle 的宽口径收得到。
"""

# 中文行首声明的样本：正例 + 两个负例（否定形态 / 核对记录）。
_CN_DECL_FIXTURE = """# demo CHANGELOG

## [v0.3.0] — 2026-10-28

### 新增(mcp)：工具暴露名带 server 命名空间

破坏式变更（0.x 阶段 minor 版）：模型侧看到的工具名变成 `server.原始名`。
历史 checkpoint 里的 tool_call 名字与新名对不上。

## [v0.2.0] — 2026-10-28

### 杂务：版本例行发布

破坏性变更：无

## [v0.1.0] — 2026-10-28

### 杂务：版本例行发布

破坏性变更核对（均无影响）：逐条看过。
"""


# 生成器（breaking_index.py）**至今仍收不到**的形态——反证就拿这些当样本：
#   v0.11.0 粗体 **BREAKING**: / 项目符号里的 BREAKING CHANGE / 小写 breaking change
#   v0.12.0 散文形态的 import 路径搬家 —— 这条生成器已在本轮补上（不再是盲区），
#            但仍留在样本里：oracle 必须独立收得到它，不依赖生成器的修复。
#   v0.5.0  行中的 BREAKING CHANGE: —— 生成器只认**行首**标记行，收不到；
#           oracle 的宽口径收得到（2026-10-25 收窄「短语须后接冒号」后仍在）。
_OLD_PARSER_BLINDS = ["v0.11.0", "v0.12.0", "v0.5.0"]


def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # ---- oracle 必须收得到旧解析器收不到的形态 ----
    got = sorted(flagged_versions(_BLINDSPOT_FIXTURE), key=version_key)
    check("散文路径搬家被收", "v0.12.0" in got, True)
    check("粗体/项目符号/小写 BREAKING 被收", "v0.11.0" in got, True)
    check("普通小节不误判", "v0.10.0" in got, False)
    check("裸 ⚠️ 注意标记不误判", "v0.9.0" in got, False)
    check("⚠️ + 破坏性关键词同现被收", "v0.8.0" in got, True)
    check("散文里的「隐式 breaking：」不算标记行（2026-10-21 收窄）",
          "v0.7.0" in got, False)
    check("转述别的版本的 BREAKING CHANGE 不算本版破坏性（2026-10-25 收窄）",
          "v0.6.0" in got, False)
    check("行中的 BREAKING CHANGE: 标记行照收（宽口径仍在）",
          "v0.5.0" in got, True)

    # 反证：拿**生成器**（breaking_index.py）跑同一份样本，确认它对某些形态
    # 仍然漏收 —— oracle 收得到、生成器收不到，才证明两者不在同一处盲区。
    # （只用于自测展示对比，判据本身不依赖它。）
    try:
        import breaking_index
        gen = {e["version"] for e in breaking_index.extract(_BLINDSPOT_FIXTURE)}
    except Exception:  # pragma: no cover
        gen = set()
    missed_by_gen = sorted(set(_OLD_PARSER_BLINDS) - gen, key=version_key)
    check("生成器仍漏收粗体/项目符号/小写标记形态（反证成立）",
          "v0.11.0" in missed_by_gen, True)
    check("oracle 对生成器收不到的形态也收得到",
          {"v0.11.0", "v0.12.0", "v0.5.0"} <= set(got), True)

    # ---- 中文行首声明（本 oracle 的独立实现）----
    cn = flagged_versions(_CN_DECL_FIXTURE)
    check("行首的中文破坏性声明被收", "v0.3.0" in cn, True)
    check("否定形态（破坏性变更：无）不误判", "v0.2.0" in cn, False)
    check("核对记录（破坏性变更核对（…））不误判", "v0.1.0" in cn, False)
    # 同一份样本，抽取器（独立实现）必须得出**相同结论**——两边实现各写各的，
    # 但口径必须一致；不一致就是共同盲区。
    try:
        import breaking_index as _bi
        gen_cn = {e["version"] for e in _bi.extract(_CN_DECL_FIXTURE)}
        check("抽取器与 oracle 对中文声明的结论一致", gen_cn == cn, True)
    except Exception:  # pragma: no cover
        pass

    # ---- 对账语义 ----
    empty = check_one("x", [], "# c\n", "ok")
    check("两边都空无问题", empty, [])
    stale = check_one("x", [{"version": "v1.0.0", "title": "t", "summary": "", "detail": ""}],
                      "# c\n", "ok")
    check("矩阵有而 oracle 无 → 红", len(stale), 1)
    cl = "## [v1.0.0] — 2026-01-01\n\n### 重构(x)!：变了\n\nBREAKING CHANGE: 真的变了。\n"
    missing = check_one("x", [], cl, "ok")
    check("CHANGELOG 有而矩阵无 → 红", len(missing), 1)
    unreadable = check_one("x", [], None, "unreadable")
    check("取不到即红", len(unreadable), 1)
    nosuch = check_one("x", [{"version": "v1.0.0", "title": "t", "summary": "", "detail": ""}],
                       None, "missing")
    check("无 CHANGELOG 但矩阵有条目 → 红", len(nosuch), 1)

    # ---- 搬家规则的负向样本：同仓内两个包不算搬家 ----
    same = "## [v1.0.0] — 2026-01-01\n\n### 新增：queue 仓建立\n\n收编 2 个包：\n" \
           "- github.com/oagentkit/queue\n- github.com/oagentkit/queue/pglease\n"
    check("同仓两包不误判", "v1.0.0" in flagged_versions(same), False)

    if fails:
        print("❌ verify_breaking_oracle 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_breaking_oracle 自测通过"
          "（粗体/项目符号/小写 BREAKING + 散文路径搬家 全部收得到；旧解析器盲区反证成立）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
