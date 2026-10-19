#!/usr/bin/env python3
"""发版时刻的依赖地板时效校验：打 tag 时，依赖地板是否已经是「当时」的最新。

## 为什么需要它（oagentkit/llm#10 的实测证据）

`llm v0.12.0`（2026-10-20 00:08:18）的 go.mod 钉着
`github.com/oagentkit/textx v0.5.0` 与 `github.com/oekit/pkg v0.15.1`，
而 `textx v0.6.0`（22:51:42）与 `oekit/pkg v0.16.0`（22:52:53）**在发版前 75 分钟
就已发布**。即：新版本在打 tag 前就在了，只是地板没跟。
`llm v0.12.1`（01:21:00）原样复制了同一组旧地板——同款动作连续两次。

这不是「当前落后于最新」，而是**发版那一刻就已经落后**：这类缺陷
事后周巡检能发现，但等发现时坏 tag 已经流出去了。

## 与 verify_dep_floors.py 的分工

| | verify_dep_floors.py | 本脚本 |
|---|---|---|
| 问的问题 | 现在（main）的地板 vs **当前**最新 tag | **某个 ref 的发版时刻**的地板 vs **那一刻**的最新 tag |
| 时间敏感性 | 无（只看当下） | 有（按 tag 的发布日期回看） |
| 典型用法 | 周巡检 / 报告 | 打 tag 前自检 / 复盘某个已发布 tag |

两者不冲突：前者回答「现在该抬什么」，本脚本回答「这个 tag 当时该不该抬却没抬」。
**顺序敏感的门禁会误伤**——本脚本的时间口径就是为了不误伤：
一个仓在 T 时刻没跟上某依赖，而该依赖是 T **之后**才发布的，不算它的错。

## 判据（独立，不复用生成器）

1. 取 ref（tag / commit）的**提交时刻 T**；
2. 取该 ref 处 go.mod 的 require 段，得到每个第一方依赖的**地板**；
3. 对每个依赖：列出它的所有根 tag，取**发布日期 ≤ T** 的那些里版本号最大的一个
   —— 即「T 时刻它本该接的版本」；
4. 地板 < 该版本 → **红**，并报出：哪个仓、哪个依赖、地板多少、当时最新多少、
   那个版本是什么时候发布的。

第一方 = `github.com/oagentkit/*` 与 `github.com/oekit/*`（与 verify_dep_floors.py 同口径：
oagentkit 的模块大量以 oekit/pkg、oekit/concurrency 为基座）。

## 怎么用

    # 发版前（还没打 tag）：拿将要打 tag 的那个 commit 自检
    verify_tag_floor.py --root /path/to/oagentkit --repo llm --ref HEAD

    # 复盘某个已发布 tag 当时的状态
    verify_tag_floor.py --root /path/to/oagentkit --repo llm --ref v0.12.0

    # 全矩阵扫（哪些仓的哪个 tag 是「当时就落后」的）
    verify_tag_floor.py --root /path/to/oagentkit --all-tags

    # 联网模式
    verify_tag_floor.py --org oagentkit --repo llm --ref v0.12.0

退出码: 0 = 无落后；1 = 有落后 / 判不了。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_ORGS = ("github.com/oagentkit/", "github.com/oekit/")
ROOT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
REQUIRE_RE = re.compile(r"^\s*([\w.\-]+(?:\.[\w\-]+)*/[\w.\-/]+)\s+(v[\w.\-+]+)")


def parse_version(v: str) -> tuple | None:
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", (v or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def version_lt(a: str, b: str) -> bool | None:
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return pa < pb


def owner_repo(module_path: str) -> tuple[str, str] | None:
    parts = module_path.split("/")
    if len(parts) >= 3 and ("." in parts[0] or ":" in parts[0]):
        return parts[1], parts[2]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def is_first_party(module_path: str) -> bool:
    return any(module_path.startswith(p) for p in DEFAULT_ORGS)


# ---------------------------------------------------------------- 取数


def local_tags_with_dates(root: str, owner: str, repo: str,
                          min_version: str | None = None) -> list[tuple[str, str]]:
    """→ [(tag, ISO 日期)]，日期取 tag 的 creatordate（annotated tag 的 tagger 时间）。

    只在本地克隆里才有这个精度；联网模式退回提交日期（见 docstring 的说明）。
    """
    d = Path(root) / repo
    if not (d / ".git").exists():
        return []
    out = subprocess.run(
        ["git", "-C", str(d), "for-each-ref", "--format=%(refname:short) %(creatordate:iso-strict)",
         "refs/tags/"],
        text=True, capture_output=True,
    )
    res = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        tag, date = parts[0], parts[1]
        if not ROOT_TAG_RE.match(tag):
            continue
        if min_version and not (version_lt(min_version, tag) or min_version == tag):
            continue
        res.append((tag, date))
    return res


def local_commit_date(root: str, repo: str, ref: str) -> str | None:
    d = Path(root) / repo
    out = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI", ref],
                         text=True, capture_output=True)
    return out.stdout.strip() or None


def local_go_mod(root: str, repo: str, ref: str) -> str | None:
    d = Path(root) / repo
    out = subprocess.run(["git", "-C", str(d), "show", f"{ref}:go.mod"],
                         text=True, capture_output=True)
    return out.stdout if out.returncode == 0 else None


def api_tags_with_dates(org: str, repo: str,
                        min_version: str | None = None) -> list[tuple[str, str]]:
    """联网模式：tags 列表 + 每个候选 tag 的提交日期。

    联网拿不到「tag 的创建时间」（annotated tag 的 tagger 时间不在 API 里），
    只能用 tag 指向的**提交日期**近似。口径差异记在这里，不藏。
    """
    raw = repo_meta.gh_api(f"repos/{org}/{repo}/tags?per_page=100")
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except json.JSONDecodeError:
        return []
    res = []
    for t in tags:
        name = t.get("name", "")
        if not ROOT_TAG_RE.match(name):
            continue
        if min_version and not (version_lt(min_version, name) or min_version == name):
            continue  # 低于或等于地板的版本不可能是「当时该接的」——省掉 API 调用
        sha = (t.get("commit") or {}).get("sha", "")
        date = ""
        if sha:
            c = repo_meta.gh_api(f"repos/{org}/{repo}/commits/{sha}")
            if c:
                try:
                    date = (json.loads(c).get("commit") or {}).get("committer", {}).get("date", "")
                except json.JSONDecodeError:
                    date = ""
        if date:
            res.append((name, date))
    return res


def api_commit_date(org: str, repo: str, ref: str) -> str | None:
    raw = repo_meta.gh_api(f"repos/{org}/{repo}/commits/{ref}")
    if not raw:
        return None
    try:
        return (json.loads(raw).get("commit") or {}).get("committer", {}).get("date")
    except json.JSONDecodeError:
        return None


def api_go_mod(org: str, repo: str, ref: str) -> str | None:
    raw = repo_meta.gh_api(f"repos/{org}/{repo}/contents/go.mod?ref={ref}")
    if not raw:
        return None
    try:
        import base64
        return base64.b64decode(json.loads(raw).get("content", "")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 判定


def floors_from_go_mod(text: str) -> dict[str, str]:
    """go.mod require 段 → {module path: 版本}（只收第一方）。"""
    out: dict[str, str] = {}
    in_block = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if s.startswith("require ") and not s.startswith("require ("):
            s = s[len("require "):]
        if not s or s.startswith("//"):
            continue
        if not (in_block or s.startswith("require ")):
            continue
        m = REQUIRE_RE.match(s)
        if not m:
            continue
        path, ver = m.group(1), m.group(2)
        if is_first_party(path):
            out[path] = ver
    return out


def latest_at(tags: list[tuple[str, str]], when: str) -> tuple[str, str] | None:
    """发布日期 ≤ when 的 tag 里版本号最大的一个 → (tag, 日期)。"""
    cands = [(t, d) for t, d in tags if d and d <= when]
    if not cands:
        return None
    best = None
    for t, d in cands:
        k = parse_version(t)
        if k is None:
            continue
        if best is None or k > best[0]:
            best = (k, t, d)
    return (best[1], best[2]) if best else None


def check_ref(repo: str, ref: str, when: str, floors: dict[str, str],
              tags_of) -> list[dict]:
    """→ [{dep, floor, should_be, released_at}]"""
    problems = []
    for dep, floor in sorted(floors.items()):
        parsed = owner_repo(dep)
        if parsed is None:
            continue
        owner, drepo = parsed
        tags = tags_of(owner, drepo, floor)
        if not tags:
            problems.append({"dep": dep, "floor": floor, "should_be": None,
                             "released_at": None, "note": "取不到该依赖的 tag（判不了）"})
            continue
        hit = latest_at(tags, when)
        if hit is None:
            continue
        should_be, released_at = hit
        if version_lt(floor, should_be):
            problems.append({"dep": dep, "floor": floor, "should_be": should_be,
                             "released_at": released_at, "note": ""})
    return problems


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="发版时刻的依赖地板时效校验")
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--deps-root", default="/Users/zhangyi/Projects/oekit",
                    help="依赖仓（oekit/*）的本地克隆父目录；取不到时回落联网取 tag")
    ap.add_argument("--org", help="联网模式：org 名")
    ap.add_argument("--repo", help="只查这一个仓")
    ap.add_argument("--ref", default="HEAD", help="要校验的 ref（tag / commit），默认 HEAD")
    ap.add_argument("--all-tags", action="store_true",
                    help="扫全矩阵的每个 tag：哪些 tag 是「当时就落后」的")
    ap.add_argument("--latest-only", action="store_true",
                    help="只判每个仓的**最新** tag（CI 用：历史 tag 的日期在 2026-10-19 "
                         "全量 tag 重写后已不可信，只有重写之后的 tag 才是有效证据）")
    ap.add_argument("--debt", default=str(HERE / "tag_floor_debt.json"),
                    help="已知欠账基线（repo@tag）；基线外的新增落后 → 硬失败")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root（本地各仓目录的父目录）或 --org（联网读）")
        return 1

    if args.root:
        def tags_of(owner: str, repo: str, min_version: str | None = None):
            # 先看本仓所在的克隆根，再看依赖仓的克隆根（如 oekit/*），
            # 都取不到才回落联网——联网慢，但不会因为「本地没克隆」而判不了。
            for base in (args.root, args.deps_root):
                if base and (Path(base) / repo / ".git").exists():
                    t = local_tags_with_dates(base, owner, repo, min_version)
                    if t:
                        return t
            return api_tags_with_dates(owner, repo, min_version)

        def commit_date(repo: str, ref: str):
            return local_commit_date(args.root, repo, ref)

        def go_mod(repo: str, ref: str):
            return local_go_mod(args.root, repo, ref)
    else:
        org = args.org

        def tags_of(owner: str, repo: str, min_version: str | None = None):
            return api_tags_with_dates(owner, repo, min_version)

        def commit_date(repo: str, ref: str):
            return api_commit_date(org, repo, ref)

        def go_mod(repo: str, ref: str):
            return api_go_mod(org, repo, ref)

    if args.all_tags:
        return scan_all_tags(args, tags_of, commit_date, go_mod)

    if args.latest_only:
        return scan_latest(args, tags_of, commit_date, go_mod)

    repos = [args.repo] if args.repo else list_repos(args)
    problems_all: list[dict] = []
    for repo in repos:
        when = commit_date(repo, args.ref)
        if not when:
            print(f"❌ {repo} {args.ref}: 取不到提交时刻（判不了）")
            return 1
        text = go_mod(repo, args.ref)
        if text is None:
            print(f"❌ {repo} {args.ref}: 取不到 go.mod（判不了）")
            return 1
        floors = floors_from_go_mod(text)
        for p in check_ref(repo, args.ref, when, floors, tags_of):
            p["repo"] = repo
            p["ref"] = args.ref
            problems_all.append(p)

    if not problems_all:
        print(f"✅ {args.repo or '全矩阵'} {args.ref}：依赖地板 == 发版时刻的最新，无落后")
        return 0

    print(f"❌ 依赖地板在发版时刻就落后：{len(problems_all)} 处\n")
    for p in problems_all:
        if p.get("note"):
            print(f"  - {p['repo']} {p['ref']} / {p['dep']}：{p['note']}")
        else:
            print(f"  - {p['repo']} {p['ref']} / {p['dep']}：地板 {p['floor']}，"
                  f"但发版时刻最新已是 {p['should_be']}（该版本发布于 {p['released_at']}）")
    print("\n修法：把 go.mod 的该依赖抬到上面的版本，go mod tidy 后重发 tag。"
          "（版本号从 git tag 推导，不要手写）")
    return 1


def list_repos(args) -> list[str]:
    if args.root:
        d = Path(args.root)
        return sorted(p.name for p in d.iterdir() if (p / ".git").exists() and p.name != ".github")
    raw = repo_meta.gh_api(f"orgs/{args.org}/repos?per_page=100")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return sorted(r["name"] for r in data if r.get("name"))


def scan_latest(args, tags_of, commit_date, go_mod) -> int:
    """每个仓只判它**最新**的那个 tag。

    为什么只判最新：2026-10-19 org 做了一次全量 tag/历史重写，那批 tag 的日期
    被改成同一时刻（00:45:00Z），用它们回看「发版时刻的最新」已经没有意义
    ——实测会凭空多出 100+ 条历史噪声。重写之后的 tag 日期才是有效证据。
    """
    repos = list_repos(args)
    if not repos:
        print("❌ 一个仓都没读到（拒绝判定）")
        return 1
    debt_path = Path(args.debt)
    debt = set()
    if debt_path.exists():
        try:
            debt = set(json.loads(debt_path.read_text(encoding="utf-8")).get("tags", []))
        except json.JSONDecodeError:
            debt = set()
    stale: list[dict] = []
    for repo in repos:
        tags = V_local_tags(args, repo)
        if not tags:
            continue
        latest = sorted(tags, key=lambda t: parse_version(t[0]) or (0, 0, 0))[-1][0]
        when = commit_date(repo, latest)
        text = go_mod(repo, latest)
        if not when or text is None:
            continue
        for p in check_ref(repo, latest, when, floors_from_go_mod(text), tags_of):
            p["repo"] = repo
            p["ref"] = latest
            stale.append(p)

    keys = {f"{p['repo']}@{p['ref']}" for p in stale}
    new = sorted(keys - debt)
    fixed = sorted(debt - keys)
    if stale:
        print(f"ℹ️  各仓最新 tag 的地板时效（{len(keys)} 个 tag 当时就落后）：")
        for p in stale:
            print(f"    - {p['repo']}@{p['ref']} / {p['dep']}：地板 {p['floor']}，"
                  f"发版时刻最新已是 {p['should_be']}（发布于 {p['released_at']}）")
        print()
    for k in fixed:
        print(f"  · 欠账基线里 {k} 已不再落后 —— 请更新 {debt_path.name}")
    if new:
        print(f"❌ 以下最新 tag 的依赖地板在发版时刻就落后（基线外的新增）：{len(new)} 处\n")
        for k in new:
            print(f"  - {k}")
        print("\n修法：抬 go.mod 地板 → go mod tidy → 重新发 tag（版本号从 git tag 推导）。")
        return 1
    print(f"✅ 各仓最新 tag 的依赖地板时效通过（基线内欠账 {len(keys & debt)} 个，"
          f"基线外新增 0 个）")
    return 0


def V_local_tags(args, repo) -> list[tuple[str, str]]:
    if args.root:
        return local_tags_with_dates(args.root, repo, repo)
    raw = repo_meta.gh_api(f"repos/{args.org}/{repo}/tags?per_page=100")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = []
    for t in data:
        name = t.get("name", "")
        if ROOT_TAG_RE.match(name):
            out.append((name, ""))
    return out


def scan_all_tags(args, tags_of, commit_date, go_mod) -> int:
    """扫全矩阵的每个 root tag：判它「打 tag 时」地板是否已落后。"""
    repos = list_repos(args)
    if not repos:
        print("❌ 一个仓都没读到（拒绝判定）")
        return 1
    rows: list[dict] = []
    for repo in repos:
        if not args.root:
            continue
        for tag, _d in sorted(local_tags_with_dates(args.root, repo, repo)
                              if (Path(args.root) / repo).exists() else []):
            when = commit_date(repo, tag)
            text = go_mod(repo, tag)
            if not when or text is None:
                continue
            floors = floors_from_go_mod(text)
            for p in check_ref(repo, tag, when, floors, tags_of):
                p["repo"] = repo
                p["ref"] = tag
                rows.append(p)

    if not rows:
        print("✅ 全矩阵逐 tag 扫描：没有「打 tag 时地板就已落后」的发布")
        return 0
    print(f"❌ 全矩阵逐 tag 扫描：{len(rows)} 处「打 tag 时地板就已落后」\n")
    for p in sorted(rows, key=lambda r: (r["repo"], r["ref"])):
        if p.get("note"):
            print(f"  - {p['repo']}@{p['ref']} / {p['dep']}：{p['note']}")
        else:
            print(f"  - {p['repo']}@{p['ref']} / {p['dep']}：地板 {p['floor']}，"
                  f"发版时刻最新已是 {p['should_be']}（发布于 {p['released_at']}）")
    return 1


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # go.mod 解析
    gomod = "module x\n\ngo 1.26.0\n\nrequire (\n\tgithub.com/oagentkit/textx v0.5.0\n" \
            "\tgithub.com/oekit/pkg v0.15.1\n\tgithub.com/cloudwego/eino v0.9.21\n)\n"
    check("只收第一方", floors_from_go_mod(gomod),
          {"github.com/oagentkit/textx": "v0.5.0", "github.com/oekit/pkg": "v0.15.1"})

    # 时间口径：T 之后才发布的 tag 不算当时就落后
    tags = [("v0.5.0", "2026-10-19T10:00:00+08:00"),
            ("v0.6.0", "2026-10-19T22:51:42+08:00"),
            ("v0.7.0", "2026-10-21T09:00:00+08:00")]
    hit = latest_at(tags, "2026-10-20T00:08:18+08:00")
    check("取 T 时刻之前的最新", hit[0], "v0.6.0")
    check("T 之后的 tag 不算", latest_at(tags, "2026-10-19T12:00:00+08:00")[0], "v0.5.0")

    # 核心判定：llm v0.12.0 那一条
    problems = check_ref("llm", "v0.12.0", "2026-10-20T00:08:18+08:00",
                         {"github.com/oagentkit/textx": "v0.5.0"},
                         lambda o, r, mv=None: [("v0.6.0", "2026-10-19T22:51:42+08:00")])
    check("地板落后被抓到", len(problems), 1)
    if problems:
        check("报出应接的版本", problems[0]["should_be"], "v0.6.0")
        check("报出当前地板", problems[0]["floor"], "v0.5.0")

    # 负向：地板已是当时最新 → 不报
    ok = check_ref("llm", "v0.12.1", "2026-10-20T01:21:00+08:00",
                   {"github.com/oagentkit/textx": "v0.6.0"},
                   lambda o, r, mv=None: [("v0.6.0", "2026-10-19T22:51:42+08:00")])
    check("地板已最新则不报", ok, [])

    # 负向：依赖在 T 之后才发布 → 不算这个仓的错
    later = check_ref("x", "v1.0.0", "2026-10-20T00:00:00+08:00",
                      {"github.com/oagentkit/textx": "v0.5.0"},
                      lambda o, r, mv=None: [("v0.6.0", "2026-10-21T09:00:00+08:00")])
    check("后发布的依赖不算落后", later, [])

    # 取不到 tag → 判不了，必须报出来（不许当成「没落后」）
    unknown = check_ref("x", "v1.0.0", "2026-10-20T00:00:00+08:00",
                        {"github.com/oagentkit/textx": "v0.5.0"},
                        lambda o, r, mv=None: [])
    check("取不到即报", len(unknown), 1)

    if fails:
        print("❌ verify_tag_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_tag_floor 自测通过"
          "（时间口径 / 落后判定 / 后发布不算账 / 取不到即报）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
