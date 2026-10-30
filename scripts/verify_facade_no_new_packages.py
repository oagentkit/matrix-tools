#!/usr/bin/env python3
"""门面的「不得新增包」门禁——**规则级**判定，不靠事后快照。

## 为什么不用冻结基线就完事

`verify_facade_frozen.py` 用的是**冻结基线快照**（`facade_package_baseline.json`，
2026-10-20 人工取样）。它能拦「基线之后又长出新包」，
但对**基线取样之前**发生的违规完全免疫。

实测就踩在这个缝里：

- 门面 README 明写「本仓不接受任何新功能，只做别名转发与安全修复」，
  「v1.x 退役条件**已满足**」；
- 而 `agentkit/port/alias.go` 首行自述「本文件是**新包**的别名转发表」，
  由 `ddb4149 feat(facade): 转发 llm/port 的 51 个导出符号` 引入
  ——**在「已废弃」承诺之后**；
- 冻结基线取样时（2026-10-20 / v1.8.21）已经把它收了进去
  ⇒ 冻结门禁对这个违规**免疫**，一直绿。

## 本脚本的判据（规则级）

1. 找出门面 README **首次声明废弃**的那个提交 —— 那是规则的生效时点。
2. 列出该时点**之后**新增的包目录（含 alias.go 的新增）。
3. 与冻结基线交叉：
   - 新增且**不在**基线 ⇒ **红**（基线之后又长了新包）
   - 新增且**在**基线 ⇒ **黄**（基线之前就违规、已被快照吸收 —— 需要显式决策）
4. 找不到废弃声明 ⇒ **黄**（判不了，规则不适用）—— 不是「没问题」。
   ⚠️ 实测 2026-10-31：这条原先只报 info ⇒ 把门面的措辞从「不接受任何新功能」
   改成同义表述，整条**规则级**判定就静默失效而 CI 照样全绿 ⇒
   兜底冻结快照盲区的那条网自己消失了。判黄 + CI 的 --fail-on-yellow 才拦得住。

## 为什么这是规则级而不是快照级

判据取自**门面自己的承诺**（README 的废弃声明）+ **git 历史**（新增包的提交），
不依赖任何人工快照。快照只用来回答「这个违规是否已被吸收」，
不作为「什么算违规」的判据。

## 通用性

废弃标记词可配（`--marker`），默认中英各一组；组织名与仓名从参数来。

用法:
    verify_facade_no_new_packages.py --facade <门面仓路径> [--baseline <基线.json>]
    verify_facade_no_new_packages.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import matrix_facts as facts_layer  # noqa: E402
from judge_report import Report, emit  # noqa: E402

# 废弃声明的标记词。中英各一组——姊妹矩阵的 README 可能用英文。
DEFAULT_MARKERS = ["已废弃", "不接受任何新功能", "deprecated", "no new features"]


def _read_at(repo_dir: Path, sha: str, path: str) -> str | None:
    return facts_layer.git_show_file(repo_dir, sha, path)


def deprecation_commit(repo_dir: Path, markers: list[str],
                       readme: str = "README.md") -> tuple[str | None, str]:
    """门面首次声明废弃的提交 → (sha, 命中的标记词)；找不到返回 (None, "")。

    取**最早**的一个命中提交 —— 规则从声明那一刻起生效。
    """
    commits = _log_shas(repo_dir, readme)
    for sha in commits:
        text = _read_at(repo_dir, sha, readme)
        if not text:
            continue
        for marker in markers:
            if marker in text:
                return sha, marker
    return None, ""


def _log_shas(repo_dir: Path, path: str) -> list[str]:
    """某文件的提交历史（按时间正序）→ [sha, ...]。

    直接用 `git log --format=%H` 而不是事实层的 `git_log`——
    后者自带一个四段式 --format，传第二个 --format 会被 git 覆盖（后写的赢），
    解析就会失配。
    """
    raw = facts_layer.git("log", "--reverse", "--format=%H", "--", path, cwd=repo_dir)
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def added_packages_after(repo_dir: Path, since_sha: str) -> dict[str, str]:
    """since_sha 之后新增的包目录 → {包名: 新增它的提交 sha}。

    判据用 `git log --diff-filter=A`（新增文件）——「新增一个包」在 git 里的
    形态就是「新增了 `<pkg>/alias.go`」。改名（--diff-filter=R）也算新增形态，
    但门面改名等于「删一个加一个」，这里只抓 A（新增），R 由别的守卫兜。
    """
    out: dict[str, str] = {}
    raw = facts_layer.git("log", "--diff-filter=A", "--format=%H",
                          f"{since_sha}..HEAD", "--", "*/alias.go", cwd=repo_dir)
    for line in (raw or "").splitlines():
        sha = line.strip()
        if not sha:
            continue
        names = facts_layer.git("show", "--name-only", "--format=", sha, cwd=repo_dir)
        for path in (names or "").splitlines():
            path = path.strip()
            if not path.endswith("/alias.go"):
                continue
            pkg = path.split("/")[0]
            out.setdefault(pkg, sha)
    return out


def baseline_packages(baseline_path: Path | None) -> tuple[set[str], set[str]]:
    """冻结基线 → (包集合, 已知例外集合)。

    ## 两者的区别是本脚本的核心

    - `packages`：**合规面**——这些包在门面废弃**之前**就存在，
      转发它们是「保住存量下游的兼容面」，是门面的本职工作。
    - `knownExceptions`：**显式登记的例外**——废弃之后才加进来的包，
      但因存量下游可能已在用，必须继续转发。
      登记在案 = 「已知并接受」；没登记 = 「被快照悄悄吸收的违规」。

    把两者分开的意义：基线不再把「违规」和「合规」混在一起。
    实测基线吸收了 3 个废弃后新增的包（`port` / `injectband` / `invoke`），
    而冻结门禁对此**完全免疫**——它只知道「包在集合里」。
    """
    if baseline_path is None or not baseline_path.is_file():
        return set(), set()

    def names_of(items) -> set[str]:
        out = set()
        for item in items or []:
            if isinstance(item, dict):
                name = item.get("package") or item.get("name") or item.get("pkg")
                if name:
                    out.add(name)
            elif isinstance(item, str):
                out.add(item)
        return out

    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    return names_of(data.get("packages")), names_of(data.get("knownExceptions"))


def judge(facade_dir: Path, baseline: set[str], known: set[str], markers: list[str],
          report: Report) -> None:
    repo = facade_dir.name
    sha, marker = deprecation_commit(facade_dir, markers)
    if sha is None:
        # 判不了 ≠ 没问题（硬规则 3）。
        #
        # 为什么不能只报 info（实测 2026-10-31）：本守卫的**触发前提**是门面 README 里
        # 那句「已废弃 / 不接受任何新功能」——判据是**规则级**的（取自门面自己的承诺
        # + git 历史），正因如此它能兜住冻结快照门禁的盲区。
        #
        # 而一旦找不到那句声明，整条规则级判定就**整体退化为「不适用」**——
        # 把措辞从「不接受任何新功能」改成「不再维护」「进入冻结」这类同义表述即可，
        # 而 CI **照样全绿** ⇒ 兜底盲区的那条网自己消失了。
        #
        # 所以这里判**黄**（判不了）：读不出承诺 ⇒ 无法判定门面是否违约。
        # CI 用 --fail-on-yellow 把它升级为失败 ⇒ 措辞一改就有人被叫住。
        report.yellow(
            repo,
            "门面 README 里找不到废弃声明 ⇒ **规则级判定不适用**，"
            "本守卫无法判断门面是否违约",
            f"确认 README 是否写明「已废弃 / 不接受任何新功能」（标记词：{markers}）。"
            "⚠️ 这不是「门面没违规」，是「读不出门面的承诺 ⇒ 判不了」——"
            "把措辞改成同义表述就会让整条规则级门禁静默失效，"
            "而冻结快照门禁（verify_facade_frozen）只抓「长出新包」，抓不到这一类")
        return
    report.info(repo, f"废弃声明始于提交 {sha[:8]}（命中「{marker}」）")

    added = added_packages_after(facade_dir, sha)
    if not added:
        report.info(repo, "废弃声明之后没有新增过包")
        return
    for pkg, add_sha in sorted(added.items()):
        if pkg in known:
            report.info(repo, f"包 {pkg} 是废弃后新增的，但**已显式登记为已知例外**"
                              f"（提交 {add_sha[:8]}）")
        elif pkg in baseline:
            report.yellow(
                repo,
                f"包 {pkg} 是在废弃声明**之后**新增的（提交 {add_sha[:8]}），"
                "但只出现在冻结基线里 ⇒ 冻结门禁对它免疫",
                f"要么把 {pkg} 从门面上撤掉（它是废弃门面，不该再有新包），"
                "要么把它登记进基线的 knownExceptions ——"
                "让「已知例外」与「合规」在数据上分开。"
                "现在基线把违规和合规混在一起，冻结门禁因此看不见这次违规")
        else:
            report.red(
                repo,
                f"包 {pkg} 是在废弃声明之后新增的（提交 {add_sha[:8]}），且不在冻结基线里",
                f"撤掉 {pkg}（门面不接受任何新功能）；"
                "如果确实需要它，先改门面的废弃承诺，再谈基线")


def selftest() -> int:
    """反证自测：废弃后新增的包必须被抓出来（含「已被基线吸收」的黄灯形态）。"""
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
        repo = root / "facade"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        (repo / "README.md").write_text("# facade\n\n正常门面\n", encoding="utf-8")
        (repo / "old").mkdir()
        (repo / "old" / "alias.go").write_text(
            'package old\n\nimport upstream "github.com/acme/llm/old"\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat: 初始门面")
        # 声明废弃
        (repo / "README.md").write_text("# facade\n\n**已废弃**，本仓不接受任何新功能。\n",
                                        encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs: 声明废弃")
        # 废弃之后新增一个包
        (repo / "newpkg").mkdir()
        (repo / "newpkg" / "alias.go").write_text(
            'package newpkg\n\nimport upstream "github.com/acme/llm/newpkg"\n', encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat(facade): 新增包")

        report = Report(title="门面不得新增包")
        judge(repo, baseline=set(), known=set(), markers=DEFAULT_MARKERS, report=report)
        check("废弃后新增的包判红", report.has_red(), True)
        check("指出包名",
              any("newpkg" in f.message for f in report.reds), True)
        check("废弃前的老包不报", any("old" in f.message for f in report.reds), False)

        # 已被基线吸收 ⇒ 黄灯而不是红灯
        report2 = Report(title="门面不得新增包")
        judge(repo, baseline={"newpkg", "old"}, known=set(), markers=DEFAULT_MARKERS, report=report2)
        check("基线吸收后转黄", report2.has_red(), False)
        check("基线吸收后仍报黄", report2.has_yellow(), True)

        # ── 找不到废弃声明 ⇒ 判黄（判不了），不是「没问题」（实测 2026-10-31）──
        #
        # 形态：判据的**触发前提**是门面 README 里那句「不接受任何新功能」。
        # 把措辞改成同义表述（「不再维护」等）⇒ 整条规则级判定退化为「不适用」
        # ⇒ 而旧实现只报 info ⇒ CI 全绿 ⇒ 兜底冻结快照盲区的那条网自己消失了。
        #
        # 反证样本：造一个**没有**废弃声明的仓 ⇒ 必须判黄（且不得判红——
        # 「读不出承诺」不等于「门面违约」）。
        repo_no_marker = root / "facade_nomarker"
        repo_no_marker.mkdir()
        git(repo_no_marker, "init", "-q", "-b", "main")
        git(repo_no_marker, "config", "user.email", "t@example.com")
        git(repo_no_marker, "config", "user.name", "t")
        (repo_no_marker / "README.md").write_text(
            "# facade\n\n本仓不再维护，仅保留兼容转发。\n", encoding="utf-8")
        (repo_no_marker / "old").mkdir()
        (repo_no_marker / "old" / "alias.go").write_text(
            'package old\n\nimport upstream "github.com/acme/llm/old"\n', encoding="utf-8")
        git(repo_no_marker, "add", "-A")
        git(repo_no_marker, "commit", "-q", "-m", "feat: 初始门面")

        report3 = Report(title="门面不得新增包")
        judge(repo_no_marker, baseline=set(), known=set(), markers=DEFAULT_MARKERS, report=report3)
        check("找不到废弃声明时判黄（判不了 ≠ 没问题）", report3.has_yellow(), True)
        check("找不到废弃声明时不判红（读不出承诺 ≠ 违约）", report3.has_red(), False)
        check("黄灯说明了规则级判定不适用",
              any("规则级判定不适用" in f.message for f in report3.yellows), True)

    if fails:
        print("❌ verify_facade_no_new_packages 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_facade_no_new_packages 自测通过"
          "（废弃后新增包判红 / 基线吸收判黄 / 找不到废弃声明判黄）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="门面的「不得新增包」规则级门禁")
    ap.add_argument("--facade", help="门面仓本地路径")
    ap.add_argument("--baseline", default=str(HERE / "facade_package_baseline.json"),
                    help="冻结基线 JSON（用于判断违规是否已被吸收）")
    ap.add_argument("--marker", action="append", help="废弃声明标记词（可多次）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.facade:
        print("❌ 需要 --facade <门面仓路径>")
        return 2
    facade_dir = Path(args.facade)
    if not (facade_dir / ".git").exists():
        print(f"❌ 找不到门面仓：{facade_dir}")
        return 2

    markers = args.marker or DEFAULT_MARKERS
    baseline, known = baseline_packages(Path(args.baseline) if args.baseline else None)
    report = Report(title="门面不得新增包")
    judge(facade_dir, baseline, known, markers, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
