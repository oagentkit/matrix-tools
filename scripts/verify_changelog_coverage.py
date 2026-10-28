#!/usr/bin/env python3
"""CHANGELOG 覆盖门禁（**独立 oracle**）—— issue matrix-tools#41 的根治门禁。

## 守的是哪两件事

判据是**两级**，缺一不可：

**① 条目级覆盖**
每个「非零信息量」的 commit，在提交版 CHANGELOG 的**对应版本段**里必须有一条对应条目
（匹配键 = 类型 + 主题）。漏条目 = 红。

**② body 级覆盖**
每个 commit 的 body 里**非空、去重后**的每一行，必须出现在提交版的对应版本段里。
body 缺失 = 红。

第 ② 条才是本脚本存在的理由：**条目在、body 整段消失**的产物看起来完全正常
（标题在、层级对、段数对），逐字节门禁也比不出来——它比的正是「丢 body 之后的结果」。
发布说明的实质内容（迁移步骤、版本对照表、判据锚点）恰恰都在 body 里。

## 为什么必须是**独立 oracle**（不复用生成器的解析逻辑）

门禁要比的是「提交版 CHANGELOG」与「git 历史里应当出现的内容」。判据一旦复用
`gen_changelogs.py` 的解析/渲染函数，生成器丢 body 时门禁会**跟着一起丢**——
于是门禁全绿，而发布说明已经残缺（实测 2026-10-20：7 个仓合计 927 处正文缺失，
逐字节门禁一条都没报出来）。

所以本脚本**不 import 生成器的任何函数**：

- 「哪些提交该出现在发布说明里」→ 自己从 `git log --format=%H%x1f%P%x1f%s%x1f%b`
  的**原始历史**算（tag 区间 + 零信息量提交过滤）；
- 「提交版里长什么样算覆盖」→ 只依赖 CHANGELOG 的**公开格式约定**
  （版本段标题 `## [vX.Y.Z]`、条目行以 `### ` / `- ` 开头、类型标签用中文），
  这层约定是本 org 对外发布的格式，属于被检对象的**规格**而不是它的**实现**。

## 「非零信息量提交」是怎么判的

版本段里要剔除两类提交（否则门禁会去要求一条**永远不会被渲染**的假条目）：

1. **merge commit**（两个以上父提交）——「Merge pull request #N from …」对读者零信息量；
2. **只改 CHANGELOG.md 的提交**——生成器自己产出的提交，否则它会出现在自己的版本段里，
   自我指涉。

⚠️ 判据用**实际改了哪些文件**（不看 subject 文案）——文案会变，文件不会。
这也意味着：若生成提交同时改了别的文件（实测踩过：生成 CHANGELOG 的提交顺手改了
AGENTS.md），它就**不再**被剔除，于是它会出现在版本段里 —— 门禁与生成器对此一致，
不会误报。

## 用法

    verify_changelog_coverage.py --root /path/to/oagentkit            # 本地各仓
    verify_changelog_coverage.py --root ... --repo agent --repo rag    # 只查指定仓
    verify_changelog_coverage.py --org oagentkit                       # 联网（按需克隆）
    verify_changelog_coverage.py --ci --org oagentkit                   # 按 PR 触及范围裁剪
    verify_changelog_coverage.py --ci --org oagentkit \
        --scope-mode repo                                              # 非 PR 事件只查本仓
    verify_changelog_coverage.py --selftest                            # 离线自测

退出码: 0 = 覆盖完整；1 = 有缺失（条目 / 正文）或判不了。

## 范围口径（--scope-mode）

| 模式 | PR 事件 | 非 PR 事件（push / 定时 / 手动） |
|---|---|---|
| `matrix`（**默认**） | 只查 PR 的 base 仓 | **全矩阵** |
| `repo` | 只查 PR 的 base 仓 | **只查调用方本仓**（定时事件除外，见下） |

`repo` 模式解决的是 issue matrix-tools#76：**一个仓的欠账不该把 13 个无关仓的
`main` 一起标红**，而那些红徽章又**永不自愈**（欠账修好后没人重跑那个仓）。

⚠️ 两条口径纪律：

1. **定时（schedule）事件在 `repo` 模式下仍然是全矩阵**——那是**周期性全量审计**，
   跨仓欠账的唯一硬失败归属地就在这里；把它也收窄成「只查本仓」＝ 跨仓欠账
   再也没人抓（静默失效）。
2. **默认必须是 `matrix`**（＝旧行为）——加了开关却默认换口径，等于没加开关，
   一合并就全 org 变行为。换成 `repo` 要显式传参（调用层传），并先在**单个仓试点**。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULT_VERSIONS = HERE.parent / "versions.json"

# git log 的字段分隔符。%x1f = unit sep（字段间），%x1d = group sep（正文与文件清单间）。
FIELD_SEP = "\x1f"
GROUP_SEP = "\x1d"
REC_SEP = "\x1e"

# 主模块版本线的 tag：vX.Y.Z（与 repo_meta.MAIN_TAG_RE 同一形态）
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+")

# 非 PR 事件的范围口径（--scope-mode）
#
#   matrix —— 旧口径：非 PR 事件（push / 定时 / 手动）扫**全矩阵**。
#   repo  —— 新口径：非 PR 事件只扫**调用方本仓**。
#
# ⚠️ 默认必须是 matrix（＝旧行为）：「加了开关却默认换口径」＝ 没加开关——
# 一合并就全 org 换行为，且没有回退窗口。换成 repo 要**显式传参**，
# 并先在单个仓试点（见 .github/AGENTS.md 的门禁本体纪律）。
SCOPE_MODES = ("matrix", "repo")
DEFAULT_SCOPE_MODE = "matrix"

# conventional commit：type(scope)!: subject
# ⚠️ 叹号位置有两种写法，都要认（与生成器同源事实）：
#   标准 feat(api)!: … ／ 本矩阵实际在用 refactor!(agent): …
CC_RE = re.compile(
    r"^(?P<type>[a-z]+)(?P<bang1>!)?(?:\((?P<scope>[^)]*)\))?(?P<bang2>!)?:\s*(?P<subject>.+)$"
)

# 派单流程强制要求的 PR 标题前缀 `[round-2] `——它是**编排标记**，
# 不属于 conventional commit 本身，解析前只剥离它。
ROUND_PREFIX_RE = re.compile(r"^\s*\[round-[^\]]*\]\s*")

# GitHub squash merge 给 subject 加的 PR 号后缀「(#N)」。
PR_SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")

# markdown 列表符（去掉后比较用）
LIST_MARKS = ("* ", "- ", "+ ")

# 类型 → CHANGELOG 里出现的中文标签（**本 oracle 自己维护的映射**，
# 不从生成器 import——生成器改标签时门禁不该跟着一起变）。
TYPE_LABELS: dict[str, tuple[str, ...]] = {
    "feat": ("新增", "feat"),
    "fix": ("修复", "fix"),
    "perf": ("性能", "perf"),
    "refactor": ("重构", "refactor"),
    "security": ("安全", "security"),
    "docs": ("文档", "docs"),
    "test": ("测试", "test"),
    "build": ("构建", "build"),
    "chore": ("杂务", "chore"),
    "ci": ("CI", "ci"),
    "style": ("格式", "style"),
    "revert": ("回滚", "revert"),
}

# 标签 → 类型（反查表；小写形式一并登记，CI 这类大小写不同的标签要认）
LABEL_TO_TYPE: dict[str, str] = {}
for _t, _marks in TYPE_LABELS.items():
    for _m in _marks:
        LABEL_TO_TYPE.setdefault(_m, _t)
        LABEL_TO_TYPE.setdefault(_m.lower(), _t)

# CHANGELOG 的版本段标题：`## [v1.2.3] — 2026-01-01`
SECTION_RE = re.compile(r"^##\s*\[(?P<v>v\d+\.\d+\.\d+[^\]]*)\]")

# 条目行：小标题 `### ` 或要点 `- `
ENTRY_PREFIXES = ("### ", "- ")


# --------------------------------------------------------------------------
# 取数：原始 git 历史
# --------------------------------------------------------------------------

def git(repo_dir: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo_dir), *args],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def semver_key(v: str) -> tuple:
    """semver 比较键（数字比，字符串比会把 v0.9.10 判成小于 v0.9.9）。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", (v or "").strip())
    if not m:
        return (0, 0, 0, 0, "")
    maj, mi, pa, pre = m.groups()
    return (int(maj), int(mi), int(pa), 0 if pre else 1, pre or "")


