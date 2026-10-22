#!/usr/bin/env python3
"""issue 回执落地门禁：被**机器自动关闭**的 issue 必须留下一条事实型回执。

## 为什么需要它（实测 oagentkit/llm#29，issue matrix-tools#71）

issue 被 **PR 正文 `Closes #N` 在合入时自动关闭**时，回执评论可能**一条都没留下**。

机制上的原因：`gh issue close --comment` 对**已关闭**的 issue 会**静默不贴评论**
（打印 `! Issue ... is already closed` 并以 0 退出）——**退出码仍是 0**。
任何「按退出码判成功」的收尾脚本都会误报「已回执」。

实测样本（llm#29）：

- `state=CLOSED`、`closedAt=2026-10-21T08:48:08Z`
- `closedByPullRequestsReferences` = `https://github.com/oagentkit/llm/pull/32`
- 被自动关闭时 **`comments` = 0 条**

而 PR#32 本身完全正常：CI 全绿（run 39571785487）、修复随 llm v0.15.0 发布。
⇒ **关单成功、CI 绿、tag 已打，没有任何地方会报错**——只有专门去翻 issue 评论的人才会发现。

⇒ 关单来源有**两条**路（PR 正文 / commit message 里的关闭关键词），
「机器关的单」这一形态必须两条都覆盖；回执也必须**机器核验得了**才算数。
这两条路各自的漏法见下面「判据」一节与 issue matrix-tools#88。

## 判据（只取产物，不取退出码）

### 范围：这个 issue 是**被机器关的**还是**被人关的**

「缺口」的前提是「**机器自动关的单**」：

- 被某个 **PR** 关闭（`closedByPullRequestsReferences` 非空，**含跨仓 PR**——
  GitHub 支持 `Closes owner/repo#N`；实测 mcpkit#17 是被 toolgate#24 关的）；
- 被某个**提交**关闭（commit message 里的关闭关键词）。

**人工关的单不算**（`gh issue close` / UI 关单）：撤回的前提不成立、
判为 wontfix 的……本来就不该有回执。算进来就是上线即一片红 ⇒
与「红了就加豁免」同命。

⚠️ **为什么不能只看 `closedByPullRequestsReferences`**（issue matrix-tools#88）：
那个字段只覆盖「被 PR 关闭」这一条路，而 GitHub **还会读 commit message 里的
关闭关键词**——squash 合并会把 commit message 原样带进 main ⇒
commit message 里的 `Closes #N` 照样触发自动关单，那条路在 REST 侧**完全不可见**。
实测 matrix-tools#86：PR 正文刻意写 `Refs: #86`，但 squash 用的 commit message
末尾有 `Closes #86` ⇒ 合并后 2 秒被自动关闭，而
`closedByPullRequestsReferences` = **空** ⇒ 旧口径直接跳过它 ⇒
「关单成功但零回执」从这条缝里溜过去。

所以范围判据取**两个来源的并集**：

| 来源 | 覆盖的关闭方式 |
|---|---|
| `closedByPullRequestsReferences` 非空 | 被 PR 关闭（含跨仓 PR） |
| issue 时间线 `closedEvent.closer` | 被 PR / 被**提交** / 人工（`closer` 为 null） |

closer 取**最后一次**关闭事件（issue 可能被关过又重开过）。

### 回执：必须带**可核验锚点**

「回执」= 评论正文里带**锚点**（能指向一个产物）的一条，锚点三选一：

- PR 链接（`pull/N`）
- 版本号（`vX.Y.Z`）
- 完整提交 sha（40 位十六进制）

⚠️ **为什么不能只看「验收类措辞」**（issue matrix-tools#88）：旧判据是
「PR 链接 / 版本号 / 验收类结论**任一**命中即算回执」，而**认领/派单评论里
也会出现「验收」**——实测 matrix-tools#86 的认领评论里有一节标题就叫
「## 验收」（不含任何锚点：PR 还没开、还没有版本号）⇒ 旧判据把它算成回执
⇒ 「零回执」的形态又被认领评论顶掉了。

「认领评论」说的是「**我要做**」，回执说的是「**做完了，这是 PR / 版本 / 门禁结论**」。
判据要能分开这两者 ⇒ 锚点是分水岭。

三档对应三档语义（**判不了 ≠ 没问题**）：

| 回执状态 | 含义 | 判定 |
|---|---|---|
| `anchored` | 有锚点 | 绿 |
| `unanchored` | 有完成声明（已修复/已落地…）但**没有锚点** | **黄**——它确实是回执，只是机器核验不了；要求补锚点 |
| `none` | 什么都没有（典型：只有认领/派单评论） | **红**——确认缺口 |

为什么给 `unanchored` 留黄档而不是直接判红：判红等于说「它没回执」，
而它**确实写了回执**（已修复/已落地 + 核验过程）——那是冤枉；
判绿等于放过「机器核验不了」——那正是本门禁要消灭的东西。
黄档 = 「可疑 / 待确认」，正是它的位置。

「缺口」= 同时满足三条：

1. 该 issue 是**被机器自动关闭**的；
2. 它**没有**任何回执型评论（`none`）；
3. 它不在豁免范围内（标签 `wontfix`/`invalid`/`duplicate`，
   或 `scripts/receipt_exemptions.json` 里显式列出）。

**判据只看 issue 上真实存在的评论**——不看任何命令的退出码。
这正是本门禁要防的那类失效（「命令返回 0」不等于「它做成了事」）。

## 豁免名单必须显式，而且必须会过期

`scripts/receipt_exemptions.json` 是**显式豁免名单**：每条写清理由、可逐条核对。
它还必须**会过期**——某条豁免对应的 issue 后来有了回执、或不再是被 PR 自动关闭的，
本门禁判**黄灯**要求删掉它。
（豁免名单只增不减 = 永久豁免清单，与 issue matrix-tools#72 的教训同构。）

## 三档

| 档 | 含义 | 行为 |
|---|---|---|
| 红 | 确认缺口（被机器自动关闭 + 无回执 + 不在豁免内） | 退出码 1 |
| 黄 | **判不了**（取数失败 / 关闭来源判不出 / 结果被 limit 截断）、**豁免已失效**、或**回执只有完成声明没有锚点** | 不失败，但必须出现在报告里 |
| 绿 | 无缺口 | — |

「判不了 ≠ 没问题」：取数失败时判**黄灯**而不是绿灯——否则门禁会在网络故障时
静默放行，而故障恰好是最需要它的时候。

「有回执但核验不了」同样判**黄灯**：它确实写了回执（所以不是红），
但机器核验不了（所以不是绿）——要求补锚点。

## 用法

    # 全矩阵扫描（联网）
    verify_receipts.py --org oagentkit

    # 只扫一个仓
    verify_receipts.py --org oagentkit --repo llm

    # 离线复现 llm#29 的缺口形态（真实取值形态的历史快照）
    verify_receipts.py --fixture scripts/fixtures/receipts_llm29.json

    # 离线复现「commit message 关单 + 零回执」的缺口形态
    verify_receipts.py --fixture scripts/fixtures/receipts_commit_close.json

    # 离线自测（含反证样本）
    verify_receipts.py --selftest

退出码: 0 = 无缺口（黄灯也不失败）；1 = 有缺口。
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
from judge_report import RED, YELLOW, Finding, Report, emit  # noqa: E402
from matrix_facts import load_matrix  # noqa: E402

# 回执判据：评论正文里出现下列**任一锚点**即算「有回执」。
#
# 为什么只认评论、不认 issue 正文：判据要窄而稳定。正文是提 issue 人写的，
# 可能只是复述问题（「我这边也遇到过 v0.15.0 的问题」），拿它当回执会让门禁变瞎。
#
# 为什么锚点必须是「**能指向一个产物**的标识」：旧判据是
# 「PR 链接 / 版本号 / 验收类结论**任一**命中」，而「验收类结论」这一档
# 最容易被**认领/派单评论**命中（实测 matrix-tools#86 的认领评论里
# 有一节标题就叫「## 验收」，内容是「交付时要满足什么」）⇒
# 认领评论被当成了回执 ⇒ 「关单成功但零回执」从这条缝里溜过去。
# 锚点是「做完了」与「我要做」的分水岭。
RECEIPT_ANCHORS = (
    re.compile(r"pull/\d+"),                 # PR 链接：https://github.com/oagentkit/llm/pull/32
    re.compile(r"\bv\d+\.\d+\.\d+\b"),       # 版本号：v0.15.0
    re.compile(r"\b[0-9a-f]{40}\b"),         # 完整提交 sha（工具仓没有 tag 时用得上）
)

# 完成声明：说明「事情做完了」，但**不带锚点**时只判黄不判绿。
#
# 刻意**不含**裸「验收」二字——「验收：假 server 测试（…）」这类句子出现在
# **派单/认领**评论里讲的是「交付时要满足什么」，不是「已经验收完」。
# 带「已」前缀的措辞才表示完成。
COMPLETION_MARKER = re.compile(
    r"已修复|已落地|已验收|已合并|已完成|验收通过|修复完成")

# 标签层面的豁免。**唯一允许的隐式豁免**——因为它写在 issue 上，肉眼可见、可核对。
EXEMPT_LABELS = frozenset({"wontfix", "invalid", "duplicate"})

DEFAULT_FIXTURE = HERE / "fixtures" / "receipts_llm29.json"
COMMIT_CLOSE_FIXTURE = HERE / "fixtures" / "receipts_commit_close.json"
DEFAULT_EXEMPTIONS = HERE / "receipt_exemptions.json"


# ---------------------------------------------------------------- 事实读取
# （下面这几个函数只做「把 issue 的原始字段读出来」，不做判定；
#   判定全在 judge_* 里，自测要掐的也是这几个函数。)


def _auto_closed_prs(issue: dict) -> list[int]:
    """该 issue 是被哪些 PR 自动关闭的（空 = 不是被 PR 自动关闭的）。"""
    out: list[int] = []
    for ref in issue.get("closedByPullRequestsReferences") or []:
        n = ref.get("number")
        if isinstance(n, int):
            out.append(n)
    return out


def _labels(issue: dict) -> set[str]:
    return {str(l.get("name", "")).strip().lower() for l in (issue.get("labels") or [])}


def _receipt_state(issue: dict) -> str:
    """该 issue 的回执状态 → 'anchored' | 'unanchored' | 'none'。

    - `anchored`：有**锚点**（PR 链接 / 版本号 / 提交 sha）⇒ 这条是真回执；
    - `unanchored`：只有完成声明（已修复/已落地…）但**没有锚点**
      ⇒ 它确实是回执，但**机器核验不了**（判黄，要求补锚点）；
    - `none`：什么都没有（典型：只有认领/派单评论）⇒ 确认缺口。
    """
    marked = False
    for c in issue.get("comments") or []:
        body = c.get("body") or ""
        if any(p.search(body) for p in RECEIPT_ANCHORS):
            return "anchored"
        if COMPLETION_MARKER.search(body):
            marked = True
    return "unanchored" if marked else "none"


def _close_source(issue: dict) -> str:
    """该 issue 是**被机器关的**还是**被人关的** → 'machine' | 'manual' | 'unknown'。

    两个来源取并集（issue matrix-tools#88）：

    - `closedByPullRequestsReferences` 非空 ⇒ 被 PR 关闭过（**含跨仓 PR**）；
    - 时间线 `closedEvent.closer` 是 PR / Commit ⇒ 被机器关的
      （**Commit 这一支是旧口径完全看不见的那条路**：commit message 里的
      关闭关键词触发的自动关单）。

    两者都没有、且 closer 明确是人工（null）⇒ 'manual'（不在本门禁范围）；
    closer 判不出来 ⇒ 'unknown'（**判不了 ≠ 没问题** ⇒ 判黄）。
    """
    if _auto_closed_prs(issue):
        return "machine"

    closer = issue.get("closer")
    if isinstance(closer, dict):
        kind = closer.get("type")
        if kind in ("pull_request", "commit"):
            return "machine"
        if kind == "manual":
            return "manual"
    return "unknown"


def _closer_label(issue: dict) -> str:
    """人读的「谁关的单」标签（给报告用）。"""
    prs = _auto_closed_prs(issue)
    closer = issue.get("closer") if isinstance(issue.get("closer"), dict) else {}
    if closer.get("type") == "pull_request" and closer.get("pr"):
        repo = closer.get("pr_repo")
        where = f"（{repo}）" if repo else ""
        return f"PR #{closer['pr']}{where}"
    if closer.get("type") == "commit" and closer.get("oid"):
        return f"提交 {str(closer['oid'])[:12]}"
    if prs:
        return "PR " + "、".join(f"#{p}" for p in prs)
    return "自动关闭"


# ---------------------------------------------------------------- 判定层


def judge_repo(repo: str, issues: list[dict], exemptions: set[int]) -> list[Finding]:
    """判定一个仓的已关闭 issue → 结论列表（judge_report.Finding）。

    只判「**被机器自动关闭**」这一形态；人工关闭的不在本门禁范围。
    """
    findings: list[Finding] = []

    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue

        source = _close_source(issue)
        state = _receipt_state(issue)
        subject = f"{repo}#{number}"

        if number in exemptions:
            # 豁免条目必须**会过期**：它豁免的那个缺口已经不成立了，
            # 就该把这一条删掉——否则豁免名单会变成永久豁免清单。
            if source != "machine" or state != "none":
                findings.append(Finding(
                    YELLOW, subject,
                    "豁免条目已经失效——该 issue 现在不再是被机器自动关闭的，或已经有回执了",
                    "删掉 scripts/receipt_exemptions.json 里的这一条："
                    "豁免名单只该装当前真的豁免的东西"))
            continue

        if source == "unknown":
            findings.append(Finding(
                YELLOW, subject,
                "取不到这个 issue 的关闭来源——判不了，不等于没问题",
                "确认 gh 认证可用（GH_TOKEN）与网络可达；"
                "判不了时门禁不会报红，但这一条必须有人看"))
            continue
        if source == "manual":
            continue                      # 人工关闭 → 不在本门禁范围
        if _labels(issue) & EXEMPT_LABELS:
            continue                      # 标签显式豁免

        if state == "anchored":
            continue

        if state == "unanchored":
            findings.append(Finding(
                YELLOW, subject,
                f"被 {_closer_label(issue)} 自动关闭了，回执只有完成声明、"
                "没有可核验锚点（PR 链接 / 版本号 / 提交 sha）",
                "在回执里补上锚点（PR 链接 + 首次发布的 tag + 门禁结论）；"
                "机器核验不了锚点的回执，和没有回执只差一步"))
            continue

        findings.append(Finding(
            RED, subject,
            f"被 {_closer_label(issue)} 自动关闭，但一条回执都没留下",
            "补一条事实型回执（PR 链接 + 首次发布的 tag + 门禁结论）："
            "gh issue comment <n> --repo <repo> --body-file <f>，"
            "补完回读 issue 确认评论真在（发出成功 ≠ 落地）"))

    return findings


def scan(repos: list[str],
         exemptions: dict[str, set[int]],
         fetcher,
         limit: int = 100) -> list[Finding]:
    """扫一批仓 → 结论列表。fetcher(repo) → list[dict] | None（None = 取不到 = 判不了）。"""
    findings: list[Finding] = []

    for repo in repos:
        issues = fetcher(repo)
        if issues is None:
            findings.append(Finding(
                YELLOW, repo,
                "取不到该仓的已关闭 issue 或其关闭来源——判不了，不等于没问题",
                "确认 gh 认证可用（GH_TOKEN）与网络可达；"
                "判不了时门禁不会报红，但这一条必须有人看"))
            continue
        if len(issues) >= limit:
            findings.append(Finding(
                YELLOW, repo,
                f"取到的已关闭 issue 达到 limit={limit} 上限，可能被截断——没取到的那部分没被检查",
                f"调高 --limit（当前 {limit}）"))
        findings.extend(judge_repo(repo, issues, exemptions.get(repo, set())))

    return findings


# ---------------------------------------------------------------- 配置读取


def load_exemptions(path: Path) -> tuple[dict[str, set[int]], str | None]:
    """读显式豁免名单 → (按仓分组的号集合, 错误信息)。"""
    if not path.is_file():
        return {}, f"豁免名单 {path} 不存在"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {}, f"豁免名单 {path} 解析失败：{exc}"
    if not isinstance(data, dict):
        return {}, f"豁免名单 {path} 顶层不是对象"

    out: dict[str, set[int]] = {}
    for entry in data.get("exemptions") or []:
        if not isinstance(entry, dict):
            return {}, f"豁免名单 {path} 有条目不是对象：{entry!r}"
        repo = entry.get("repo")
        number = entry.get("number")
        if not repo or not isinstance(number, int):
            return {}, f"豁免名单 {path} 有条目缺 repo/number：{entry!r}"
        if not entry.get("reason"):
            return {}, f"豁免名单 {path} 的 {repo}#{number} 没写 reason——豁免必须能逐条核对"
        out.setdefault(str(repo), set()).add(number)
    return out, None


def load_fixture(path: Path) -> dict[str, list[dict]]:
    """读离线快照 → {仓: [issue, ...]}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        raise ValueError(f"{path} 里没有 repos 字典")
    return {str(k): list(v or []) for k, v in repos.items()}


