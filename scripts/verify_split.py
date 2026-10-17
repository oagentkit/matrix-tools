#!/usr/bin/env python3
"""模块矩阵校验器 —— 拆分前/拆分后都要跑。

三项硬校验，任一失败 exit 1：
  1. 覆盖度：go list ./... 的每个包都登记在 modules.json 里，且没有幽灵条目
  2. 分层方向：tier N 的模块不得依赖 tier > N 的模块（自底向上不变量）
  3. 依赖闭包：每个模块声明的 dependsOn 必须覆盖其包在仓内的全部实际依赖

用法:
    verify_split.py --root .                       # 在 agentkit 单仓跑
    verify_split.py --root . --layout split        # 在拆分后的多仓布局跑
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MODULES = "docs/split-matrix/modules.json"
DEFAULT_ORG = "oagentkit"  # Open Agent Kit —— agent/LLM 编排工具链矩阵


def sh(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"命令失败: {' '.join(cmd)}")
    return r.stdout


def load(root: Path) -> dict:
    return json.loads((root / MODULES).read_text(encoding="utf-8"))


def check_coverage(root: Path, data: dict) -> list[str]:
    errs: list[str] = []
    try:
        raw = sh(["go", "list", "./..."], root)
    except SystemExit:
        return ["go list 失败，跳过覆盖度校验"]
    prefix = data["sourceModule"]
    pkgs = {ln[len(prefix) + 1:] for ln in raw.split() if ln.startswith(prefix + "/")}

    mapped: set[str] = set()
    for mod in data["modules"]:
        for p in mod["packages"]:
            if p in mapped:
                errs.append(f"包 {p} 被多个仓重复登记")
            mapped.add(p)

    # 待退役的包不进任何仓，但必须显式登记 —— 静默漏掉正是本脚本要防的失败模式
    retiring = {r["pkg"] for r in data.get("retiringPackages", [])}
    for r in data.get("retiringPackages", []):
        if not r.get("replacement"):
            errs.append(f"待退役包 {r['pkg']} 缺 replacement 字段")

    missing = sorted(pkgs - mapped - retiring)
    ghost = sorted(mapped - pkgs)
    if missing:
        errs.append(f"未登记的包 {len(missing)} 个: {missing}")
    if ghost:
        errs.append(f"矩阵有但仓里没有的幽灵条目 {len(ghost)} 个: {ghost}")
    return errs


def check_tier_direction(data: dict) -> list[str]:
    errs: list[str] = []
    tier = {m["repo"]: m["tier"] for m in data["modules"]}
    for mod in data["modules"]:
        for dep in mod.get("dependsOn", []):
            if dep not in tier:
                errs.append(f"{mod['repo']} 依赖未登记的仓 {dep}")
            elif tier[dep] > mod["tier"]:
                errs.append(
                    f"分层倒挂: {mod['repo']}(tier{mod['tier']}) 依赖 "
                    f"{dep}(tier{tier[dep]})，违反自底向上"
                )
    return errs


IMPORT_RE = re.compile(r'"git\.enjoye\.top/enjoydream/agentkit([^"]*)"')


def check_depends_closure(root: Path, data: dict) -> list[str]:
    """按 declares 的 dependsOn 校验：模块内包的跨仓依赖必须被声明。"""
    errs: list[str] = []
    owner: dict[str, str] = {}
    for mod in data["modules"]:
        for p in mod["packages"]:
            owner[p] = mod["repo"]

    try:
        # 用【逗号】分隔 imports，不能用 \n —— 换行会让每个包跨多行，
        # splitlines() 只取到第一行（通常是 stdlib 的 context），依赖闭包就永远校验不到东西。
        raw = sh(["go", "list", "-f", "{{.ImportPath}}|{{join .Imports \",\"}}", "./..."], root)
    except SystemExit:
        return ["go list -deps 失败，跳过依赖闭包校验"]

    prefix = data["sourceModule"]
    declared = {m["repo"]: set(m.get("dependsOn", [])) for m in data["modules"]}
    for line in raw.splitlines():
        if "|" not in line:
            continue
        imp, imports = line.split("|", 1)
        pkg = imp[len(prefix) + 1:] if imp.startswith(prefix + "/") else None
        if pkg is None or pkg not in owner:
            continue
        src_repo = owner[pkg]
        for dep in imports.split(","):
            dep = dep.strip()
            if not dep.startswith(prefix + "/"):
                continue
            dep_pkg = dep[len(prefix) + 1:]
            dst_repo = owner.get(dep_pkg)
            if dst_repo and dst_repo != src_repo and dst_repo not in declared[src_repo]:
                errs.append(f"{src_repo} 未在 dependsOn 声明对 {dst_repo} 的依赖（经由包 {pkg}）")
    return sorted(set(errs))


def check_layout(root: Path, layout: str, data: dict) -> list[str]:
    """拆分后布局校验：每个仓目录下必须有 go.mod，且 module 行与矩阵一致。"""
    errs: list[str] = []
    for mod in data["modules"]:
        d = root / mod["repo"]
        if not d.is_dir():
            errs.append(f"缺目录 {mod['repo']}/")
            continue
        gomod = d / "go.mod"
        if not gomod.exists():
            errs.append(f"{mod['repo']}/ 缺 go.mod")
            continue
        m = re.search(r"^module\s+(\S+)", gomod.read_text(encoding="utf-8"), re.M)
        want = data["importRoot"].replace("{org}", layout) + "/" + mod["repo"]
        if not m:
            errs.append(f"{mod['repo']}/go.mod 无 module 行")
        elif m.group(1) != want:
            errs.append(f"{mod['repo']}/go.mod module={m.group(1)}，应为 {want}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--layout", default="single", choices=["single", "split"],
                    help="single=单仓校验矩阵本身；split=多仓布局校验")
    ap.add_argument("--org", default=DEFAULT_ORG,
                    help=f"--layout split 时用于校验 module 行的组织名（默认 {DEFAULT_ORG}）")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    data = load(root)
    if args.layout == "split" and not args.org:
        print(f"错误：--layout split 需要 --org（默认 {DEFAULT_ORG}）", file=sys.stderr)
        return 2

    errs: list[str] = []
    errs += check_coverage(root, data)
    errs += check_tier_direction(data)
    errs += check_depends_closure(root, data)
    if args.layout == "split":
        errs += check_layout(root, args.org, data)

    pkgs = sum(len(m["packages"]) for m in data["modules"])
    repos = len(data["modules"])
    print(f"# 矩阵: {repos} 仓 / {pkgs} 包 / tier0-{max(m['tier'] for m in data['modules'])}")

    if errs:
        print(f"\n!! {len(errs)} 项校验失败：", file=sys.stderr)
        for e in errs:
            print(f"   - {e}", file=sys.stderr)
        return 1

    print("# 三项校验全绿：覆盖度 / 分层方向 / 依赖闭包")
    return 0


if __name__ == "__main__":
    sys.exit(main())