def list_tags(repo_dir: Path) -> list[str]:
    """主模块版本线的 tag，**升序**（区间计算要升序）。"""
    out = git(repo_dir, "tag", "--list")
    tags = [t.strip() for t in out.splitlines() if TAG_RE.match(t.strip())]
    return sorted(tags, key=semver_key)


def commits_between(repo_dir: Path, prev: str | None, cur: str) -> list[dict]:
    """区间 (prev..cur] 内的提交，含 sha / parents / subject / body / files。

    一次 `git log` 取全：sha、���父、subject、body、改动文件清单。
    ⚠️ 区间语义必须与生成器一致（`git log prev..cur`，**不是** --first-parent）：
    走 PR 合并进来的分支上的 commit 本来就该出现在发布说明里。
    """
    rng = f"{prev}..{cur}" if prev else cur
    fmt = f"--format=%x1e%H{FIELD_SEP}%P{FIELD_SEP}%s{FIELD_SEP}%b{GROUP_SEP}"
    out = git(repo_dir, "log", fmt, "--name-only", rng)
    items: list[dict] = []
    for chunk in out.split(REC_SEP):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(FIELD_SEP)
        if len(parts) < 3:
            continue
        sha = parts[0].strip()
        parents = parts[1].split()
        subject = parts[2].strip()
        rest = parts[3] if len(parts) > 3 else ""
        body, _, files_blob = rest.partition(GROUP_SEP)
        files = [ln.strip() for ln in files_blob.splitlines() if ln.strip()]
        items.append({
            "sha": sha,
            "parents": parents,
            "subject": subject,
            "body": body.strip("\n"),
            "files": files,
        })
    return items


def is_zero_information(c: dict) -> bool:
    """该提交是否「零信息量」（不该出现在发布说明里）。

    ① merge commit（两个以上父提交）；
    ② 只改 CHANGELOG.md 的提交（生成器自己产出的提交，否则自我指涉）。

    ⚠️ ②的判据是**实际改了哪些文件**，不看 subject 文案。
    """
    if len(c.get("parents") or []) > 1:
        return True
    return c.get("files") == ["CHANGELOG.md"]


def last_changelog_commit(repo_dir: Path) -> str | None:
    """最后一个改动过 CHANGELOG.md 的提交 sha（用来定位「预写」发生在哪一刻）。

    为什么需要它：待发版段是「预写」那一刻写下的，它只覆盖**那一刻之前**的提交。
    之后落地的提交（PR 在途、别的仓的变更）不属于它的覆盖范围——
    要求它们已入段，会把「预写 → 提交 → 打 tag」流程里**打 tag 之前**的那段窗口
    判成缺失（这正是 issue 标题抱怨的「预写段在打 tag 前必然红」的同族问题）。
    """
    out = git(repo_dir, "log", "-1", "--format=%H", "--", "CHANGELOG.md").strip()
    return out or None


# --------------------------------------------------------------------------
# 解析：提交 → 应出现的条目 / 正文行
# --------------------------------------------------------------------------

def parse_subject(raw_subject: str) -> tuple[str, str]:
    """原始 subject → (type, 主题文本)。

    - 先剥 `[round-N] ` 编排前缀与 GitHub 的「(#N)」后缀；
    - 再按 conventional commit 解析；解析不出 type 时 type = "other"，
      主题文本 = 整个 subject（生成器对这类提交也是这么渲染的）。
    """
    s = ROUND_PREFIX_RE.sub("", raw_subject or "").strip()
    s = PR_SUFFIX_RE.sub("", s).strip()
    m = CC_RE.match(s)
    if m:
        return m.group("type"), m.group("subject").strip()
    return "other", s


def _norm(s: str) -> str:
    return (s or "").strip()


def _collapse(s: str) -> str:
    return " ".join((s or "").split())


def _strip_list_mark(s: str) -> str:
    s = _norm(s)
    for mark in LIST_MARKS:
        if s.startswith(mark):
            return s[len(mark):].strip()
    return s


def _echo_normalize(s: str) -> str:
    """把「subject 侧」与「body 行侧」归一到同一个口径，才能判回声。

    两侧都要做这些事（顺序固定）：
      ① 剥 bullet 标记与缩进（`* ` / `- ` / `+ `）；
      ② 剥 `[round-N] ` 编排前缀（**可能多层**）；
      ③ 剥结尾的 PR 号后缀 `(#N)`；
      ④ 折叠空白。

    ⚠️ 只剥一层 bullet / 只剥一层 round 前缀都会漏判——实测样本：
        subject: `[round-2] feat(mcp): 自检 / 工具目录差集 (#11)`
        body 行: `* feat(mcp): 自检 / 工具目录差集`
      归一化后两边都是 `feat(mcp): 自检 / 工具目录差集` ⇒ 是回声，不该被要求出现在段里。
    """
    s = _norm(s)
    s = _strip_list_mark(s)
    while True:
        s2 = ROUND_PREFIX_RE.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = PR_SUFFIX_RE.sub("", s).strip()
    return " ".join(s.split())


