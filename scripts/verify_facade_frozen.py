#!/usr/bin/env python3
"""门面包集合冻结守卫：已废弃的门面仓**不得再长出新包**。

## 守的是哪条缺口

门面仓 `oagentkit/agentkit` 是原单仓的**薄门面**——48 个包全是 re-export，零逻辑。
它的退役计划在 `SPLIT-PLAN.md` §8-4 就裁定（「README 写死退役时间表：v1.x 后转 archive」），
而它的 README 状态行写着「**v1.x 退役条件已满足**」——**退役条件已满足**，
门面 README 首行「门面仓 · 已废弃」，正文「本仓已废弃：只保留兼容转发，不是开发目标，新代码不要引它」。

但「废弃」此前只有**一半**是机器约束：

- ✅ 「不许再引门面」有机器约束 —— `check_facade_imports.py`（扫各仓是否仍 import 门面/旧单仓）；
- ❌ 「门面不得继续长出新包」**没有任何机器约束** —— 只有 48 个 `alias.go` 的 diff 可见性。
  有人往门面加一个新包目录 + 一批别名，CI 全绿。

废弃的语义是「只保留既有兼容面、不接受新功能」，而「新增包」正是门面继续长胖的主要形态。
本守卫把这条从文档承诺变成 CI 里的一条红灯。

## 判据（单向）

门面的**实际包集合 ⊆ 冻结基线**（`scripts/facade_package_baseline.json`）。

- **⊆ 而不是 ==**：「不得删除」不归这条守卫管。上游模块真删包时，门面跟着删是对的
  （否则 alias.go 指向已不存在的包，编译不过）。基线里有、门面上没有 = 正常，不报。
- **「新增」怎么判定**（两条都报红）：
  1. 门面仓根下一层**含 .go 文件的目录**就是门面包。目录名不在基线里 ⇒ 新增包。
     （**没有 alias.go 的 .go 目录也算包**：那是门面自研包，比多转发一个上游更严重。）
  2. 目录名在基线里，但 `alias.go` 的转发目标（`import upstream "..."`）与基线记的不一致
     ⇒ **多出来一个转发目标**（改指新上游 = 门面又长出一张新脸）。
- 基线本身是**快照**，不从 modules.json 推导——矩阵一新增包，从它推导的基线就跟着变大，
  冻结当场失效（守卫会对着一个自己会变大的上限比对，永远绿）。

## 取不到门面仓时怎么判：判不了 ≠ 没问题，但「不存在」≠「判不了」

| 门面仓状态 | 含义 | 处理 |
|---|---|---|
| `ok` | 正常 | 照常判定 |
| `archived` | GitHub 已归档（禁止 push） | 照常判定 + 提示（归档**之前**也可能已经长歪了）；且物理上已无法再新增 |
| `absent` | 仓不存在（404，且 org 仓列表里也没有它） | **判绿**——门面不在 ⇒「门面继续生长」物理上不可能发生 |
| `error` | 网络失败 / 取不到快照 / 状态判不了 | **判红**——判不了不能当成「没问题」 |

`absent` 与 `error` 必须分开：`error` 是**判不了**（红），`absent` 是**判得了、结论是
「没有新增可言」**（绿）。混为一谈要么制造永久假红（门面真删了还一直红），
要么制造假绿（网络一抖就当「门面没了」）。

## 怎么自证有效性

`--selftest` 离线跑**真实逻辑**（造临时门面仓 + 真调用 `facade_packages()` / `check()`，
不 mock 判定函数），覆盖：与基线一致⇒绿 / 新增包⇒红 / 删包⇒绿 / 新增转发目标⇒红 /
数量相同但集合不同⇒红（负向样本）/ 门面自研包⇒红 / 空门面⇒判不了。

## 输出约定

守卫正常运行：**信息性输出一律走 stderr，stdout 只放机器可读的结论行**
（`facade-frozen: ok facade=ok packages=48 baseline=48 new_packages=0 new_targets=0`，
`--json` 时 stdout 是 JSON 对象）。
自测的报告按本仓惯例走 stdout（与其它 4 条门面守卫一致）。

用法:
    verify_facade_frozen.py --facade <门面仓路径>      # 本地模式
    verify_facade_frozen.py --org oagentkit             # 联网模式（读门面仓 main 的快照）
    verify_facade_frozen.py --baseline <基线文件>       # 换基线（默认 scripts/facade_package_baseline.json）
    verify_facade_frozen.py --json                      # 机器可读输出
    verify_facade_frozen.py --selftest                  # 离线自测

退出码: 0 = 门面没有长出新包；1 = 长了新包 / 判不了；2 = 用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402  取数层（gh api / 源码快照）的唯一实现，别处别再写一份

DEFAULT_BASELINE = HERE / "facade_package_baseline.json"
DEFAULT_FACADE_REPO = "agentkit"

# 门面 alias.go 的转发目标写法：import upstream "github.com/oagentkit/xxx/yyy"
_ALIAS_IMPORT_RE = re.compile(r'import\s+upstream\s+"(?P<path>[^"]+)"')


# ---------------------------------------------------------------- 取门面

def facade_packages(facade_path: Path) -> dict[str, str]:
    """门面仓 → {包名: upstream import 路径}。

    「包」= 仓根下一层**含 .go 文件的目录**。

    为什么不是「含 alias.go 的目录」：门面自研一个**没有转发目标**的 .go 目录，
    同样是门面在长胖（而且比多转发一个上游更严重——门面本该零逻辑）。
    取不到转发目标记 ""，由判定层按「自研包」报红。
    """
    out: dict[str, str] = {}
    for child in sorted(facade_path.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not any(child.glob("*.go")):
            continue
        upstream = ""
        alias = child / "alias.go"
        if alias.is_file():
            m = _ALIAS_IMPORT_RE.search(
                alias.read_text(encoding="utf-8", errors="ignore"))
            if m:
                upstream = m.group("path")
        out[child.name] = upstream
    return out


def load_baseline(path: Path) -> dict[str, str]:
    """读冻结基线 → {包名: upstream 路径}；格式不对抛 ValueError。

    基线自身的自洽也要查：条目缺字段 / 包名重复 / packageCount 与实际条数不符 ⇒
    基线坏了，不能拿来当判据（否则「基线是空的」会被读成「门面长出了 48 个包」）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("packages")
    if not isinstance(entries, list) or not entries:
        raise ValueError("packages 必须是非空数组")
    declared = data.get("packageCount")
    if isinstance(declared, int) and declared != len(entries):
        raise ValueError(
            f"packageCount={declared} 与 packages 实际条数 {len(entries)} 不一致")
    out: dict[str, str] = {}
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError(f"条目必须是对象: {e!r}")
        pkg = e.get("package")
        if not pkg or not isinstance(pkg, str):
            raise ValueError(f"条目缺少 package 字段: {e!r}")
        if not e.get("module"):
            raise ValueError(f"条目缺少 module 字段: {e!r}")
        if pkg in out:
            raise ValueError(f"基线里有重复的包名: {pkg}")
        out[pkg] = e.get("upstream", "")
    return out


