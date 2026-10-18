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
from matrix_paths import OLD_ROOT, misleading  # noqa: E402  路径规则与 rewrite_imports.py 同源

OLD_ROOT_SHORT = OLD_ROOT.rsplit("/", 1)[-1]  # agentkit，用于文档里写短路径

TIER_TITLE = {
    0: "基础层 · 零仓内依赖或仅依赖同仓包",
    1: "能力层 · 依赖 tier0",
    2: "集成层 · 依赖 tier0/tier1 + 重第三方地板",
    3: "编排层 · 依赖 llm 等 tier2 门面",
}
TIER_SLUG = {0: "foundation", 1: "capability", 2: "integration", 3: "orchestration"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--site", default="https://oagentkit.github.io")
    args = ap.parse_args()

    d = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = d["defaultOrg"]
    mods = sorted(d["modules"], key=lambda m: (m["tier"], m["repo"]))
    npkg = sum(len(m["packages"]) for m in mods)

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
    L.append("## 模块")
    L.append("")
    L.append("依赖严格自底向上，四个能力层：")
    L.append("")

    for tier in sorted({m["tier"] for m in mods}):
        L.append(f"### {TIER_TITLE[tier]}")
        L.append("")
        for m in [x for x in mods if x["tier"] == tier]:
            pkgs = "、".join(f"`{p.rsplit('/', 1)[-1]}`" for p in m["packages"])
            L.append(f"- [{m['repo']}](https://github.com/{org}/{m['repo']}): "
                     f"{m['ghDescription']}")
            L.append(f"  - 包：{pkgs}")
            if m.get("dependsOn"):
                deps = "、".join(f"[{x}](https://github.com/{org}/{x})" for x in sorted(m["dependsOn"]))
                L.append(f"  - 依赖：{deps}")
            L.append(f"  - 安装：`go get github.com/{org}/{m['repo']}@latest`")
        L.append("")

    L.append("## 门面仓（迁移过渡）")
    L.append("")
    L.append(f"- [agentkit](https://github.com/{org}/agentkit): 原单仓 "
             f"`git.enjoye.top/enjoydream/agentkit` 的兼容门面，{npkg} 包 re-export，"
             f"v1.x 后退役。新代码不要引它。")
    L.append(f"- 完整旧→新包路径映射见该仓 [MIGRATION.md](https://github.com/{org}/agentkit/blob/main/MIGRATION.md)。")
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
    L.append(f"- **import 路径 = `github.com/{org}/{{仓名}}/{{包名末段}}`**。"
             "旧单仓 `git.enjoye.top/enjoydream/agentkit/<包>` 的**中间层目录会被丢掉**，"
             "因为新矩阵里那层组织职责由**仓**承担。例如：")
    for old, new in traps:
        L.append(f"    - `{old}` → `{new}`")
    # 「仓名==末段」的条目单独点名：它是最容易被「优化」掉的错误。
    same = [(o, n) for o, n in traps if n.rsplit("/", 2)[-2] == n.rsplit("/", 1)[-1]]
    tail = (f"其中 `{same[0][0]}` → `{same[0][1]}` 尤其坑："
            "仓名与包名末段相同时**不能省掉一段**，直觉会以为可以合并。"
            if same else "")
    L.append(f"  {tail}完整 {len(traps)} 条易踩对照、及全部 {npkg} 条映射见 "
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