def _is_echo(line: str, subject_text: str, raw_subject: str) -> bool:
    """该行是否只是把 subject 又复述了一遍（生成器会去掉这类回声行）。

    判据（两边都归一化后比较）：
      ① 归一化后的 body 行 == 归一化后的 subject；
      ② 归一化后的 body 行**按 conventional commit 解析出的主题** == subject 主题
         （body 行写成「* feat(x): 主题」而 subject 只给了主题时命中这条）；
      ③ 归一化后的 body 行 == subject 主题本身。
    """
    body_n = _echo_normalize(line)
    if not body_n:
        return False
    if body_n == _echo_normalize(raw_subject):
        return True
    if body_n == _echo_normalize(subject_text):
        return True
    m = CC_RE.match(body_n)
    if m:
        head = _echo_normalize(m.group("subject"))
        if head and head == _echo_normalize(subject_text):
            return True
    return False


def expected_body_lines(c: dict, subject_text: str) -> list[str]:
    """该提交的 body 里**应当出现**的行（非空、去重、去回声、去公共缩进）。

    顺序保持 git 里的原样——发布说明的正文顺序本身就是信息。
    """
    raw = c.get("body") or ""
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    # 去 git 续行加的 4 空格公共缩进（不去的话 markdown 表格会被缩成代码块）
    while lines and all(ln.startswith("    ") for ln in lines):
        lines = [ln[4:] for ln in lines]
    out: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if _is_echo(s, subject_text, c.get("subject", "")):
            continue
        key = _collapse(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# --------------------------------------------------------------------------
# 解析：提交版 CHANGELOG
# --------------------------------------------------------------------------

def parse_sections(text: str) -> dict[str, list[str]]:
    """提交版 CHANGELOG → {版本段: 该段的行}。

    版本段标题行本身**不入段**——标题里的日期会随「预写那天」变，
    拿它做比对会造出与内容无关的假红。
    """
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for ln in (text or "").splitlines():
        m = SECTION_RE.match(ln)
        if m:
            cur = m.group("v")
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(ln)
    return out


def entry_lines(section: list[str]) -> list[str]:
    return [ln for ln in section if ln.startswith(ENTRY_PREFIXES)]


def entry_type(line: str) -> str | None:
    """条目行的类型标签 → type；认不出返回 None。

    条目行的形态是「<类型标签>(scope)：<主题>」或「<类型标签>：<主题>」；
    「other」桶没有标签（生成器直接渲染原始 subject），于是返回 None。
    """
    body = line
    for prefix in ENTRY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
            break
    left = body.split("：", 1)[0].strip()
    if not left:
        return None
    label = re.split(r"[(（]", left, maxsplit=1)[0].strip()
    if not label:
        return None
    return LABEL_TO_TYPE.get(label) or LABEL_TO_TYPE.get(label.lower())


def entry_subject(line: str) -> str:
    """条目行里承载主题的那部分。"""
    body = line
    for prefix in ENTRY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
            break
    if "：" in body:
        return body.split("：", 1)[1].strip()
    return body


def entry_raw_text(line: str) -> str:
    """条目行去掉列表符后的**整行**文本（不切标签）。

    生成器 `gen_changelogs.py` 的 `render_entry` 在 `CC_RE` 匹配失败（解析不出
    conventional commit）时，把 subject **原样**渲成 bullet ⇒ 这时整行文本就是
    提交主题本身（含主题里的全角冒号）。

    而本文件的 `entry_type` / `entry_subject` 按「首个全角冒号」切标签——那条口径
    只对 **typed** 条目成立（生成器渲成 `### 新增(invoke)：主题`）。中文提交的
    subject 常带全角冒号（`feat(invoke)：…`）⇒ other 条目会被误读成 typed 条目
    ⇒ 提交侧判 other、CHANGELOG 侧判 feat，两边对不上 ⇒ 门禁误报「漏条目」
    （真实事故：guardx 2026-10-28）。

    ⇒ other 类提交要按本函数的**整行文本**比对。
    """
    body = line
    for prefix in ENTRY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
            break
    return body


def body_line_present(section: list[str], want: str) -> bool:
    """该段里是否出现了这一行正文（先精确比，再退到空白折叠比）。"""
    target = _norm(want)
    for ln in section:
        if _norm(ln) == target:
            return True
    target2 = _collapse(want)
    for ln in section:
        if _collapse(ln) == target2:
            return True
    return False


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

class RepoReport:
    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.missing_entries: list[str] = []   # 漏条目
        self.wrong_type: list[str] = []        # 条目在但类型不符
        self.missing_body: list[str] = []      # 正文行缺失
        self.section_missing: list[str] = []    # 缺版本段
        self.notes: list[str] = []

    @property
    def problems(self) -> int:
        return (len(self.missing_entries) + len(self.wrong_type)
                + len(self.missing_body) + len(self.section_missing))

    def render(self) -> str:
        lines = [f"── {self.repo} ──"]
        if self.section_missing:
            lines.append(f"   ❌ 缺版本段: {', '.join(self.section_missing)}")
        if self.missing_entries:
            lines.append(f"   ❌ 漏条目 {len(self.missing_entries)} 条:")
            lines += [f"      - {x}" for x in self.missing_entries[:10]]
            if len(self.missing_entries) > 10:
                lines.append(f"      …（共 {len(self.missing_entries)} 条）")
        if self.wrong_type:
            lines.append(f"   ❌ 条目类型不符 {len(self.wrong_type)} 条:")
            lines += [f"      - {x}" for x in self.wrong_type[:10]]
            if len(self.wrong_type) > 10:
                lines.append(f"      …（共 {len(self.wrong_type)} 条）")
        if self.missing_body:
            lines.append(f"   ❌ 正文缺失 {len(self.missing_body)} 行:")
            lines += [f"      - {x}" for x in self.missing_body[:10]]
            if len(self.missing_body) > 10:
                lines.append(f"      …（共 {len(self.missing_body)} 行）")
        for n in self.notes:
            lines.append(f"   ℹ️ {n}")
        if not self.problems:
            lines.append("   ✅ 覆盖完整")
        return "\n".join(lines)


def check_repo(repo_dir: Path, repo: str) -> RepoReport:
    """校验一个仓的 CHANGELOG 覆盖情况。

    ## 判定面

    - **已发布版本**（tag 区间）：段里该有的条目、正文、类型必须都在，缺一即红；
    - **待发版段**（提交版里版本号高于最高 tag 的那一段）：**有**就校验它覆盖的提交，
      **没有不报红**——「tag 之后有提交但还没预写」是发版前的常态，不是缺陷。
    - 「段缺失」只在「提交版里有某段、但该段应覆盖的提交缺了」时才是问题。
    """
    rep = RepoReport(repo)
    cl_path = repo_dir / "CHANGELOG.md"
    if not cl_path.exists():
        rep.section_missing.append("(整个 CHANGELOG.md 缺失)")
        return rep
    committed = parse_sections(cl_path.read_text(encoding="utf-8"))

    tags = list_tags(repo_dir)
    if not tags:
        rep.notes.append("没有任何版本 tag，无从判定（判不了，不当成「没问题」）")
        return rep

    # ── 期望侧：按 tag 切段，每段内的提交（已剔除零信息量） ──
    expected: dict[str, list[dict]] = {}
    prev: str | None = None
    for t in tags:
        cs = [c for c in commits_between(repo_dir, prev, t) if not is_zero_information(c)]
        expected[t] = cs
        prev = t

    highest_tag = tags[-1]
    # ── 待发版段（提交版里**有**才校验；没有**不因此报红**） ──
    #
    # 为什么「没有就不报红」：本矩阵的打法是「先合入 main、再在 main 上预写待发版段、
    # 然后打 tag」。于是两次发版之间，绝大多数仓都处在「tag 之后有实质提交、但还没预写」
    # 的状态——那是**发版前的常态**，不是缺陷。要求「tag 之后的每个提交都得有段覆盖」
    # 会让门禁在两次发版之间**永远红**（issue 标题抱怨的「预写段在打 tag 前必然红」
    # 的同族问题，只是方向相反）。
    #
    # 「段缺失」只在这一种情形才是问题：**提交版里有某段，但该段应覆盖的提交缺了**。
    pending_version = None
    for v in sorted(committed, key=semver_key, reverse=True):
        if semver_key(v) > semver_key(highest_tag):
            pending_version = v
            break
    if pending_version is not None:
        all_head = commits_between(repo_dir, highest_tag, "HEAD")
        end_sha = last_changelog_commit(repo_dir)
        cut = next((i for i, c in enumerate(all_head) if c["sha"] == end_sha), None)
        if cut is None:
            rep.notes.append(
                f"待发版段 {pending_version} 存在，但定位不到它的覆盖范围"
                "（最后一个改动 CHANGELOG.md 的提交不在最高 tag 之后），跳过该段校验")
        else:
            # all_head 是「新 → 旧」；cut 及其之后 = 预写时看到的那批提交。
            # 比 cut 更新的提交是「预写之后才落地的」（PR 在途等），不要求它们已入段。
            expected[pending_version] = [c for c in all_head[cut:]
                                         if not is_zero_information(c)]

    # ── 比对 ──
    for version, commits in expected.items():
        if version not in committed:
            rep.section_missing.append(version)
            for c in commits:
                ctype, subject = parse_subject(c["subject"])
                rep.missing_entries.append(f"{version} {ctype}: {subject}  ({c['sha'][:7]})")
                for bl in expected_body_lines(c, subject):
                    rep.missing_body.append(f"{version} {ctype}: {subject} → {bl}")
            continue
        section = committed[version]
        entries = entry_lines(section)
        for c in commits:
            ctype, subject = parse_subject(c["subject"])
            subject_n = _collapse(subject)
            hit_type = None
            hit_any = False
            for ln in entries:
                if ctype == "other":
                    # 生成器把解析不出 conventional 的 subject **原样**渲成 bullet
                    # ⇒ 期望主题可能自带全角冒号（中文正文常见，如 `feat(x)：…`），
                    # 按「首个全角冒号」切标签会把它切掉 ⇒ 先按**整行文本全等**比对
                    #（生成器就是原样渲染的，整行文本即主题本身）。
                    if subject_n and _collapse(entry_raw_text(ln)) == subject_n:
                        hit_type = ctype
                        hit_any = True
                        break
                if subject_n and subject_n not in _collapse(entry_subject(ln)):
                    continue
                hit_any = True
                t = entry_type(ln)
                if t == ctype or (ctype == "other" and t is None):
                    hit_type = ctype
                    break
                if t is None and hit_type is None:
                    # 类型标签认不出（格式变了？）——按覆盖处理，但记一笔
                    hit_type = "unknown"
            if hit_type is None:
                if hit_any:
                    rep.wrong_type.append(
                        f"{version} 期望 {ctype}: {subject}  ({c['sha'][:7]})")
                else:
                    rep.missing_entries.append(
                        f"{version} {ctype}: {subject}  ({c['sha'][:7]})")
            for bl in expected_body_lines(c, subject):
                if not body_line_present(section, bl):
                    rep.missing_body.append(
                        f"{version} {ctype}: {subject} → {bl}")
    return rep


# --------------------------------------------------------------------------
# 范围：谁造成漂移谁负责
# --------------------------------------------------------------------------

def matrix_repos(versions_path: Path = DEFAULT_VERSIONS) -> list[str]:
    """有 CHANGELOG 要求的仓：矩阵模块 + 门面仓。"""
    try:
        data = json.loads(versions_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    repos = [m["repo"] for m in data.get("modules", [])]
    repos += [f["repo"] for f in data.get("facades", [])]
    return repos


def ci_scope(versions_path: Path = DEFAULT_VERSIONS,
             scope_mode: str = DEFAULT_SCOPE_MODE) -> tuple[list[str], str | None, str]:
    """按 GitHub 事件上下文判定「本次要校验哪些仓」→ (仓列表, ref, 说明)。

    ## 判定依据（信息源）

    - `GITHUB_EVENT_NAME` + `GITHUB_EVENT_PATH`：PR 事件里带着 **base 仓**与 **PR 号**；
    - `GITHUB_REPOSITORY`：事件所属的仓。

    GitHub 的 PR 是**单仓**的：PR 能改动的文件全部落在它自己的 base 仓里
    （跨仓只可能通过 submodule 指针体现）。所以

        **PR 触及的仓 = PR 的 base 仓**

    而不是「矩阵里的所有仓」。这就是「谁造成漂移谁负责」：
    改 `agent` 的 PR 只该被 `agent` 的 CHANGELOG 欠账挡住，
    改 `.github` 的 PR 不该被任何模块仓的欠账挡住。

    ## 非 PR 事件的两档口径（--scope-mode）

    - `matrix`（**默认**＝旧行为）：全矩阵（周期性全量审计兜底，**不可省**，
      否则是漏检）。
    - `repo`：**只校验调用方本仓**。

    为什么要 `repo` 这一档（issue matrix-tools#76）：跨仓门禁在 push 事件下走全矩阵，
    于是「哪个仓最后被 push，哪个仓就替别人的欠账背红」——**一个仓的欠账把 13 个
    无关仓的 `main` 一起标红**；欠账修好后那些仓再没被 push，**红徽章永不自愈**
    （实测挂了约 13 小时，且没有任何门禁会报「有 8 个仓的徽章是陈旧的」）。

    ⚠️ **`schedule` 事件在 `repo` 模式下仍然是全矩阵**：那是**周期性全量审计**，
    跨仓欠账的**唯一硬失败归属地**（matrix-tools 的每日 job）。把它一起收窄，
    等于跨仓欠账再也没人抓——那正是本门禁要防的「静默失效」。

    ⚠️ 调用方**不在**「有 CHANGELOG 要求的仓」清单里（基建仓：.github /
    matrix-tools / 站点仓）⇒ 范围为空 ⇒ 本次不校验。它们是**审计归属方**
    （审计全矩阵），不是**被审计方**（不需要 CHANGELOG 覆盖）。

    ## 为什么 PR 上要用 **PR head** 而不是各仓 main

    裁决（issue matrix-tools#41 评论）：只裁剪范围不换 ref 解决不了问题——
    合入前各仓 main 上还是**旧产物**，照样红。要判的是「这个 PR 提交的内容够不够」，
    就必须拿 PR 的内容来判。
    """
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    known = matrix_repos(versions_path)
    if event_name in ("pull_request", "pull_request_target") and event_path:
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            event = {}
        pr = event.get("pull_request") or {}
        base_full = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or repo_full
        number = pr.get("number")
        base_repo = base_full.split("/")[-1] if base_full else ""
        ref = f"pull/{number}/head" if number else None
        # 「PR 触及的仓」∩「有 CHANGELOG 要求的仓」= 本次校验范围。
        # PR 落在矩阵外的仓（.github / matrix-tools / 站点仓）⇒ 范围为空 ⇒ 不校验。
        effective = [base_repo] if base_repo in known else []
        note = (f"事件 {event_name}：PR 的 base 仓 = {base_full or '(未知)'}；"
                f"「PR 触及的仓」= [{base_repo or '(未知)'}];"
                f"其中有 CHANGELOG 要求的 = {effective or '（无 ⇒ 本次不校验）'}；"
                f"校验 ref = {ref or '(默认分支)'}")
        return effective, ref, note
    if scope_mode == "repo" and event_name != "schedule":
        # 只校验调用方本仓（见上面「非 PR 事件的两档口径」）。
        self_repo = repo_full.split("/")[-1] if repo_full else ""
        effective = [self_repo] if self_repo in known else []
        note = (f"事件 {event_name or '(本地)'}：范围模式 repo → 只校验调用方本仓"
                f"（{self_repo or '(未知)'}）；"
                f"其中有 CHANGELOG 要求的 = {effective or '（无 ⇒ 本次不校验）'}；"
                f"全矩阵审计由 matrix-tools 的每日定时 job 承担")
        return effective, None, note
    note = (f"事件 {event_name or '(本地)'}：非 PR 事件 → 全矩阵审计"
            f"（{len(known)} 个仓）")
    return known, None, note


# --------------------------------------------------------------------------
# 取仓：本地目录 / 联网克隆
# --------------------------------------------------------------------------

def _git_run(target: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(target), *args],
                          capture_output=True, text=True)


def _fast_forward_worktree(target: Path) -> bool:
    """把工作树推进到远端默认分支的**当前**状态。

    ## 为什么必须这一步（实测过的真实故障）

    缓存命中时旧实现只跑 `git fetch --tags`——**tag 更新了，工作树没动**。
    于是门禁拿**新 tag** 去比**旧工作树里的 CHANGELOG.md**，报出
    「缺版本段 v0.9.3 / v0.8.2 / v0.8.3 / v0.14.2 …」这类**根本不存在的**漂移。

    本地实跑同一脚本却是全绿的 —— 假红的来源就在这里。
    而 CI 的缓存键是 `run_id`（每轮一个）+ `restore-keys` 前缀 + 7 天淘汰，
    意味着**首次克隆之后工作树被永久冻结**，门禁会持续假红约 7 天，
    把真实漂移的信号彻底淹没。

    所以：**取 tag 与取工作树内容必须是同一个时点**。tag 用 fetch 拿，
    内容必须跟着 reset 走。

    推不动就返回 False —— 调用方据此判「判不了」，不能当成「内容没变」。
    """
    # origin/HEAD 是 clone 时写下的默认分支指针；老 clone 可能没有，退回 origin/main
    candidates = []
    head = _git_run(target, "rev-parse", "--verify", "--quiet", "origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        candidates.append("origin/HEAD")
    candidates.append("origin/main")
    candidates.append("origin/master")
    for ref in candidates:
        r = _git_run(target, "reset", "--quiet", "--hard", ref)
        if r.returncode == 0:
            return True
    return False


def ensure_clone(org: str, repo: str, dest: Path, ref: str | None) -> Path | None:
    """把仓克隆/更新到 dest/repo，并 checkout 到 ref（给了的话）。取不到返回 None。"""
    target = dest / repo
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet",
                            f"https://github.com/{org}/{repo}.git", str(target)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
    else:
        subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "--tags", "origin"],
                       capture_output=True, text=True)
        # 缓存命中：**必须推进工作树**，否则拿新 tag 比旧内容 → 假红。
        _fast_forward_worktree(target)
    if ref:
        r = subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "origin",
                            f"+{ref}:pr-head"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        r = subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach",
                            "pr-head"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
    return target


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return r.stdout