# ---------------------------------------------------------------- 判定（纯函数）

def check(baseline: dict[str, str], actual: dict[str, str]) -> list[dict]:
    """门面的实际包集合必须 ⊆ 冻结基线。返回问题列表（空 = 没长新包）。

    纯函数：判据与取数分离——取数层的问题不许混进判据层，自测也才能离线跑。
    """
    problems: list[dict] = []
    for name, upstream in sorted(actual.items()):
        if name not in baseline:
            detail = f"门面多出包目录 {name}/（冻结基线里没有）"
            if upstream:
                detail += f"，转发到 {upstream}"
            else:
                detail += "（无 alias.go 转发目标 = 门面自研包，门面应当零逻辑）"
            problems.append({"kind": "new-package", "package": name,
                             "detail": detail})
            continue
        want = baseline[name]
        if upstream != want:
            problems.append({
                "kind": "new-target", "package": name,
                "detail": (f"门面 {name} 的转发目标是 {upstream or '（无）'}，"
                           f"冻结基线记的是 {want}——多出来一个转发目标 = "
                           "门面又长出一张新脸；要改门面就得显式重新冻结基线"),
            })
    return problems


def unreadable_reason(actual: dict[str, str], baseline: dict[str, str]) -> str | None:
    """门面一个包都没读到 ⇒ 判不了（**不是**「没问题」）。→ 原因描述；正常时 None。

    为什么必须查这一条：把 --facade 指错目录（指向门面仓的**父目录**、指向
    matrix-tools 本仓……）会读到 0 个包，而「0 个包 ⊆ 48 个包」在判据上**是绿的**——
    那是**假绿**，守卫会对着一个空目录说「门面没长新包」。
    门面不可能合理地一个包都没有，所以这个状态一律当「判不了」判红。
    """
    if actual or not baseline:
        return None
    return ("从门面仓里一个包都没读到——多半是 --facade 指错了目录、"
            "或门面被清空；两者都不是「没问题」")