def live_closed_issues(org: str, repo: str, limit: int = 100) -> list[dict] | None:
    """联网取某仓的已关闭 issue，并把**关闭来源**并进去。

    → list[dict]（每条带 `closer` 键）；取不到返回 None（**判不了 ≠ 没有**）。

    两个取数源都要拿到：issue 本体（评论 / 标签 / 被哪些 PR 关闭）与
    关闭来源（时间线上的 closedEvent.closer）。**少任何一个都不判**——
    缺前者判不了回执，缺后者判不了「是不是机器关的单」。
    """
    issues = repo_meta.list_closed_issues(org, repo, limit=limit)
    if issues is None:
        return None
    closers = repo_meta.list_closed_issue_closers(org, repo, limit=limit)
    if closers is None:
        return None                      # 关闭来源取不到 ⇒ 整仓判不了
    for issue in issues:
        number = issue.get("number")
        issue["closer"] = closers.get(
            number, {"type": "unknown"}) if isinstance(number, int) else {"type": "unknown"}
    return issues


def matrix_repos(root: Path) -> list[str]:
    """矩阵的仓清单 —— 唯一数据源是 modules.json（业务模块 + 基建仓）。"""
    matrix = load_matrix(root / "modules.json")
    seen: list[str] = []
    for repo in list(matrix.repos) + list(matrix.infra_repos()):
        if repo and repo not in seen:
            seen.append(repo)
    return seen