def _write_changelog(repo: Path, text: str) -> None:
    (repo / "CHANGELOG.md").write_text(text, encoding="utf-8")


# 样本仓的「正确」CHANGELOG——**手写**的，不经过生成器。
#
# 覆盖了样本仓里全部**非零信息量**提交：
#   v0.1.0 段：feat(alpha)（带 body）
#   v0.2.0 段：feat(beta)（侧枝合进来的）+ chore(deps)（带 body）+ docs
GOOD_CHANGELOG = """# demo CHANGELOG

## [v0.1.0] — 2026-01-01

### 新增(alpha)：第一个功能

正文第一行
正文第二行

## [v0.2.0] — 2026-01-02

### 新增(beta)：侧枝功能

- 杂务(deps)：升依赖

依赖说明正文

- 文档：写点文档

### 新增(gamma)：回声样本

真正的正文
"""


def _scope_selftest() -> list[str]:
    """范围判定的自测——**必须带反证样本**。

    这条判据最危险的失效形态是**收窄过头**：口径一旦把**定时全矩阵审计**也收窄成
    「只查本仓」，跨仓欠账就再也没人抓 ⇒ 静默失效（而门禁照样全绿）。

    所以反证样本至少要证明：

      ① `repo` 模式下 push 事件**确实**只返回调用方本仓（这就是 matrix-tools#76
         要修的那件事——反证：不能仍然返回全矩阵）；
      ② `repo` 模式下**定时事件仍然是全矩阵**（反证：不能把周期审计一起收窄）；
      ③ 基建仓（不在「有 CHANGELOG 要求的仓」清单里）作为调用方 ⇒ 不校验任何仓；
      ④ PR 事件**不受模式影响**（谁造成漂移谁负责）；
      ⑤ `matrix` 模式（默认）**保持旧口径**全矩阵——默认换口径等于没加开关。

    全部**离线**：用临时 versions.json + 临时事件文件，不联网。
    """
    import os
    import shutil
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    tmp = Path(tempfile.mkdtemp(prefix="changelog-scope-selftest-"))
    saved = {k: os.environ.get(k) for k in
             ("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_REPOSITORY")}
    try:
        versions = tmp / "versions.json"
        versions.write_text(json.dumps({
            "modules": [{"repo": "alpha"}],
            "facades": [{"repo": "beta"}],
        }, ensure_ascii=False), encoding="utf-8")

        event = tmp / "event.json"
        event.write_text(json.dumps({
            "pull_request": {"number": 7,
                             "base": {"repo": {"full_name": "oagentkit/alpha"}}},
        }), encoding="utf-8")

        def scope(event_name: str, repo: str, mode: str, pr_event: bool = False):
            os.environ["GITHUB_EVENT_NAME"] = event_name
            os.environ["GITHUB_REPOSITORY"] = repo
            if pr_event:
                os.environ["GITHUB_EVENT_PATH"] = str(event)
            else:
                os.environ.pop("GITHUB_EVENT_PATH", None)
            return ci_scope(versions, mode)

        # ① repo 模式 + push → 只查调用方本仓（#76 的修复本体）
        repos, ref, _ = scope("push", "oagentkit/alpha", "repo")
        check("repo 模式 push: 只查调用方本仓", repos, ["alpha"])
        check("repo 模式 push: 不用 PR head", ref, None)

        # ② repo 模式 + 定时 → 仍然是全矩阵（反证：周期审计不能被收窄）
        repos, _, _ = scope("schedule", "oagentkit/alpha", "repo")
        check("repo 模式 schedule: 仍是全矩阵审计", repos, ["alpha", "beta"])

        # ③ 基建仓作为调用方 ⇒ 不校验任何仓（它不是发布件，是审计归属方）
        repos, _, _ = scope("push", "oagentkit/matrix-tools", "repo")
        check("repo 模式 push 基建仓: 不校验任何仓", repos, [])

        # ④ PR 事件不受模式影响
        repos, ref, _ = scope("pull_request", "oagentkit/alpha", "repo", pr_event=True)
        check("repo 模式 PR: 仍只查 base 仓", repos, ["alpha"])
        check("repo 模式 PR: 仍用 PR head", ref, "pull/7/head")
        repos, _, _ = scope("pull_request", "oagentkit/alpha", "matrix", pr_event=True)
        check("matrix 模式 PR: 仍只查 base 仓", repos, ["alpha"])

        # ⑤ 默认口径保持旧行为：非 PR 事件 = 全矩阵
        repos, _, _ = scope("push", "oagentkit/alpha", "matrix")
        check("matrix 模式 push: 保持全矩阵", repos, ["alpha", "beta"])

        # ⑥ 门面仓（facade）也在「有 CHANGELOG 要求的仓」里
        repos, _, _ = scope("push", "oagentkit/beta", "repo")
        check("repo 模式 push 门面仓: 只查门面", repos, ["beta"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return fails


def selftest() -> int:
    """自测：三组样本——**漏条目 / 正文缺失 / 零信息量提交不误报**。

    为什么要自测：守卫最危险的失效形态是「一直绿，但从没生效过」。
    本门禁尤其危险——判据写松一点就会**静默放过所有缺失**。
    所以自测必须包含**故意造坏**的被检对象，要求它把每类缺陷都报出来。

    样本仓里的 CHANGELOG 是**手写**的（不经过生成器）——本门禁是独立 oracle，
    自测也不许依赖被检对象的实现。
    """
    import shutil
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    tmp = Path(tempfile.mkdtemp(prefix="changelog-coverage-selftest-"))
    try:
        repo = tmp / "demo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")

        # v0.1.0：一个带 body 的功能提交
        (repo / "a.go").write_text("package a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "feat(alpha): 第一个功能\n\n"
             "* feat(alpha): 第一个功能\n\n"
             "正文第一行\n正文第二行")
        _git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")

        # 待发版段：一个 chore 提交（带 body）+ 一个只改文档的提交
        (repo / "b.go").write_text("package b\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore(deps): 升依赖\n\n依赖说明正文")
        (repo / "c.md").write_text("doc\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "docs: 写点文档")

        # subject 回声样本：三件套齐全——[round-N] 前缀 + (#N) 后缀 + body 里的 bullet 复述
        (repo / "e.go").write_text("package e\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "[round-2] feat(gamma): 回声样本 (#42)\n\n"
             "* feat(gamma): 回声样本\n\n"
             "真正的正文")

        # 侧枝 + merge commit：merge commit 本身零信息量，但它带进来的
        # 「feat(beta): 侧枝功能」**不是**零信息量 ⇒ 它必须出现在待发版段里。
        _git(repo, "checkout", "-q", "-b", "side")
        (repo / "d.go").write_text("package d\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat(beta): 侧枝功能")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "side", "-m",
             "Merge pull request #1 from demo/side")

        # 只改 CHANGELOG.md 的提交（生成器自己产出的那种）+ 它的 merge commit
        _git(repo, "checkout", "-q", "-b", "chore/gen")
        _write_changelog(repo, GOOD_CHANGELOG)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "docs(changelog): 补 CHANGELOG")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "chore/gen", "-m",
             "Merge pull request #2 from demo/chore/gen")
        _write_changelog(repo, GOOD_CHANGELOG)

        # ① 基线：完整覆盖 → 无问题
        rep = check_repo(repo, "demo")
        check("基线: 覆盖完整", rep.problems, 0)
        check("基线: 不误报条目", len(rep.missing_entries), 0)
        check("基线: 不误报正文", len(rep.missing_body), 0)
        check("基线: 不误报类型", len(rep.wrong_type), 0)

        # ② 漏条目 → 必须报出
        #    ⚠️ 正文行**仍在**段里 ⇒ 这一组同时证明两级判据是**互相独立**的：
        #    条目没了 ≠ 正文也没了，反之亦然。
        bad = GOOD_CHANGELOG.replace("### 新增(alpha)：第一个功能\n\n", "")
        _write_changelog(repo, bad)
        rep = check_repo(repo, "demo")
        check("漏条目: 报出漏条目", len(rep.missing_entries), 1)
        check("漏条目: 报出漏的正是那个功能", "第一个功能" in rep.missing_entries[0], True)
        check("漏条目: 正文仍在段里时不该报正文缺失",
              len(rep.missing_body), 0)

        # ③ 正文缺失 → 必须报出（条目还在，只是 body 整段消失）
        bad = GOOD_CHANGELOG.replace("正文第一行\n正文第二行\n\n", "")
        _write_changelog(repo, bad)
        rep = check_repo(repo, "demo")
        check("正文缺失: 条目没漏", len(rep.missing_entries), 0)
        check("正文缺失: 报出正文缺失", len(rep.missing_body), 2)
        check("正文缺失: 报的是第一行", "正文第一行" in rep.missing_body[0], True)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ④ 零信息量提交不误报：merge commit + 只改 CHANGELOG.md 的提交
        #    （这两个提交都在样本仓的历史里，基线必须仍然是干净的）
        rep = check_repo(repo, "demo")
        check("零信息量: merge commit 不误报条目", len(rep.missing_entries), 0)
        check("零信息量: merge commit 不误报正文", len(rep.missing_body), 0)
        check("零信息量: 只改 CHANGELOG.md 的提交不误报", rep.problems, 0)

        # ⑤ 类型不符 → 必须报出（条目在，但说的是另一类事）
        bad = GOOD_CHANGELOG.replace("### 新增(alpha)：第一个功能",
                                     "### 修复(alpha)：第一个功能")
        _write_changelog(repo, bad)
        rep = check_repo(repo, "demo")
        check("类型不符: 报出类型不符", len(rep.wrong_type), 1)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ⑥ 缺版本段 → 必须报出
        #    ⚠️ 缺的是**已发布版本**的段（v0.1.0 有 tag，CHANGELOG 里却没有）。
        #    缺「待发版段」不算问题（见 ⑧）。
        bad = ("# demo CHANGELOG\n\n## [v0.2.0] — 2026-01-02\n"
               + GOOD_CHANGELOG.split("## [v0.2.0] — 2026-01-02\n", 1)[1])
        _write_changelog(repo, bad)
        rep = check_repo(repo, "demo")
        check("缺版本段: 报出缺段", len(rep.section_missing) > 0, True)
        check("缺版本段: 报的是已发布版本", "v0.1.0" in rep.section_missing[0], True)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ⑦ 「提交版可以更丰富」：段里多写一段手写发布说明，不该被判红
        richer = GOOD_CHANGELOG.replace(
            "依赖说明正文\n",
            "依赖说明正文\n\n- 手写：这版还带了一处手写发布说明\n\n手写正文一行\n")
        _write_changelog(repo, richer)
        rep = check_repo(repo, "demo")
        check("更丰富: 段内多出来的手写内容不判红", rep.problems, 0)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ⑧ 「tag 之后有提交、但没有待发版段」→ **不报红**
        #
        # 这条是**假红防线**：本矩阵的打法是「先合入 main、再在 main 上预写待发版段、
        # 然后打 tag」。于是两次发版之间，几乎每个仓都处在「tag 之后有实质提交、
        # 但还没预写」的状态——那是**发版前的常态**，不是缺陷。
        # 若门禁要求「tag 之后的每个提交都得有段覆盖」，它会在两次发版之间**永远红**
        # （issue 标题抱怨的「预写段在打 tag 前必然红」的同族问题，方向相反）。
        #
        # 样本：把待发版段（v0.2.0）整段删掉，但保留 tag 之后的实质提交。
        no_pending = GOOD_CHANGELOG.split("## [v0.2.0]")[0]
        _write_changelog(repo, no_pending)
        rep = check_repo(repo, "demo")
        check("无待发版段: 不因 tag 后有提交而报红", rep.problems, 0)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ⑨ subject 回声（`[round-N] ` 前缀 + `(#N)` 后缀 + `* ` bullet 三件套齐全）
        #    → **不报红**：回声行是生成器刻意去掉的，不该被要求出现在段里。
        #    （实测样本：mcpkit 的 `[round-2] feat(mcp): … (#11)`，
        #      body 首行 `* feat(mcp): …` 就是回声——归一化口径没做对会报假红。）
        rep = check_repo(repo, "demo")
        check("subject 回声: 不要求回声行出现在段里", rep.problems, 0)
        check("subject 回声: 回声行没被当成正文缺失",
              any("回声样本" in x for x in rep.missing_body), False)
        # 回声行**写进去**也不该判红（提交版可以更丰富）
        with_echo = GOOD_CHANGELOG.replace(
            "### 新增(gamma)：回声样本\n",
            "### 新增(gamma)：回声样本\n\n* feat(gamma): 回声样本\n")
        _write_changelog(repo, with_echo)
        rep = check_repo(repo, "demo")
        check("subject 回声: 回声行写进去也不判红", rep.problems, 0)
        _write_changelog(repo, GOOD_CHANGELOG)

        # ⑩ 全角冒号 subject（解析不出 conventional ⇒ other 类）→ **不误报漏条目**
        #
        # 生成器把这种 subject **原样**渲成 bullet；而中文提交的 subject 常带全角
        # 冒号（`feat(invoke)：…`）⇒ CHANGELOG 侧按「首个全角冒号」切标签会把主题
        # 切掉 ⇒ 提交侧判 other、CHANGELOG 侧判 feat，两边对不上 ⇒ 误报「漏条目」
        # （真实事故：guardx 2026-10-28，那条提交在仓里其实是合规的）。
        #
        # 这条样本是**反证**：判据没修 ⇒ 这里当场红。
        repo2 = tmp / "demo2"
        repo2.mkdir()
        _git(repo2, "init", "-q", "-b", "main")
        _git(repo2, "config", "user.email", "t@example.com")
        _git(repo2, "config", "user.name", "T")
        (repo2 / "f.go").write_text("package f\n")
        _git(repo2, "add", "-A")
        _git(repo2, "commit", "-q", "-m",
             "feat(invoke)：动作层结论——缺第三格\n\n正文一行")
        _git(repo2, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        good2 = ("# demo2 CHANGELOG\n\n## [v0.1.0] — 2026-01-01\n\n"
                 "- feat(invoke)：动作层结论——缺第三格\n\n正文一行\n")
        _write_changelog(repo2, good2)
        rep2 = check_repo(repo2, "demo2")
        check("全角冒号: 原样渲染的条目算覆盖", rep2.problems, 0)
        check("全角冒号: 不误报漏条目", len(rep2.missing_entries), 0)
        # 反证：把那条条目删掉 → **必须**报出漏条目（判据没被放空成「永远绿」）
        bad2 = good2.replace("- feat(invoke)：动作层结论——缺第三格\n", "")
        _write_changelog(repo2, bad2)
        rep2 = check_repo(repo2, "demo2")
        check("全角冒号: 删掉条目必须报出漏条目", len(rep2.missing_entries), 1)
        check("全角冒号: 漏的正是那条",
              "动作层结论" in (rep2.missing_entries[0] if rep2.missing_entries else ""), True)
        # 反证（第二条）：把那条条目**换成别的**条目 → 段里仍有条目行，但缺的是
        # 这一条 ⇒ 必须报出漏条目。这条抓住「判据放空」（other 类恒判覆盖）——
        # 删掉整条时段里没有任何条目行，放空与否都判不出差别。
        other2 = good2.replace("- feat(invoke)：动作层结论——缺第三格\n",
                                "- 其他(别的)：别的条目\n")
        _write_changelog(repo2, other2)
        rep2 = check_repo(repo2, "demo2")
        check("全角冒号: 换成别的条目必须报出漏条目", len(rep2.missing_entries), 1)
        check("全角冒号: 换条目时漏的仍是那条",
              "动作层结论" in (rep2.missing_entries[0] if rep2.missing_entries else ""), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails += _scope_selftest()

    if fails:
        print("❌ verify_changelog_coverage 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_changelog_coverage 自测通过"
          "（漏条目 / 正文缺失 / 零信息量不误报 / 类型不符 / 缺版本段 / 更丰富不误红 / "
          "无待发版段不误红 / subject 回声不误红 / 全角冒号 other 类不误报 / "
          "范围口径 repo 模式只查本仓且定时仍全矩阵）")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CHANGELOG 覆盖门禁（独立 oracle）")
    ap.add_argument("--root", help="各仓本地目录的父目录（每个子目录是一个 git 仓）")
    ap.add_argument("--org", help="联网读各仓（按需克隆到 --clone-dir）")
    ap.add_argument("--clone-dir", help="克隆落地目录（默认 ~/.cache/oagentkit/changelog-coverage）")
    ap.add_argument("--repo", action="append", default=[], help="只查指定仓（可重复）")
    ap.add_argument("--ci", action="store_true",
                    help="按 GitHub 事件上下文裁剪范围：PR → 只查本 PR 触及的仓")
    ap.add_argument("--scope-mode", choices=SCOPE_MODES, default=DEFAULT_SCOPE_MODE,
                    help="非 PR 事件（push / 定时 / 手动）的范围口径："
                         "matrix = 全矩阵（默认＝旧行为）；"
                         "repo = 只校验调用方本仓（全矩阵审计由 matrix-tools 的每日定时 job 承担）")
    ap.add_argument("--ref", help="要校验的 ref（默认各仓当前 HEAD）")
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--print-scope", action="store_true",
                    help="只打印本次要校验的仓与 ref（每行「仓名<TAB>ref」），然后退出。"
                         "给 CI 的 shell 复用同一份范围判定用——"
                         "范围逻辑只此一份，别在 workflow YAML 里另造。"
                         "打印的是**本次实际校验的仓**（含 --scope-mode 的影响）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root) if args.root else None
    org = args.org
    clone_dir = Path(args.clone_dir) if args.clone_dir else (
        Path.home() / ".cache" / "oagentkit" / "changelog-coverage")

    scope_note = ""
    if args.ci:
        repos, ci_ref, scope_note = ci_scope(Path(args.versions), args.scope_mode)
        if ci_ref and not args.ref:
            args.ref = ci_ref
        if args.repo:
            # 显式指定仓时以它为准（本地调试用）
            repos = list(args.repo)
        print(f"ℹ️ 范围判定：{scope_note}", file=sys.stderr)
        if root and args.ref:
            print(f"ℹ️ --root 模式：校验各仓**当前 HEAD**（ref={args.ref} 由本地目录决定，"
                  "不重新 checkout）", file=sys.stderr)
    elif args.repo:
        repos = list(args.repo)
    elif org:
        repos = matrix_repos(Path(args.versions))
    elif root:
        repos = matrix_repos(Path(args.versions))
    else:
        ap.error("必须给 --root / --org / --repo 之一（--selftest 模式除外）")

    if args.print_scope:
        # 机器可读：每行「仓名<TAB>ref」。ref 为空 = 用各仓默认分支。
        #
        # ⚠️ stdout 只放这一种行：**信息性输出一律走 stderr**，否则调用方
        # （CI 的 shell 循环）会把提示语当成仓名读进来。
        # 空范围 ⇒ 一行都不输出（调用方的循环自然空转，退出码 0）。
        for repo in repos:
            print(f"{repo}\t{args.ref or ''}")
        return 0

    if not repos:
        print("ℹ️ 本次没有需要校验的仓（PR 未触及任何有 CHANGELOG 要求的仓）",
              file=sys.stderr)
        return 0

    reports: list[RepoReport] = []
    for repo in repos:
        if root:
            repo_dir = root / repo
        else:
            repo_dir = ensure_clone(org, repo, clone_dir, args.ref)
        if repo_dir is None or not (repo_dir / ".git").exists():
            print(f"❌ {repo}: 取不到仓（判不了，不能当成「没问题」）")
            reports.append(RepoReport(repo))
            reports[-1].notes.append("取不到仓（判不了）")
            reports[-1].section_missing.append("(取不到仓)")
            continue
        reports.append(check_repo(repo_dir, repo))

    print()
    for rep in reports:
        print(rep.render())

    total = sum(r.problems for r in reports)
    if total:
        print()
        print(f"❌ CHANGELOG 覆盖门禁未通过：{total} 处问题"
              f"（漏条目 {sum(len(r.missing_entries) for r in reports)} / "
              f"正文缺失 {sum(len(r.missing_body) for r in reports)} / "
              f"类型不符 {sum(len(r.wrong_type) for r in reports)} / "
              f"缺版本段 {sum(len(r.section_missing) for r in reports)}）")
        return 1
    print()
    print(f"✅ CHANGELOG 覆盖完整（{len(reports)} 个仓）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