# ---------------------------------------------------------------- 取门面仓（联网）

def _listed_in_org(org: str, repo: str) -> bool | None:
    """仓是否还在 org 的仓列表里。→ True / False；判不了返回 None。

    为什么要二次确认：`gh api repos/<org>/<repo>` 返回 404 有两种可能——
    仓真没了，还是这个 token 没权限看它。不确认就把「没权限」当成「门面没了」，
    守卫会**假绿**。
    """
    raw = repo_meta.gh_api(f"orgs/{org}/repos?per_page=100")
    if raw is None:
        return None
    try:
        repos = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(repos, list):
        return None
    if len(repos) >= 100:
        # 列表被截断 ⇒ 可能还有没看到的仓，不能据此判「不存在」
        return None
    return repo in {r.get("name") for r in repos}


def resolve_remote(org: str, repo: str, ref: str, dest: Path) -> tuple[Path | None, str, str]:
    """联网取门面仓 → (仓根 or None, 状态, 说明)。状态 ∈ {ok, archived, absent, error}。

    「不存在」与「判不了」在这里就分开——见模块 docstring 的状态表。
    """
    raw, status = repo_meta.gh_api_status(f"repos/{org}/{repo}")
    if status == "not_found":
        listed = _listed_in_org(org, repo)
        if listed is False:
            return None, "absent", (
                f"{org}/{repo} 不存在（404，且 {org} 的仓列表里也没有它）")
        if listed is None:
            return None, "error", (
                f"{org}/{repo} 返回 404，但无法确认它是「不存在」还是「没权限」")
        return None, "error", (
            f"{org}/{repo} 返回 404，但它仍在 {org} 的仓列表里（多半是权限问题）")
    if status != "ok" or raw is None:
        return None, "error", f"取不到 {org}/{repo} 的仓库信息（网络/权限失败）"
    try:
        archived = bool(json.loads(raw).get("archived"))
    except (ValueError, TypeError):
        return None, "error", f"{org}/{repo} 的仓库信息解析失败"
    root = repo_meta.fetch_repo_snapshot(org, repo, dest, ref)
    if root is None:
        return None, "error", f"取不到 {org}/{repo}@{ref} 的源码快照"
    if archived:
        return root, "archived", (
            f"{org}/{repo} 已归档（GitHub 禁止 push ⇒ 物理上已无法再新增；"
            "但归档之前也可能已经长歪，仍要判）")
    return root, "ok", ""


# ---------------------------------------------------------------- 输出

def _conclusion(state: str, packages: int, baseline: int, problems: list[dict],
                as_json: bool, ok: bool | None = None, reason: str = "") -> None:
    """stdout 只放机器可读的结论行；其余一律 stderr。"""
    new_packages = sum(1 for p in problems if p["kind"] == "new-package")
    new_targets = sum(1 for p in problems if p["kind"] == "new-target")
    verdict = (not problems) if ok is None else ok
    if as_json:
        print(json.dumps({
            "ok": verdict,
            "facade_state": state,
            "facade_packages": packages,
            "baseline_packages": baseline,
            "new_packages": new_packages,
            "new_targets": new_targets,
            "reason": reason,
            "problems": problems,
        }, ensure_ascii=False, indent=2))
        return
    status = "ok" if verdict else "fail"
    print(f"facade-frozen: {status} facade={state} packages={packages} "
          f"baseline={baseline} new_packages={new_packages} new_targets={new_targets}")


