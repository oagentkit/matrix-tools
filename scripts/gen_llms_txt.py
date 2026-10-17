#!/usr/bin/env python3
"""从 modules.json 生成 llms.txt —— 面向 LLM 的模块索引。

对标 oekit 的 https://oekit.github.io/llms.txt：模块清单 + 分层 + 安装行 + AI Agent 要点。
模块信息全部来自 modules.json，不手写，避免与矩阵漂移。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    L.append("- **每仓根目录的 `AGENTS.md` 是该仓的权威规范**：分层不变量、"
             "错误处理、日志、并发安全、API 命名，以及该仓特有的工程红线。")
    L.append("- 安装一律 `go get github.com/oagentkit/<mod>@latest`；不要引门面仓 `agentkit`。")
    L.append(f"- 完整包路径映射见 [matrix-tools/modules.json](https://github.com/{org}/matrix-tools)。")
    L.append("- 改 CI 规则只去 [{org}/.github](https://github.com/{org}/.github) 仓"
             "（`go-ci.yml@main`）；各仓 `ci.yml` 只是薄调用层，不要在各仓复制规则。")
    L.append("- 不确定 API 时以源码为准：各仓均带测试，`go doc github.com/oagentkit/<mod>[/<pkg>]` 可查。")
    L.append("")
    L.append("## 易踩的坑")
    L.append("")
    L.append("- **`progress` 撞名但语义相反**：`oekit/concurrency/progress` 是长任务**阶段进度追踪器**，"
             "而 `oagentkit/runtime/progress` 是泛型**事件总线**。按包名对齐会做错。")
    L.append("- **`pacer` ≠ `ratelimit`**：前者是聚合缓冲（攒够窗口再发），后者是准入控制。")
    L.append("- **`runtime/breaker` 待退役**：与 `oekit/concurrency/breaker` 重叠但非同族"
             "（后者是自适应概率丢弃，前者是三态熔断器）。上游需求见 "
             "[oekit/concurrency#17](https://github.com/oekit/concurrency/issues/17)，"
             "落地前不要删除。")
    L.append("")

    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print(f"# 生成 {args.out}（{len(L)} 行，{len(mods)} 模块 / {npkg} 包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
