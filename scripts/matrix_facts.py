#!/usr/bin/env python3
"""矩阵**事实层**：把原始事实取出来，交给各守卫脚本独立判定。

## 为什么要有这一层（切分的依据）

矩阵里已经踩过两次「共享判断逻辑 ⇒ 共同盲区」：

1. `verify_breaking_index.py` 复用了**生成器** `breaking_index.py` 的解析函数，
   生成器漏收的形态它跟着一起漏 —— 于是「矩阵继续说无破坏性变更」。
2. CHANGELOG 逐字节门禁比的是「生成器丢 body 之后的结果」，
   门禁全绿而发布说明已经残缺（实测 7 仓 773 处正文缺失）。

两次的根因都是同一个：**判定逻辑被复用了**。

所以这里的切分原则是：

- **事实可以共享，判定不能共享。**
  「某仓的 go.mod 写了什么」「某仓真实 import 了谁」「某仓有哪些 tag」
  是**事实**，取错了所有用它的守卫一起错；而且事实层**不含判定**，
  不存在「判据写反 ⇒ 所有守卫一起放过」的路径。
  各守卫的**判定**各自独立实现，互不 import。

## 高内聚

本模块只回答一个问题：**「事实是什么」**。
取不到的，**一律显式区分** `not_found`（确实没有）与 `error`（判不了）——
把两者折叠成 `None` 会让守卫把网络失败当成「当时没有」而静默放过。

## 低耦合

本模块**不 import 任何守卫脚本**，也不 import 任何生成器。
它只依赖标准库与 `modules.json` 这一个数据源。

## 通用性

不硬编码任何仓名、包名、组织名、层号：

- 组织名从 `--org` 来（姊妹矩阵 oekit 换参数即可复用）；
- 仓与层的关系从 `modules.json` 读；
- 依赖方向、模块路径等**从数据源推导**，不写死。

姊妹矩阵（oekit）与本矩阵（oagentkit）用的是同一套判据、不同的数据源。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# go.mod 的 module 指令
_MODULE_RE = re.compile(r"^module\s+(\S+)", re.M)
# go.mod 的 go / toolchain 指令
_GO_RE = re.compile(r"^go\s+(\S+)", re.M)
_TOOLCHAIN_RE = re.compile(r"^toolchain\s+(\S+)", re.M)
# require 段里的单行 require（含间接依赖）
_REQUIRE_RE = re.compile(r"^\s*([\w.\-/]+\.[\w.\-/]+[\w.\-/]*)\s+(v\S+)", re.M)
# Go import 语句（单行与多行块都覆盖：块内是 "path" 形态）
_IMPORT_BLOCK_RE = re.compile(r"import\s*\(([^)]*)\)", re.S)
_IMPORT_ONE_RE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_QUOTED_RE = re.compile(r'"([^"]+)"')


# ---------------------------------------------------------------- 数据源


@dataclass(frozen=True)
class Matrix:
    """矩阵的机器可读定义（modules.json）。"""

    path: Path
    data: dict

    @property
    def org(self) -> str:
        return self.data.get("defaultOrg", "")

    @property
    def modules(self) -> list[dict]:
        return self.data.get("modules", [])

    @property
    def repos(self) -> list[str]:
        return [m["repo"] for m in self.modules]

    def go_module_targets(self) -> list[tuple[str, int | None]]:
        """矩阵里所有**是 Go module** 的仓 → [(仓名, 层位)]。

        层位对 `modules[]` 里的仓有值；对 `infraRepos[]` 里的 Go module
        （当前只有门面 `agentkit`）是 `None` —— 它不是矩阵成员，没有层位概念，
        但它的 go.mod 同样钉着一整排依赖，同样要被地板门禁扫到。

        ## 为什么这条取数要**只有一份**（实测 2026-10-31）

        此前两条地板守卫各写各的取仓谓词：
        - `verify_dep_floor.py`：modules[] + `infraRepos[].goModule` 为真；
        - `verify_dep_floors.py`：modules[] + `infraRepos[].topics` 含 deprecated/facade。

        当时两边集合**恰好相同**（门面两个字段都满足）⇒ 看着没问题；
        但两个谓词不同 ⇒ 改数据源的一个字段，就可能让某个仓从其中一条守卫的
        视野里消失，而**两条各自看起来都正常**。
        （与「计划与执行共用同一套不完整规则 ⇒ 两边一起漏 ⇒ 报成功而实际没做」同族。）

        ⇒ 正确解法不是加一条「两个集合必须相同」的检查，而是**消除重复**：
        「哪些仓是 Go module」是**事实**（不是判定），事实层共享是安全的，
        且结构上就不可能再不一致。
        """
        out: list[tuple[str, int | None]] = []
        for m in self.modules:
            repo = m.get("repo")
            if repo:
                tier = m.get("tier")
                out.append((repo, int(tier) if tier is not None else None))
        for r in self.data.get("infraRepos", []):
            repo = r.get("repo")
            if repo and r.get("goModule"):
                out.append((repo, None))
        return out

    def all_go_repos(self) -> list[str]:
        """所有 Go module 仓的仓名（顺序同 `go_module_targets`）。"""
        return [repo for repo, _ in self.go_module_targets()]

    def tier_of(self, repo: str) -> int | None:
        for m in self.modules:
            if m["repo"] == repo:
                return int(m["tier"])
        return None

    def declared_deps(self, repo: str) -> set[str]:
        """modules.json 里**声明**的跨仓依赖。

        注意：这是**声明**，不是事实。声明会漂（实测 llm/obs 的 dependsOn
        早已与代码不符），所以它只能用来做「对账」，不能当成真相。
        """
        for m in self.modules:
            if m["repo"] == repo:
                return set(m.get("dependsOn", []))
        return set()

    def packages_of(self, repo: str) -> set[str]:
        for m in self.modules:
            if m["repo"] == repo:
                return set(m.get("packages", []))
        return set()

    def infra_repos(self) -> list[str]:
        return [r.get("repo", "") for r in self.data.get("infraRepos", [])]


def load_matrix(path: str | Path) -> Matrix:
    p = Path(path)
    return Matrix(path=p, data=json.loads(p.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- 本地事实


@dataclass
class RepoFacts:
    """一个仓的**本地**事实。取不到的字段是 None（= 判不了），不是空值。"""

    repo: str
    root: Path
    module_path: str | None = None
    go_directive: str | None = None
    toolchain: str | None = None
    requires: set[str] = field(default_factory=set)
    requires_direct: set[str] = field(default_factory=set)
    require_indirect: set[str] = field(default_factory=set)
    ci_go_version: str | None = None
    package_dirs: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    import_source: str = "none"     # go_list | source_scan | none
    import_error: str | None = None


def parse_go_mod(text: str) -> dict:
    """go.mod 原文 → {module, go, toolchain, requires, requires_direct, require_indirect}。

    只取**事实**。

    **direct 与 indirect 必须分开**——这是本模块最容易出错的地方：
    `github.com/oagentkit/runtime v0.7.1 // indirect` 出现在 toolgate 的 go.mod 里，
    是**被 mcpkit 拉进来的**，不是 toolgate 自己的依赖。
    把它算成「toolgate 依赖 runtime」会得出「漏声明」这种假阳性结论。

    三类取值：
      - requires         ：全部 require（含 indirect），元素形态 "path version"
      - requires_direct  ：**非** indirect 的 require
      - require_indirect ：标记为 indirect 的 require
    """
    all_req: set[str] = set()
    direct: set[str] = set()
    indirect: set[str] = set()
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        if in_block or stripped.startswith("require "):
            body = stripped[len("require "):] if stripped.startswith("require ") else stripped
            is_indirect = "// indirect" in body
            body = body.split("//", 1)[0].strip()
            if not body:
                continue
            parts = body.split()
            if len(parts) >= 2 and parts[1].startswith("v"):
                item = f"{parts[0]} {parts[1]}"
                all_req.add(item)
                (indirect if is_indirect else direct).add(item)
    return {
        "module": (_MODULE_RE.search(text).group(1) if _MODULE_RE.search(text) else None),
        "go": (_GO_RE.search(text).group(1) if _GO_RE.search(text) else None),
        "toolchain": (_TOOLCHAIN_RE.search(text).group(1) if _TOOLCHAIN_RE.search(text) else None),
        "requires": all_req,
        "requires_direct": direct,
        "require_indirect": indirect,
    }


def parse_ci_go_version(text: str) -> str | None:
    """从 workflow 原文里取 `go-version: '1.24'` 的值。"""
    m = re.search(r"go-version:\s*['\"]?([0-9][0-9.]*)['\"]?", text)
    return m.group(1) if m else None


def _package_dirs_from_paths(paths: list[str]) -> set[str]:
    dirs: set[str] = set()
    for p in paths:
        if not p.endswith(".go"):
            continue
        parts = p.split("/")
        dirs.add("." if len(parts) == 1 else parts[0])
    return dirs


def _scan_imports_from_source(root: Path) -> set[str]:
    """源码正则扫描 import（**降级路径**）。

    比 `go list` 弱（认不出 build tag 下的文件、也不解析别名），但不需要
    模块缓存与网络——CI 的冷启动环境里 `go list` 可能直接失败。
    调用方必须把这条路径的结论降级为「仅供参考」。
    """
    imports: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "testdata", "vendor")]
        for fn in filenames:
            if not fn.endswith(".go"):
                continue
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for block in _IMPORT_BLOCK_RE.findall(text):
                imports.update(_QUOTED_RE.findall(block))
            imports.update(_IMPORT_ONE_RE.findall(text))
    return imports


def _scan_imports_via_go_list(root: Path) -> tuple[set[str], str | None]:
    """`go list` 取真实 import（**首选路径**）。

    为什么优先它：源码正则认不出 build tag 排除的文件，也不解析 import 别名，
    而 `go list` 给的是**编译器视角**的真实依赖。
    取不到时返回 (set(), 错误原因) —— 调用方据此降级，而不是当成「没有依赖」。

    **测试依赖也取**：`.TestImports` / `.XTestImports` 一并计入。
    只在 _test.go 里 import 的模块，同样要进 go.mod 的 require，
    同样进消费方的模块图 —— 漏掉它会把「测试专用依赖」误判成「陈旧声明」。
    """
    try:
        proc = subprocess.run(
            ["go", "list", "-e", "-f",
             "{{join .Imports \"\\n\"}}\n{{join .TestImports \"\\n\"}}\n{{join .XTestImports \"\\n\"}}",
             "./..."],
            cwd=str(root), capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"go list 调用失败: {exc}"
    if proc.returncode != 0 and not proc.stdout.strip():
        return set(), f"go list 退出码 {proc.returncode}: {proc.stderr.strip()[:200]}"
    imports = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    if not imports:
        return set(), "go list 未返回任何 import（仓内可能没有包）"
    return imports, None


def read_local_repo(root: Path, repo: str, org: str | None = None,
                    prefer_go_list: bool = True) -> RepoFacts | None:
    """读一个仓的本地事实。仓不存在返回 None。"""
    repo_dir = root / repo
    if not repo_dir.is_dir():
        return None

    facts = RepoFacts(repo=repo, root=repo_dir)

    gomod = repo_dir / "go.mod"
    if gomod.is_file():
        parsed = parse_go_mod(gomod.read_text(encoding="utf-8"))
        facts.module_path = parsed["module"]
        facts.go_directive = parsed["go"]
        facts.toolchain = parsed["toolchain"]
        facts.requires = parsed["requires"]
        facts.requires_direct = parsed["requires_direct"]
        facts.require_indirect = parsed["require_indirect"]

    # CI 的 go-version：优先读各仓自己的 workflow
    wf_dir = repo_dir / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
            v = parse_ci_go_version(wf.read_text(encoding="utf-8", errors="ignore"))
            if v:
                facts.ci_go_version = v
                break

    # 包目录
    dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in (".git", "testdata", "vendor")]
        rel = Path(dirpath).relative_to(repo_dir)
        for fn in filenames:
            if fn.endswith(".go"):
                dirs.add("." if str(rel) == "." else rel.parts[0])
    facts.package_dirs = dirs

    # 真实 import
    if prefer_go_list:
        imports, err = _scan_imports_via_go_list(repo_dir)
        if imports:
            facts.imports = imports
            facts.import_source = "go_list"
        else:
            facts.imports = _scan_imports_from_source(repo_dir)
            facts.import_source = "source_scan"
            facts.import_error = err
    else:
        facts.imports = _scan_imports_from_source(repo_dir)
        facts.import_source = "source_scan"
    return facts


def first_party_modules(imports: set[str], org: str) -> dict[str, set[str]]:
    """import 路径集合 → {模块名: 该模块被引的包路径集合}。

    只认 `github.com/<org>/<repo>...` 形态的第一方 import；
    `github.com/<org>` 本身（伞仓）单独归到 `__facade__`。
    """
    out: dict[str, set[str]] = {}
    prefix = f"github.com/{org}/"
    for imp in imports:
        if not imp.startswith(prefix):
            continue
        rest = imp[len(prefix):]
        repo = rest.split("/", 1)[0]
        out.setdefault(repo, set()).add(rest)
    return out


# ---------------------------------------------------------------- git 事实


def git(*args: str, cwd: Path | None = None) -> str | None:
    """跑一条 git 命令；失败返回 None（判不了）。"""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_tags(repo_dir: Path) -> list[str]:
    """仓里的全部 tag（事实，不做版本序判断）。"""
    out = git("tag", cwd=repo_dir)
    return out.split() if out else []


def git_log(repo_dir: Path, *args: str) -> list[dict]:
    """`git log` 的结构化输出（sha / subject / body / date）。"""
    raw = git("log", "--format=%H%x1f%s%x1f%b%x1f%cI", *args, cwd=repo_dir)
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        out.append({"sha": parts[0], "subject": parts[1],
                    "body": parts[2], "date": parts[3]})
    return out


def git_show_file(repo_dir: Path, rev: str, path: str) -> str | None:
    """取某个提交上某文件的原文；取不到返回 None（判不了 ≠ 没有）。"""
    proc = subprocess.run(["git", "-C", str(repo_dir), "show", f"{rev}:{path}"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return None
    return proc.stdout


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    gomod = """module github.com/oagentkit/llm