# ---------------------------------------------------------------- 自测


def _levels(findings: list[Finding]) -> list[str]:
    return [f.level for f in findings]


def _red_subjects(findings: list[Finding]) -> list[str]:
    return [f.subject for f in findings if f.level == RED]


def selftest() -> int:
    """反证自测。

    除了「该红的红、该绿的不红」，还要证明**自测本身有区分力**：
    把被防的那一路掐掉后，结论必须变——否则自测只是「跑通即绿」的摆设。

    两个缺口各有专门的反证样本（issue matrix-tools#88）：

    - **缺口 A（关单来源）**：commit message 里的 `Closes #N` 关的单，
      旧口径完全看不见 ⇒ 反证样本 = `receipts_commit_close.json`；
    - **缺口 B（回执识别）**：只有认领/派单评论（里面含「验收」二字），
      旧口径把它当成回执 ⇒ 反证样本 = 把 llm#29 的评论换成认领评论。
    """
    import copy

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # ---- 1. 真实取值形态的 fixture：llm#29 的缺口必须被抓出
    try:
        snap = load_fixture(DEFAULT_FIXTURE)
        commit_snap = load_fixture(COMMIT_CLOSE_FIXTURE)
    except (OSError, ValueError) as exc:
        print(f"❌ verify_receipts 自测失败：fixture 读不到：{exc}")
        return 1

    findings = scan(list(snap.keys()), {}, lambda repo: snap.get(repo, []))
    check("llm#29 缺口被判红", _red_subjects(findings), ["llm#29"])
    check("缺口指向 PR#32",
          any("PR #32" in f.message for f in findings), True)
    check("缺口带修法", all(f.hint for f in findings if f.level == RED), True)

    # ---- 2. 补上回执 ⇒ 转绿
    with_receipt = copy.deepcopy(snap)
    with_receipt["llm"][0]["comments"] = [{
        "id": "IC_kwDOVNnBQs8AAAABeNiJ-Q",
        "author": {"login": "zy84338719"},
        "body": "PR: https://github.com/oagentkit/llm/pull/32\n"
                "随 llm v0.15.0 发布\n验收：CI 全绿",
    }]
    check("有回执后不报红",
          _red_subjects(scan(list(with_receipt.keys()), {},
                              lambda repo: with_receipt.get(repo, []))),
          [])

    # ---- 3. 人工关闭（不是被机器关的）⇒ 不在本门禁范围
    manual = copy.deepcopy(snap)
    manual["llm"][0]["closedByPullRequestsReferences"] = []
    manual["llm"][0]["closer"] = {"type": "manual"}
    check("人工关闭不报红",
          _red_subjects(scan(list(manual.keys()), {},
                              lambda repo: manual.get(repo, []))),
          [])
    check("人工关闭不判黄", _levels(scan(list(manual.keys()), {},
                                        lambda repo: manual.get(repo, []))), [])

    # ---- 4. 豁免标签 ⇒ 不报红
    labeled = copy.deepcopy(snap)
    labeled["llm"][0]["labels"] = [{"name": "wontfix"}]
    check("wontfix 标签不报红",
          _red_subjects(scan(list(labeled.keys()), {},
                              lambda repo: labeled.get(repo, []))),
          [])

    # ---- 5. 显式豁免 ⇒ 不报红
    check("显式豁免不报红",
          _red_subjects(scan(list(snap.keys()), {"llm": {29}},
                              lambda repo: snap.get(repo, []))),
          [])

    # ---- 6. 豁免条目已失效（该 issue 后来有了回执）⇒ 判黄
    stale = scan(list(with_receipt.keys()), {"llm": {29}},
                 lambda repo: with_receipt.get(repo, []))
    check("豁免失效判黄", _levels(stale), [YELLOW])

    # ---- 7. 取数失败 ⇒ 判黄（判不了 ≠ 没问题）
    unknown = scan(["llm"], {}, lambda repo: None)
    check("取数失败判黄", _levels(unknown), [YELLOW])
    check("取数失败不算缺口", _red_subjects(unknown), [])

    # ---- 8. 取到上限 ⇒ 判黄（可能被截断的那部分没被检查）
    truncated = scan(["llm"], {}, lambda repo: [{}] * 100, limit=100)
    check("截断判黄", _levels(truncated), [YELLOW])

    # ---- 9. 缺口 A 的反证样本：commit message 关的单 + 零回执 ⇒ 判红
    commit_findings = scan(list(commit_snap.keys()), {},
                           lambda repo: commit_snap.get(repo, []))
    check("commit message 关单被判红", _red_subjects(commit_findings), ["matrix-tools#86"])
    check("commit 关单的结论指向提交",
          any("提交" in f.message for f in commit_findings), True)

    # ---- 10. 缺口 B 的反证样本：只有认领评论（含「验收」二字、无锚点）⇒ 判红
    claimed = copy.deepcopy(snap)
    claimed["llm"][0]["comments"] = [{
        "id": "IC_kwDOVNnBQs8AAAABeNiJ-Q",
        "author": {"login": "zy84338719"},
        "body": "## 认领\n\n这条我来做。\n\n"
                "### 验收\n\n假 server 测试（不引真 MCP 服务端）；"
                "三态判定含「工具缺失 ≠ server 挂」的具体用例。",
    }]
    check("只有认领评论判红",
          _red_subjects(scan(list(claimed.keys()), {},
                              lambda repo: claimed.get(repo, []))),
          ["llm#29"])
    check("认领评论不算回执", _receipt_state(claimed["llm"][0]), "none")

    # ---- 11. 有完成声明但没有锚点 ⇒ 判黄（不是红，也不是绿）
    unanchored = copy.deepcopy(snap)
    unanchored["llm"][0]["comments"] = [{
        "id": "IC_kwDOVNnBQs8AAAABeNiJ-Q",
        "author": {"login": "zy84338719"},
        "body": "【已修复并验收关闭】\n\n3 个 tier0 基础包的 Go 地板已降到 1.24.0，"
                "CI go-version 与 go.mod 成对。",
    }]
    unanchored_findings = scan(list(unanchored.keys()), {},
                               lambda repo: unanchored.get(repo, []))
    check("无锚点的回执判黄", _levels(unanchored_findings), [YELLOW])
    check("无锚点的回执不算缺口", _red_subjects(unanchored_findings), [])

    # ---- 12. 关闭来源判不了 ⇒ 判黄
    unknown_closer = copy.deepcopy(snap)
    unknown_closer["llm"][0]["closedByPullRequestsReferences"] = []
    unknown_closer["llm"][0]["closer"] = {"type": "unknown"}
    unknown_closer_findings = scan(list(unknown_closer.keys()), {},
                                   lambda repo: unknown_closer.get(repo, []))
    check("关闭来源判不了判黄", _levels(unknown_closer_findings), [YELLOW])
    check("关闭来源判不了不算缺口", _red_subjects(unknown_closer_findings), [])

    # ---- 13. 反证自测：把被防的那一路掐掉后，结论必须变
    orig_source = _close_source
    orig_state = _receipt_state
    try:
        globals()["_close_source"] = lambda issue: "manual"
        check("反证A：掐掉「机器关单」后不再报红",
              _red_subjects(scan(list(snap.keys()), {},
                                  lambda repo: snap.get(repo, []))),
              [])
        check("反证C：掐掉「机器关单」后 commit 关单也不再报红",
              _red_subjects(scan(list(commit_snap.keys()), {},
                                  lambda repo: commit_snap.get(repo, []))),
              [])
        globals()["_close_source"] = orig_source

        globals()["_receipt_state"] = lambda issue: "none"
        check("反证B：掐掉「回执识别」后有回执的也报红",
              _red_subjects(scan(list(with_receipt.keys()), {},
                                  lambda repo: with_receipt.get(repo, []))),
              ["llm#29"])
        check("反证D：掐掉「回执识别」后认领评论也报红",
              _red_subjects(scan(list(claimed.keys()), {},
                                  lambda repo: claimed.get(repo, []))),
              ["llm#29"])
        globals()["_receipt_state"] = lambda issue: "anchored"
        check("反证E：把回执一律当有 ⇒ 认领评论不再报红",
              _red_subjects(scan(list(claimed.keys()), {},
                                  lambda repo: claimed.get(repo, []))),
              [])
    finally:
        globals()["_close_source"] = orig_source
        globals()["_receipt_state"] = orig_state

    if fails:
        print("❌ verify_receipts 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_receipts 自测通过"
          "（llm#29 缺口复现 / 有回执转绿 / 范围收窄 / 豁免过期判黄 / "
          "判不了判黄 / commit 关单判红 / 认领评论判红 / 无锚点回执判黄 / "
          "反证 A–E）")
    return 0


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="issue 回执落地门禁")
    ap.add_argument("--org", help="组织名（联网扫描）")
    ap.add_argument("--repo", action="append", default=[],
                    help="只扫这些仓（可重复）")
    ap.add_argument("--root", default=str(HERE.parent),
                    help="矩阵根目录（用于找 modules.json）")
    ap.add_argument("--fixture", default=None,
                    help="离线跑：读一份 gh issue list 输出的 JSON 快照")
    ap.add_argument("--exemptions", default=str(DEFAULT_EXEMPTIONS),
                    help="显式豁免名单路径")
    ap.add_argument("--limit", type=int, default=100,
                    help="每仓最多取多少条已关闭 issue（取到上限判黄）")
    ap.add_argument("--selftest", action="store_true", help="离线自测（含反证）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--json-out", default=None, help="把 JSON 报告写到该文件")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也失败")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    exemptions, err = load_exemptions(Path(args.exemptions))
    report = Report(title="issue 回执落地")
    if err:
        report.yellow("exemptions", err,
                      "豁免名单读不到 ⇒ 按「没有豁免」处理（更严），但这一条要有人看")

    if args.fixture:
        path = Path(args.fixture)
        try:
            snap = load_fixture(path)
        except (OSError, ValueError) as exc:
            report.red("fixture", f"快照读不到：{exc}",
                       "确认 --fixture 指向的是 gh issue list --json 的输出")
            return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
        report.findings.extend(scan(list(snap.keys()), exemptions,
                                     lambda repo: snap.get(repo, []), limit=args.limit))
    elif args.org:
        if args.repo:
            repos = list(args.repo)
        else:
            try:
                repos = matrix_repos(Path(args.root))
            except (OSError, ValueError) as exc:
                report.red("modules.json", f"仓清单读不到：{exc}",
                           "确认 --root 指向含 modules.json 的矩阵根目录")
                return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
        report.findings.extend(scan(
            repos, exemptions,
            lambda repo: live_closed_issues(args.org, repo, limit=args.limit),
            limit=args.limit))
    else:
        ap.error("需要 --org（联网扫描）或 --fixture（离线快照）之一")

    code = emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if args.json_out:
        Path(args.json_out).write_text(report.render_json(), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
