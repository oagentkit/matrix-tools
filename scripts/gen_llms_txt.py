#!/usr/bin/env python3
"""从 modules.json 生成 llms.txt —— 面向 LLM 的模块索引。

对标 oekit 的 https://oekit.github.io/llms.txt：模块清单 + 分层 + 安装行 + AI Agent 要点。
模块信息全部来自 modules.json，不手写，避免与矩阵漂移。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matrix_paths import (  # noqa: E402  路径规则与 rewrite_imports.py 同源
    OLD_ROOT,
    build_mapping,
    misleading,
    new_import_path,
)

OLD_ROOT_SHORT = OLD_ROOT.rsplit("/", 1)[-1]  # agentkit，用于文档里写短路径

# 门面包集合的**权威登记**：门面冻结基线。
#
# 为什么不能拿矩阵包数（npkg）当代门面包数：
#   门面按废弃承诺**冻结**——矩阵后来新增的包（guardx/wire、guardx/sandbox、
#   guardx/toolfail、llm/decision）它**不转发**（这是正确行为，不是欠账）。
#   但生成器曾拿 npkg 填「门面 N 包 re-export」，于是 llms.txt 宣称门面 52 包、
#   门面实际只有 48 包 ⇒ AI Agent 照 llms.txt 去门面找那 4 个包，必然 404。
#   对外生成物在撒谎，比门禁判红更糟——门禁会报，人读版不会。
#
# 所以门面包数一律从冻结基线取；矩阵包数与门面包数是**两个不同的数**。
FACADE_BASELINE = HERE / "facade_package_baseline.json"


def facade_package_names() -> list[str]:
    """门面转发包名清单（取自冻结基线）。

    判不了（基线缺失/结构异常）返回空列表 —— 调用方必须把它当成
    「判不了」而不是「0 个包」，由 judge 层决定红还是黄。
    """
    if not FACADE_BASELINE.is_file():
        return []
    try:
        data = json.loads(FACADE_BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for item in data.get("packages", []):
        if isinstance(item, dict):
            name = item.get("package") or item.get("name")
        else:
            name = item
        if name:
            out.append(str(name))
    return out


def packages_without_facade(mods: list[dict]) -> list[tuple[str, str]]:
    """矩阵里有、门面上**没有**转发等价物的包 → [(矩阵包名, 真实 import 路径)]。

    ## 为什么要在对外文档里点名这些包

    门面按废弃承诺**冻结** ⇒ 矩阵后来新增的包它不转发（**正确行为**，不是欠账）。
    但下游读 llms.txt 时，如果只看到「门面 48 包 re-export」，
    他会以为「矩阵所有包都能从门面走」⇒ 需要某个新包时去门面 import ⇒ 404。

    ⇒ 必须显式说清「这几个包**没有**门面等价物，直接依赖真实模块」。
    这让「矩阵有、门面无」这件事从「隐含的坑」变成「写在脸上的事实」。

    ## 口径

    门面目录名 = 包路径的**末段**（拆分时的 import 改写规则：包名一律保留，
    只加模块前缀）⇒ 判定用末段比；对外路径用 `matrix_paths.new_import_path`
    生成（它处理了「末段与仓名相同要收口到 module 根」这条唯一例外，
    例如 `llm/decision` → `github.com/oagentkit/llm/decision`，不是 `llm/llm/decision`）。
    """
    facade_names = set(facade_package_names())
    if not facade_names:
        return []
    out: list[tuple[str, str]] = []
    for mod in mods:
        repo = mod["repo"]
        for pkg in mod.get("packages", []):
            leaf = pkg.split("/")[-1]
            if leaf in facade_names:
                continue
            out.append((pkg, new_import_path("{org}", repo, pkg)))
    return out

TIER_TITLE = {
    0: "基础层 · 零重量级第三方地板（纯 stdlib / 第一方基座）",
    1: "能力层 · 依赖 tier0，仍不引重量级第三方 SDK",
    2: "集成层 · 依赖 tier0/tier1 + 重第三方地板",
    3: "编排层 · 依赖 llm 等 tier2 门面",
}
TIER_SLUG = {0: "foundation", 1: "capability", 2: "integration", 3: "orchestration"}


def compat_by_repo(data: dict) -> dict[str, list[dict]]:
    """账本里 kind == "compat" 的条目按仓归组。

    账本（modules.json 的 retiringPackages）里混着两种东西：
      - kind == "retired"：已退役的包（breaker）—— 那条路径已经没了；
      - kind == "compat" ：过渡期兼容镜像（llm/llm）—— **路径仍然可用**。

    llms.txt 是给 Agent 读的索引，只有后者需要挂在「包」清单旁边：
    存量下游还钉在旧路径上，索引里查不到，它就会以为那条路径已经没了
    （llm v0.9.0 那次「包上移」就是在 llms.txt 里被读到「原路径已移除」）。
    """
    out: dict[str, list[dict]] = {}
    for entry in data.get("retiringPackages") or []:
        if entry.get("kind") == "compat" and entry.get("repo"):
            out.setdefault(entry["repo"], []).append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--site", default="https://oagentkit.github.io")
    ap.add_argument("--versions", default=str(HERE.parent / "versions.json"),
                    help="机器可读版本矩阵（破坏性变更段的来源）")
    args = ap.parse_args()

    d = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = d["defaultOrg"]
    mods = sorted(d["modules"], key=lambda m: (m["tier"], m["repo"]))
    npkg = sum(len(m["packages"]) for m in mods)
    # 门面包数：**独立于矩阵包数**，取自门面冻结基线。
    # 门面冻结 ⇒ 矩阵新增的包它不转发 ⇒ 门面包数 < 矩阵包数，两个数不能混用。
    facade_pkgs = facade_package_names()
    nfacade = len(facade_pkgs)
    if nfacade == 0:
        # 判不了 ≠ 没问题：基线读不到就写「0 包 re-export」，
        # 等于对外发布一条假陈述。宁可生成失败。
        print(f"❌ 读不到门面冻结基线，无法确定门面包数：{FACADE_BASELINE}")
        return 1
    # 矩阵有、门面无转发等价物的包（要在对外文档里点名，见函数 docstring）
    no_facade_pkgs = packages_without_facade(mods)
    # 旧→新映射条数：**不是**矩阵包数。rewrite_imports 的规则集 =
    #   矩阵包（52）+ historical_paths.json 里的历史路径（已发布过、下游可能仍引用）
    # ⇒ 用同一个 build_mapping 求，条数与 `rewrite_imports.py --print-rules` 天然一致。
    nmapping = len(build_mapping(d["modules"], org))

    # 破坏性变更：**从 versions.json 取**，不自己抽 CHANGELOG。
    #
    # 为什么：llms.txt（人读版）与 versions.json（机器读版）曾各说各话——
    # versions.json 的 breakingIndex 整段是空的，而 llms.txt 的「易踩的坑」里
    # 写得比谁都细。两处分别维护，缺口就在两处分别出现。
    #
    # 改成同源（都从各仓 CHANGELOG 派生，中间经 versions.json 汇流）之后，
    # 人读版与机读版的信息量就对等了：矩阵说「有破坏性变更」，llms.txt 一定列得出来。
    versions = {}
    vpath = Path(args.versions)
    if vpath.is_file():
        versions = json.loads(vpath.read_text(encoding="utf-8"))
    breaking: list[tuple[str, dict]] = []
    for m in versions.get("modules", []):
        for e in m.get("breakingIndex", []):
            breaking.append((m["repo"], e))
    for f in versions.get("facades", []):
        for e in f.get("breakingIndex", []):
            breaking.append((f["repo"], e))

    L: list[str] = []
    L.append("# oagentkit")
    L.append("")
    L.append(f"> Open Agent Kit — Agent / LLM 编排工具链模块矩阵。"
             f"{len(mods)} 个独立发版的 Go 模块：模型客户端、MCP 工具池、"
             f"技能库、知识检索、Agent 编排、运行时基础设施。")
    L.append("")
    L.append("oagentkit 是一套按**框架耦合分层**组织的 Go 模块矩阵。依赖严格自底向上，"
             "每仓独立 SemVer + 独立 CI。**每个仓库根目录都有 AGENTS.md**"
             "（面向 AI Agent 的规范与硬规则）。")
    L.append("")
    L.append("## 与 oekit 的关系")
    L.append("")
    L.append("oagentkit 是一套**独立矩阵**，与 [oekit](https://oekit.github.io/llms.txt)"
             "（Open Easy Kit，Go 微服务工具链）是姊妹项目。oagentkit **依赖** oekit 的：")
    L.append("")
    L.append("- `github.com/oekit/pkg` — 微工具集（errors/stringx/encoding/mathx）")
    L.append("- `github.com/oekit/concurrency` — goroutine 池/单飞/重试/令牌桶/滑动窗口/时间轮")
    L.append("- `github.com/oekit/logx` — 日志门面")
    L.append("")
    L.append("两者只共享 `o`/`kit` 元素，**不是子品牌关系**。")
    L.append("")
    L.append("## 版本矩阵（机器可读）")
    L.append("")
    L.append(f"- {args.site}/versions.json（由 matrix-tools `scripts/gen_versions.py` 生成）")
    L.append(f"- 矩阵数据源：[matrix-tools/modules.json](https://github.com/{org}/matrix-tools)"
             f"（{len(mods)} 模块 / {npkg} 包）")
    L.append("")

    # 破坏性变更段（与 versions.json 同源）
    L.append("## 破坏性变更")
    L.append("")
    if breaking:
        L.append(f"登记在册的破坏性变更共 {len(breaking)} 条（由各仓 CHANGELOG 自动抽取，"
                 f"机器可读同源见 versions.json 的 `breakingIndex` 字段）：")
        L.append("")
        for repo, e in breaking:
            L.append(f"- **{repo} {e['version']}** — {e['title']}")
            summary = " ".join((e.get("summary") or "").split())
            # 摘要与标题相同（无 trailer、靠标题判定的那种）就不重复印一遍
            if summary and summary != " ".join(e["title"].split()):
                L.append(f"  {summary}")
    else:
        L.append("（本次生成未读到 versions.json 的 breakingIndex —— "
                 "生成时给 `--versions` 指向矩阵仓的 versions.json）")
    L.append("")
    # 依赖地板段：机器可读同源（versions.json 的 depFloor）
    L.append("## 依赖地板")
    L.append("")
    L.append("**Go 的依赖面按 module 结算**：import 某仓的**任意一个包**，该仓的整条 "
             "`require` 就进你的模块图。所以「这个仓的地板」= 「用它的包要吃的依赖」。")
    L.append("")
    clean = [m for m in mods if m["tier"] <= 1]
    L.append(f"**tier0/tier1 共 {len(clean)} 个仓，地板为空** —— "
             f"只用这些仓的包，你的模块图里不会出现重量级第三方 SDK"
             f"（门禁：`matrix-tools/scripts/verify_dep_floor.py`）：")
    L.append("")
    for m in sorted(clean, key=lambda x: (x["tier"], x["repo"])):
        L.append(f"- `{m['repo']}`（tier{m['tier']}）")
    L.append("")
    L.append("带地板的集成层（tier2+）——用它们的包要接受这些依赖：")
    L.append("")
    floors = []
    for m in sorted(mods, key=lambda x: (x["tier"], x["repo"])):
        for f in versions.get("modules", []):
            if f["repo"] == m["repo"] and f.get("depFloor"):
                floors.append((m["repo"], m["tier"], f["depFloor"]))
    for repo, tier, floor in floors:
        L.append(f"- `{repo}`（tier{tier}）：{'、'.join(f'`{x}`' for x in floor)}")
    L.append("")
    L.append("## 模块")
    L.append("")
    L.append("依赖严格自底向上，四个能力层：")
    L.append("")

    compat = compat_by_repo(d)
    for tier in sorted({m["tier"] for m in mods}):
        L.append(f"### {TIER_TITLE[tier]}")
        L.append("")
        for m in [x for x in mods if x["tier"] == tier]:
            pkgs = "、".join(f"`{p.rsplit('/', 1)[-1]}`" for p in m["packages"])
            L.append(f"- [{m['repo']}](https://github.com/{org}/{m['repo']}): "
                     f"{m['ghDescription']}")
            L.append(f"  - 包：{pkgs}")
            for cp in compat.get(m["repo"], []):
                L.append(
                    f"  - 过渡期兼容镜像：`{cp['pkg']}` —— 可用 import 路径 "
                    f"`{cp['importPath']}`（{cp.get('availableIn', '—')}）；"
                    f"规范路径仍是 `{cp['canonicalPath']}`，新代码用它"
                )
            if m.get("dependsOn"):
                deps = "、".join(f"[{x}](https://github.com/{org}/{x})" for x in sorted(m["dependsOn"]))
                L.append(f"  - 依赖：{deps}")
            L.append(f"  - 安装：`go get github.com/{org}/{m['repo']}@latest`")
        L.append("")

    L.append("## 门面仓（迁移过渡 · 已废弃）")
    L.append("")
    L.append(f"- [agentkit](https://github.com/{org}/agentkit): 原单仓 "
             f"`git.enjoye.top/enjoydream/agentkit` 的兼容门面，{nfacade} 包 re-export。"
             f"**已废弃**：v1.x 退役条件已满足，只保留兼容转发、不接受新功能，"
             f"**新代码不要引它**。归档待旧单仓 "
             f"`git.enjoye.top/enjoydream/agentkit` 下线后执行；在此之前本仓继续可用，"
             f"已有 tag 的 `go get` 照常解析。")
    L.append(f"- 完整旧→新包路径映射见该仓 [MIGRATION.md](https://github.com/{org}/agentkit/blob/main/MIGRATION.md)。")
    L.append(f"- 门面**包集合已冻结**：矩阵新增的包不再要求门面跟上（见 "
             f"`scripts/facade_package_baseline.json` 与 `scripts/verify_facade_frozen.py`）。")
    if no_facade_pkgs:
        L.append(f"- **以下 {len(no_facade_pkgs)} 个矩阵包没有门面等价物**"
                 f"（门面冻结在它们之前，不转发新包是正确行为）：")
        for pkg, path in no_facade_pkgs:
            L.append(f"  - `{path.replace('{org}', org)}` —— 直接依赖真实模块，**不要**去门面 import")
    L.append("")
    L.append("## 基建")
    L.append("")
    L.append(f"- [{org}/.github](https://github.com/{org}/.github) — org profile + "
             f"**全 org 共享的 CI 门禁**（reusable workflow）")
    L.append(f"- [{org}/matrix-tools](https://github.com/{org}/matrix-tools) — "
             f"版本矩阵生成 / 审计 / import 改写 / 建仓 / 发布流水线")
    L.append("")
    L.append("## AI Agent 要点")
    L.append("")
    L.append(f"- **每仓根目录的 `AGENTS.md` 是该仓的权威规范**：分层不变量、"
             "错误处理、日志、并发安全、API 命名，以及该仓特有的工程红线。")
    L.append(f"- 安装一律 `go get github.com/oagentkit/<mod>@latest`；不要引门面仓 `agentkit`。")

    # import 路径构造规则 —— 消费方从旧单仓迁过来时最先踩的坑。
    # 例子全部从 modules.json 实时生成，不是手写的，所以矩阵变了这里自动跟着变。
    traps = misleading(d["modules"], org)
    L.append(f"- **import 路径 = `github.com/{org}/{{仓名}}/{{包名末段}}`**；"
             f"**末段与仓名相同**时收口成 `github.com/{org}/{{仓名}}`（不重复末段）。"
             "旧单仓 `git.enjoye.top/enjoydream/agentkit/<包>` 的**中间层目录会被丢掉**，"
             "因为新矩阵里那层组织职责由**仓**承担。例如：")
    for old, new in traps:
        L.append(f"    - `{old}` → `{new}`")
    # 「末段==仓名」的条目单独点名：它收口到 module 根，
    # 直觉反而会以为要重复成 llm/llm —— 规则唯一的例外，必须显式讲清。
    # 判定看斜杠数：收口后是 github.com/{org}/{repo}，只有 2 个斜杠。
    same = [(o, n) for o, n in traps if n.count("/") == 2]
    tail = (f"其中 `{same[0][0]}` → `{same[0][1]}` 尤其坑："
            "包名末段与仓名相同时**收口到 module 根**（不重复末段），"
            "直觉会以为要写成 `llm/llm`。"
            if same else "")
    L.append(f"  {tail}完整 {len(traps)} 条易踩对照、及全部 {nmapping} 条映射见 "
             f"[matrix-tools/scripts/rewrite_imports.py --print-rules]"
             f"(https://github.com/{org}/matrix-tools)，"
             f"机器可读映射见 [versions.json](https://{org}.github.io/versions.json) 的 "
             f"`fromLegacy` 字段。")

    L.append(f"- 完整包路径映射见 [matrix-tools/modules.json](https://github.com/{org}/matrix-tools)。")
    L.append("- 改 CI 规则只去 [{org}/.github](https://github.com/{org}/.github) 仓"
             "（`go-ci.yml@main`）；各仓 `ci.yml` 只是薄调用层，不要在各仓复制规则。")
    L.append("- 不确定 API 时以源码为准：各仓均带测试，`go doc github.com/oagentkit/<mod>[/<pkg>]` 可查。")
    L.append("")
    L.append("## 易踩的坑")
    L.append("")
    # 举例挑「中间层被丢弃」的条目（与上面 AI Agent 要点里的坑互补）
    dropped = next(((o, n) for o, n in traps if o != "agentkit/llm"), None)
    if dropped:
        L.append(f"- **import 路径不是纯前缀映射**：旧单仓 `{OLD_ROOT_SHORT}/<包>` 迁到新矩阵时，"
                 f"中间层目录会被丢掉 —— `{dropped[0]}` → `{dropped[1]}`。"
                 "按「仓库名 + 完整旧路径」拼会直接构建断裂。")
    else:
        L.append("- **import 路径不是纯前缀映射**：旧单仓迁新矩阵时中间层目录会被丢掉，"
                 "按「仓库名 + 完整旧路径」拼会直接构建断裂。")
    L.append("- **`guardx/audit` 与 `guardx/bashguard` 已迁出**（guardx v0.7.0）："
             "`github.com/oagentkit/obs/audit`（依赖 oekit/logx）、"
             "`github.com/oagentkit/tools/bashguard`（依赖 mvdan.cc/sh/v3）。"
             "guardx 回到**一个 module、一条版本线、零外部依赖**的 tier0 底座 —— "
             "Go 的依赖面按 module 结算：只 import `guardx/logredact` 的下游，"
             "模块图里不该多出任何第三方依赖。旧路径的嵌套 module tag 仍在但**已冻结**。")
    L.append("- **`progress` 撞名但语义相反**：`oekit/concurrency/progress` 是长任务**阶段进度追踪器**，"
             "而 `oagentkit/runtime/progress` 是泛型**事件总线**。按包名对齐会做错。")
    L.append("- **`pacer` ≠ `ratelimit`**：前者是聚合缓冲（攒够窗口再发），后者是准入控制。")
    L.append("- **`runtime/breaker` 已退役**：`llm` v0.4.0 起熔断全量使用 "
             "`oekit/concurrency/breaker`（三态状态机 + 代绑定 + 冷却期/探测时限独立配置 + "
             "计数型触发）。上游 "
             "[#17](https://github.com/oekit/concurrency/issues/17)→"
             "[#25](https://github.com/oekit/concurrency/issues/25) 已全部落地。")
    L.append("- **`oekit/concurrency/breaker` 的 `0` 语义是陷阱**：`WithConsecutiveFailures(0)` "
             "表示**关闭计数型触发**、`WithCooldown(0)` 表示**沿用 probeTimeout**；"
             "而旧 `agentkit/breaker` 的 `New(0, …)` 里 0 是「用缺省」。照抄会让熔断静默失效。")
    L.append("- **`GetBreaker` 是进程级单例且 opts 只认首次创建**：同名不同参会静默沿用首次配置。"
             "需要多套配置时把参数并进 breaker 名字做命名空间隔离。")
    L.append("- **上游未导出 breaker 缺省值**：需要 trip/cooldown 缺省请在调用方显式固化常量，"
             "别依赖「传 0 走默认」——上游没有这个约定。")
    L.append("")
    L.append("")

    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print(f"# 生成 {args.out}（{len(L)} 行，{len(mods)} 模块 / {npkg} 包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
