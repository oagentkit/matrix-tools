#!/usr/bin/env python3
"""从 modules.json 生成各仓的 README.md 与 AGENTS.md。

为什么生成而不是手写：13 个仓的文档有 90% 是同构的（分层声明、硬规则、依赖表、
「不属于本仓」表），手写必然漂移。仓特有的纪律走 modules.json 的 agentNotes 字段。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/split-matrix/modules.json"

TIER_ROLE = {
    0: "基础层，零仓内依赖或仅依赖同仓包",
    1: "能力层，依赖 tier0",
    2: "集成层，依赖 tier0/tier1 + 重第三方地板",
    3: "编排层，依赖 llm 等 tier2 门面",
}

COMMON_RULES = """### 分层与依赖

- 依赖严格自底向上，**禁止反向依赖**。本仓是 tier {tier}，只允许依赖 tier ≤ {tier} 的仓。
{extra_dep}
- 改 module 路径必须同步：全部 import、go.mod、README 安装行。

### 错误处理

- 一律使用 `github.com/oekit/pkg/errors`；业务错误用 BizError。
- 所有 error 禁止 `_` 忽略。

### 并发安全

- 禁止裸 `go func`：用 `github.com/oekit/concurrency/async` 的 GoSafe 系列（panic 被 recover）。
- 共享状态用 mutex / atomic 保护；包级全局可变状态用 `atomic.Pointer[T]`。

### API 设计

- 函数选项模式命名 `XxxOption`（禁止裸 `Option`，多包 co-import 会冲突）。
- 导出符号 PascalCase + godoc 注释；包文档放 `doc.go`。
"""


def dep_line(m: dict, all_mods: dict) -> str:
    deps = m.get("dependsOn", [])
    if not deps:
        return "- 本仓无矩阵内依赖，可被任意层引用。"
    return "- 本仓依赖的矩阵内仓：" + "、".join(
        f"`{d}`" for d in sorted(deps)
    ) + "。"


def build_readme(m: dict, all_mods: dict) -> str:
    repo = m["repo"]
    pkgs = m["packages"]
    ext = m.get("heavyDeps", [])
    rows = "\n".join(
        f"| `{p.rsplit('/', 1)[-1]}` | {m.get('pkgDesc', {}).get(p, '')} |" for p in pkgs
    )
    notes = m.get("agentNotes", "")
    return f"""# {repo}

{m['ghDescription']} —— oagentkit 模块矩阵 **tier {m['tier']}**（{TIER_ROLE[m['tier']]}）。

```
go get github.com/oagentkit/{repo}
```

| 包 | 职责 |
|---|---|
{rows}

## 定位

**tier {m['tier']}** — {TIER_ROLE[m['tier']]}。

{dep_line(m, all_mods)}

## 外部依赖地板

{chr(10).join(f'- `{d}`' for d in ext) if ext else '- 零外部依赖。'}

## 与 oekit 的关系

本矩阵（oagentkit，Agent/LLM 编排工具链）**依赖** oekit（Open Easy Kit，Go 微服务工具链）：
`oekit/pkg` / `oekit/concurrency` / `oekit/logx` 等。oekit 是另一套独立矩阵，不是子品牌。

## 开发

```bash
go test ./... -race
go vet ./...
gofmt -l .
```

规范见 [AGENTS.md](AGENTS.md)。

## License

MIT
{notes}"""


def build_agents(m: dict, all_mods: dict) -> str:
    repo = m["repo"]
    pkgs = "\n".join(f"`{p}/`" for p in m["packages"])
    logging_rule = (
        "- **本仓不引入日志依赖。** tier0 加日志等于抬地板；需要观测输出时返回 error 或交给调用方。"
        if not m.get("loggingRule")
        else m["loggingRule"]
    )
    return f"""# AGENTS.md — oagentkit/{repo}

> {m['ghDescription']}
> module：`github.com/oagentkit/{repo}` · org：[oagentkit = Open Agent Kit](https://github.com/oagentkit)
> 矩阵分层：**tier {m['tier']}**（{TIER_ROLE[m['tier']]}）

## 本仓包含

```
{pkgs}
```

## 硬规则

{COMMON_RULES.format(tier=m['tier'], extra_dep=dep_line(m, all_mods))}
### 日志

{logging_rule}

## 仓特有纪律

{m.get('agentNotes', '（无）')}

## 明确不属于本仓的

| 能力 | 归属 |
|---|---|
| 文本/差异/会话/报告 | `github.com/oagentkit/textx` |
| 事件总线/流控/子进程/热插拔/钩子 | `github.com/oagentkit/runtime` |
| 脱敏/出口围栏/权限/沙箱解析/审计 | `github.com/oagentkit/guardx` |
| DB 队列与选主 | `github.com/oagentkit/queue` |
| 通用 errors/stringx/encoding/mathx | `github.com/oekit/pkg` |
| goroutine 池/单飞/重试/令牌桶 | `github.com/oekit/concurrency` |
"""


def main() -> int:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    all_mods = {m["repo"]: m for m in data["modules"]}
    targets = sys.argv[1:] or list(all_mods)
    for repo in targets:
        m = all_mods.get(repo)
        if not m:
            print(f"矩阵里没有 {repo}", file=sys.stderr)
            return 1
        d = ROOT / ".worktrees/oagentkit-matrix" / repo
        if not d.is_dir():
            print(f"跳过 {repo}: 工作目录不存在")
            continue
        (d / "README.md").write_text(build_readme(m, all_mods), encoding="utf-8")
        (d / "AGENTS.md").write_text(build_agents(m, all_mods), encoding="utf-8")
        print(f"  生成 {repo}/README.md 与 AGENTS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
