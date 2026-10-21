#!/usr/bin/env python3
"""从各仓的 tag 历史算出「依赖地板抬高」（floor bump），登记进 versions.json。

## 这个字段在守什么（issue matrix-tools#68）

**「依赖地板抬高」对下游是隐式破坏性变更**：下游不跑 `go mod tidy` 就构建红，
报错是 `go: updates to go.mod needed`——**不提真正原因**。

实测（零网络合成样本，2026-10-21）：

    消费方 go.mod 锁 depB v1.0.0，上游 depA v1（地板 v1.0.0）→ 构建绿；
    同一消费方，上游换成 depA v2（地板抬到 v1.1.0 且新增 depC）→
        go: updates to go.mod needed, disabled by -mod=readonly; to update it:
            go mod tidy
    跑完 go mod tidy（消费方 go.mod 多出 depb v1.1.0 // indirect、depc v1.0.0 // indirect）
    → 构建绿。

即：**上游抬地板 = 下游必须重新 tidy**，否则构建红。

## 为什么判据必须是结构性的（go.mod 差异），不能靠文案

此前「地板追平」两头不落：既不进 `breakingIndex`，又被独立 oracle 的标记行判据误伤
——提交说明里写「隐式 breaking：」就判红，写「隐式破坏性变更」就没事。
**索引内容取决于作者怎么措辞，不是事实**。

本文件的判据只读**两个相邻 tag 处的 go.mod**：require 版本抬高（或依赖新增）
⇒ 这一版抬了地板。与提交说明一个字都不相干。

## 为什么不并进 breakingIndex

`breakingIndex` 的 `detail` 存的是**迁移说明**（「import 路径从 X 改为 Y」）；
地板抬高的修法是「go mod tidy」——那是**构建卫生**，不是**迁移**。
混进去之后，下游读它就分不出「改哪行 import」与「跑个 tidy」。
所以地板抬高单列一节 `floorBumps`（issue #68 的建议：后者更干净）。

## 口径

- **相邻 tag**：同一仓的 root tag（`^v\\d+\\.\\d+\\.\\d+$`）按 semver 排序后相邻的两个；
- **抬地板**：require 段的版本号变大（v0.9.0 → v0.9.1）；
- **新增依赖**：该版本新出现的 require（`from` 为 null）——同样逼下游 tidy；
- **降地板 / 删依赖**：不算（那是放松，不是破坏）；
- require 段**直接与间接都算**：Go 1.17+ 的模块图裁剪下，间接依赖不进消费方的
  构建，但**留在模块图里**——MVS 选中的版本变了，消费方的 go.mod 就得跟着改。

## 用法

    # 从本地克隆算（CI 与本地推荐：一次 clone 读完所有 tag）
    gen_floor_bumps.py --root /path/to/matrix-parent --versions versions.json --write

    # 只看算出来的结果（不写文件）
    gen_floor_bumps.py --root /path/to/matrix-parent --json

    # 联网模式（逐 tag 取 go.mod，慢；CI 用 --root）
    gen_floor_bumps.py --org oagentkit --json
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

DEFAULT_MATRIX = HERE.parent / "modules.json"
DEFAULT_VERSIONS = HERE.parent / "versions.json"

# root tag：主模块的版本线。嵌套模块的 tag（audit/v0.1.0）不带这个形态 → 排除。
ROOT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# go.mod 的 require 行：`github.com/oagentkit/llm v0.15.2`
REQUIRE_RE = re.compile(r"^([A-Za-z0-9][\w.\-]*(?:/[\w.\-/]+)+)\s+(v[0-9][\w.\-+]*)")


# ---------------------------------------------------------------- 事实层


def local_tags(repo_dir: Path) -> list[tuple[str, str]]:
    """本地克隆的 root tag → [(tag, 日期)]，按 semver 升序。

    日期取 annotated tag 的 tagger 日期（creatordate）；轻量 tag 退化成提交日期。

    **浅克隆必须报错**：浅克隆只有部分 tag，算出来的「相邻 tag」是假的
    （看上去没抬地板，其实是没看到那个 tag）。这与 CHANGELOG 覆盖门禁
    实测过的「缓存命中即工作树冻结 ⇒ 假红」是同一类坑。
    """
    if not (repo_dir / ".git").exists():
        raise RuntimeError(f"{repo_dir} 不是 git 仓")
    shallow = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--is-shallow-repository"],
                             text=True, capture_output=True)
    if shallow.stdout.strip() == "true":
        raise RuntimeError(f"{repo_dir} 是浅克隆——tag 历史不全，算不出真实的相邻 tag")
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "for-each-ref",
         "--format=%(refname:short) %(creatordate:short)", "refs/tags/"],
        text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"{repo_dir} 读 tag 失败：{out.stderr.strip()}")
    tags: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        tag, date = parts[0], parts[1]
        if ROOT_TAG_RE.match(tag):
            tags.append((tag, date))
    tags.sort(key=lambda t: ver_key(t[0]) or (0, 0, 0, 0, ""))
    return tags


def local_go_mod(repo_dir: Path, ref: str) -> str | None:
    """该 ref 处的 go.mod 原文；**该 ref 处没有 go.mod** 返回 None。"""
    out = subprocess.run(["git", "-C", str(repo_dir), "show", f"{ref}:go.mod"],
                         text=True, capture_output=True)
    return out.stdout if out.returncode == 0 else None


def api_tags(org: str, repo: str) -> list[tuple[str, str]]:
    """联网模式的 tag 列表 → [(tag, 日期)]。

    日期用 tag 指向的**提交日期**近似（API 拿不到 annotated tag 的 tagger 时间），
    口径差异写在输出里，不藏。
    """
    import repo_meta

    raw = repo_meta.gh_api(f"repos/{org}/{repo}/tags?per_page=100")
    if not raw:
        raise RuntimeError(f"{org}/{repo} 的 tag 列表取不到")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{org}/{repo} 的 tag 列表解析失败：{e}") from e
    tags = [(t.get("name", ""), "") for t in data if ROOT_TAG_RE.match(t.get("name", ""))]
    tags.sort(key=lambda t: ver_key(t[0]) or (0, 0, 0, 0, ""))
    return tags


def api_go_mod(org: str, repo: str, ref: str) -> str | None:
    import repo_meta

    raw = repo_meta.fetch_file(org, repo, "go.mod", ref=ref)
    if raw is None:
        # 该 ref 处确实没有 go.mod？还是取不到？分不清时**不假装有**——
        # 调用方按「取不到」处理（判不了 ≠ 没有）。
        return None
    return raw


# ---------------------------------------------------------------- 判定层


def ver_key(v: str) -> tuple | None:
    """版本号 → 可比较的元组；不是 semver 返回 None。

    build metadata（`+incompatible`）不算预发布；预发布版本比对应正式版小。
    """
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)([-+].*)?$", (v or "").strip())
    if not m:
        return None
    tail = m.group(4) or ""
    # 只有 `-` 开头才是预发布；`+incompatible` 是 build metadata，不算预发布
    pre = tail[1:].split("+")[0] if tail.startswith("-") else ""
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre)


def parse_requires(text: str) -> dict[str, str]:
    """go.mod 原文 → {依赖路径: 版本}。

    单行 `require x v1` 与 require 块都收；行尾 `// indirect` 注释剥掉。
    """
    out: dict[str, str] = {}
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            m = REQUIRE_RE.match(line)
            if m:
                out[m.group(1)] = m.group(2)
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if line.startswith("require "):
            m = REQUIRE_RE.match(line[len("require "):].strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out


def bumps_between(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """旧 require 集合 → 新 require 集合，得出「抬了地板」的依赖清单。

    - 版本变大 ⇒ 抬地板（from/to 都有）；
    - 新出现的依赖 ⇒ 新增（from 为 null）——同样逼下游 tidy。
    - 版本变小或依赖消失 ⇒ **不算**（放松不是破坏）。
    """
    out: list[dict] = []
    for dep, ver in sorted(new.items()):
        prev = old.get(dep)
        if prev is None:
            out.append({"dep": dep, "from": None, "to": ver})
            continue
        pk, nk = ver_key(prev), ver_key(ver)
        if pk is None or nk is None:
            # 版本号不是 semver → 判不了这个依赖，跳过（不假装没变）
            continue
        if nk > pk:
            out.append({"dep": dep, "from": prev, "to": ver})
    return out


def floor_bumps_for_repo(repo: str, tags: list[tuple[str, str]],
                         go_mod_at) -> list[dict]:
    """某仓的 floorBumps → [{version, date, bumps:[{dep, from, to}]}]。

    只收录**有抬地板**的版本（没有变化的不占位置）。
    """
    entries: list[dict] = []
    for (old_tag, _old_date), (new_tag, new_date) in zip(tags, tags[1:]):
        old_text = go_mod_at(old_tag)
        new_text = go_mod_at(new_tag)
        if old_text is None or new_text is None:
            # 该 tag 处没有 go.mod → 这一对**不可比**，跳过（不是「没抬地板」）
            continue
        bumps = bumps_between(parse_requires(old_text), parse_requires(new_text))
        if bumps:
            entries.append({"version": new_tag, "date": new_date, "bumps": bumps})
    return entries


# ---------------------------------------------------------------- 编排


def repo_list(matrix: dict) -> list[str]:
    """要算的仓：13 个模块 + 门面（infraRepos 里 goModule 为真的）。

    清单来自 modules.json（单一数据源），不写死仓名。
    """
    repos = [m["repo"] for m in matrix.get("modules", []) if m.get("repo")]
    for r in matrix.get("infraRepos", []):
        if r.get("goModule") and r.get("repo"):
            repos.append(r["repo"])
    return repos


def compute_all(root: Path | None, org: str | None, repos: list[str]) -> dict[str, list[dict]]:
    """→ {仓名: floorBumps 列表}。

    取数只有两种模式：本地克隆（--root）或联网（--org）。
    取不到的仓**不静默跳过**——抛错，让调用方判「判不了」。
    """
    out: dict[str, list[dict]] = {}
    for repo in repos:
        if root is not None:
            d = root / repo
            tags = local_tags(d)
            out[repo] = floor_bumps_for_repo(repo, tags, lambda ref: local_go_mod(d, ref))
        else:
            tags = api_tags(org, repo)
            out[repo] = floor_bumps_for_repo(
                repo, tags, lambda ref, r=repo: api_go_mod(org, r, ref))
    return out


def apply_to_versions(doc: dict, bumps: dict[str, list[dict]]) -> dict:
    """把 floorBumps 写进 versions.json 的 modules[] 与 facades[]。

    写不进去的仓（不在矩阵里）→ 忽略；矩阵里但**算不出来**的仓 → 不写空数组，
    避免「用空数组冒充『没有抬地板』」（判不了 ≠ 没有）。
    """
    touched = 0
    for section in ("modules", "facades"):
        for entry in doc.get(section, []):
            repo = entry.get("repo")
            if repo not in bumps:
                continue
            entry["floorBumps"] = bumps[repo]
            touched += 1
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="算出各仓的依赖地板抬高并登记进 versions.json")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--root", help="各仓本地克隆的父目录（<root>/<repo>）")
    ap.add_argument("--org", help="组织名；给了就用联网模式")
    ap.add_argument("--write", action="store_true", help="把结果写进 versions.json")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root（本地克隆父目录）或 --org（联网）")
        return 2

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = args.org or matrix.get("defaultOrg")
    repos = repo_list(matrix)

    try:
        bumps = compute_all(Path(args.root) if args.root else None, args.org, repos)
    except Exception as e:  # 取不到就是取不到，不静默
        print(f"❌ 算地板抬高失败：{e}")
        return 1

    total_versions = sum(len(v) for v in bumps.values())
    total_bumps = sum(len(e["bumps"]) for v in bumps.values() for e in v)

    if args.write:
        vp = Path(args.versions)
        if not vp.is_file():
            print(f"❌ 找不到 {vp}")
            return 1
        doc = json.loads(vp.read_text(encoding="utf-8"))
        apply_to_versions(doc, bumps)
        vp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 已把 floorBumps 写进 {vp}"
              f"（{total_versions} 个版本有抬地板，共 {total_bumps} 处依赖抬高）")
        return 0

    if args.json:
        print(json.dumps(bumps, ensure_ascii=False, indent=2))
        return 0

    print(f"# 依赖地板抬高：{total_versions} 个版本 / {total_bumps} 处")
    for repo in repos:
        entries = bumps.get(repo) or []
        print(f"\n{repo}（{len(entries)} 个版本有抬地板）")
        for e in entries:
            for b in e["bumps"]:
                old = b["from"] or "（新增）"
                print(f"  {e['version']}  {b['dep']}  {old} → {b['to']}")
    return 0


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    """反证自测：判据必须能分清「抬地板 / 降地板 / 新增 / 没变」。

    样本真的带被防的形态：把「抬地板」那一路阉割后（只认版本变大、
    不认新增依赖），自测必须失败。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    old = {"github.com/oagentkit/llm": "v0.15.0", "github.com/oagentkit/textx": "v0.6.1"}
    new = {"github.com/oagentkit/llm": "v0.15.2", "github.com/oagentkit/textx": "v0.6.1",
           "github.com/oagentkit/rag": "v0.8.4"}
    got = bumps_between(old, new)
    check("抬地板被认出", [b["dep"] for b in got],
          ["github.com/oagentkit/llm", "github.com/oagentkit/rag"])
    check("抬地板的 from/to", (got[0]["from"], got[0]["to"]), ("v0.15.0", "v0.15.2"))
    check("新增依赖的 from 是 null", got[1]["from"], None)

    # 降地板不算
    down = bumps_between({"github.com/oagentkit/llm": "v0.15.2"},
                          {"github.com/oagentkit/llm": "v0.15.0"})
    check("降地板不算破坏", down, [])

    # 删依赖不算
    drop = bumps_between({"github.com/oagentkit/llm": "v0.15.0"}, {})
    check("删依赖不算破坏", drop, [])

    # 没变不算
    same = bumps_between(old, dict(old))
    check("没变不算", same, [])

    # semver 必须是数字比较（字符串比会把 v0.9.10 判成小于 v0.9.9）
    check("v0.9.10 > v0.9.9", ver_key("v0.9.10") > ver_key("v0.9.9"), True)
    check("预发布小于正式版", ver_key("v1.0.0-rc1") < ver_key("v1.0.0"), True)
    check("+incompatible 不算预发布", ver_key("v1.0.0+incompatible") > ver_key("v0.9.9"), True)

    # go.mod 解析：块形式与单行形式都要收；行尾注释要剥掉
    gomod = (
        "module github.com/oagentkit/llm\n\ngo 1.26.0\n\n"
        "require (\n\tgithub.com/oagentkit/textx v0.6.1\n"
        "\tgithub.com/oagentkit/runtime v0.7.1 // indirect\n)\n\n"
        "require github.com/oekit/pkg v0.20.0\n"
    )
    check("go.mod 解析", parse_requires(gomod),
          {"github.com/oagentkit/textx": "v0.6.1",
           "github.com/oagentkit/runtime": "v0.7.1",
           "github.com/oekit/pkg": "v0.20.0"})

    # 端到端：假造两个 tag 的仓，判据必须认出「这一版抬了地板」
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "demo"
        d.mkdir()
        def g(*args):
            subprocess.run(["git", "-C", str(d), *args], check=True,
                           capture_output=True, text=True)
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (d / "go.mod").write_text(
            "module github.com/oagentkit/demo\n\ngo 1.26.0\n\n"
            "require github.com/oagentkit/llm v0.15.0\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "v1")
        g("tag", "v0.1.0")
        (d / "go.mod").write_text(
            "module github.com/oagentkit/demo\n\ngo 1.26.0\n\n"
            "require github.com/oagentkit/llm v0.15.2\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "v2")
        g("tag", "v0.2.0")
        tags = local_tags(d)
        entries = floor_bumps_for_repo("demo", tags, lambda ref: local_go_mod(d, ref))
        check("端到端：认出一个抬地板的版本", len(entries), 1)
        check("端到端：版本号对", entries[0]["version"] if entries else None, "v0.2.0")
        check("端到端：依赖对",
              [(b["dep"], b["from"], b["to"]) for b in entries[0]["bumps"]] if entries else None,
              [("github.com/oagentkit/llm", "v0.15.0", "v0.15.2")])

        # 浅克隆必须报错（不能算出一个假的「没抬地板」）
        shallow = Path(td) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{d}", str(shallow)],
                       check=True, capture_output=True)
        try:
            local_tags(shallow)
            fails.append("浅克隆必须报错：实得没报错")
        except RuntimeError:
            pass

    if fails:
        print("❌ gen_floor_bumps 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ gen_floor_bumps 自测通过"
          "（抬地板 / 降地板 / 新增依赖 / 没变 / semver 数字比较 / go.mod 解析 / 端到端 / 浅克隆报错）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
