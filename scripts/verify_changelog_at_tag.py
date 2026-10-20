#!/usr/bin/env python3
"""tag ↔ CHANGELOG 版本节 对账门禁（issue #17 的根治门禁）。

## 守的是哪两件事

**T1｜当前 CHANGELOG ↔ 已发布 tag，双向一一对应**

- tag 有 → CHANGELOG 必须有该版本段（不许「发了版没写记录」）
- CHANGELOG 有 → tag 必须有（不许「写了记录没发版」）；
  唯一豁免是**待发版段**：版本号高于当前最高 tag、且是 CHANGELOG 里最高的一段
  （发版流程用 `gen_changelogs.py --next-version` 预写的那一段）

**T2｜每个已发布 tag 的快照里，CHANGELOG 必须含该 tag 自己的版本段**

这一条才是下游真正会咬到的那一层：下游 `go get` 拿到的是 **tag 快照**，
快照里的 CHANGELOG 缺自己那段 = 发布说明里没有这个版本自己的内容
（issue #17 的原话）。人读发布说明、工具从模块缓存读 CHANGELOG，读到的都是残缺那份。

## 为什么 T2 需要「存量欠账基线」

issue #17 报告 7 个模块全中；实测（2026-10-20）**13 个模块 + 门面共 23 个 tag** 中招。
这些 tag 已经在 GitHub 上发布，且 org 有 `protect-tags` ruleset 限制 tag 的更新与删除，
**不能重打**（重打会让同名 tag 对应新内容，下游 go.sum 的 h1 对不上，
直接硬失败「checksum mismatch」——agent 仓 2026-10-19 已经踩过一次）。

所以存量用基线文件 `changelog_tag_debt.json` 记账：

- 基线里的 tag → 报为**存量欠账**（打印出来，不静默）
- 基线外的 stale tag → **硬失败**（说明又发版发漏了）
- 基线里已经不 stale 的 → 提示更新基线（警告，不挡）

这就是「欠账可见、增量归零」：门禁不会因为存量而永远红，也不会因为存量而永远绿。

## 性能：tag 快照的持久缓存

T2 要读**每个已发布 tag 的 CHANGELOG.md 快照**。org 有 `protect-tags` ruleset
（tag 不可改不可删）⇒ **快照内容不可变** ⇒ `(repo, tag, 该 tag 的 commit sha)`
是稳定缓存键，命中即可跳过 API 调用。

实测（2026-10-20，14 仓 102 个 tag）：

| | API 调用 | 耗时 |
|---|---|---|
| 冷缓存 | 116 | 46s |
| 热缓存 | **14** | 26s |

「列 tag」也并发取（实测串行列 tag 单独就吃掉 32s）。

⚠️ 缓存**只收确定状态**（ok / not_found）。`error`（判不了）是瞬时状态，
缓存下来等于把一次网络抖动变成永久的静默通过——这是这类门禁最危险的失效形态。

## 根治：让 T2 从今天起可满足

`gen_changelogs.py` 只渲染**已存在 tag** 的版本段 → 「先提交 CHANGELOG 再打 tag」
的打法下，tag 快照里永远不可能有自己那段（鸡生蛋问题）。
`gen_changelogs.py --next-version vX.Y.Z` 可以把「上一个 tag 之后的提交」预写成
待发版段，于是发版顺序变成：

    生成 CHANGELOG（含 vX.Y.Z 段） → 提交 → 打 tag vX.Y.Z

tag 快照里就有自己那段了，T2 对新 tag 恒成立。

用法:
    verify_changelog_at_tag.py --root /path/to/oagentkit   # 本地 git 读 tag 快照
    verify_changelog_at_tag.py --org oagentkit             # 联网读 tag 快照
    verify_changelog_at_tag.py --selftest                  # 离线自测

退出码: 0 = 一致（存量欠账已记账）；1 = 有新问题。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_VERSIONS = HERE.parent / "versions.json"
DEFAULT_MATRIX = HERE.parent / "modules.json"
DEBT_FILE = HERE / "changelog_tag_debt.json"

# tag 快照的持久缓存目录（tag 在 protect-tags 下不可变 ⇒ 内容可缓存）。
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "oagentkit" / "changelog-at-tag"

VERSION_HEADING = re.compile(r"^##\s*\[?(?P<v>v\d+\.\d+\.\d+)\]?")
ROOT_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def version_key(v: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sections(text: str) -> set[str]:
    """CHANGELOG 里的版本段集合。"""
    out = set()
    for line in (text or "").splitlines():
        m = VERSION_HEADING.match(line)
        if m:
            out.add(m.group("v"))
    return out


# --------------------------------------------------------------------------
# 取数：本地 git / GitHub API
# --------------------------------------------------------------------------


def root_tags_local(root: str, repo: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(Path(root) / repo), "tag", "--list"],
                         text=True, capture_output=True)
    return sorted({t.strip() for t in out.stdout.splitlines() if ROOT_TAG.match(t.strip())},
                  key=version_key, reverse=True)


def root_tags_api(org: str, repo: str) -> dict[str, str]:
    """联网取该仓的根 tag → {tag 名: 该 tag 指向的 commit sha}。

    顺带取 sha 是为了给**持久缓存**当失效判据（见 TagSnapshotCache）：
    tag 名可能被改指到别的 commit，光看名字认不出「同一个 tag 的内容变了」。
    """
    raw = repo_meta.gh_api(f"repos/{org}/{repo}/tags?per_page=100")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for t in data:
        name = t.get("name", "")
        if not ROOT_TAG.match(name):
            continue
        sha = ((t.get("commit") or {}).get("sha")) or ""
        out[name] = sha
    return {k: out[k] for k in sorted(out, key=version_key, reverse=True)}


class TagSnapshotCache:
    """tag 快照的**持久缓存**（tag 不可变 ⇒ 内容不可变 ⇒ 可缓存）。

    ## 为什么能缓存

    org 有 `protect-tags` ruleset：tag 发布后不可改、不可删 ⇒ **某个 tag 处的
    CHANGELOG.md 内容是不可变的**。于是 `(repo, tag, 该 tag 的 commit sha)` 是
    一个**稳定键**：命中即可跳过 API 调用。

    ## 为什么不用「跳过基线内的欠账 tag」来省调用

    基线里的欠账 tag 也一样不可变、一样可以缓存——跳过它们只省掉「校验」，
    却丢掉了「基线条目已不再 stale ⇒ 提示更新基线」这个信号；
    而缓存省的是**调用本身**，一个信号都不丢。实测（2026-10-20，14 仓 102 个 tag）：
    当前约 **130 次**调用（14 列 tag + 14 取 main + 102 取 tag 快照），
    跳过基线只省 **26 次（20%）**，而热缓存只需 **28 次左右（省 78%）**。

    ## 一条硬规矩：error（判不了）绝不入缓存

    「判不了」是**瞬时状态**（网络抖动 / API 报错）。把它缓存下来等于把一次抖动
    变成永久的「既不豁免也不报」的静默通过——正是这类门禁最危险的失效形态。
    缓存只收**确定状态**（ok / not_found）。
    """

    def __init__(self, root: Path | None):
        self.root = Path(root) if root else None

    def _path(self, repo: str, tag: str) -> Path | None:
        if self.root is None:
            return None
        return self.root / repo / f"{tag}.json"

    def get(self, repo: str, tag: str, sha: str) -> tuple[str | None, str] | None:
        """命中返回 (正文, 状态)；未命中/失效返回 None。"""
        p = self._path(repo, tag)
        if p is None or not p.is_file():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if d.get("sha") != sha:
            return None          # tag 被改指到别的 commit → 缓存作废
        status = d.get("status")
        if status not in ("ok", "not_found"):
            return None          # 只认确定状态
        return (d.get("text"), status)

    def put(self, repo: str, tag: str, sha: str, text: str | None, status: str) -> None:
        """写缓存。error 状态不写；写失败不影响门禁判定。"""
        p = self._path(repo, tag)
        if p is None or status not in ("ok", "not_found"):
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"sha": sha, "status": status, "text": text},
                                    ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def changelog_local(root: str, repo: str, ref: str) -> tuple[str | None, str]:
    """ref 处的 CHANGELOG.md 原文；→ (正文, 状态)，状态 ∈ {ok, not_found, error}。

    not_found = 该 ref 的树里确实没有 CHANGELOG.md；error = 判不了（ref 不存在 / git 失败）。
    """
    repo_dir = str(Path(root) / repo)
    p = subprocess.run(["git", "-C", repo_dir, "show", f"{ref}:CHANGELOG.md"],
                       text=True, capture_output=True, timeout=60)
    if p.returncode == 0:
        return p.stdout, "ok"
    if "does not exist" in (p.stderr or "") or "exists on disk" in (p.stderr or ""):
        # ref 在，但那个路径下没有文件 —— 当时就没有 CHANGELOG.md
        return None, "not_found"
    return None, "error"


def changelog_api(org: str, repo: str, ref: str) -> tuple[str | None, str]:
    """→ (正文, 状态)，状态 ∈ {ok, not_found, error}。"""
    return repo_meta.fetch_file_status(org, repo, "CHANGELOG.md", ref=ref)


def has_unreleased_local(root: str, repo: str, highest_tag: str) -> bool:
    """最高 tag 之后是否还有提交（= 有待发版的改动）。"""
    if not highest_tag:
        return True
    out = subprocess.run(
        ["git", "-C", str(Path(root) / repo), "rev-list", f"{highest_tag}..HEAD", "--count"],
        text=True, capture_output=True)
    try:
        return int(out.stdout.strip() or "0") > 0
    except ValueError:
        return False


def has_unreleased_api(org: str, repo: str, highest_tag: str) -> bool:
    """main 的 HEAD 与最高 tag 是否同一个 commit。"""
    if not highest_tag:
        return True

    def sha(ref: str) -> str:
        raw = repo_meta.gh_api(f"repos/{org}/{repo}/commits/{ref}")
        if not raw:
            return ""
        try:
            return json.loads(raw).get("sha", "")
        except json.JSONDecodeError:
            return ""

    return sha("main") != sha(highest_tag)


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def check_current(tags: list[str], current: str | None, unreleased: bool) -> tuple[list[str], list[str], list[str]]:
    """T1：当前 CHANGELOG ↔ tag 双向。

    → (tag 有但缺版本段, 版本段有但没 tag, 待发版段)

    「版本段有而没 tag」只有一种合法情况：**待发版段**——版本号高于当前最高 tag、
    是 CHANGELOG 里最高的一段、且仓里确实还有没发版的提交（unreleased）。
    三个条件缺一即判为孤儿段（写了记录却没发版 / 写了永远不存在的版本）。
    """
    if current is None:
        return [], [], []
    secs = sections(current)
    tagset = set(tags)
    missing = [t for t in tags if t not in secs]
    orphan = []
    pending: list[str] = []
    if secs - tagset:
        highest_sec = max(secs - tagset, key=version_key)
        highest_tag = max(tags, key=version_key) if tags else None
        for v in sorted(secs - tagset, key=version_key):
            if (v == highest_sec and highest_tag and unreleased
                    and version_key(v) > version_key(highest_tag)):
                pending.append(v)  # 待发版段：允许
            else:
                orphan.append(v)
    return missing, orphan, pending


def check_tag_snapshot(repo: str, tags: list[str], debt: set[str],
                       fetch) -> tuple[list[str], list[str], list[str], list[str]]:
    """T2：每个 tag 的快照里是否含自己那段。

    → (新增 stale, 已在基线里的 stale, 无 CHANGELOG 可豁免的 tag, 判不了的 tag)

    ⚠️ 「取不到」与「当时确实没有」必须分开：
    - not_found = 该 ref 的树里确实没有 CHANGELOG.md（当时就没有，非缺陷）→ 豁免
    - error     = **判不了**（网络失败、解析失败、ref 不存在）→ 必须报出来

    把两者都当成「豁免」的话，一次网络抖动就会让这条门禁**静默少查若干个 tag**
    ——而门禁照样是绿的。这正是「一直绿、但从没生效过」的形态。

    为什么这里的 not_found 可以信任：contents API 对「文件不存在」和「仓不存在」
    都返回 404，但本函数只对**已经从 tag 列表里拿到的 ref** 调用——而 tag 列表
    非空本身就证明了仓存在（拿不到 tag 的仓在 main() 里已被单独报成问题）。
    """
    new_stale: list[str] = []
    known_stale: list[str] = []
    exempt: list[str] = []
    unknown: list[str] = []
    for t in tags:
        cl, status = fetch(t)
        if cl is None:
            if status == "not_found":
                exempt.append(t)   # 当时确实还没有 CHANGELOG.md —— 非缺陷
            else:
                unknown.append(t)   # 判不了：不能当成「没有」
            continue
        if t not in sections(cl):
            key = f"{repo}@{t}"
            (known_stale if key in debt else new_stale).append(key)
    return new_stale, known_stale, exempt, unknown


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="tag ↔ CHANGELOG 版本节 对账门禁")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--debt", default=str(DEBT_FILE))
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--org", help="联网读各仓 tag 与 CHANGELOG")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="tag 快照的持久缓存目录（tag 不可变 ⇒ 命中即可信）")
    ap.add_argument("--no-cache", action="store_true", help="不用持久缓存（每次都重新取）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root（本地各仓目录的父目录）或 --org（联网读）")
        return 1

    snap_cache = TagSnapshotCache(None if args.no_cache else Path(args.cache_dir))

    versions = load_json(Path(args.versions))
    org = args.org or load_json(Path(args.matrix)).get("defaultOrg")
    repos = [m["repo"] for m in versions.get("modules", [])]
    repos += [f["repo"] for f in versions.get("facades", [])]

    debt_data = load_json(Path(args.debt)) if Path(args.debt).exists() else {"tags": []}
    debt = set(debt_data.get("tags", []))

    problems: list[str] = []
    warnings: list[str] = []
    all_new_stale: list[str] = []
    all_known_stale: list[str] = []
    all_unknown: list[str] = []
    checked = 0

    # 联网模式：先把要取的 CHANGELOG **并发**取回来。
    # 串行实测 6m51s（14 仓 × 约 10 个 tag ≈ 140 次调用），而 matrix-truth job
    # 的预算是 10 分钟——只剩 2 分钟余量，一次网络抖动就整 job 超时。
    cache: dict[tuple[str, str], tuple[str | None, str]] = {}
    if not args.root:
        work: list[tuple[str, str]] = []
        tag_map: dict[str, list[str]] = {}
        sha_map: dict[str, dict[str, str]] = {}
        # 「列 tag」也是 API 调用，**串行**做的话 14 仓 × ~2.3s ≈ 32s 就把
        # 整轮的时间吃掉了（实测热缓存 36s，其中 32s 花在列 tag 上）。
        # 所以先并发列 tag，再并发取快照。
        with ThreadPoolExecutor(max_workers=8) as pool:
            tag_futures = {pool.submit(root_tags_api, org, r): r for r in repos}
            for fut in as_completed(tag_futures):
                r = tag_futures[fut]
                try:
                    tags = fut.result()
                except Exception:      # noqa: BLE001 —— 列不到 tag 一律当「判不了」
                    tags = {}
                tag_map[r] = list(tags)
                sha_map[r] = tags
        for repo in repos:
            tags = sha_map.get(repo, {})
            work.append((repo, "main"))
            for t in tags:
                # 持久缓存命中 ⇒ 不用发 API 调用（tag 快照不可变，命中即可信）
                hit = snap_cache.get(repo, t, tags[t])
                if hit is not None:
                    cache[(repo, t)] = hit
                    continue
                work.append((repo, t))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(changelog_api, org, r, t): (r, t) for r, t in work}
            for fut in as_completed(futures):
                r, t = futures[fut]
                try:
                    cache[(r, t)] = fut.result()
                except Exception:      # noqa: BLE001 —— 取数异常一律当「判不了」
                    cache[(r, t)] = (None, "error")
        # 持久缓存：把取到的 tag 快照落盘（tag 不可变 ⇒ 内容可缓存）。
        # 只写**确定状态**（ok / not_found）；error 绝不入缓存（见 TagSnapshotCache）。
        for (r, t), (text, st) in cache.items():
            if t == "main":
                continue        # main 每次都变，不缓存
            snap_cache.put(r, t, sha_map.get(r, {}).get(t, ""), text, st)
        print(f"ℹ️ tag 快照：缓存命中 {len(cache) - len(work)} / "
              f"{len(cache)}，本次实际发起 API 调用 {len(work)} 次"
              f"（缓存目录 {snap_cache.root}）")

    for repo in repos:
        if args.root:
            tags = root_tags_local(args.root, repo)
            current, _ = changelog_local(args.root, repo, "HEAD")
            fetch = lambda t, r=repo: changelog_local(args.root, r, t)  # noqa: E731
            unreleased = has_unreleased_local(args.root, repo, tags[0] if tags else "")
        else:
            tags = tag_map.get(repo, [])
            current, _ = cache.get((repo, "main"), (None, "error"))
            fetch = lambda t, r=repo: cache.get((r, t), (None, "error"))  # noqa: E731
            unreleased = has_unreleased_api(org, repo, tags[0] if tags else "")
        if not tags:
            problems.append(f"{repo}: 读不到任何 tag（判不了，不能当成「没有发布」）")
            continue
        checked += 1

        missing, orphan, pending = check_current(tags, current, unreleased)
        for t in missing:
            problems.append(f"{repo} {t}: 已发布 tag，但 CHANGELOG 里没有该版本段"
                            f"（tag 有 → CHANGELOG 有）")
        for v in orphan:
            problems.append(f"{repo} {v}: CHANGELOG 里有该版本段，但没有对应的 tag"
                            f"（CHANGELOG 有 → tag 有）")
        if pending:
            warnings.append(f"{repo}: 待发版段 {pending}（尚无 tag，属正常）")

        new_stale, known_stale, _exempt, unknown = check_tag_snapshot(repo, tags, debt, fetch)
        all_new_stale.extend(new_stale)
        all_known_stale.extend(known_stale)
        all_unknown.extend(f"{repo}@{t}" for t in unknown)

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    for k in all_new_stale:
        problems.append(f"{k}: 该 tag 的 CHANGELOG 快照里没有它自己的版本段"
                        f"（下游 go get 拿到的发布说明是残缺的）")
    for k in all_unknown:
        problems.append(f"{k}: 该 tag 的 CHANGELOG 取不到（判不了 ≠ 没有）"
                        f"——不能当成「当时没有 CHANGELOG.md」而豁免")
    fixed = sorted(debt - set(all_known_stale) - set(all_new_stale))
    for k in fixed:
        warnings.append(f"{k}: 欠账基线里已不再 stale —— 请更新 {Path(args.debt).name}")

    if all_known_stale:
        print(f"ℹ️  存量欠账 {len(all_known_stale)} 个 tag（已发布、tag 受 ruleset 保护不能重打）：")
        for k in sorted(all_known_stale):
            print(f"    - {k}")
        print("   根治办法：按 CHANGELOG-at-tag 的顺序补发补丁 tag"
              "（gen_changelogs.py --next-version → 提交 → 打 tag）。\n")

    if problems:
        print(f"❌ tag ↔ CHANGELOG 对账失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        return 1

    for w in warnings:
        print(f"  · {w}")
    print(f"✅ tag ↔ CHANGELOG 对账通过：{checked} 个仓，"
          f"当前 CHANGELOG 与已发布 tag 双向一致；tag 快照新增 stale 0 个")
    return 0


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # T1：tag 有但缺段 / 段有但没 tag / 待发版段
    tags = ["v1.0.0", "v0.9.0"]
    cur = "## [v1.0.0] — 2026-01-01\n\n## [v0.9.0] — 2026-01-01\n"
    missing, orphan, pending = check_current(tags, cur, True)
    check("齐全时无缺失", missing, [])
    check("齐全时无孤儿段", orphan, [])
    check("齐全时无待发版段", pending, [])

    missing, orphan, pending = check_current(["v1.0.0", "v0.9.0"],
                                             "## [v1.0.0] — 2026-01-01\n", True)
    check("tag 有而段缺", missing, ["v0.9.0"])
    check("缺段不算孤儿", orphan, [])

    # 没有未发版提交时，更高的版本段是孤儿段（写了记录却没发版）
    missing, orphan, pending = check_current(["v1.0.0"],
                                             "## [v1.0.0] — 2026-01-01\n\n## [v1.1.0] — 2026-01-02\n",
                                             False)
    check("无未发版提交时多出的段是孤儿", orphan, ["v1.1.0"])
    check("孤儿段不算 pending", pending, [])

    # 有未发版提交且该段最高 → 待发版段（允许）
    missing, orphan, pending = check_current(["v1.0.0"],
                                             "## [v1.0.0] — 2026-01-01\n\n## [v1.1.0] — 2026-01-02\n",
                                             True)
    check("最高段且高于最高 tag → 待发版段", pending, ["v1.1.0"])
    check("待发版段不算孤儿", orphan, [])

    # 有未发版提交，但多出的段不是最高段 → 仍是孤儿
    missing, orphan, pending = check_current(["v1.0.0"],
                                             "## [v1.2.0] — 2026-01-03\n\n## [v1.1.0] — 2026-01-02\n",
                                             True)
    check("只有最高段算待发版段", pending, ["v1.2.0"])
    check("非最高段仍是孤儿", orphan, ["v1.1.0"])

    # T2：快照里有没有自己那段
    def fetch(t: str) -> tuple[str | None, str]:
        return {
            "v1.0.0": ("## [v1.0.0] — 2026-01-01\n", "ok"),
            "v0.9.0": ("## [v0.8.0] — 2025-12-01\n", "ok"),   # 有 CHANGELOG 但缺自己那段
            "v0.8.0": (None, "not_found"),                       # 当时确实没有 CHANGELOG
            "v0.7.0": (None, "error"),                            # 判不了（网络失败等）
        }.get(t, (None, "error"))

    new_stale, known, exempt, unknown = check_tag_snapshot(
        "x", ["v1.0.0", "v0.9.0", "v0.8.0"], {"x@v0.9.0"}, fetch)
    check("基线内的 stale 记为欠账", known, ["x@v0.9.0"])
    check("基线外的 stale 报新增", new_stale, [])
    check("快照无 CHANGELOG 豁免", exempt, ["v0.8.0"])
    check("取不到不算豁免（判不了）", unknown, [])

    # 取数失败必须落到 unknown，不能落到 exempt——否则一次网络抖动就会让门禁
    # 静默少查若干 tag，而它照样是绿的。
    new_stale, known, exempt, unknown = check_tag_snapshot(
        "x", ["v1.0.0", "v0.7.0"], set(), fetch)
    check("取数失败不豁免", exempt, [])
    check("取数失败记为判不了", unknown, ["v0.7.0"])
    check("取数失败不当成 stale", new_stale, [])

    new_stale, known, exempt, unknown = check_tag_snapshot(
        "x", ["v1.0.0", "v0.9.0", "v0.8.0"], set(), fetch)
    check("基线外的 stale 报新增", new_stale, ["x@v0.9.0"])
    check("基线外的不记欠账", known, [])

    # ---- 持久缓存（tag 快照不可变 ⇒ (repo, tag, sha) 是稳定键）----
    with tempfile.TemporaryDirectory() as td:
        c = TagSnapshotCache(Path(td))
        check("未命中返回 None", c.get("x", "v1.0.0", "sha1"), None)
        c.put("x", "v1.0.0", "sha1", "## [v1.0.0]\n", "ok")
        hit = c.get("x", "v1.0.0", "sha1")
        check("命中返回缓存内容", hit, ("## [v1.0.0]\n", "ok"))
        # tag 被改指到别的 commit ⇒ 缓存作废（不能拿旧内容当新的）
        check("sha 变化则缓存失效", c.get("x", "v1.0.0", "sha2"), None)
        # error（判不了）绝不入缓存：一次网络抖动不该变成永久状态
        c.put("x", "v0.9.0", "sha9", None, "error")
        check("error 不入缓存", c.get("x", "v0.9.0", "sha9"), None)
        # not_found（当时确实没有 CHANGELOG）是确定状态，可以缓存
        c.put("x", "v0.8.0", "sha8", None, "not_found")
        check("not_found 可缓存", c.get("x", "v0.8.0", "sha8"), (None, "not_found"))
        # 禁用缓存时（root=None）一律不命中
        c2 = TagSnapshotCache(None)
        c2.put("x", "v1.0.0", "sha1", "text", "ok")
        check("禁用缓存时不命中", c2.get("x", "v1.0.0", "sha1"), None)

    if fails:
        print("❌ verify_changelog_at_tag 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_changelog_at_tag 自测通过"
          "（双向对账 / 待发版段豁免 / 欠账基线 / 快照无 CHANGELOG 豁免 / 取数失败不豁免 / "
          "持久缓存命中与失效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
