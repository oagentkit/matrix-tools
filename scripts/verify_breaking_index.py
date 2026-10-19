#!/usr/bin/env python3
"""breakingIndex 对账门禁：矩阵登记的破坏性变更 == 各仓 CHANGELOG 里实际有的。

## 背景（2026-10-19 实测）

`versions.json` 的 `breakingIndex` 曾整段是空的（13 个模块全为 `[]`），
而各仓 CHANGELOG 里**明确标着** BREAKING 变更。矩阵说「无破坏性变更」、
CHANGELOG 说「import 路径已变」——入口与事实不一致，下游只能**先构建断裂**、
再去翻 llms.txt 才知道怎么改。

## 这条门禁守什么

守的是「有没有」，**不理解语义**（成本极低）：

- CHANGELOG 里有、矩阵里没有 → **红**（矩阵漏收）
- 矩阵里有、CHANGELOG 里没有 → **红**（矩阵条目过期/手改过）
- 两边都在但内容不一致（摘要/明细） → **红**（矩阵没跟着 CHANGELOG 重生成）
- CHANGELOG **取不到** → **红**（判不了 ≠ 没有；静默放过是这条守卫最危险的失效形态）

## 为什么取不到也要红

「全绿但什么都没查」是所有守卫最常见的失效形态。这里若把「取不到」当成
「没有 BREAKING」，那么断网/限流/仓库改名都会让门禁变绿——而它恰恰是
「矩阵可能漏收破坏性变更」这件事唯一的检出点。取不到就红，配合 repo_meta
的重试，把瞬时失败挡在外面。

用法:
    verify_breaking_index.py --root /path/to/oagentkit   # 读本地各仓 CHANGELOG
    verify_breaking_index.py --org oagentkit             # 联网读各仓 CHANGELOG
    verify_breaking_index.py --selftest                  # 离线自测

退出码: 0 = 一致；1 = 漂移 / 判不了。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import breaking_index  # noqa: E402
import repo_meta  # noqa: E402

DEFAULT_VERSIONS = HERE.parent / "versions.json"
DEFAULT_MATRIX = HERE.parent / "modules.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_changelog(root: str | None, org: str | None, repo: str) -> tuple[str | None, str]:
    """→ (正文, 状态)。状态 ∈ {ok, missing, unreadable}。

    missing = 本地确实没有这个文件（仓没写 CHANGELOG，这是合法状态，
    此时矩阵的 breakingIndex 只能是空的）；unreadable = 想读但读不到
    （网络失败等）——这是「判不了」，必须红。
    """
    if root:
        p = Path(root) / repo / "CHANGELOG.md"
        if p.is_file():
            return p.read_text(encoding="utf-8"), "ok"
        return None, "missing"
    text = repo_meta.fetch_file(org, repo, "CHANGELOG.md")
    if text is None:
        return None, "unreadable"
    return text, "ok"


def check_one(repo: str, matrix_entries: list[dict], changelog_text: str | None,
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

    actual = breaking_index.extract(changelog_text)
    want = {(e["version"], e["title"]): e for e in matrix_entries}
    have = {(e["version"], e["title"]): e for e in actual}

    for key in sorted(have.keys() - want.keys()):
        problems.append(f"{repo} {key[0]}: CHANGELOG 标了破坏性变更，矩阵 breakingIndex 里没有"
                        f"（{key[1][:50]}）")
    for key in sorted(want.keys() - have.keys()):
        problems.append(f"{repo} {key[0]}: 矩阵 breakingIndex 登记了，CHANGELOG 里没有"
                        f"（{key[1][:50]}）——矩阵条目过期？")
    for key in sorted(want.keys() & have.keys()):
        if want[key] != have[key]:
            problems.append(f"{repo} {key[0]}: breakingIndex 内容与 CHANGELOG 不一致"
                            f"（{key[1][:40]}）——重跑 gen_versions.py")
    return problems


def collect_repos(versions: dict, matrix: dict) -> list[str]:
    """要核对哪些仓：矩阵模块 + 门面仓。"""
    repos = [m["repo"] for m in versions.get("modules", [])]
    repos += [f["repo"] for f in versions.get("facades", [])]
    return repos


def matrix_entries_for(versions: dict, repo: str) -> list[dict]:
    for m in versions.get("modules", []):
        if m["repo"] == repo:
            return m.get("breakingIndex", [])
    for f in versions.get("facades", []):
        if f["repo"] == repo:
            return f.get("breakingIndex", [])
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="breakingIndex 与各仓 CHANGELOG 的对账门禁")
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
    for repo in collect_repos(versions, load_json(Path(args.matrix))):
        text, status = read_changelog(args.root, org, repo)
        problems.extend(check_one(repo, matrix_entries_for(versions, repo), text, status))
        checked += 1

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    if problems:
        print(f"❌ breakingIndex 对账失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：跑 gen_versions.py --org {org} --root <各仓父目录> 重新生成 versions.json"
              "（判定规则见 scripts/breaking_index.py）")
        return 1

    n = sum(len(matrix_entries_for(versions, r))
            for r in collect_repos(versions, load_json(Path(args.matrix))))
    print(f"✅ breakingIndex 与 {checked} 个仓的 CHANGELOG 一致（共 {n} 条破坏性变更登记在册）")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # 一致：空矩阵 + 无 CHANGELOG → 无问题
    check("两边都空", check_one("x", [], None, "missing"), [])
    # 矩阵有、CHANGELOG 没有 → 过期
    stale = check_one("x", [{"version": "v1.0.0", "title": "t", "summary": "", "detail": ""}],
                      "# c\n", "ok")
    check("过期条目被抓到", len(stale), 1)
    # CHANGELOG 有、矩阵没有 → 漏收
    cl = "## [v1.0.0] — 2026-01-01\n\n### 重构(x)!：变了\n\nBREAKING CHANGE: 真的变了。\n"
    missing = check_one("x", [], cl, "ok")
    check("漏收被抓到", len(missing), 1)
    # 一致：两边都有
    entries = breaking_index.extract(cl)
    check("一致无问题", check_one("x", entries, cl, "ok"), [])
    # 内容不一致（摘要被手改）→ 抓
    tampered = [dict(entries[0], summary="手改的摘要")]
    check("内容漂移被抓到", len(check_one("x", tampered, cl, "ok")), 1)
    # 取不到 → 判不了 → 必须红（不许静默放过）
    check("取不到即红", len(check_one("x", [], None, "unreadable")), 1)

    if fails:
        print("❌ verify_breaking_index 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_breaking_index 自测通过（漏收 / 过期 / 内容漂移 / 判不了即红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