go 1.24.0

toolchain go1.27.2

require (
\tgithub.com/oagentkit/textx v0.6.1
\tgithub.com/cloudwego/eino v0.9.21 // indirect
)

require github.com/oekit/pkg v0.18.0
"""
    parsed = parse_go_mod(gomod)
    check("module 路径", parsed["module"], "github.com/oagentkit/llm")
    check("go 指令", parsed["go"], "1.24.0")
    check("toolchain", parsed["toolchain"], "go1.27.2")
    check("require 条数（含 indirect）", len(parsed["requires"]), 3)
    check("require 形态", parsed["requires"],
          {"github.com/oagentkit/textx v0.6.1",
           "github.com/cloudwego/eino v0.9.21",
           "github.com/oekit/pkg v0.18.0"})
    check("direct require 不含 indirect", parsed["requires_direct"],
          {"github.com/oagentkit/textx v0.6.1", "github.com/oekit/pkg v0.18.0"})
    check("indirect require 单列", parsed["require_indirect"],
          {"github.com/cloudwego/eino v0.9.21"})

    check("CI go-version", parse_ci_go_version("with:\n  go-version: '1.24'\n"), "1.24")
    check("CI go-version 双引号", parse_ci_go_version('go-version: "1.26"'), "1.26")
    check("CI 无 go-version", parse_ci_go_version("runs-on: ubuntu-latest"), None)

    # ---- go_module_targets：仓清单取数的**唯一来源**（2026-10-31）----
    #
    # 此前两条地板守卫各写各的取仓谓词（goModule vs topics 含 deprecated/facade）
    # ⇒ 改数据源一个字段就可能让某个仓从其中一条守卫的视野里消失，
    # 而两条各自看起来都正常。⇒ 消除重复，两边共用事实层这一份。
    m = Matrix(path=Path("/x"), data={
        "modules": [{"repo": "a", "tier": 0}, {"repo": "b", "tier": 2}],
        "infraRepos": [
            {"repo": "facade", "goModule": True, "topics": ["deprecated", "facade"]},
            {"repo": "notgo", "goModule": False, "topics": ["ci"]},
            # 故意给一个「是 Go module 但 topics 里没有 deprecated/facade」的仓
            # —— 旧谓词（按 topics）会漏掉它，新谓词（按 goModule）不会。
            {"repo": "newgo", "goModule": True, "topics": ["ci"]},
            {"repo": "norepo"},        # 没有 repo 字段 ⇒ 必须跳过（不 KeyError）
        ],
    })
    check("go module 仓清单", m.all_go_repos(), ["a", "b", "facade", "newgo"])
    check("模块有层位、infra 无层位",
          m.go_module_targets(),
          [("a", 0), ("b", 2), ("facade", None), ("newgo", None)])
    check("非 Go module 的 infra 不进清单", "notgo" in m.all_go_repos(), False)
    check("没有 repo 字段的 infra 被跳过", len(m.all_go_repos()), 4)

    check("包目录", _package_dirs_from_paths(
        ["llm.go", "llm/wrap/wrap.go", "go.mod", "README.md"]),
        {".", "llm"})

    imports = {"context", "github.com/oagentkit/textx/textutil",
               "github.com/oagentkit/llm/wrap", "github.com/oekit/pkg/errors"}
    fp = first_party_modules(imports, "oagentkit")
    check("第一方模块归集", sorted(fp), ["llm", "textx"])
    check("第一方包路径", fp["textx"], {"textx/textutil"})

    # 伞仓（org 本身）不应被当成模块
    fp2 = first_party_modules({"github.com/oagentkit"}, "oagentkit")
    check("伞仓不归入模块", fp2, {})

    if fails:
        print("❌ matrix_facts 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ matrix_facts 自测通过（go.mod / CI 版本 / 包目录 / import 归集 / Go module 仓清单）")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="矩阵事实层（只取事实，不判定）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()
    raise SystemExit(selftest())
