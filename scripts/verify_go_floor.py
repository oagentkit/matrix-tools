#!/usr/bin/env python3
"""Go 地板门禁：tier0 必须是 1.24.x，且 CI go-version 必须与 go.mod 的 go 指令成对。

## 为什么要有这条

Go 的 `go` 指令是**真正的地板**：它决定消费方最低能用哪个 Go 编译你的模块。
下游 `go get` 你的包时，如果你的 go.mod 写着 `go 1.26.0`，那用 Go 1.24 的用户
**直接构建失败**——`go: module requires go >= 1.26.0`。

`toolchain` 行**不是**地板（编译器实证）：`go 1.24.0` + `toolchain go1.28.0`
在 `GOTOOLCHAIN=local` 下可以通过；真正被拒的是 `go 1.28.0`。
所以门槛只看 `go` 指令，`toolchain` 只作为「开发时用哪个工具链」的提示。

实测（2026-10-20，本矩阵）：textx / guardx / runtime 三个 **tier0 基础包**声明
`go 1.26.0`，但它们的依赖链上只有 `oekit/pkg` 与 `oekit/concurrency`
（两者都是 `go 1.24.0`），代码本身也完全兼容 1.24——**地板被无谓抬高了**。
基础包抬高地板 = 把「用 Go 1.24 也能用的轻量能力」挡在 Go 1.26 之外。

口径依据（负责人裁决 2026-10-20）：
「基础包需要依赖低版本的 go1.24，业务包依赖 1.24 / 1.25 / 1.26 都行。」

## 判据（三条，逐条独立）

1. **tier0 的 go 指令必须是 1.24.x**
   tier0 是「需要依赖低版本的」那一类。它的依赖链必须都是 1.24 兼容的，
   自己声明 1.25+ 就是无谓抬高。tier1 及以上**不设此约束**——
   业务层用 1.24 / 1.25 / 1.26 都合法，不能把合规仓报成漂移。

2. **CI go-version 必须与 go.mod 的 go 指令成对**
   两者是同一个承诺的两种表达。CI 用 1.26 构建、go.mod 写 1.24 时，
   CI 全绿但用户按 1.24 编译失败——**门禁绿了，承诺没兑现**。
   这条对**所有** tier 生效（含 tier1+），因为它是「成对」而不是「地板高低」。

3. **tier0 不得有 toolchain 行高于 1.24 的提示**
   `toolchain` 不构成门槛，但 `toolchain go1.27.2` 会让 `go` 命令**自动下载**
   1.27.2，与「最低支持版本是 1.24」的精神相悖。tier0 允许无 toolchain 行或
   `toolchain go1.24.x`；显式写了更高的 → 红。

## 判据独立性（重要）

本脚本**直接解析 `go.mod` 文本与 workflow YAML 文本**，
不 import `gen_versions.py` / `verify_dep_floor.py` 等任何生成器或守卫，
不复用它们的推导逻辑。原因：门禁与被检对象共享判断逻辑就会有共同盲区——
共享就有一起漏的时候，门禁全绿而承诺没兑现。

tier 归属直接读 `modules.json`（这是矩阵的**数据源**，不是生成物）；
go 指令与 CI go-version 都从各仓的原始文件读。

## 用法

    verify_go_floor.py --root /path/to/oagentkit   # 读本地各仓 go.mod + ci.yml
    verify_go_floor.py --org oagentkit             # 联网读各仓 go.mod + ci.yml
    verify_go_floor.py --selftest                  # 离线自测

退出码: 0 = 合规；1 = 有漂移 / 判不了（判不了即红）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"

# tier0 允许的 go 指令版本前缀（1.24.x）
TIER0_GO_PREFIX = "1.24."

# tier0 允许的 toolchain 版本前缀（1.24.x）；不写 toolchain 行也算合规
TIER0_TOOLCHAIN_PREFIX = "1.24."

# CI go-version 与 go.mod go 指令的比较口径：
# CI 写 '1.24'，go.mod 写 '1.24.0' → 视为成对（CI 的 '1.24' 会被 setup-go
# 解析为最新 1.24.x，与 go.mod 的 1.24.0 同属 1.24 系列）。
# CI 写 '1.26'、go.mod 写 '1.24.0' → 不成对。
_GO_VER_RE = re.compile(r"^go\s+(\S+)\s*$", re.M)
_TOOLCHAIN_RE = re.compile(r"^toolchain\s+go(\S+)\s*$", re.M)
_CI_GO_VERSION_RE = re.compile(r"go-version:\s*['\"]?([^'\"\s]+)['\"]?")


def ver_tuple(v: str) -> tuple[int, ...]:
    """'1.24.0' -> (1, 24, 0)；'1.24' -> (1, 24)。非数字段忽略。"""
    out = []
    for part in v.split("."):
        digits = re.match(r"^(\d+)", part)
        if not digits:
            break
        out.append(int(digits.group(1)))
    return tuple(out)


def parse_go_directive(gomod_text: str) -> tuple[str | None, str | None]:
    """→ (go 指令版本, toolchain 版本)。取不到返回 (None, None)。"""
    m = _GO_VER_RE.search(gomod_text)
    go = m.group(1) if m else None
    t = _TOOLCHAIN_RE.search(gomod_text)
    toolchain = t.group(1) if t else None
    return go, toolchain


def parse_ci_go_version(workflow_text: str) -> str | None:
    """从 ci.yml 薄调用层里取 go-version 值。

    只认 `with:` 段下的 go-version——共享 workflow 本体的默认值不算，
    那是「没传时的兜底」，不是该仓的承诺。
    """
    m = _CI_GO_VERSION_RE.search(workflow_text)
    return m.group(1) if m else None


def is_tier0(tier) -> bool:
    return str(tier) == "0"


def check_repo(repo: str, tier, go_version: str | None, toolchain: str | None,
               ci_go_version: str | None) -> list[str]:
    """→ 问题描述列表（空 = 合规）"""
    problems: list[str] = []

    if go_version is None:
        problems.append(f"{repo}: go.mod 里读不到 go 指令（判不了，不能当成合规）")
        return problems

    # 判据 1 + 3：tier0 的地板与 toolchain
    if is_tier0(tier):
        if not go_version.startswith(TIER0_GO_PREFIX):
            problems.append(
                f"{repo}: tier0 基础包的 go 指令是 {go_version}，必须是 {TIER0_GO_PREFIX}x"
                f"（tier0 是「需要依赖低版本 Go」的那一类，抬高地板会挡掉 Go 1.24 用户）")
        if toolchain is not None and not toolchain.startswith(TIER0_TOOLCHAIN_PREFIX):
            problems.append(
                f"{repo}: tier0 的 toolchain 是 go{toolchain}，"
                f"最高只能 go{TIER0_TOOLCHAIN_PREFIX}x"
                f"（toolchain 不是门槛，但会让 go 命令自动下载更高版本，"
                f"与「最低支持 1.24」相悖）")

    # 判据 2：CI go-version 与 go.mod go 指令成对（所有 tier）
    if ci_go_version is None:
        problems.append(
            f"{repo}: ci.yml 里读不到 go-version（判不了，"
            f"不能当成「成对」）")
    else:
        ci_tuple = ver_tuple(ci_go_version)
        go_tuple = ver_tuple(go_version)
        # 只比 major.minor：CI 的 '1.24' 会被 setup-go 解析为最新 1.24.x
        if ci_tuple[:2] != go_tuple[:2]:
            problems.append(
                f"{repo}: CI go-version 是 {ci_go_version}，"
                f"但 go.mod 的 go 指令是 {go_version}——两者必须成对"
                f"（CI 用高版本构建、go.mod 承诺低版本时，CI 全绿但"
                f"用户按最低版本编译失败）")

    return problems


def matrix_tiers(matrix_path: Path) -> dict[str, int]:
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    return {m["repo"]: m.get("tier") for m in data.get("modules", [])}


def local_facts(root: Path, repo: str) -> tuple[str | None, str | None, str | None]:
    """→ (go.mod 文本, ci.yml 文本, 错误信息)。任一取不到 → (None, None, err)。"""
    base = root / repo
    if not base.is_dir():
        return None, None, f"{repo}: 仓目录不存在"
    gomod = base / "go.mod"
    if not gomod.is_file():
        return None, None, f"{repo}: 没有 go.mod"
    ci = base / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        return None, None, f"{repo}: 没有 .github/workflows/ci.yml"
    try:
        return (gomod.read_text(encoding="utf-8"),
                ci.read_text(encoding="utf-8"),
                None)
    except OSError as e:
        return None, None, f"{repo}: 读文件失败：{e}"


def remote_facts(org: str, repo: str) -> tuple[str | None, str | None, str | None]:
    """联网取各仓 go.mod 与 ci.yml。取不到 → (None, None, err)。"""
    gomod = repo_meta.fetch_file(org, repo, "go.mod")
    if gomod is None:
        return None, None, f"{repo}: go.mod 取不到（判不了）"
    ci = repo_meta.fetch_file(org, repo, ".github/workflows/ci.yml")
    if ci is None:
        return None, None, f"{repo}: .github/workflows/ci.yml 取不到（判不了）"
    return gomod, ci, None


def main() -> int:
    ap = argparse.ArgumentParser(description="Go 地板门禁：tier0 必须是 1.24.x，CI go-version 与 go.mod 成对")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--org", help="联网读各仓 go.mod / ci.yml")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root（本地各仓目录的父目录）或 --org（联网读）")
        return 1

    tiers = matrix_tiers(Path(args.matrix))
    if not tiers:
        print("❌ modules.json 里没有模块（拒绝判定）")
        return 1

    org = args.org or json.loads(Path(args.matrix).read_text(encoding="utf-8")).get("defaultOrg")

    problems: list[str] = []
    checked = 0
    for repo, tier in sorted(tiers.items()):
        if args.root:
            gomod_text, ci_text, err = local_facts(Path(args.root), repo)
        else:
            gomod_text, ci_text, err = remote_facts(org, repo)
        if err:
            problems.append(err)
            continue
        go_version, toolchain = parse_go_directive(gomod_text)
        ci_go_version = parse_ci_go_version(ci_text)
        problems.extend(check_repo(repo, tier, go_version, toolchain, ci_go_version))
        checked += 1

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    if problems:
        print(f"❌ Go 地板门禁失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：tier0 的 go.mod 写 `go 1.24.0`（toolchain 留空或 ≤1.24），"
              "并把该仓 ci.yml 的 go-version 改成与 go.mod 成对的值。")
        return 1

    t0 = [r for r, t in sorted(tiers.items()) if is_tier0(t)]
    print(f"✅ Go 地板合规（检查 {checked} 个仓；tier0 = {', '.join(t0)} 均声明 "
          f"{TIER0_GO_PREFIX}x，CI go-version 与 go.mod 全部成对）")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # tier0 合规：go 1.24.0 + CI 1.24
    check("tier0 合规", check_repo("x", 0, "1.24.0", None, "1.24"), [])
    # tier0 无 toolchain 行也算合规
    check("tier0 无 toolchain", check_repo("x", 0, "1.24.0", None, "1.24"), [])
    # tier0 toolchain 1.24.x 合规
    check("tier0 toolchain 1.24", check_repo("x", 0, "1.24.0", "1.24.3", "1.24"), [])
    # tier0 go 1.26 → 红（CI 也给 1.26，隔离出「地板抬高」这一条）
    p = check_repo("x", 0, "1.26.0", None, "1.26")
    check("tier0 go 1.26 被抓", len(p), 1)
    check("tier0 go 1.26 指名道姓", "x" in p[0] and "1.26.0" in p[0], True)
    # tier0 go 1.26 + CI 1.24 → 两条判据同时命中
    check("tier0 地板抬高+不成对=2条", len(check_repo("x", 0, "1.26.0", None, "1.24")), 2)
    # tier0 toolchain 1.27 → 红
    p = check_repo("x", 0, "1.24.0", "1.27.2", "1.24")
    check("tier0 toolchain 1.27 被抓", len(p), 1)
    # tier1 及以上：go 1.26 不报（业务层合法）
    check("tier1 go 1.26 不报", check_repo("x", 1, "1.26.0", "go1.27.2", "1.26"), [])
    check("tier2 go 1.26 不报", check_repo("x", 2, "1.26.0", "go1.27.2", "1.26"), [])
    check("tier3 go 1.26 不报", check_repo("x", 3, "1.26.0", "go1.27.2", "1.26"), [])
    # tier1 及以上：CI 与 go.mod 不成对仍要红（成对纪律对所有 tier 生效）
    p = check_repo("x", 1, "1.26.0", None, "1.24")
    check("tier1 CI 不成对被抓", len(p), 1)
    p = check_repo("x", 2, "1.24.0", None, "1.26")
    check("tier2 CI 不成对被抓", len(p), 1)
    # go 指令读不到 → 判不了即红
    check("go 指令缺失即红", len(check_repo("x", 0, None, None, "1.24")), 1)
    # CI go-version 读不到 → 判不了即红
    check("CI 缺失即红", len(check_repo("x", 0, "1.24.0", None, None)), 1)

    # 解析层
    check("解析 go 指令", parse_go_directive("module x\n\ngo 1.24.0\n\nrequire a b\n"),
          ("1.24.0", None))
    check("解析 toolchain",
          parse_go_directive("module x\n\ngo 1.24.0\n\ntoolchain go1.27.2\n"),
          ("1.24.0", "1.27.2"))
    check("解析 CI go-version",
          parse_ci_go_version("jobs:\n  ci:\n    with:\n      go-version: '1.24'\n"),
          "1.24")
    check("解析 CI go-version（双引号）",
          parse_ci_go_version("      go-version: \"1.26\"\n"), "1.26")
    check("版本元组", ver_tuple("1.24.0"), (1, 24, 0))
    check("版本元组（两段）", ver_tuple("1.24"), (1, 24))

    if fails:
        print("❌ verify_go_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_go_floor 自测通过（tier0 地板 1.24.x / tier1+ 不误报 / "
          "CI 与 go.mod 成对对所有 tier 生效 / 判不了即红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
