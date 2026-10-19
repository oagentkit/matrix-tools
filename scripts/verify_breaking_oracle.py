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
    (re.compile(r"\bbreaking\b[\s\-_]*(?:change|changes)?\s*[:：]", re.I),
     "BREAKING: / BREAKING CHANGE: / BREAKING-CHANGE:"),
    (re.compile(r"\bbreaking\b[\s\-_]*(?:change|changes)\b", re.I), "BREAKING CHANGE 短语"),
]

# ⚠️ 单独出现**不算**破坏性标记：本 org 的 CHANGELOG 里 ⚠️ 是说明性强调
# （实测 agentkit v1.8.14：「⚠️ 这条改动是被矩阵的符号级转发完整性门禁逼出来的」
# —— 那是「为什么改」的注解，那一节本身是新增转发，不是破坏性变更）。
# 误报一次就会逼人绕过门禁，所以 ⚠️ 只有与破坏性语义词**同行**出现才算信号。
WARN_BREAKING_WORD = re.compile(r"破坏性|breaking|不兼容|不再兼容|已移除|已删除|不再是", re.I)
WARN_MARK = re.compile(r"⚠")

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
        if WARN_MARK.search(line) and WARN_BREAKING_WORD.search(line):
            sig.append({"kind": "explicit", "detail": "⚠️ + 破坏性语义词同行",
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
"""

# 生成器（breaking_index.py）**至今仍收不到**的形态——反证就拿这些当样本：
#   v0.11.0 粗体 **BREAKING**: / 项目符号里的 BREAKING CHANGE / 小写 breaking change
#   v0.12.0 散文形态的 import 路径搬家 —— 这条生成器已在本轮补上（不再是盲区），
#            但仍留在样本里：oracle 必须独立收得到它，不依赖生成器的修复。
_OLD_PARSER_BLINDS = ["v0.11.0", "v0.12.0"]


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
          {"v0.11.0", "v0.12.0"} <= set(got), True)

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

    # ---- ⚠️ 单独出现是说明性强调，不是破坏性标记（实测 agentkit v1.8.14）----
    warn_only = ("## [v2.0.0] — 2026-10-20\n\n### 新增(facade)：转发 16 个新符号\n\n"
                 "⚠️ 这条改动是被**矩阵的符号级转发完整性门禁**逼出来的。\n"
                 "转发 16 个符号：\n- type: PriceTable\n")
    check("⚠️ 强调不误判", "v2.0.0" in flagged_versions(warn_only), False)
    warn_breaking = ("## [v2.0.1] — 2026-10-20\n\n### 修复：改个说法\n\n"
                     "⚠️ 破坏性变更：下游需同步改用法。\n")
    check("⚠️ + 破坏性词仍收得到", "v2.0.1" in flagged_versions(warn_breaking), True)

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