# ---------------------------------------------------------------- 自测

def _write_facade(root: Path, pkgs: dict[str, str | None]) -> None:
    """造一个临时门面仓：{包名: upstream 路径}；upstream 为 None = 只有 .go 文件的自研包。"""
    for name, upstream in pkgs.items():
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        if upstream is None:
            (d / "impl.go").write_text(f"package {name}\n", encoding="utf-8")
        else:
            (d / "alias.go").write_text(
                f'package {name}\n\nimport upstream "{upstream}"\n',
                encoding="utf-8")


def _selftest() -> int:
    """离线自测：证明这条守卫真能报红，而不是「一直绿，但从没生效过」。

    用**真实逻辑**跑**真实样本**：造临时门面仓 + 真调用 `facade_packages()` / `check()`，
    不 mock 判定函数——mock 掉判据的自测等于没测。
    """
    failures: list[str] = []

    def check_ok(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    base = {
        "alpha": "github.com/oagentkit/x/alpha",
        "beta": "github.com/oagentkit/y/beta",
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def case(name: str) -> Path:
            p = root / name
            p.mkdir(parents=True, exist_ok=True)
            return p

        # ① 与基线完全一致 ⇒ 绿
        c1 = case("c1")
        _write_facade(c1, dict(base))
        check_ok("与基线一致→无问题", check(base, facade_packages(c1)), [])

        # ② 新增包 ⇒ 红
        c2 = case("c2")
        _write_facade(c2, {**base, "gamma": "github.com/oagentkit/z/gamma"})
        kinds = {(p["kind"], p["package"]) for p in check(base, facade_packages(c2))}
        check_ok("新增包被判出", ("new-package", "gamma") in kinds, True)

        # ③ 删包（上游删包、门面跟着删）⇒ 绿
        #    这一条是「单向」判据的核心：删包**不归这条守卫管**
        #    （上游真删包时门面必须跟着删，否则 alias.go 编译不过）。
        c3 = case("c3")
        _write_facade(c3, {"alpha": base["alpha"]})
        check_ok("删包不报红（上游删除场景）", check(base, facade_packages(c3)), [])

        # ④ 新增转发目标（改指新上游）⇒ 红
        c4 = case("c4")
        _write_facade(c4, {**base, "beta": "github.com/oagentkit/z/beta-new"})
        kinds = {(p["kind"], p["package"]) for p in check(base, facade_packages(c4))}
        check_ok("新增转发目标被判出", ("new-target", "beta") in kinds, True)

        # ⑤ 负向样本：数量相同但集合不同 ⇒ 红
        #    只比「包数」的实现（门面 2 个 = 基线 2 个）会漏掉这种替换，
        #    所以判据必须比集合，不能比数量。
        c5 = case("c5")
        _write_facade(c5, {"alpha": base["alpha"], "gamma": "github.com/oagentkit/z/gamma"})
        kinds = {(p["kind"], p["package"]) for p in check(base, facade_packages(c5))}
        check_ok("数量相同但集合不同也要报", ("new-package", "gamma") in kinds, True)

        # ⑥ 门面自研包（只有 .go 文件、没有 alias.go 转发目标）⇒ 红
        c6 = case("c6")
        _write_facade(c6, {**base, "selfmade": None})
        kinds = {(p["kind"], p["package"]) for p in check(base, facade_packages(c6))}
        check_ok("门面自研包被判出", ("new-package", "selfmade") in kinds, True)

    # ⑦ 空门面（--facade 指错目录）⇒ 判不了，不能当成「没问题」判绿
    check_ok("空门面判不了", unreadable_reason({}, base) is not None, True)
    check_ok("读到包就不判不了", unreadable_reason({"alpha": base["alpha"]}, base), None)

    if failures:
        print("❌ verify_facade_frozen 自测失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ verify_facade_frozen 自测通过"
          "（与基线一致→绿 / 新增包→红 / 删包→绿 / 新增转发目标→红 / "
          "数量相同集合不同→红 / 门面自研包→红 / 空门面→判不了）")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="校验门面包集合是否仍是冻结基线的子集（门面已废弃，不得再长出新包）")
    ap.add_argument("--facade", help="门面仓本地路径（本地模式）")
    ap.add_argument("--org", help="联网模式：GitHub 组织名，如 oagentkit")
    ap.add_argument("--facade-repo", default=DEFAULT_FACADE_REPO,
                    help="联网模式下的门面仓名（默认 agentkit）")
    ap.add_argument("--ref", default="main", help="联网模式读的 ref（默认 main）")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help="冻结基线文件（默认 scripts/facade_package_baseline.json）")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="机器可读输出（stdout）")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑离线自测（证明守卫本身有效）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"❌ 找不到冻结基线文件: {baseline_path}", file=sys.stderr)
        return 2
    try:
        baseline = load_baseline(baseline_path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ 冻结基线文件格式错误: {e}", file=sys.stderr)
        return 2

    tmpdir: str | None = None
    try:
        if args.facade:
            facade_dir = Path(args.facade).expanduser()
            if not facade_dir.is_dir():
                print(f"❌ 门面仓路径不存在: {facade_dir}", file=sys.stderr)
                return 2
            actual = facade_packages(facade_dir)
            state, note = "local", ""
        elif args.org:
            tmpdir = tempfile.mkdtemp(prefix="facade-frozen-")
            root, state, note = resolve_remote(
                args.org, args.facade_repo, args.ref, Path(tmpdir))
            if root is None:
                if state == "absent":
                    # 门面仓不在了 ⇒「门面继续生长」物理上不可能发生 ⇒ 判绿（但要显眼）。
                    _conclusion(state, 0, len(baseline), [], args.as_json)
                    print(f"⚠️  {note}", file=sys.stderr)
                    print("    门面已不存在 ⇒「门面继续生长」在物理上不可能发生，故判定通过。",
                          file=sys.stderr)
                    print("    若门面被重建或改名，本守卫会立即恢复判定（届时新增即红）。",
                          file=sys.stderr)
                    return 0
                # 判不了 ≠ 没问题：网络失败/状态判不了一律判红。
                print(f"❌ 判不了（不能当成「没问题」）：{note}", file=sys.stderr)
                print("    门面仓状态取不到 ⇒ 本守卫这次没有履行；"
                      "请重跑或检查网络 / gh 认证。", file=sys.stderr)
                return 1
            actual = facade_packages(root)
        else:
            print("必须给 --facade <门面仓路径> 或 --org <组织名>", file=sys.stderr)
            return 2
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    problems = check(baseline, actual)
    dropped = sorted(set(baseline) - set(actual))

    reason = unreadable_reason(actual, baseline)
    if reason:
        print(f"❌ 判不了（不能当成「没问题」）：{reason}", file=sys.stderr)
        _conclusion(state, len(actual), len(baseline), [], args.as_json,
                    ok=False, reason=reason)
        return 1

    _conclusion(state, len(actual), len(baseline), problems, args.as_json)

    # ↓ 以下全是信息性输出 → stderr（stdout 只留上面那行结论）
    if note:
        print(f"ℹ️  {note}", file=sys.stderr)
    if dropped:
        print(f"ℹ️  门面比基线少 {len(dropped)} 个包（上游删包、门面跟着删，允许）："
              f"{', '.join(dropped)}", file=sys.stderr)
    if problems:
        print(f"\n❌ 门面长出了 {len(problems)} 处新东西"
              "（门面已废弃，不得继续生长）：\n", file=sys.stderr)
        for p in problems:
            print(f"   [{p['kind']:<11}] {p['package']:<12} {p['detail']}", file=sys.stderr)
        print("\n修法：门面的新增必须回退（门面已废弃，不接受新功能）；"
              "若确实要保留，先在本仓重新冻结基线"
              "（scripts/facade_package_baseline.json）并在 why 里写清理由——"
              "那是需要 review 的决定。", file=sys.stderr)
    else:
        print(f"✅ 门面没有长出新包（{len(actual)} 个包 ⊆ 冻结基线 {len(baseline)} 个）",
              file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
