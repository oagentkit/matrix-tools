#!/usr/bin/env python3
"""第一方依赖方向门禁：矩阵的**层级不变量**由真实 import 图判定。

## 守的不变量

1. **tier N 的模块不得依赖 tier > N 的模块**（自底向上，无环）。
2. **真实依赖必须被声明**：代码 import 了（或 go.mod require 了）某第一方模块，
   `modules.json` 的 `dependsOn` 就必须声明它。
3. **声明不得陈旧**：`dependsOn` 里声明了、代码里已不引的依赖要被指出来。
4. **矩阵内部不得消费门面**：门面是给**存量下游**的兼容层，矩阵自己用它就说明分层错了。

## 为什么必须读真实 import（而不是只读 modules.json）

原来的 `verify_split.py` 只校验 modules.json 里**声明的** `dependsOn` 图。
声明会漂——实测 `llm` 声明依赖 `obs`/`runtime`、`obs` 声明依赖 `textx`，
而代码里早已不引（obsx 已迁入 llm、breaker 已委托 oekit/concurrency）。
**在一个漂了的声明图上做层级校验，等于什么都没校验。**

而且原脚本：
- 读的是已失效的路径 `docs/split-matrix/modules.json`（当前布局下不存在）；
- **没有被任何 CI 引用** —— 而 profile README 却写着「这条不变量由
  verify_split.py 在每次拆分/发版时强制校验」。

本脚本是它的替代：**读真实 import**（`go list`，编译器视角），
并接进 CI。声明图只用来做**对账**，不用来判定层级。

## 判定逻辑不复用

本脚本**不 import** `gen_versions.py` / `verify_split.py` / 任何生成器。
事实来自 `matrix_facts`（共享取数层），判定在这里独立实现——
生成器与门禁共用解析函数，正是「共同盲区」的来源。

## 通用性

不硬编码仓名/层号/组织名：层从 modules.json 读，组织名从数据源读，
门面仓从 `infraRepos[].topics` 含 `deprecated` 推断。姊妹矩阵换数据源即可复用。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matrix_facts as facts_layer  # noqa: E402
from judge_report import Report, emit  # noqa: E402

DEFAULT_MATRIX = "matrix-tools/modules.json"


# ---------------------------------------------------------------- 事实装配


def _facade_repos(matrix: facts_layer.Matrix) -> set[str]:
    """门面仓集合：infraRepos 里 topics 含 `deprecated` 的那些。

    用英文 topics 而不是中文 role 字段，是为了跨矩阵通用
    （姊妹矩阵的角色文案不同，topics 是稳定键）。
    """
    out = set()
    for infra in matrix.data.get("infraRepos", []):
        topics = infra.get("topics") or []
        if "deprecated" in topics or "facade" in topics:
            name = infra.get("repo")
            if name:
                out.add(name)
    return out


def _module_paths(root: Path, matrix: facts_layer.Matrix) -> dict[str, str]:
    """仓 → 它的 module 路径（github.com/org/repo）。

    **包含 infraRepos**（门面仓也是真仓、也有 go.mod）——
    否则「矩阵内消费门面」这条判定会因为门面不在 modules 里而永远看不到。
    """
    out: dict[str, str] = {}
    repos = list(matrix.repos) + [r for r in matrix.infra_repos() if r]
    for repo in repos:
        gomod = root / repo / "go.mod"
        if not gomod.is_file():
            continue
        parsed = facts_layer.parse_go_mod(gomod.read_text(encoding="utf-8"))
        if parsed["module"]:
            out[repo] = parsed["module"]
    return out


def _deps_of(repo: str, facts: facts_layer.RepoFacts, modpaths: dict[str, str],
             org: str) -> tuple[set[str], set[str]]:
    """repo 的第一方依赖 → (直接依赖, 间接依赖)。

    **直接依赖** = 代码真实 import 了谁 ∪ go.mod 里**非 indirect** 的 require。
    两条来源取并集——只看其中一条都会漏：
    - 只看 import：会漏「代码用别名 import」的少数情况；
    - 只看 require：会漏「只 import 不 require」的病态情况。

    **间接依赖**（`// indirect`）单独归一类：它是上游拉进来的，
    不是本仓的依赖。把它算进「直接依赖」会得出「漏声明」这种假阳性
    （实测 toolgate 的 go.mod 里有 `oagentkit/runtime v0.7.1 // indirect`，
    那是 mcpkit 带进来的，不是 toolgate 自己的依赖）。
    """
    direct: set[str] = set()
    indirect: set[str] = set()
    for other in facts_layer.first_party_modules(facts.imports, org):
        if other in modpaths:
            direct.add(other)
    for req in facts.requires:
        path = req.split(" ", 1)[0]
        for other, mp in modpaths.items():
            if path == mp:
                if req in facts.require_indirect:
                    indirect.add(other)
                else:
                    direct.add(other)
    direct.discard(repo)
    indirect.discard(repo)
    return direct, indirect - direct


# ---------------------------------------------------------------- 判定


def judge(root: Path, matrix: facts_layer.Matrix, prefer_go_list: bool = True) -> Report:
    """判定第一方依赖方向。返回报告（不打印、不决定退出码）。"""
    report = Report(title="第一方依赖方向")
    org = matrix.org
    facades = _facade_repos(matrix)
    modpaths = _module_paths(root, matrix)

    # 事实：逐仓取真实 import
    repo_deps: dict[str, set[str]] = {}
    repo_indirect: dict[str, set[str]] = {}
    for repo in matrix.repos:
        facts = facts_layer.read_local_repo(root, repo, org=org, prefer_go_list=prefer_go_list)
        if facts is None:
            report.yellow(repo, "本地找不到该仓，跳过判定",
                          "在矩阵根目录下运行，或用 --matrix 指定数据源")
            continue
        if facts.import_source != "go_list":
            report.yellow(
                repo,
                f"import 取数降级为源码扫描（go list 不可用：{facts.import_error}）",
                "源码扫描认不出 build tag 与 import 别名，结论仅供参考；"
                "CI 里 go list 失败应当判红而非降级")
        direct, indirect = _deps_of(repo, facts, modpaths, org)
        repo_deps[repo] = direct
        repo_indirect[repo] = indirect

    # 判定 1：tier 倒挂
    for repo, deps in sorted(repo_deps.items()):
        tier = matrix.tier_of(repo)
        if tier is None:
            continue
        for dep in sorted(deps):
            dep_tier = matrix.tier_of(dep)
            if dep_tier is None:
                continue
            if dep_tier > tier:
                report.red(
                    repo,
                    f"tier 倒挂：tier{tier} 的模块依赖了 tier{dep_tier} 的 {dep}",
                    f"把 {repo} 降到 tier{dep_tier}，或让 {dep} 不被 {repo} 依赖")

    # 判定 2：真实依赖未声明
    for repo, deps in sorted(repo_deps.items()):
        declared = matrix.declared_deps(repo)
        for dep in sorted(deps - declared):
            if dep in facades:
                continue  # 门面由判定 4 单独报，措辞不同
            report.red(
                repo,
                f"依赖了 {dep} 但 modules.json 的 dependsOn 没声明",
                f"在 modules.json 里给 {repo} 的 dependsOn 加上 {dep}——"
                "依赖闭包校验与层级校验都以声明图为输入，漏声明等于漏校验")

    # 判定 3：声明陈旧
    for repo, deps in sorted(repo_deps.items()):
        declared = matrix.declared_deps(repo)
        for dep in sorted(declared - deps):
            report.yellow(
                repo,
                f"modules.json 声明依赖 {dep}，但代码里已无引用",
                f"删掉 {repo} 的 dependsOn 里的 {dep}，或恢复实际引用——"
                "声明图漂了，层级校验就是在漂了的图上做的")

    # 判定 4：矩阵内消费门面
    for repo, deps in sorted(repo_deps.items()):
        for facade in sorted(deps & facades):
            report.red(
                repo,
                f"矩阵内部消费了门面仓 {facade}",
                f"直接依赖真实模块 {facade} 背后的实现仓，"
                "门面只服务存量下游，矩阵自己用它会让分层倒挂")

    # 判定 5：间接依赖把更高层的模块拖进模块图
    for repo, indirect in sorted(repo_indirect.items()):
        tier = matrix.tier_of(repo)
        if tier is None:
            continue
        for dep in sorted(indirect):
            dep_tier = matrix.tier_of(dep)
            if dep_tier is not None and dep_tier > tier:
                report.yellow(
                    repo,
                    f"间接依赖把 tier{dep_tier} 的 {dep} 拖进了模块图",
                    f"检查 {repo} 依赖的那个上游模块为什么需要 {dep}——"
                    "Go 的依赖面按 module 结算，间接依赖一样进消费方的模块图")

    return report


# ---------------------------------------------------------------- 自测


def _mk_repo(root: Path, repo: str, module: str, go_ver: str,
             imports: list[str]) -> None:
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(f"module {module}\n\ngo {go_ver}\n", encoding="utf-8")
    body = "package p\n\nimport (\n"
    body += "".join(f'\t"{imp}"\n' for imp in imports)
    body += ")\n"
    (d / "p.go").write_text(body, encoding="utf-8")


def selftest() -> int:
    """反证自测：造一个**有违规**的矩阵，要求门禁把它报出来。

    样本含四类缺陷：
      ① tier 倒挂（tier0 引 tier2）
      ② 依赖未声明
      ③ 声明陈旧
      ④ 矩阵内消费门面
    以及两个**不误报**样本：合规的 tier0→tier1 依赖、门面的正确使用方式
    （矩阵外的仓消费门面不算违规）。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        matrix_data = {
            "defaultOrg": "acme",
            "modules": [
                {"repo": "base", "tier": 0, "packages": ["b"], "dependsOn": []},
                {"repo": "mid", "tier": 1, "packages": ["m"], "dependsOn": ["base"]},
                {"repo": "top", "tier": 2, "packages": ["t"], "dependsOn": ["mid"]},
                {"repo": "ghost", "tier": 2, "packages": ["g"], "dependsOn": ["stale"]},
                {"repo": "stale", "tier": 1, "packages": ["s"], "dependsOn": []},
            ],
            "infraRepos": [
                {"repo": "facade", "topics": ["go", "deprecated", "facade"]},
            ],
        }
        matrix_path = root / "modules.json"
        matrix_path.write_text(json.dumps(matrix_data), encoding="utf-8")
        matrix = facts_layer.load_matrix(matrix_path)

        # 合规：base 无依赖；mid → base（已声明）；top → mid（已声明）
        _mk_repo(root, "base", "github.com/acme/base", "1.24.0", [])
        _mk_repo(root, "mid", "github.com/acme/mid", "1.24.0", ["github.com/acme/base/b"])
        _mk_repo(root, "top", "github.com/acme/top", "1.26.0", ["github.com/acme/mid/m"])
        # stale 有仓无依赖，但 ghost 声明了它 → 陈旧声明
        _mk_repo(root, "stale", "github.com/acme/stale", "1.24.0", [])
        # 门面仓也要有真实的 go.mod——它是真仓，module 路径参与依赖归集
        _mk_repo(root, "facade", "github.com/acme/facade", "1.24.0", [])
        # ④ 矩阵内消费门面：ghost(tier2) 引 facade
        _mk_repo(root, "ghost", "github.com/acme/ghost", "1.26.0",
                 ["github.com/acme/facade/f"])

        # 制造 tier 倒挂：让 base(tier0) 引 top(tier2)
        _mk_repo(root, "base", "github.com/acme/base", "1.24.0", ["github.com/acme/top/t"])

        rep = judge(root, matrix, prefer_go_list=False)
        reds = [f"{f.subject}: {f.message}" for f in rep.reds]
        yellows = [f"{f.subject}: {f.message}" for f in rep.yellows]

        check("检出 tier 倒挂",
              any("tier 倒挂" in r and r.startswith("base") for r in reds), True)
        check("检出依赖未声明",
              any("没声明" in r for r in reds), True)
        check("检出矩阵内消费门面",
              any("消费了门面仓" in r for r in reds), True)
        check("检出声明陈旧",
              any("已无引用" in y for y in yellows), True)

        # 不误报：mid→base 已声明，不该报「没声明」
        check("合规依赖不误报",
              any(r.startswith("mid") and "没声明" in r for r in reds), False)
        # 不误报：top→mid 已声明且方向正确
        check("合规方向不误报",
              any(r.startswith("top") and "tier 倒挂" in r for r in reds), False)

        # 不误报：indirect require 不算直接依赖
        _mk_repo(root, "base", "github.com/acme/base", "1.24.0", [])
        (root / "mid" / "go.mod").write_text(
            "module github.com/acme/mid\n\ngo 1.24.0\n\nrequire (\n"
            "\tgithub.com/acme/base v0.1.0 // indirect\n)\n", encoding="utf-8")
        _mk_repo(root, "ghost", "github.com/acme/ghost", "1.26.0",
                 ["github.com/acme/facade/f"])
        matrix_data["modules"][3]["dependsOn"] = []
        matrix_path.write_text(json.dumps(matrix_data), encoding="utf-8")
        matrix3 = facts_layer.load_matrix(matrix_path)
        rep3 = judge(root, matrix3, prefer_go_list=False)
        check("indirect require 不算漏声明",
              any("mid" in r and "没声明" in r for r in
                  [f"{f.subject}: {f.message}" for f in rep3.reds]), False)

        # 反证：把违规去掉后必须转绿
        _mk_repo(root, "base", "github.com/acme/base", "1.24.0", [])
        _mk_repo(root, "ghost", "github.com/acme/ghost", "1.26.0", ["github.com/acme/base/b"])
        matrix_data["modules"][3]["dependsOn"] = ["base"]
        matrix_path.write_text(json.dumps(matrix_data), encoding="utf-8")
        matrix2 = facts_layer.load_matrix(matrix_path)
        rep2 = judge(root, matrix2, prefer_go_list=False)
        check("修好后转绿", rep2.has_red(), False)

    if fails:
        print("❌ verify_import_direction 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_import_direction 自测通过（tier 倒挂/未声明/陈旧/门面 四类检出 + 不误报）")
    return 0


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="第一方依赖方向门禁（真实 import 图）")
    ap.add_argument("--root", default=".", help="矩阵根目录（各仓的父目录）")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也判失败")
    ap.add_argument("--no-go-list", action="store_true", help="不跑 go list（只用源码扫描）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    matrix_path = Path(args.matrix) if args.matrix else root / DEFAULT_MATRIX
    if not matrix_path.is_file():
        print(f"❌ 找不到矩阵数据源：{matrix_path}")
        return 2
    matrix = facts_layer.load_matrix(matrix_path)
    rep = judge(root, matrix, prefer_go_list=not args.no_go_list)
    return emit(rep, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
