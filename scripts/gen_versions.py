#!/usr/bin/env python3
"""从 GitHub tag 生成机器可读的版本矩阵 versions.json。

结构对齐 oekit 的 versions.json 惯例：每模块给 latest / packages / matrixDeps /
tier / breakingIndex。结构变更后重跑即可刷新，无需手工维护。

三处**曾经说谎**、现在由生成器兜住的地方：

1. **breakingIndex 曾整段是空的**（13 个模块全为 `[]`），而各仓 CHANGELOG 里
   明确标着 BREAKING 变更。矩阵说「无破坏性变更」、CHANGELOG 说「import 路径
   已变」——下游只能先构建断裂、再去翻 llms.txt 才知道怎么改。
   现在 breakingIndex 由各仓 CHANGELOG **自动抽取**（判定规则见 breaking_index.py）。

2. **packages[].importPath 曾登记变更前的旧路径**。这是矩阵作为机器可读源的
   底线：机器照着它写 import，不能是旧的。importPath 由 matrix_paths 的唯一规则
   生成，并由 verify_import_paths.py 与各仓**真实的 go.mod / 包目录**对账。

3. **latest 曾用「tags 数组的第一个」**。嵌套模块（guardx/audit 等）的 tag 与
   主模块的 tag 在同一个仓里，"第一个"是谁取决于 API 返回顺序——取错就是
   「最新版本 v0.1.0」。现在只认 `^v\\d+\\.\\d+\\.\\d+`。

用法:
    gen_versions.py                           # 读 modules.json，写 versions.json（不联网）
    gen_versions.py --org oagentkit           # 联网读各仓 tag，标注 latest
    gen_versions.py --root /path/to/oagentkit # 从本地各仓 CHANGELOG 抽 breakingIndex
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matrix_paths import (  # noqa: E402  import 路径构造规则与 rewrite_imports.py 同源
    OLD_ROOT,
    new_import_path,
    short_legacy,
)
import breaking_index  # noqa: E402  breakingIndex 的抽取规则本体
import gen_floor_bumps  # noqa: E402  floorBumps 的抽取规则本体（生成器层内部复用）
import repo_meta  # noqa: E402  远端取数（latest tag / CHANGELOG）
import verify_dep_floor  # noqa: E402  地板口径与白名单的唯一实现

DEFAULT_MATRIX = HERE.parent / "modules.json"
DEFAULT_OUT = HERE.parent / "versions.json"


def changelog_text(root: str | None, org: str | None, repo: str) -> str | None:
    """取某仓的 CHANGELOG 原文：本地优先，其次联网。取不到返回 None。"""
    if root:
        p = Path(root) / repo / "CHANGELOG.md"
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return None
    if org:
        return repo_meta.fetch_file(org, repo, "CHANGELOG.md")
    return None


def gomod_text(root: str | None, org: str | None, repo: str) -> str | None:
    """取某仓的 go.mod 原文（本地优先，其次联网）。取不到返回 None。"""
    if root:
        p = Path(root) / repo / "go.mod"
        return p.read_text(encoding="utf-8") if p.is_file() else None
    if org:
        return repo_meta.fetch_file(org, repo, "go.mod")
    return None


def dep_floor(root: str | None, org: str | None, repo: str) -> list[str]:
    """某仓的重量级第三方直接依赖（消费者 import 它任一包就会被拖进来的那些）。

    口径与门禁 verify_dep_floor.py **同源**（走它的 floor_of 唯一实现），
    不在这里另立一套——两份实现各写一遍，迟早各漂各的。
    """
    text = gomod_text(root, org, repo)
    if not text:
        return []
    return verify_dep_floor.floor_of(verify_dep_floor.requires(text))


def build_breaking_index(root: str | None, org: str | None, repo: str) -> list[dict]:
    """某模块的 breakingIndex（从 CHANGELOG 抽取；无 CHANGELOG 或取不到 → 空）。"""
    text = changelog_text(root, org, repo)
    if not text:
        return []
    return breaking_index.extract(text)


def floor_bumps_map(args, data: dict) -> tuple[dict[str, list[dict]], str]:
    """floorBumps 的来源：**能算就算**，算不出就**搬运**既有值。

    为什么要有「搬运」这一路：floorBumps 要读各仓的 **tag 历史**
    （相邻两个 tag 处的 go.mod 差异），而 gen_versions.py 的常规调用（`--org`）
    只读各仓的当前文件——算不出历史。此时若直接不写这个字段，
    `release.py sync-matrix` 重生成 versions.json 时就会**把它抹掉**
    （那是一条「矩阵源刷新」路径，抹掉即等于删登记）。

    所以：算得出就算；算不出就把既有值原样搬过去。
    真正的对账由 scripts/verify_floor_bumps.py 守着——它**独立重算事实**
    再与登记比对，搬运出来的陈旧值照样会被它抓出来。

    返回 (映射, 来源说明)。
    """
    repos = gen_floor_bumps.repo_list(data)
    if args.root:
        try:
            computed = gen_floor_bumps.compute_all(Path(args.root), None, repos)
            return computed, f"现算（--root {args.root}）"
        except Exception as e:  # 取不到就退回去搬，不静默
            note = f"现算失败（{e}），改为搬运既有值"
    else:
        note = "无 --root（算 tag 历史需要各仓克隆），搬运既有值"

    carried: dict[str, list[dict]] = {}
    src = Path(args.preserve_from) if args.preserve_from else None
    if args.no_preserve:
        note += "（--no-preserve：不搬运）"
        src = None
    if src and src.is_file():
        try:
            doc = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = {}
        for section in ("modules", "facades"):
            for entry in doc.get(section, []):
                repo = entry.get("repo")
                if repo and "floorBumps" in entry:
                    carried[repo] = entry["floorBumps"]
    return carried, note


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--org", help="给了就联网读各仓最新 tag")
    ap.add_argument("--root", help="各仓本地目录的父目录（用于读 CHANGELOG 抽 breakingIndex）")
    ap.add_argument("--preserve-from", default=str(DEFAULT_OUT),
                    help="搬运 floorBumps 的来源文件（默认：同目录的 versions.json）")
    ap.add_argument("--no-preserve", action="store_true",
                    help="不搬运既有 floorBumps（只保留现算结果）")
    args = ap.parse_args()

    data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = args.org or data.get("defaultOrg")

    out = {
        "$comment": "oagentkit 版本矩阵（机器可读）。由 matrix-tools/scripts/gen_versions.py 生成，勿手改。",
        "$pathRule": f"import 路径 = github.com/{org or '{org}'}/<仓名>/<包名末段>；"
                     f"旧路径 {OLD_ROOT}/<包> 的中间层目录会被丢弃。"
                     f"每个包的 fromLegacy 给出该包在旧单仓里的路径。"
                     f"权威输出：rewrite_imports.py --print-rules",
        "$breakingIndexRule": "由各仓 CHANGELOG.md 自动抽取（判定规则见 scripts/breaking_index.py）。"
                              "对账门禁：scripts/verify_breaking_index.py。",
        "$depFloorRule": "每仓的重量级第三方直接依赖（Go 的依赖面按 module 结算：import 本仓任一包，"
                         "本仓的整条 require 就进消费方的模块图）。"
                         "tier0/tier1 必须为空 —— 门禁：scripts/verify_dep_floor.py。",
        "$floorBumpsRule": "「依赖地板抬高」= 相邻两个 tag 之间 go.mod 的 require 版本抬高"
                           "（或新增依赖）。对下游是**隐式破坏性变更**：不跑 go mod tidy "
                           "就构建红（go: updates to go.mod needed）。"
                           "判据是结构性的（go.mod 差异），与提交说明的措辞无关——"
                           "不并进 breakingIndex：那里存的是迁移说明，"
                           "而地板抬高的修法是「跑个 tidy」，不是迁移。"
                           "生成器：scripts/gen_floor_bumps.py；"
                           "对账门禁：scripts/verify_floor_bumps.py。",
        "org": org,
        "orgUrl": f"https://github.com/{org}" if org else None,
        "sourceMatrix": data.get("sourceModule"),
        "tierCount": len({m["tier"] for m in data["modules"]}),
        "moduleCount": len(data["modules"]),
        "packageCount": sum(len(m["packages"]) for m in data["modules"]),
        "modules": [],
    }

    missing_changelog: list[str] = []
    for m in data["modules"]:
        repo = m["repo"]
        nested = m.get("nestedModules", [])
        # 嵌套模块提供的包：它们的版本线独立于本仓
        nested_owner = {n["path"]: n["module"] for n in nested}

        packages = []
        for p in m["packages"]:
            entry = {
                "pkg": p,
                "importPath": new_import_path(org, repo, p) if org else None,
                # 旧单仓里的包路径，供工具做机器可读的旧→新替换，不必解析脚本源码
                "fromLegacy": short_legacy(p),
            }
            # 由嵌套模块提供的包：版本线在那个模块上，机器必须知道去哪儿取
            if p in nested_owner:
                entry["module"] = nested_owner[p]
            packages.append(entry)

        mod = {
            "repo": repo,
            "url": f"https://github.com/{org}/{repo}" if org else None,
            "tier": m["tier"],
            "latest": repo_meta.latest_tag(org, repo) if (args.org and org) else None,
            "packages": packages,
            "matrixDeps": [
                {"repo": d, "url": f"https://github.com/{org}/{d}" if org else None}
                for d in sorted(m.get("dependsOn", []))
            ],
            "breakingIndex": build_breaking_index(args.root, args.org if args.org else org, repo),
            # 地板：import 本仓任一包会被拖进消费方模块图的重量级第三方依赖
            "depFloor": dep_floor(args.root, args.org if args.org else org, repo),
        }
        if nested:
            mod["nestedModules"] = [
                {
                    "module": n["module"],
                    "url": f"https://github.com/{org}/{repo}" if org else None,
                    "latest": (repo_meta.nested_tag(org, repo, n["path"] + "/")
                               if (args.org and org) else None),
                    "packages": [{"pkg": n["path"], "importPath": n["module"]}],
                }
                for n in nested
            ]
        if not mod["breakingIndex"] and not changelog_text(args.root, args.org if args.org else org, repo):
            missing_changelog.append(repo)
        out["modules"].append(mod)

    # 门面仓（infraRepos 里标了 goModule 的）：不进 modules 列表——它 v1.x 后退役，
    # 不是矩阵的长期成员——但它**自己也在发版、也有破坏性变更**（v1.8.0 起
    # re-export 地板跟到各模块新版）。消费方从门面装走的到底是哪一版，
    # 机器读得到才算数，所以单列一节，门禁也一并覆盖。
    facades = []
    for r in data.get("infraRepos", []):
        if not r.get("goModule"):
            continue
        repo = r["repo"]
        facades.append({
            "repo": repo,
            "url": f"https://github.com/{org}/{repo}" if org else None,
            "latest": repo_meta.latest_tag(org, repo) if (args.org and org) else None,
            "retiring": True,
            "breakingIndex": build_breaking_index(args.root, args.org if args.org else org, repo),
            # 地板：import 本仓任一包会被拖进消费方模块图的重量级第三方依赖
            "depFloor": dep_floor(args.root, args.org if args.org else org, repo),
        })
        if not facades[-1]["breakingIndex"] and not changelog_text(
                args.root, args.org if args.org else org, repo):
            missing_changelog.append(repo)
    if facades:
        out["facades"] = facades

    # floorBumps：能算就算，算不出就搬运既有值（见 floor_bumps_map 的说明）
    bumps, bumps_note = floor_bumps_map(args, data)
    n_bump_versions = 0
    for section in ("modules", "facades"):
        for entry in out.get(section, []):
            repo = entry.get("repo")
            if repo in bumps:
                entry["floorBumps"] = bumps[repo]
                n_bump_versions += len(bumps[repo])

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    n_break = sum(len(m["breakingIndex"]) for m in out["modules"])
    n_break += sum(len(f["breakingIndex"]) for f in facades)
    print(f"# 生成 {args.out}：{out['moduleCount']} 模块 / {out['packageCount']} 包 / "
          f"tier0-{out['tierCount']-1} / breakingIndex {n_break} 条")
    print(f"# floorBumps {n_bump_versions} 个版本有抬地板（来源：{bumps_note}）")
    if missing_changelog:
        print(f"# ⚠️ {len(missing_changelog)} 个模块没读到 CHANGELOG（breakingIndex 只能是空的）："
              f"{', '.join(missing_changelog)}")
        print(f"#   给 --root <各仓父目录> 或 --org {org} 重跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
