#!/usr/bin/env python3
"""import 路径对账门禁：矩阵登记的 importPath == 各仓**真实**的模块路径与包目录。

## 背景（2026-10-19 实测）

`versions.json` 的 `packages[].importPath` 曾登记**变更前**的旧路径
（`github.com/oagentkit/llm/llm`，而实际发布路径是 `github.com/oagentkit/llm`）。

这条比「漏收一条变更」更严重：importPath 是给**机器**用的权威路径，
机器照着它写 import 就直接编译不过——而矩阵不报错、不告警。

所以本门禁不看「规则算出来应该是什么」，而是**去各仓的 go.mod 与目录里看
实际是什么**，两边对不上就红：

1. 仓里 go.mod 的 module 路径 == github.com/{org}/{repo}（模块改名了矩阵要知道）；
2. 矩阵登记的 importPath == 规则算出来的（matrix_paths 是唯一构造点）；
3. 矩阵登记的包**在仓里真实存在**（有 .go 文件的目录）；
4. 嵌套 module 的路径 == 它自己的 go.mod 里的 module 路径
   （guardx 拆出 guardx/audit、guardx/bashguard 之后，这条是新加的）。

## 边界

- 「仓里存在但矩阵没登记」的**幽灵包**由 verify_ghost_packages.py 守，本脚本不重复。
- 本地模式（--root）读工作区；联网模式（--org）读远端 main 分支。

用法:
    verify_import_paths.py --root /path/to/oagentkit
    verify_import_paths.py --org oagentkit
    verify_import_paths.py --selftest

退出码: 0 = 一致；1 = 有出入 / 判不了。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matrix_paths import leaf, new_import_path  # noqa: E402  import 路径的唯一构造点
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"
DEFAULT_VERSIONS = HERE.parent / "versions.json"


def local_repo_facts(root: str, repo: str) -> dict | None:
    """本地取某仓的事实：module 路径 / 包目录 / 嵌套 module 路径。取不到返回 None。"""
    base = Path(root) / repo
    if not base.is_dir():
        return None
    gomod = base / "go.mod"
    module_path = repo_meta.module_path_from_gomod(
        gomod.read_text(encoding="utf-8")) if gomod.is_file() else None
    pkg_dirs = set()
    nested = {}
    # 仓根本身就可能是一个包（包名末段 == 仓名时包落 module 根：llm / rag）。
    # 只扫子目录会把它们误报成「登记了但仓里没有」。
    if any(f.suffix == ".go" and f.is_file() for f in base.iterdir() if f.is_file()):
        pkg_dirs.add(repo)
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        if any(f.suffix == ".go" and f.is_file() for f in d.iterdir()):
            pkg_dirs.add(d.name)
        sub = d / "go.mod"
        if sub.is_file():
            nested[d.name] = repo_meta.module_path_from_gomod(
                sub.read_text(encoding="utf-8"))
    return {"module_path": module_path, "pkg_dirs": pkg_dirs, "nested": nested}


def remote_repo_facts(org: str, repo: str) -> dict | None:
    """联网取某仓的事实。取不到返回 None（判不了）。"""
    gomod = repo_meta.fetch_file(org, repo, "go.mod")
    paths = repo_meta.fetch_tree_paths(org, repo)
    if gomod is None or paths is None:
        return None
    module_path = repo_meta.module_path_from_gomod(gomod)
    # package_dirs 把仓根记成 "."，这里换回仓名（包名末段 == 仓名时包落 module 根）
    pkg_dirs = {repo if d == "." else d for d in repo_meta.package_dirs(paths)}
    nested = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) == 2 and parts[1] == "go.mod":
            text = repo_meta.fetch_file(org, repo, p)
            nested[parts[0]] = repo_meta.module_path_from_gomod(text) if text else None
    return {"module_path": module_path, "pkg_dirs": pkg_dirs, "nested": nested}


def check_repo(org: str, repo: str, packages: list[dict], facts: dict,
               nested_decl: list[dict]) -> list[str]:
    """→ 问题描述列表（空 = 一致）"""
    problems: list[str] = []
    expected_module = f"github.com/{org}/{repo}"

    if facts["module_path"] is None:
        problems.append(f"{repo}: 取不到 go.mod 的 module 路径（判不了）")
    elif facts["module_path"] != expected_module:
        problems.append(f"{repo}: go.mod 的 module 路径是 {facts['module_path']}，"
                        f"矩阵期望 {expected_module}（模块改名了矩阵要知道）")

    for p in packages:
        pkg = p["pkg"]
        recorded = p.get("importPath")
        expected = new_import_path(org, repo, pkg)
        if recorded != expected:
            problems.append(f"{repo}/{pkg}: 矩阵登记的 importPath 是 {recorded}，"
                            f"按规则应是 {expected}")
        # 仓里的目录名 = 包路径的**末段**（modules.json 记的是旧单仓里的路径，
        # 中间层目录在拆分时被丢掉了——规则见 matrix_paths.leaf）
        if leaf(pkg) not in facts["pkg_dirs"]:
            problems.append(f"{repo}/{pkg}: 矩阵登记了这个包，仓里却没有对应的 .go 目录")
        # 由嵌套 module 提供的包：路径必须落在那个 module 上
        # （嵌套 module 路径与它自己 go.mod 是否一致，在下面的 nested 循环里统一查，
        #   这里再查一遍就是同一条问题报两次）
        owner = p.get("module")
        if owner and not any(n["module"] == owner for n in nested_decl):
            problems.append(f"{repo}/{pkg}: 声明由 {owner} 提供，但 nestedModules 里没有它")

    # 嵌套 module 自身的路径也要对
    for n in nested_decl:
        real = facts["nested"].get(n["path"])
        if real is None:
            problems.append(f"{repo}: 声明了嵌套 module {n['module']}，"
                            f"但仓里 {n['path']}/go.mod 不存在")
        elif real != n["module"]:
            problems.append(f"{repo}: 嵌套 module 路径是 {real}，矩阵登记 {n['module']}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="import 路径与各仓真实状态的对账门禁")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS))
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--org", help="联网读各仓 go.mod 与目录树")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root 或 --org")
        return 1

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    versions = json.loads(Path(args.versions).read_text(encoding="utf-8"))
    org = args.org or matrix.get("defaultOrg")

    by_repo = {m["repo"]: m for m in versions.get("modules", [])}

    problems: list[str] = []
    checked = 0
    for m in matrix["modules"]:
        repo = m["repo"]
        facts = local_repo_facts(args.root, repo) if args.root else remote_repo_facts(org, repo)
        if facts is None:
            problems.append(f"{repo}: 取不到仓的真实状态（判不了，不能当成「一致」）")
            continue
        checked += 1
        recorded = by_repo.get(repo, {}).get("packages", [])
        problems.extend(check_repo(org, repo, recorded, facts, m.get("nestedModules", [])))

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定）")
        return 1

    if problems:
        print(f"❌ import 路径对账失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：矩阵与仓里的真实状态对不上时，先定哪边是真相（多半是仓里改了"
              "模块路径/拆了包），再改 modules.json 并重跑 gen_versions.py。")
        return 1

    print(f"✅ import 路径与 {checked} 个仓的真实 go.mod / 包目录一致")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    org = "oagentkit"
    facts = {
        "module_path": "github.com/oagentkit/llm",
        "pkg_dirs": {"llm", "wrap", "llmtest"},
        "nested": {},
    }
    pkgs = [
        {"pkg": "llm", "importPath": "github.com/oagentkit/llm"},
        {"pkg": "wrap", "importPath": "github.com/oagentkit/llm/wrap"},
    ]
    check("一致无问题", check_repo(org, "llm", pkgs, facts, []), [])

    # 负向：登记的 importPath 是旧路径
    stale = check_repo(org, "llm", [{"pkg": "llm", "importPath": "github.com/oagentkit/llm/llm"}],
                       facts, [])
    check("旧路径被抓到", len(stale), 1)

    # 负向：登记了仓里没有的包
    ghost = check_repo(org, "llm", [{"pkg": "nope", "importPath": "github.com/oagentkit/llm/nope"}],
                       facts, [])
    check("幽灵包被抓到", len(ghost), 1)

    # 负向：go.mod 的 module 路径与矩阵不符
    wrong = check_repo(org, "llm", pkgs,
                       dict(facts, module_path="git.enjoye.top/enjoydream/agentkit/llm"), [])
    check("模块路径改名被抓到", len(wrong), 1)

    # 负向：取不到 go.mod
    check("判不了即红", len(check_repo(org, "llm", pkgs, dict(facts, module_path=None), [])), 1)

    # 嵌套 module：路径对得上 / 对不上
    nf = {"module_path": "github.com/oagentkit/guardx",
          "pkg_dirs": {"fence", "logredact", "audit", "bashguard"},
          "nested": {"audit": "github.com/oagentkit/guardx/audit",
                     "bashguard": "github.com/oagentkit/guardx/bashguard"}}
    decl = [{"path": "audit", "module": "github.com/oagentkit/guardx/audit"},
            {"path": "bashguard", "module": "github.com/oagentkit/guardx/bashguard"}]
    npkgs = [{"pkg": "fence", "importPath": "github.com/oagentkit/guardx/fence"},
             {"pkg": "audit", "importPath": "github.com/oagentkit/guardx/audit",
              "module": "github.com/oagentkit/guardx/audit"}]
    check("嵌套 module 一致", check_repo(org, "guardx", npkgs, nf, decl), [])
    bad_nested = check_repo(org, "guardx", npkgs,
                            dict(nf, nested=dict(nf["nested"], audit="github.com/oagentkit/audit")),
                            decl)
    check("嵌套 module 路径不符被抓到", len(bad_nested), 1)

    if fails:
        print("❌ verify_import_paths 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_import_paths 自测通过（旧路径 / 幽灵包 / 模块改名 / 判不了 / 嵌套 module）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
