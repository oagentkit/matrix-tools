#!/usr/bin/env python3
"""floorBumps 登记对账门禁（**独立 oracle**）：矩阵登记的「依赖地板抬高」必须等于事实。

## 守的是什么（issue matrix-tools#68）

`versions.json` 的 `floorBumps` 登记每一版**抬了依赖地板**的事实。
它对下游的意义是：「这一版会让你的构建红，除非你跑 `go mod tidy`」。

实测机制（零网络合成样本）：

    消费方 go.mod 锁 depB v1.0.0，上游 depA v1（地板 v1.0.0）→ 构建绿；
    上游换成 depA v2（地板抬到 v1.1.0 且新增 depC）→
        go: updates to go.mod needed, disabled by -mod=readonly

所以这字段是**下游能不能提前知道要 tidy**的唯一机器可读来源。

## 为什么必须独立于生成器

矩阵踩过两次「门禁与被检对象共享判断逻辑 ⇒ 共同盲区」的坑
（breakingIndex 对账复用生成器解析函数；CHANGELOG 逐字节门禁比的是生成器丢 body 后的结果）。
两次的形态都是：**生成器漏收时门禁跟着一起漏，门禁全绿而矩阵对下游说谎**。

所以本脚本**不 import `gen_floor_bumps`**，自己取 tag、自己解析 go.mod、
自己比较版本号。两者结论不一致时**以本条为准**。

## 判据

1. 取每个仓的 root tag（`^v\\d+\\.\\d+\\.\\d+$`），按 semver 排序；
2. 对每一对相邻 tag，取两处 go.mod，算出 require 的版本差异（抬高 / 新增）；
3. 与 `versions.json` 登记的 `floorBumps` 逐仓比对：
   - **漏登记**（事实有、登记没有）→ 红；
   - **多登记**（登记有、事实没有）→ 红；
   - **内容不符**（版本对但依赖/from/to 不一致）→ 红；
   - **判不了**（仓读不到、浅克隆、git 报错）→ 红（「判不了」≠「没问题」）。

## 用法

    verify_floor_bumps.py --root /path/to/matrix-parent --versions versions.json
    verify_floor_bumps.py --org oagentkit --versions versions.json
    verify_floor_bumps.py --selftest
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

from judge_report import Report, emit  # noqa: E402

DEFAULT_VERSIONS = HERE.parent / "versions.json"

# 主模块版本线。嵌套模块的 tag（audit/v0.1.0）不带这个形态。
TAG_LINE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# require 行：`路径 版本`（版本必须是 v 开头，路径必须含 `/`）
REQ_LINE = re.compile(r"^(?P<path>[^\s]+/[^\s]+)\s+(?P<ver>v[^\s]+)")


# ---------------------------------------------------------------- 事实


def _semver_tuple(text: str) -> tuple | None:
    """版本号 → 比较用元组；不是 `vX.Y.Z` 返回 None。

    独立实现（不与生成器共用）：预发布后缀按 `-` 切分，`+build` 不算预发布。
    """
    m = TAG_LINE.match((text or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _is_prerelease(text: str) -> bool:
    """是否是预发布版本（`v1.0.0-rc1` 是；`v1.0.0+incompatible` 不是）。"""
    body = (text or "").strip()
    if "-" not in body:
        return False
    tail = body.split("-", 1)[1]
    return bool(tail.split("+")[0])


def _tag_order(text: str) -> tuple:
    """tag 的排序键：主版本号升序，预发布排在正式版**之前**。"""
    base = _semver_tuple(text)
    if base is None:
        return (0, 0, 0, 0)
    return (base[0], base[1], base[2], 0 if _is_prerelease(text) else 1)


def _root_tags_from_git(repo_dir: Path) -> list[tuple[str, str]]:
    """本地克隆的 root tag → [(tag, 日期)]。

    **浅克隆 = 判不了**：浅克隆只有部分 tag，「相邻 tag」是假的
    （看上去没抬地板，其实是没看到那个 tag）。
    """
    if not (repo_dir / ".git").exists():
        raise RuntimeError(f"{repo_dir} 不是 git 仓")
    probe = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--is-shallow-repository"],
                           text=True, capture_output=True)
    if probe.stdout.strip() == "true":
        raise RuntimeError(f"{repo_dir} 是浅克隆——tag 历史不全，判不了")
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "for-each-ref",
         "--format=%(refname:short)|%(creatordate:short)", "refs/tags/"],
        text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"{repo_dir} 读 tag 失败：{res.stderr.strip()}")
    found = []
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 2:
            continue
        tag, date = parts[0].strip(), parts[1].strip()
        if TAG_LINE.match(tag):
            found.append((tag, date))
    found.sort(key=lambda t: _tag_order(t[0]))
    return found


def _gomod_from_git(repo_dir: Path, ref: str) -> str | None:
    res = subprocess.run(["git", "-C", str(repo_dir), "show", f"{ref}:go.mod"],
                         text=True, capture_output=True)
    if res.returncode != 0:
        return None
    return res.stdout


def _requirements(text: str) -> dict[str, str]:
    """go.mod 原文 → {路径: 版本}。

    独立实现：逐行扫，认 `require (` 块与单行 `require `；
    剥掉 `// indirect` 一类的行尾注释。
    """
    found: dict[str, str] = {}
    inside = False
    for raw in text.splitlines():
        stripped = raw.split("//", 1)[0].strip()
        if not stripped:
            continue
        if inside:
            if stripped.startswith(")"):
                inside = False
                continue
            m = REQ_LINE.match(stripped)
            if m:
                found[m.group("path")] = m.group("ver")
            continue
        if stripped.startswith("require"):
            rest = stripped[len("require"):].strip()
            if rest.startswith("("):
                inside = True
                continue
            m = REQ_LINE.match(rest)
            if m:
                found[m.group("path")] = m.group("ver")
    return found


# ---------------------------------------------------------------- 判定


def _raised(old: dict[str, str], new: dict[str, str]) -> list[tuple[str, str | None, str]]:
    """两处 go.mod 的 require 差异 → [(依赖, from, to)]，只收「抬高」与「新增」。

    降地板与删依赖不算（放松不是破坏）。
    """
    raised: list[tuple[str, str | None, str]] = []
    for path in sorted(new):
        after = new[path]
        before = old.get(path)
        if before is None:
            raised.append((path, None, after))
            continue
        b, a = _semver_tuple(before), _semver_tuple(after)
        if b is None or a is None:
            continue
        if a > b or (a == b and _is_prerelease(before) and not _is_prerelease(after)):
            raised.append((path, before, after))
    return raised


def truth_for_repo(repo_dir: Path) -> list[dict]:
    """某仓的事实 → [{version, date, bumps:[{dep, from, to}]}]（只收有抬地板的版本）。"""
    tags = _root_tags_from_git(repo_dir)
    entries: list[dict] = []
    for (prev_tag, _prev_date), (tag, date) in zip(tags, tags[1:]):
        before = _gomod_from_git(repo_dir, prev_tag)
        after = _gomod_from_git(repo_dir, tag)
        if before is None or after is None:
            continue
        raised = _raised(_requirements(before), _requirements(after))
        if raised:
            entries.append({
                "version": tag,
                "date": date,
                "bumps": [{"dep": d, "from": f, "to": t} for d, f, t in raised],
            })
    return entries


def _norm(entries: list[dict]) -> dict[str, list[tuple[str, str | None, str]]]:
    """登记条目 → {版本: [(依赖, from, to)]}，便于逐版本比对。"""
    out: dict[str, list[tuple[str, str | None, str]]] = {}
    for e in entries or []:
        ver = e.get("version")
        if not ver:
            continue
        out[ver] = [(b.get("dep"), b.get("from"), b.get("to"))
                    for b in e.get("bumps", [])]
    return out


def compare(truth: list[dict], registered: list[dict]) -> dict:
    """事实 vs 登记 → {漏登记, 多登记, 内容不符}。

    比对口径：**按依赖集合比**，不比顺序（JSON 里的顺序不该成为差异来源）。
    """
    t, r = _norm(truth), _norm(registered)
    missing = sorted(set(t) - set(r))
    extra = sorted(set(r) - set(t))
    mismatched = []
    for ver in sorted(set(t) & set(r)):
        if sorted(t[ver]) != sorted(r[ver]):
            mismatched.append({
                "version": ver,
                "truth": sorted(t[ver]),
                "registered": sorted(r[ver]),
            })
    return {"missing": missing, "extra": extra, "mismatched": mismatched}


# ---------------------------------------------------------------- 编排


def registered_map(doc: dict) -> dict[str, list[dict]]:
    """versions.json → {仓名: floorBumps}（modules[] 与 facades[] 两段都收）。"""
    out: dict[str, list[dict]] = {}
    for section in ("modules", "facades"):
        for entry in doc.get(section, []):
            repo = entry.get("repo")
            if repo:
                out[repo] = entry.get("floorBumps") or []
    return out


def judge(root: Path | None, org: str | None, doc: dict, report: Report) -> None:
    registered = registered_map(doc)
    if not registered:
        report.red("versions.json", "文档里没有任何 modules/facades 条目")
        return

    for repo in sorted(registered):
        if root is not None:
            repo_dir = root / repo
            if not (repo_dir / ".git").exists():
                report.red(repo, f"判不了：{repo_dir} 不是 git 仓（拿不到 tag 历史）",
                           "克隆该仓（含完整 tag 历史）后重跑")
                continue
            try:
                truth = truth_for_repo(repo_dir)
            except RuntimeError as e:
                report.red(repo, f"判不了：{e}",
                           "浅克隆拿不到完整 tag 历史——判不了不等于没问题，"
                           "请用完整克隆重跑")
                continue
        else:
            try:
                truth = truth_via_api(org, repo)
            except RuntimeError as e:
                report.red(repo, f"判不了：{e}", "网络或权限问题，重跑即可")
                continue

        diff = compare(truth, registered[repo])
        n_truth = sum(len(v) for v in _norm(truth).values())
        if not (diff["missing"] or diff["extra"] or diff["mismatched"]):
            report.info(repo, f"floorBumps 对账一致（{n_truth} 处抬地板）")
            continue

        for ver in diff["missing"]:
            detail = [f"{d} {f or '（新增）'} → {t}"
                      for d, f, t in _norm(truth).get(ver, [])]
            report.red(repo, f"漏登记：{ver} 抬了地板，但 versions.json 里没有这一版",
                       f"{ver} 抬的依赖：{'; '.join(detail)}\n"
                       "修法：python3 scripts/gen_floor_bumps.py --root <克隆父目录> "
                       "--versions versions.json --write")
        for ver in diff["extra"]:
            report.red(repo, f"多登记：versions.json 里有 {ver}，但事实里这一版没抬地板",
                       "多半是手工改过登记；重跑生成器覆盖")
        for m in diff["mismatched"]:
            report.red(repo, f"内容不符：{m['version']} 登记的依赖与事实不一致",
                       f"事实：{m['truth']}\n登记：{m['registered']}\n"
                       "修法：重跑生成器覆盖")


def truth_via_api(org: str, repo: str) -> list[dict]:
    """联网模式：逐 tag 取 go.mod（慢，但不需要本地克隆）。"""
    import repo_meta

    raw = repo_meta.gh_api(f"repos/{org}/{repo}/tags?per_page=100")
    if not raw:
        raise RuntimeError(f"{org}/{repo} 的 tag 列表取不到")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{org}/{repo} 的 tag 列表解析失败：{e}") from e
    tags = sorted([t.get("name", "") for t in data if TAG_LINE.match(t.get("name", ""))],
                  key=_tag_order)
    entries: list[dict] = []
    for prev, cur in zip(tags, tags[1:]):
        before = repo_meta.fetch_file(org, repo, "go.mod", ref=prev)
        after = repo_meta.fetch_file(org, repo, "go.mod", ref=cur)
        if before is None or after is None:
            continue
        raised = _raised(_requirements(before), _requirements(after))
        if raised:
            entries.append({
                "version": cur,
                "date": "",
                "bumps": [{"dep": d, "from": f, "to": t} for d, f, t in raised],
            })
    return entries


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    """反证自测：四类失配都必须被抓出来，且**判据本身要经得起反证**。

    关键反证：把「新增依赖」这一路阉割掉（只认版本变大）后，
    自测必须失败——否则门禁是「一直绿、从没生效过」。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    truth = [{
        "version": "v0.2.0", "date": "2026-10-21",
        "bumps": [{"dep": "github.com/oagentkit/llm", "from": "v0.15.0", "to": "v0.15.2"},
                  {"dep": "github.com/oagentkit/rag", "from": None, "to": "v0.8.4"}],
    }]

    # ① 完全一致
    d = compare(truth, truth)
    check("一致时不报差异",
          (d["missing"], d["extra"], d["mismatched"]), ([], [], []))

    # ② 漏登记
    d = compare(truth, [])
    check("漏登记被抓出", d["missing"], ["v0.2.0"])

    # ③ 多登记
    d = compare([], truth)
    check("多登记被抓出", d["extra"], ["v0.2.0"])

    # ④ 内容不符（版本对、依赖内容不同）
    wrong = [{"version": "v0.2.0", "date": "2026-10-21",
              "bumps": [{"dep": "github.com/oagentkit/llm", "from": "v0.15.0", "to": "v0.15.1"}]}]
    d = compare(truth, wrong)
    check("内容不符被抓出", len(d["mismatched"]), 1)

    # ⑤ 顺序不同不算差异（JSON 顺序不该成为差异来源）
    reordered = [{"version": "v0.2.0", "date": "2026-10-21",
                  "bumps": list(reversed(truth[0]["bumps"]))}]
    d = compare(truth, reordered)
    check("顺序不同不算差异", d["mismatched"], [])

    # ⑥ 判据本身：抬地板 / 降地板 / 新增 / 没变
    old = {"github.com/oagentkit/llm": "v0.15.0"}
    new = {"github.com/oagentkit/llm": "v0.15.2", "github.com/oagentkit/rag": "v0.8.4"}
    raised = _raised(old, new)
    check("抬地板被认出", [(d, f, t) for d, f, t in raised],
          [("github.com/oagentkit/llm", "v0.15.0", "v0.15.2"),
           ("github.com/oagentkit/rag", None, "v0.8.4")])
    check("降地板不算", _raised({"github.com/oagentkit/llm": "v0.15.2"},
                                {"github.com/oagentkit/llm": "v0.15.0"}), [])
    check("没变不算", _raised(old, dict(old)), [])

    # ⑦ semver 必须是数字比较（字符串比会把 v0.9.10 判成小于 v0.9.9）
    check("v0.9.10 > v0.9.9", _semver_tuple("v0.9.10") > _semver_tuple("v0.9.9"), True)
    check("预发布排在正式版前", _tag_order("v1.0.0-rc1") < _tag_order("v1.0.0"), True)

    # ⑧ go.mod 解析：块 + 单行 + 行尾注释
    gomod = ("module x\n\ngo 1.26.0\n\n"
             "require (\n\tgithub.com/oagentkit/textx v0.6.1\n"
             "\tgithub.com/oagentkit/runtime v0.7.1 // indirect\n)\n"
             "require github.com/oekit/pkg v0.20.0\n")
    check("go.mod 解析", _requirements(gomod),
          {"github.com/oagentkit/textx": "v0.6.1",
           "github.com/oagentkit/runtime": "v0.7.1",
           "github.com/oekit/pkg": "v0.20.0"})

    # ⑨ 反证：阉割「新增依赖」这一路后，自测必须失败
    def crippled_raised(old: dict, new: dict):
        out = []
        for path in sorted(new):
            before = old.get(path)
            after = new[path]
            if before is None:
                continue          # ← 被阉割的那一路
            b, a = _semver_tuple(before), _semver_tuple(after)
            if b is None or a is None:
                continue
            if a > b:
                out.append((path, before, after))
        return out

    check("阉割后认不出新增依赖",
          crippled_raised(old, new),
          [("github.com/oagentkit/llm", "v0.15.0", "v0.15.2")])
    if crippled_raised(old, new) == raised:
        fails.append("反证失败：阉割新增依赖后结论没变——说明自测没有区分力")

    # ⑩ 端到端：假造一个仓，事实必须与登记一致；改登记后必须报出差异
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d2 = Path(td) / "demo"
        d2.mkdir()

        def g(*args):
            subprocess.run(["git", "-C", str(d2), *args], check=True,
                           capture_output=True, text=True)

        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (d2 / "go.mod").write_text(
            "module github.com/oagentkit/demo\n\ngo 1.26.0\n\n"
            "require github.com/oagentkit/llm v0.15.0\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "v1")
        g("tag", "v0.1.0")
        (d2 / "go.mod").write_text(
            "module github.com/oagentkit/demo\n\ngo 1.26.0\n\n"
            "require github.com/oagentkit/llm v0.15.2\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "v2")
        g("tag", "v0.2.0")
        facts = truth_for_repo(d2)
        check("端到端：事实里有一个抬地板的版本", len(facts), 1)
        check("端到端：事实的版本号", facts[0]["version"] if facts else None, "v0.2.0")
        d3 = compare(facts, facts)
        check("端到端：事实与自身一致", (d3["missing"], d3["extra"], d3["mismatched"]),
              ([], [], []))
        d4 = compare(facts, [])
        check("端到端：登记为空时报漏登记", d4["missing"], ["v0.2.0"])

    if fails:
        print("❌ verify_floor_bumps 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_floor_bumps 自测通过"
          "（一致 / 漏登记 / 多登记 / 内容不符 / 顺序无关 / 判定口径 / semver / 解析 / 反证 / 端到端）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="floorBumps 登记对账门禁（独立 oracle）")
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--root", help="各仓本地克隆的父目录（<root>/<repo>）")
    ap.add_argument("--org", help="组织名；给了就用联网模式")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    vp = Path(args.versions)
    if not vp.is_file():
        print(f"❌ 找不到 {vp}")
        return 2
    doc = json.loads(vp.read_text(encoding="utf-8"))

    report = Report(title="floorBumps 对账")
    if args.root:
        judge(Path(args.root), None, doc, report)
    elif args.org:
        judge(None, args.org, doc, report)
    else:
        print("❌ 必须给 --root（本地克隆父目录）或 --org（联网）")
        return 2
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
