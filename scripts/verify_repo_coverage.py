#!/usr/bin/env python3
"""org 仓覆盖门禁：**已发版的 Go 仓必须被矩阵看见**；打了 tag 却没发 Release 必须报出。

## 为什么要有这条（实测 2026-11-22）

org 里新建了一个已发版的仓（github.com/oagentkit/toolname，annotated tag
v0.1.0 / v0.2.0），而矩阵的 modules.json 里**没有它** ⇒ 后果是
**全矩阵门禁全绿**，而矩阵对这条仓：

- llms.txt 不列它（消费方与 AI 选型看不到这个模块）；
- import 改写映射不出它（rewrite_imports 的映射表按 modules.json 生成）；
- 地板门禁、幽灵包门禁、依赖方向门禁对它**完全失明**（它们遍历的是矩阵登记的仓）；
- 而它已经在被真实消费（kernel v0.73.0 与宿主四仓已在依赖）。

⇒「CI 绿」不等于「矩阵完整」。这条门禁守的就是这个缺口。

## 判据（两条，各自独立）

1. **覆盖**：org 内某个仓同时满足
   ① 根目录有 go.mod（是 Go module）
   ② 已发版（主版本线 tag 或 GitHub Release 任一）
   ③ **不在**矩阵登记面（modules.json 的 modules ∪ infraRepos）
   ⇒ 判红。
   - 「已发版」看**两个面**：tag 与 Release。只打 tag 不发 Release 也算已发版
     （实测 toolname v0.1.0：tag 在、Release 缺 ⇒ 矩阵面与发布面都看不见它）。
   - 非 Go 仓（无根 go.mod）不在本判据面：矩阵登记的是**模块**，不是文档站。
   - 有意不登记的仓走 EXEMPTIONS 显式豁免（必须写理由）。

2. **发布面完整**：已登记的 Go 仓若有主版本线 tag 却**一个 Release 都没有** ⇒ 判红。
   - 宽限窗口（默认 24h）：tag 刚打、Release 还在补 ⇒ 只记说明不判红
     （发版过程中的一瞬状态，不该把无关的矩阵 PR 顶红）。

## 失败形态

- 取不到事实（org 仓列表、go.mod 状态、tag 列表）⇒ **判红**（判不了 ≠ 没有）。
  「确实没有」与「读不出来」由事实层分开给出（repo_meta.fetch_file_status /
  main_tags / list_releases），判定层自己决定怎么判。
- 一条仓都没查到 ⇒ 拒绝判定（避免「全绿但什么都没查」）。

## 用法

    verify_repo_coverage.py --org oagentkit
    verify_repo_coverage.py --selftest

退出码: 0 = 合规；1 = 有缺口 / 判不了。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"

# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# ⚠️ 这组参数必须**无副作用**、且能让脚本**真正走完入口**（不能只让 argparse 早退）。
SMOKE_ARGS = []

# 有意不登记矩阵的仓：repo → 理由。
# 只对「确实是 Go module 且已发版」的仓有意义（非 Go 仓 / 未发版仓不需要豁免）。
EXEMPTIONS: dict[str, str] = {}

# 「已打 tag 但还没发 Release」的宽限窗口（小时）。
# 发版流程是「打 tag → 建 Release」，中间有个几分钟的窗口；那个窗口里把无关的
# 矩阵 PR 顶红是噪音。超过窗口还没有 Release ⇒ 那是真的漏了。
DEFAULT_GRACE_HOURS = 24


@dataclass
class RepoFacts:
    """某个 org 仓的**事实**（不含判定）。"""
    name: str
    is_go_module: bool | None = None        # None = 判不了（状态取不到）
    main_tags: list[str] | None = None      # None = 判不了（tag 列表取不到）
    release_tags: list[str] | None = None   # None = 判不了（Release 列表取不到）
    draft_release_count: int = 0
    latest_tag: str | None = None
    latest_tag_at: datetime | None = None

    @property
    def published(self) -> bool:
        """是否已发版。**取不到事实时不算已发版**——但那种仓在判定里先被拦掉。"""
        return bool(self.main_tags) or bool(self.release_tags)


def collect_facts(org: str, repo: str) -> RepoFacts:
    """取某个仓的事实（联网）。取不到的显式标成 None（判不了）。"""
    f = RepoFacts(name=repo)

    text, status = repo_meta.fetch_file_status(org, repo, "go.mod")
    if status == "ok":
        f.is_go_module = True
    elif status == "not_found":
        f.is_go_module = False
    else:
        f.is_go_module = None

    tags = repo_meta.main_tags(org, repo)
    f.main_tags = tags
    if tags:
        f.latest_tag = max(tags, key=repo_meta.version_key)

    releases = repo_meta.list_releases(org, repo)
    if releases is None:
        f.release_tags = None
    else:
        f.release_tags = repo_meta.release_tags(releases)
        f.draft_release_count = sum(1 for r in releases if r.get("draft"))

    return f


def list_org_repos(org: str) -> list[str] | None:
    """org 下的仓名清单；取不到返回 None（判不了 ≠ 「org 里只有这些仓」）。"""
    raw = repo_meta.gh_api_paginated(f"orgs/{org}/repos")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [r.get("name", "") for r in data if r.get("name")]


def judge(facts: dict[str, RepoFacts], registered: set[str], *,
          now: datetime, grace_hours: int) -> tuple[list[str], list[str]]:
    """**纯判定**：同样的事实 + 同样的参数 ⇒ 同样的结论。

    → (阻断项, 说明项)。空阻断项 = 合规。
    """
    problems: list[str] = []
    notes: list[str] = []
    grace = timedelta(hours=grace_hours)

    for name, f in sorted(facts.items()):
        if name in EXEMPTIONS:
            notes.append(f"{name}: 已豁免——{EXEMPTIONS[name]}")
            continue

        # ---- 事实层的三态必须先落到判定上 ----
        if f.is_go_module is None:
            problems.append(
                f"{name}: go.mod 状态取不到（判不了，不能当成「不是 Go 仓」）")
            continue
        if f.main_tags is None or f.release_tags is None:
            problems.append(
                f"{name}: tag / Release 列表取不到（判不了，不能当成「没发过版」）")
            continue

        if not f.published:
            notes.append(f"{name}: 未发版（无主版本线 tag、无 Release）——矩阵不必登记")
            continue
        if not f.is_go_module:
            notes.append(
                f"{name}: 已发版但不是 Go module（根目录无 go.mod）——矩阵不必登记")
            continue

        # ---- 判据 1：覆盖 ----
        if name not in registered:
            problems.append(
                f"{name}: **已发版但未登记矩阵**"
                f"（主版本线 tag {f.latest_tag or '无'}、"
                f"Release {len(f.release_tags)} 个）——"
                f"矩阵看不见它：llms.txt 不列它、import 改写映射不出它、"
                f"地板/幽灵包/依赖方向门禁对它失明。"
                f"请登记进 modules.json 的 modules 或 infraRepos")
            continue

        # ---- 判据 2：发布面完整 ----
        if f.main_tags and not f.release_tags:
            if f.latest_tag_at is None:
                problems.append(
                    f"{name}: 有主版本线 tag 却一个 Release 都没有，"
                    f"且取不到 tag 时间（判不了）")
            elif now - f.latest_tag_at < grace:
                notes.append(
                    f"{name}: 有 tag 但还没发 Release"
                    f"（tag {f.latest_tag} 刚打，{grace_hours}h 宽限窗口内）")
            else:
                problems.append(
                    f"{name}: 有主版本线 tag（最新 {f.latest_tag}）却"
                    f"**一个 Release 都没有**——发布面缺一半："
                    f"消费方按 tag 能拉到代码，却看不到任何发布说明")
    return problems, notes


def registered_repos(matrix: dict) -> set[str]:
    """矩阵的**登记面**：modules ∪ infraRepos。"""
    names = {m["repo"] for m in matrix.get("modules", [])}
    names |= {r["repo"] for r in matrix.get("infraRepos", [])}
    return names


def main() -> int:
    ap = argparse.ArgumentParser(
        description="org 仓覆盖门禁：已发版的 Go 仓必须被矩阵登记；"
                    "打了 tag 却没发 Release 必须报出")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--org", help="要查的 org 名（必填；不默认取 defaultOrg——"
                          "默认联网会让入口冒烟守卫每次都撞 15s 超时）")
    ap.add_argument("--grace-hours", type=int, default=DEFAULT_GRACE_HOURS,
                    help="「已打 tag 未发 Release」的宽限窗口（小时）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    try:
        matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ modules.json 读不到：{e}")
        return 1

    org = args.org
    if not org:
        print("❌ 必须给 --org（要查哪个 org 的仓覆盖）")
        return 1

    names = list_org_repos(org)
    if names is None:
        print(f"❌ {org} 的仓列表取不到（判不了，不能当成「org 里只有这些仓」）")
        return 1
    if not names:
        print("❌ org 仓列表为空（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    registered = registered_repos(matrix)
    facts: dict[str, RepoFacts] = {}
    for name in sorted(names):
        facts[name] = collect_facts(org, name)

    now = datetime.now(timezone.utc)
    problems, notes = judge(facts, registered, now=now, grace_hours=args.grace_hours)

    if problems:
        print(f"❌ org 仓覆盖门禁失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：把漏登记的仓加进 modules.json 的 modules（含 tier / packages /"
              " heavyDeps / pkgDesc 等字段）或 infraRepos；"
              "确实不该进矩阵的仓写进本脚本的 EXEMPTIONS 并写明理由。")
        return 1

    go_mods = sum(1 for f in facts.values() if f.is_go_module)
    print(f"✅ org 仓覆盖合规（{org} 共 {len(facts)} 个仓，"
          f"其中 Go module {go_mods} 个；矩阵登记 {len(registered)} 个；"
          f"已发版未登记 0 个；已发版但无 Release 0 个）")
    for n in notes:
        print(f"  · {n}")
    return 0


# ---------------------------------------------------------------- 自测

def _f(name: str, *, go_mod: bool | None = True, tags: list[str] | None = None,
       releases: list[str] | None = None, latest: str | None = None,
       at: datetime | None = None) -> RepoFacts:
    f = RepoFacts(name=name, is_go_module=go_mod)
    f.main_tags = [] if tags is None else tags
    f.release_tags = [] if releases is None else releases
    f.latest_tag = latest
    f.latest_tag_at = at
    return f


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=30)
    fresh = now - timedelta(hours=1)

    # ---- 判据 1：覆盖 ----
    # 已发版（tag）却未登记 ⇒ 红
    p, _ = judge({"x": _f("x", tags=["v0.2.0"], latest="v0.2.0", at=old)},
                 {"y"}, now=now, grace_hours=24)
    check("已发版未登记 ⇒ 判红", len(p), 1)
    check("未登记的红指名道姓", "x" in p[0] and "未登记矩阵" in p[0], True)
    # 已发版（**只有 Release**，没有 tag）却未登记 ⇒ 同样红
    p, _ = judge({"x": _f("x", tags=[], releases=["v0.1.0"])}, {"y"},
                 now=now, grace_hours=24)
    check("只有 Release 也算已发版 ⇒ 判红", len(p), 1)
    # 已登记 ⇒ 绿
    p, _ = judge({"x": _f("x", tags=["v0.2.0"], latest="v0.2.0", at=old,
                          releases=["v0.2.0"])},
                 {"x"}, now=now, grace_hours=24)
    check("已登记 ⇒ 绿", p, [])
    # 未发版 ⇒ 绿（矩阵不必登记）
    p, n = judge({"x": _f("x")}, set(), now=now, grace_hours=24)
    check("未发版 ⇒ 绿", p, [])
    check("未发版有说明", len(n), 1)
    # 已发版但不是 Go module ⇒ 绿（矩阵登记的是模块，不是文档站）
    p, n = judge({"x": _f("x", go_mod=False, tags=["v1.0.0"], latest="v1.0.0")},
                 set(), now=now, grace_hours=24)
    check("非 Go 仓 ⇒ 绿", p, [])
    # go.mod 状态判不了 ⇒ 红（判不了 ≠ 不是 Go 仓）
    p, _ = judge({"x": _f("x", go_mod=None, tags=["v1.0.0"], latest="v1.0.0")},
                 {"x"}, now=now, grace_hours=24)
    check("go.mod 判不了 ⇒ 判红", len(p), 1)
    check("判不了的红写明理由", "判不了" in p[0], True)
    # tag 列表判不了 ⇒ 红
    f = _f("x", releases=["v1.0.0"])
    f.main_tags = None
    p, _ = judge({"x": f}, {"x"}, now=now, grace_hours=24)
    check("tag 列表判不了 ⇒ 判红", len(p), 1)

    # ---- 判据 2：发布面完整 ----
    # 有 tag 没有 Release，tag 很旧 ⇒ 红
    p, _ = judge({"x": _f("x", tags=["v0.1.0"], latest="v0.1.0", at=old)},
                 {"x"}, now=now, grace_hours=24)
    check("有 tag 无 Release（已久）⇒ 判红", len(p), 1)
    check("无 Release 的红指名道姓", "Release" in p[0], True)
    # 有 tag 没有 Release，tag 很新 ⇒ 只说明不判红（发版进行中）
    p, n = judge({"x": _f("x", tags=["v0.1.0"], latest="v0.1.0", at=fresh)},
                 {"x"}, now=now, grace_hours=24)
    check("有 tag 无 Release（刚打）⇒ 不判红", p, [])
    check("刚打的有说明", len(n), 1)
    # 有 tag 有 Release ⇒ 绿
    p, _ = judge({"x": _f("x", tags=["v0.1.0"], latest="v0.1.0", at=old,
                          releases=["v0.1.0"])},
                 {"x"}, now=now, grace_hours=24)
    check("有 tag 有 Release ⇒ 绿", p, [])

    # ---- 豁免 ----
    EXEMPTIONS["zz-exempt"] = "自测用"
    try:
        p, n = judge({"zz-exempt": _f("zz-exempt", tags=["v1.0.0"], latest="v1.0.0")},
                     set(), now=now, grace_hours=24)
        check("豁免仓 ⇒ 绿", p, [])
        check("豁免仓有说明", len(n), 1)
    finally:
        EXEMPTIONS.pop("zz-exempt", None)

    # ---- 登记面：modules ∪ infraRepos ----
    reg = registered_repos({"modules": [{"repo": "a"}], "infraRepos": [{"repo": "b"}]})
    check("登记面 = modules ∪ infraRepos", reg, {"a", "b"})

    if fails:
        print("❌ verify_repo_coverage 自测失败：")
        for f_ in fails:
            print(f"   - {f_}")
        return 1
    print("✅ verify_repo_coverage 自测通过（已发版未登记 ⇒ 红 / 未发版与非 Go 仓 ⇒ 绿 / "
          "判不了 ⇒ 红 / 已打 tag 未发 Release ⇒ 红（带宽限窗口））")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
