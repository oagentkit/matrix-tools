#!/usr/bin/env python3
"""待发版段可观测性守卫：让「预写段被吞掉」这个状态**看得见**。

## 守的是什么

CHANGELOG 覆盖门禁（`verify_changelog_coverage.py`）的 `pending_version`
是**从提交版 CHANGELOG 里找**的：

    pending_version = None
    for v in sorted(committed, ...):
        if semver_key(v) > semver_key(highest_tag):
            pending_version = v

于是：预写段一旦被删（被一次无参重生成吞掉），`pending_version` 就是 None，
**最高 tag 之后的全部提交根本不进入校验集** ⇒ 门禁无对象可比 ⇒ 全绿。

实测后果（agent v0.10.1）：
`go.mod` 的 Go 地板从 `1.26.0` 降到 `1.24.0`（这是个真实的 API 兼容性变化），
但既没有 tag 也没有 CHANGELOG 段，而 4 道门禁同时通过。

本脚本给这个状态装一个**黄灯**：它不需要让 CI 变红（那是覆盖门禁的职责），
但必须让「预写段曾经存在、现在没了」这件事出现在报告里。

## 判据

对每个仓：

1. 取最高 tag（`vX.Y.Z`）。
2. 扫 CHANGELOG.md 的**提交历史**：历史上是否存在过某一段，其版本号**高于**最高 tag？
   —— 那是「预写段」存在的证据。
3. 若历史上存在过、而**当前** CHANGELOG 里没有那一段 ⇒ **黄：预写段疑似被吞掉**。

只报「疑似」而不是「确认」，因为也可能是有人**主动**放弃了那个预写段
（改版本号重写）。本守卫的职责是**把这件事指出来让人决策**，不是替人决策。

## 为什么是黄灯而不是红灯

「tag 之后有提交、但还没预写」是发版流程的**正常中间态**
（生成器的注释里明确写了：要求每次都预写会让门禁在两次发版之间永远红）。
所以：

- 「还没预写」→ 只记在 notes 里，不报；
- 「**曾经预写过、现在没了**」→ 黄灯。它是异常形态，不是中间态。

## 通用性

不绑定具体矩阵：仓清单从 `--root` 下的 git 仓自动发现，或用 `--repos` 显式给。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_changelogs  # noqa: E402  （只取 tags / semver_key / commits_between，不取它的渲染逻辑）
import matrix_facts as facts_layer  # noqa: E402  （git 历史取数）
from judge_report import Report, emit  # noqa: E402

_SECTION_RE = re.compile(r"^## \[(v\d+\.\d+\.\d+)\]", re.M)


def _sections(text: str) -> list[str]:
    return _SECTION_RE.findall(text or "")


def _changelog_history(repo_dir: Path) -> list[tuple[str, str]]:
    """CHANGELOG.md 的提交历史 → [(sha, 该提交上的 CHANGELOG 全文)]。

    取数走**事实层**（matrix_facts.git_log / git_show_file），
    不自己拼 git 命令——命令写法散落各处正是「同一类 bug 要修多次」的来源。
    """
    out: list[tuple[str, str]] = []
    for entry in facts_layer.git_log(repo_dir, "--follow", "--", "CHANGELOG.md"):
        sha = entry["sha"]
        text = facts_layer.git_show_file(repo_dir, sha, "CHANGELOG.md")
        if text is not None:
            out.append((sha, text))
    return out


def judge_repo(repo_dir: Path, report: Report) -> None:
    repo = repo_dir.name
    tags = gen_changelogs.tags_in_order(repo_dir)
    if not tags:
        report.yellow(repo, "没有任何版本 tag，跳过判定",
                      "浅克隆请用完整克隆；全新仓应先发版")
        return
    highest = tags[0]

    changelog = repo_dir / "CHANGELOG.md"
    if not changelog.is_file():
        report.yellow(repo, "没有 CHANGELOG.md，跳过判定")
        return

    current_sections = _sections(changelog.read_text(encoding="utf-8"))
    current_top = max(current_sections, key=gen_changelogs.semver_key) if current_sections else ""

    # 历史里是否出现过高于最高 tag 的段（= 预写段曾存在）
    history = _changelog_history(repo_dir)
    seen_pending: dict[str, str] = {}
    for sha, text in history:
        secs = _sections(text)
        if not secs:
            continue
        top = max(secs, key=gen_changelogs.semver_key)
        if gen_changelogs.semver_key(top) > gen_changelogs.semver_key(highest):
            seen_pending.setdefault(top, sha)

    lost = {v: sha for v, sha in seen_pending.items() if v not in current_sections}
    for version, sha in sorted(lost.items()):
        report.yellow(
            repo,
            f"预写段 {version} 曾经存在（提交 {sha[:8]}），但当前 CHANGELOG 里没有它",
            f"要么按 {version} 补发（release.py prewrite --repo {repo} --version {version}），"
            "要么明确决定放弃它——现在这个状态是**不可见的**："
            "CHANGELOG 覆盖门禁从提交版里找待发版段，段没了它就无对象可比，"
            "于是「这个改动既没有 tag 也没有发布说明」会一路全绿")

    # 中间态只记 note，不报黄
    if not lost:
        commits_after = gen_changelogs.commits_between(repo_dir, highest, "HEAD")
        substantive = [c for c in commits_after if c.get("subject")]
        if substantive and current_top and gen_changelogs.semver_key(current_top) <= gen_changelogs.semver_key(highest):
            report.note(f"{repo}: 最高 tag {highest} 之后有 {len(substantive)} 个提交，尚未预写待发版段"
                        "（发版前的正常中间态，不报）")


def judge(root: Path, repos: list[str], report: Report) -> None:
    for repo in repos:
        repo_dir = root / repo
        if not (repo_dir / ".git").exists():
            report.yellow(repo, "本地找不到该仓，跳过判定")
            continue
        judge_repo(repo_dir, report)


def discover(root: Path) -> list[str]:
    """自动发现矩阵根下的 git 仓（排除治理仓与工具仓）。"""
    skip = {".github", "matrix-tools", "oagentkit.github.io"}
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in skip or child.name.startswith("."):
            continue
        if (child / ".git").exists():
            out.append(child.name)
    return out


def selftest() -> int:
    """反证自测：造一个「预写段被吞掉」的历史，要求门禁把它报出来。"""
    import subprocess
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    def git(repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "demo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        (repo / "a.go").write_text("package a\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat: 初版")
        git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        # 预写 v0.2.0 段
        (repo / "CHANGELOG.md").write_text(
            "# demo CHANGELOG\n\n## [v0.2.0] — 2026-10-21\n\n- feat: 初版\n\n"
            "## [v0.1.0] — 2026-10-21\n\n- feat: 初版\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs(changelog): 预写 v0.2.0 段")
        # 被一次重生成吞掉：CHANGELOG 里去掉 v0.2.0 段
        (repo / "CHANGELOG.md").write_text(
            "# demo CHANGELOG\n\n## [v0.1.0] — 2026-10-21\n\n- feat: 初版\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs(changelog): 重生成")

        report = Report(title="待发版段可观测性")
        judge_repo(repo, report)
        yellows = [f.message for f in report.yellows]
        check("检出预写段被吞掉",
              any("曾经存在" in m for m in yellows), True)
        check("指出的是 v0.2.0", any("v0.2.0" in m for m in yellows), True)

        # 反证：把段加回去后必须转绿
        (repo / "CHANGELOG.md").write_text(
            "# demo CHANGELOG\n\n## [v0.2.0] — 2026-10-21\n\n- feat: 初版\n\n"
            "## [v0.1.0] — 2026-10-21\n\n- feat: 初版\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs(changelog): 补回 v0.2.0 段")
        report2 = Report(title="待发版段可观测性")
        judge_repo(repo, report2)
        check("补回后不再报", report2.has_yellow(), False)

    if fails:
        print("❌ verify_pending_release 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_pending_release 自测通过（预写段被吞掉可检出 + 补回后转绿）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="待发版段可观测性守卫")
    ap.add_argument("--root", default=".", help="矩阵根目录")
    ap.add_argument("--repos", nargs="*", help="仓名列表（默认自动发现）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    repos = args.repos or discover(root)
    report = Report(title="待发版段可观测性")
    judge(root, repos, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
