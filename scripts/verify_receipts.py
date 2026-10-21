#!/usr/bin/env python3
"""issue 回执落地门禁：被 PR 自动关闭的 issue 必须留下一条事实型回执。

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

## 判据（只取产物，不取退出码）

「回执」= 该 issue 的**评论正文**里出现下列任一：

- PR 链接（`pull/N`）
- 版本号（`vX.Y.Z`）
- 验收类结论（`验收` / `回执` / `已修复` / `已落地`）

「缺口」= 同时满足三条：

1. 该 issue 是**被 PR 自动关闭**的（`closedByPullRequestsReferences` 非空）；
2. 它**没有**任何回执型评论；
3. 它不在豁免范围内（标签 `wontfix`/`invalid`/`duplicate`，
   或 `scripts/receipt_exemptions.json` 里显式列出）。

**判据只看 issue 上真实存在的评论**——不看任何命令的退出码。
这正是本门禁要防的那类失效（「命令返回 0」不等于「它做成了事」）。

## 为什么范围必须收敛到「被 PR 自动关闭」

人工 `gh issue close` 关掉的 issue（撤回的前提不成立、判为 wontfix 的……）
本来就不该有回执。全矩阵 31 条 closed issue 里只有 8 条是被 PR 自动关闭的
（2026-10-22 实测）；把另外 23 条也算进来 ⇒ 上线即一片红 ⇒
与「红了就加豁免」同命。

## 豁免名单必须显式，而且必须会过期

`scripts/receipt_exemptions.json` 是**显式豁免名单**：每条写清理由、可逐条核对。
它还必须**会过期**——某条豁免对应的 issue 后来有了回执、或不再是被 PR 自动关闭的，
本门禁判**黄灯**要求删掉它。
（豁免名单只增不减 = 永久豁免清单，与 issue matrix-tools#72 的教训同构。）

## 三档

| 档 | 含义 | 行为 |
|---|---|---|
| 红 | 确认缺口（被 PR 自动关闭 + 无回执 + 不在豁免内） | 退出码 1 |
| 黄 | **判不了**（取数失败 / 结果被 limit 截断）或**豁免已失效** | 不失败，但必须出现在报告里 |
| 绿 | 无缺口 | — |

「判不了 ≠ 没问题」：取数失败时判**黄灯**而不是绿灯——否则门禁会在网络故障时
静默放行，而故障恰好是最需要它的时候。

## 用法

    # 全矩阵扫描（联网）
    verify_receipts.py --org oagentkit

    # 只扫一个仓
    verify_receipts.py --org oagentkit --repo llm

    # 离线复现 llm#29 的缺口形态（真实取值形态的历史快照）
    verify_receipts.py --fixture scripts/fixtures/receipts_llm29.json

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

# 回执判据：评论正文里出现下列任一即算「有回执」。
#
# 为什么只认评论、不认 issue 正文：判据要窄而稳定。正文是提 issue 人写的，
# 可能只是复述问题（「我这边也遇到过 v0.15.0 的问题」），拿它当回执会让门禁变瞎。
RECEIPT_PATTERNS = (
    re.compile(r"pull/\d+"),                 # PR 链接：https://github.com/oagentkit/llm/pull/32
    re.compile(r"\bv\d+\.\d+\.\d+\b"),       # 版本号：v0.15.0
    re.compile(r"验收|回执|已修复|已落地"),   # 验收类结论
)

# 标签层面的豁免。**唯一允许的隐式豁免**——因为它写在 issue 上，肉眼可见、可核对。
EXEMPT_LABELS = frozenset({"wontfix", "invalid", "duplicate"})

DEFAULT_FIXTURE = HERE / "fixtures" / "receipts_llm29.json"
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


def _has_receipt(issue: dict) -> bool:
    """该 issue 上有没有回执型评论。"""
    for c in issue.get("comments") or []:
        body = c.get("body") or ""
        if any(p.search(body) for p in RECEIPT_PATTERNS):
            return True
    return False


# ---------------------------------------------------------------- 判定层


def judge_repo(repo: str, issues: list[dict], exemptions: set[int]) -> list[Finding]:
    """判定一个仓的已关闭 issue → 结论列表（judge_report.Finding）。

    只判「被 PR 自动关闭」这一形态；人工关闭的不在本门禁范围。
    """
    findings: list[Finding] = []

    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue

        prs = _auto_closed_prs(issue)
        subject = f"{repo}#{number}"

        if number in exemptions:
            # 豁免条目必须**会过期**：它豁免的那个缺口已经不成立了，
            # 就该把这一条删掉——否则豁免名单会变成永久豁免清单。
            if not prs or _has_receipt(issue):
                findings.append(Finding(
                    YELLOW, subject,
                    "豁免条目已经失效——该 issue 现在不再是被 PR 自动关闭的，或已经有回执了",
                    "删掉 scripts/receipt_exemptions.json 里的这一条："
                    "豁免名单只该装当前真的豁免的东西"))
            continue

        if not prs:
            continue                      # 人工关闭 → 不在本门禁范围
        if _labels(issue) & EXEMPT_LABELS:
            continue                      # 标签显式豁免

        if _has_receipt(issue):
            continue

        pr_list = "、".join(f"#{p}" for p in prs)
        findings.append(Finding(
            RED, subject,
            f"被 PR {pr_list} 自动关闭，但一条回执都没留下",
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
                "取不到该仓的已关闭 issue——判不了，不等于没问题",
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
    """
    import copy

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # ---- 1. 真实取值形态的 fixture：llm#29 的缺口必须被抓出
    try:
        snap = load_fixture(DEFAULT_FIXTURE)
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

    # ---- 3. 人工关闭（不是被 PR 自动关闭）⇒ 不在本门禁范围
    manual = copy.deepcopy(snap)
    manual["llm"][0]["closedByPullRequestsReferences"] = []
    check("人工关闭不报红",
          _red_subjects(scan(list(manual.keys()), {},
                              lambda repo: manual.get(repo, []))),
          [])

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

    # ---- 9. 反证自测：把被防的那一路掐掉后，结论必须变
    orig_auto = _auto_closed_prs
    orig_receipt = _has_receipt
    try:
        globals()["_auto_closed_prs"] = lambda issue: []
        check("反证A：掐掉「被 PR 自动关闭」后不再报红",
              _red_subjects(scan(list(snap.keys()), {},
                                  lambda repo: snap.get(repo, []))),
              [])
        globals()["_auto_closed_prs"] = orig_auto

        globals()["_has_receipt"] = lambda issue: False
        check("反证B：掐掉「回执识别」后有回执的也报红",
              _red_subjects(scan(list(with_receipt.keys()), {},
                                  lambda repo: with_receipt.get(repo, []))),
              ["llm#29"])
    finally:
        globals()["_auto_closed_prs"] = orig_auto
        globals()["_has_receipt"] = orig_receipt

    if fails:
        print("❌ verify_receipts 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_receipts 自测通过"
          "（llm#29 缺口复现 / 有回执转绿 / 范围收窄 / 豁免过期判黄 / "
          "判不了判黄 / 反证 A、B）")
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
            lambda repo: repo_meta.list_closed_issues(args.org, repo, limit=args.limit),
            limit=args.limit))
    else:
        ap.error("需要 --org（联网扫描）或 --fixture（离线快照）之一")

    code = emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if args.json_out:
        Path(args.json_out).write_text(report.render_json(), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
