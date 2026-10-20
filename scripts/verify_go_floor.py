#!/usr/bin/env python3
"""Go 地板门禁：tier0 必须是 1.24.x，tier1+ 不得「依赖链全兼容却声明更高」，
且 CI go-version 必须与 go.mod 的 go 指令成对。

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
顺带扫出的 tier1+ 结论见 matrix-tools#34：7 个仓同样可降，3 个因三方硬要求不可降。

口径依据（负责人裁决 2026-10-20）：
「基础包需要依赖低版本的 go1.24，业务包依赖 1.24 / 1.25 / 1.26 都行。」

## 判据（四条，逐条独立）

1. **tier0 的 go 指令必须是 1.24.x**
   tier0 是「需要依赖低版本的」那一类。它的依赖链必须都是 1.24 兼容的，
   自己声明 1.25+ 就是无谓抬高。

2. **CI go-version 必须与 go.mod 的 go 指令成对**
   两者是同一个承诺的两种表达。CI 用 1.26 构建、go.mod 写 1.24 时，
   CI 全绿但用户按 1.24 编译失败——**门禁绿了，承诺没兑现**。
   这条对**所有** tier 生效（含 tier1+），因为它是「成对」而不是「地板高低」。

3. **tier0 不得有 toolchain 行高于 1.24 的提示**
   `toolchain` 不构成门槛，但 `toolchain go1.27.2` 会让 `go` 命令**自动下载**
   1.27.2，与「最低支持版本是 1.24」的精神相悖。tier0 允许无 toolchain 行或
   `toolchain go1.24.x`；显式写了更高的 → 红。

4. **tier1+ 不得「依赖链全兼容却声明更高」**
   tier1+ 不设**绝对**地板（业务层 1.24/1.25/1.26 都合法），
   但**相对**地板是硬要求：如果本仓依赖链上**每一个**依赖的 go 指令都
   ≤ 1.24.x，而本仓自己声明 1.25+，那这个更高地板**没有任何依赖需要它**
   ——是无谓抬高，必须降。

   判据用 `go list -m -json all` 读每个依赖的 `GoVersion`
   ——那是 **Go 工具链自己**算出来的值，不是任何生成器的推导。

   例外：**不可降三方硬要求**的仓走 `EXEMPTIONS` 显式豁免（见下）。

## 豁免清单（必须带依据）

`EXEMPTIONS` 登记「依赖链上确有要求更高版本的三方包」的仓。
每条必须写明：哪个三方包、它要求哪个 go 版本、我们实际 import 了它的什么。

**豁免必须显式列举且带依据字段**——只列仓名的话，下次有人会以为那三个仓
地板有问题而硬降，然后构建断裂。

### 如何撤掉豁免（上游放宽后）

1. 上游放宽后，先在本仓跑 `GOTOOLCHAIN=go1.24.0 go build ./...`；
   仍失败 → 上游**没有**放宽，保持豁免。
2. 通过 → 改本仓 go.mod 的 go 指令为 1.24.0 + CI go-version 1.24，
   **并从 EXEMPTIONS 里删掉这一条**。
3. 跑 `python3 scripts/verify_go_floor.py --selftest` 确认豁免清单自洽，
   再跑 `--org oagentkit` 确认零误报。
4. **豁免条目不得「因超时」长期挂在那里**：每条豁免都带 `since` 字段，
   便于定期复核（上游可能已经放宽了但没人回来撤）。

## 判据独立性（重要）

本脚本**直接解析 `go.mod` 文本与 workflow YAML 文本**，
依赖链的 go 版本用 `go list -m -json all`（Go 工具链自己的输出），
**不 import** `gen_versions.py` / `verify_dep_floor.py` 等任何生成器或守卫，
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
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"

# tier0 允许的 go 指令版本前缀（1.24.x）
TIER0_GO_PREFIX = "1.24."

# tier0 允许的 toolchain 版本前缀（1.24.x）；不写 toolchain 行也算合规
TIER0_TOOLCHAIN_PREFIX = "1.24."

# tier1+ 的「相对地板」判据：依赖链上每个依赖的 go 指令都 ≤ 这个版本时，
# 本仓自己声明更高就是无谓抬高。这个前缀是本矩阵的**目标地板**。
TARGET_GO_PREFIX = "1.24."

# ---------------------------------------------------------------------------
# 豁免清单：依赖链上确有要求更高版本的三方包的仓。
#
# 每条必须写明：哪个三方包、它要求哪个 go 版本、我们实际 import 了什么。
# **豁免必须带依据字段**——只列仓名的话，下次有人会以为那三个仓地板有问题
# 而硬降，然后构建断裂。
#
# 撤销方式见本文件头部的「如何撤掉豁免」一节。
# ---------------------------------------------------------------------------
EXEMPTIONS: dict[str, dict] = {
    "mcpkit": {
        "blocker": "github.com/mark3labs/mcp-go",
        "blocker_version": "v1.1.1",
        "required_go": "1.25.5",
        "imports": "mcp-go/client、mcp-go/client/transport、mcp-go/mcp、mcp-go/server",
        "since": "2026-10-20",
        "note": "MCP 协议客户端/服务端实现。降级需上游放宽 go 指令或换版本。",
    },
    "toolgate": {
        "blocker": "github.com/oagentkit/mcpkit",
        "blocker_version": "v0.8.0",
        "required_go": "1.25.5",
        "imports": "间接依赖 mcpkit → mcp-go v1.1.1",
        "since": "2026-10-20",
        "note": "无直接三方阻塞；地板由 mcpkit 传导。mcpkit 降级后本条即可撤。",
    },
    "tools": {
        "blocker": "mvdan.cc/sh/v3",
        "blocker_version": "v3.14.1",
        "required_go": "1.26.0",
        "imports": "mvdan.cc/sh/v3/syntax",
        "since": "2026-10-20",
        "note": "真 shell AST 解析（只读性判定）。降级需上游放宽 go 指令或换版本。",
    },
}

# CI go-version 与 go.mod go 指令的比较口径：
# CI 写 '1.24'，go.mod 写 '1.24.0' → 视为成对（CI 的 '1.24' 会被 setup-go
# 解析为最新 1.24.x，与 go.mod 的 1.24.0 同属 1.24 系列）。
# CI 写 '1.26'、go.mod 写 '1.24.0' → 不成对。
_GO_VER_RE = re.compile(r"^go\s+(\S+)\s*$", re.M)
_TOOLCHAIN_RE = re.compile(r"^toolchain\s+go(\S+)\s*$", re.M)
_CI_GO_VERSION_RE = re.compile(r"go-version:\s*['\"]?([^'\"\s]+)['\"]?")
# require 行的两种形态：
#   单行： require github.com/oekit/pkg v0.16.0
#   块内： \tgithub.com/oekit/pkg v0.16.1 // indirect
# 两者的共同点是「行内第一个 token 是模块路径、第二个是版本」，
# 且块内的行以空白开头、单行 require 以 "require " 开头。
_REQUIRE_LINE_RE = re.compile(
    r"^(?:require\s+|\s+)([a-z0-9][^\s]*\.[^\s]*/[^\s]+)\s+(v[^\s]+)", re.M)


def parse_requires(gomod_text: str) -> dict[str, str]:
    """go.mod 的 require → {模块路径: 版本}。

    覆盖两种形态：单行 `require X vY` 与 require 块内的 `\tX vY // indirect`。
    块内的 indirect 依赖同样参与 MVS、同样有 go 指令要求，必须计入。
    """
    out: dict[str, str] = {}
    for mod, ver in _REQUIRE_LINE_RE.findall(gomod_text):
        out[mod] = ver
    return out


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


def dep_go_versions(repo_dir: Path) -> tuple[dict[str, str] | None, str | None]:
    """→ ({依赖路径: go 指令}, 错误信息)。

    用 `go list -m -json all` 读每个依赖的 `GoVersion`——
    那是 **Go 工具链自己**算出来的值，不是任何生成器的推导。
    读不到 → (None, err)，调用方必须判红（判不了 ≠ 合规）。
    """
    env = {
        **os.environ,
        "GOWORK": "off",
        "GOPRIVATE": "github.com/oagentkit/*,github.com/oekit/*",
        "GONOPROXY": "github.com/oagentkit/*,github.com/oekit/*",
        "GONOSUMDB": "github.com/oagentkit/*,github.com/oekit/*",
    }
    try:
        r = subprocess.run(
            ["go", "list", "-m", "-json", "all"],
            capture_output=True, text=True, cwd=repo_dir, env=env, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{repo_dir.name}: go list -m 失败：{e}"
    if r.returncode != 0:
        return None, f"{repo_dir.name}: go list -m 退出码 {r.returncode}（判不了）"
    out: dict[str, str] = {}
    dec = json.JSONDecoder()
    buf, i = r.stdout, 0
    while i < len(buf):
        while i < len(buf) and buf[i] in " \n\t":
            i += 1
        if i >= len(buf):
            break
        try:
            obj, i = dec.raw_decode(buf, i)
        except ValueError:
            break
        path, gover = obj.get("Path"), obj.get("GoVersion")
        # 跳过主模块自身（Path == 仓名、Version 为 None）
        if path and gover and obj.get("Version"):
            out[path] = gover
    if not out:
        return None, f"{repo_dir.name}: go list -m 没给出任何依赖的 go 指令（判不了）"
    return out, None


def max_dep_go(deps: dict[str, str]) -> tuple[str, str] | None:
    """→ (要求最高 go 指令的依赖路径, 它的 go 指令)。无依赖返回 None。"""
    best = None
    for path, gover in deps.items():
        if best is None or ver_tuple(gover) > ver_tuple(best[1]):
            best = (path, gover)
    return best


def check_relative_floor(repo: str, tier, go_version: str | None,
                         deps: dict[str, str] | None) -> list[str]:
    """判据 4：tier1+ 不得「依赖链全兼容却声明更高」。

    - 依赖链上**每个**依赖的 go 指令都 ≤ 1.24.x，
      而本仓自己声明 > 1.24.x → 无谓抬高 → 红。
    - 在 EXEMPTIONS 里的仓 → 豁免（但要确认豁免依据仍然成立）。
    - 依赖链取不到 → 判不了即红。
    """
    problems: list[str] = []
    if go_version is None:
        return problems  # 判据 1/4 共用的「读不到 go 指令」已由调用方报过

    if is_tier0(tier):
        return problems  # tier0 由判据 1 绝对约束，不重复

    # 已登记豁免的仓：检查豁免依据是否仍然成立
    if repo in EXEMPTIONS:
        ex = EXEMPTIONS[repo]
        blocker = ex["blocker"]
        required = ex["required_go"]
        actual = deps.get(blocker) if deps else None
        if deps is None:
            problems.append(
                f"{repo}: 豁免登记中（{blocker} 要求 go {required}），"
                f"但依赖链读不到（判不了）")
        elif actual is None:
            problems.append(
                f"{repo}: 豁免依据已失效——{blocker} 已不在依赖链上"
                f"（豁免登记说它要求 go {required}，但现在查不到它）。"
                f"请撤掉这条豁免并把本仓地板降到 {TARGET_GO_PREFIX}x")
        elif ver_tuple(actual) < ver_tuple(required):
            # 上游放宽了：实际要求 < 登记的 required_go ⇒ 豁免不再必要
            problems.append(
                f"{repo}: 豁免依据已放宽——{blocker} 现在只要求 go {actual}"
                f"（豁免登记的是 {required}）。"
                f"请撤掉这条豁免并把本仓地板降到 {TARGET_GO_PREFIX}x")
        return problems

    if deps is None:
        problems.append(f"{repo}: 依赖链的 go 指令读不到（判不了，不能当成合规）")
        return problems

    if go_version.startswith(TARGET_GO_PREFIX):
        return problems  # 本仓已经是 1.24.x，无可指摘

    highest = max_dep_go(deps)
    if highest is None:
        problems.append(
            f"{repo}: go 指令是 {go_version}，但读不到任何依赖的 go 指令"
            f"（判不了，不能当成合规）")
        return problems

    dep_path, dep_go = highest
    if ver_tuple(dep_go) <= ver_tuple(TARGET_GO_PREFIX + "0"):
        problems.append(
            f"{repo}: go 指令是 {go_version}，但依赖链上最高的 go 指令只有 "
            f"{dep_go}（{dep_path}）——没有任何依赖要求更高版本，"
            f"这个地板是无谓抬高的，应降到 {TARGET_GO_PREFIX}x")
    return problems


def check_repo(repo: str, tier, go_version: str | None, toolchain: str | None,
               ci_go_version: str | None,
               deps: dict[str, str] | None = None) -> list[str]:
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

    # 判据 4：tier1+ 不得「依赖链全兼容却声明更高」（含豁免依据复核）
    problems.extend(check_relative_floor(repo, tier, go_version, deps))

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


def remote_dep_go_versions(org: str, repo: str,
                            gomod_text: str) -> tuple[dict[str, str] | None, str | None]:
    """联网取某仓依赖链上每个依赖的 go 指令。

    直接读各依赖模块的 go.mod（`proxy.golang.org/<mod>/@v/<ver>.mod` 或
    `repo_meta` 抓第一方仓的 go.mod），**不走本地 go list**——
    CI 里没有各仓的本地副本。

    读不到某个依赖的 go 指令 → 该依赖不计入「全兼容」判定
    （保守：只把**确认 ≤ 1.24** 的依赖算作兼容），
    但「一条都读不到」要判红（判不了 ≠ 合规）。
    """
    deps: dict[str, str] = {}
    for mod, ver in parse_requires(gomod_text).items():
        gover = remote_module_go_version(org, mod, ver)
        if gover:
            deps[mod] = gover
    if not deps:
        return None, f"{repo}: 依赖链的 go 指令一条都读不到（判不了）"
    return deps, None


def remote_module_go_version(org: str, mod: str, ver: str) -> str | None:
    """某依赖模块声明的 go 指令。读不到返回 None。"""
    # 第一方仓：直接抓它的 go.mod。
    # ⚠️ 基座 org 是 oekit（不是当前 org）——不能拿 org 去拼它的仓名。
    for prefix, dep_org in (("github.com/oagentkit/", org),
                             ("github.com/oekit/", "oekit")):
        if mod.startswith(prefix):
            name = mod[len(prefix):]
            text = repo_meta.fetch_file(dep_org, name, "go.mod")
            if text:
                go, _ = parse_go_directive(text)
                return go
            return None
    # 三方模块：走 Go module proxy 的 .mod 端点
    url = f"https://proxy.golang.org/{mod}/@v/{ver}.mod"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    go, _ = parse_go_directive(text)
    return go


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Go 地板门禁：tier0 必须 1.24.x，tier1+ 不得「依赖链全兼容却声明更高」，"
                    "CI go-version 与 go.mod 成对")
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
        # 判据 4 需要依赖链的 go 指令
        deps: dict[str, str] | None = None
        if args.root:
            deps, derr = dep_go_versions(Path(args.root) / repo)
        else:
            deps, derr = remote_dep_go_versions(org, repo, gomod_text)
        if derr and repo not in EXEMPTIONS:
            problems.append(derr)
        problems.extend(check_repo(repo, tier, go_version, toolchain,
                                   ci_go_version, deps))
        checked += 1

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    if problems:
        print(f"❌ Go 地板门禁失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：tier0 的 go.mod 写 `go 1.24.0`（toolchain 留空或 ≤1.24）；"
              "tier1+ 若依赖链全兼容也应降到 1.24.0；"
              "并把该仓 ci.yml 的 go-version 改成与 go.mod 成对的值。")
        return 1

    t0 = [r for r, t in sorted(tiers.items()) if is_tier0(t)]
    ex = [r for r in sorted(EXEMPTIONS) if r in tiers]
    print(f"✅ Go 地板合规（检查 {checked} 个仓；tier0 = {', '.join(t0)} 均声明 "
          f"{TIER0_GO_PREFIX}x；豁免 {len(ex)} 个（三方硬要求）：{', '.join(ex) or '无'}；"
          f"CI go-version 与 go.mod 全部成对）")
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
    # tier1 及以上：go 1.26 不报（业务层合法——前提是依赖链确有要求更高的包）
    dep_high = {"github.com/oekit/pkg": "1.24.0", "github.com/x/y": "1.26.0"}
    check("tier1 go 1.26 不报",
          check_repo("x", 1, "1.26.0", "go1.27.2", "1.26", dep_high), [])
    check("tier2 go 1.26 不报",
          check_repo("x", 2, "1.26.0", "go1.27.2", "1.26", dep_high), [])
    check("tier3 go 1.26 不报",
          check_repo("x", 3, "1.26.0", "go1.27.2", "1.26", dep_high), [])
    # tier1 及以上：CI 与 go.mod 不成对仍要红（成对纪律对所有 tier 生效）
    p = check_repo("x", 1, "1.26.0", None, "1.24", dep_high)
    check("tier1 CI 不成对被抓", len(p), 1)
    p = check_repo("x", 2, "1.24.0", None, "1.26", dep_high)
    check("tier2 CI 不成对被抓", len(p), 1)
    # go 指令读不到 → 判不了即红
    check("go 指令缺失即红", len(check_repo("x", 0, None, None, "1.24")), 1)
    # CI go-version 读不到 → 判不了即红
    check("CI 缺失即红", len(check_repo("x", 0, "1.24.0", None, None)), 1)

    # ---- 判据 4：tier1+ 不得「依赖链全兼容却声明更高」 ----
    all124 = {"github.com/oekit/pkg": "1.24.0", "github.com/oekit/concurrency": "1.24.0"}
    # 依赖链全 1.24、自己声明 1.26 → 红
    p = check_relative_floor("x", 2, "1.26.0", all124)
    check("tier1+ 依赖链全兼容却声明更高被抓", len(p), 1)
    check("判据4 指名道姓", "x" in p[0] and "1.26.0" in p[0], True)
    # 依赖链全 1.24、自己声明 1.24 → 绿
    check("tier1+ 已是 1.24 不报", check_relative_floor("x", 2, "1.24.0", all124), [])
    # 依赖链有要求 1.25.5 的 → 本仓声明 1.26 是有依据的 → 绿
    check("tier1+ 依赖要求更高则不报",
          check_relative_floor("x", 2, "1.26.0",
                               {**all124, "github.com/mark3labs/mcp-go": "1.25.5"}), [])
    # 依赖链读不到 → 判不了即红
    check("tier1+ 依赖链读不到即红", len(check_relative_floor("x", 2, "1.26.0", None)), 1)
    # tier0 不走判据 4（由判据 1 绝对约束）
    check("tier0 不走判据4", check_relative_floor("x", 0, "1.26.0", all124), [])

    # ---- 豁免清单：依据仍成立 → 绿；依据失效 → 红 ----
    check("mcpkit 豁免依据成立",
          check_relative_floor("mcpkit", 2, "1.26.0",
                               {**all124, "github.com/mark3labs/mcp-go": "1.25.5"}), [])
    # 豁免的 blocker 已不在依赖链上 → 豁免失效 → 红
    p = check_relative_floor("mcpkit", 2, "1.26.0", all124)
    check("mcpkit 豁免依据失效被抓", len(p), 1)
    check("豁免失效提示撤豁免", "撤掉这条豁免" in p[0], True)
    # 上游放宽了（要求变成 1.24） → 豁免失效 → 红
    p = check_relative_floor("mcpkit", 2, "1.26.0",
                             {**all124, "github.com/mark3labs/mcp-go": "1.24.0"})
    check("mcpkit 豁免依据放宽被抓", len(p), 1)
    # tools 豁免：sh/v3 要求 1.26.0，声明 1.26.0 成立
    check("tools 豁免依据成立",
          check_relative_floor("tools", 2, "1.26.0",
                               {**all124, "mvdan.cc/sh/v3": "1.26.0"}), [])
    # toolgate 豁免：间接依赖 mcpkit，登记的 blocker 是 mcpkit 本身
    check("toolgate 豁免依据成立",
          check_relative_floor("toolgate", 2, "1.26.0",
                               {**all124, "github.com/oagentkit/mcpkit": "1.25.5"}), [])

    # 豁免清单的每条都必须带依据字段（防止有人只加仓名）
    for repo, ex in EXEMPTIONS.items():
        for field in ("blocker", "blocker_version", "required_go", "imports", "since", "note"):
            check(f"豁免 {repo} 有 {field} 字段", field in ex, True)
        check(f"豁免 {repo} required_go 是版本号",
              bool(re.match(r"^\d+\.\d+", ex["required_go"])), True)

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
    check("解析 require",
          parse_requires("module x\n\nrequire (\n\tgithub.com/oekit/pkg v0.15.1\n"
                         "\tgithub.com/oagentkit/textx v0.6.0 // indirect\n)\n"),
          {"github.com/oekit/pkg": "v0.15.1", "github.com/oagentkit/textx": "v0.6.0"})
    # 单行 require 形态（无缩进）——guardx / runtime / textx 就是这种
    check("解析单行 require",
          parse_requires("module github.com/oagentkit/guardx\n\ngo 1.24.0\n\n"
                         "require github.com/oekit/pkg v0.16.0\n"),
          {"github.com/oekit/pkg": "v0.16.0"})
    # 单行 + 块内混合
    check("解析混合 require",
          parse_requires("module x\n\nrequire github.com/oekit/logx v0.7.1\n\n"
                         "require (\n\tgithub.com/oekit/pkg v0.15.1\n"
                         "\tgolang.org/x/time v0.14.0 // indirect\n)\n"),
          {"github.com/oekit/logx": "v0.7.1", "github.com/oekit/pkg": "v0.15.1",
           "golang.org/x/time": "v0.14.0"})
    check("最高依赖 go 版本",
          max_dep_go({"a": "1.24.0", "b": "1.25.5", "c": "1.18"}), ("b", "1.25.5"))

    if fails:
        print("❌ verify_go_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_go_floor 自测通过（tier0 地板 1.24.x / tier1+ 相对地板 / "
          "豁免依据复核 / CI 与 go.mod 成对对所有 tier 生效 / 判不了即红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
