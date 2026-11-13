# oagentkit 模块索引（tier 分层）

> 由 gen_skill.py 从 modules.json 渲染——模块集合以 modules.json 为真相源，本文件勿手改。
深入阅读 = 该仓 AGENTS.md（per-repo 硬规则 agentNotes）→ 包 doc.go。

安装：按需 `go get github.com/oagentkit/<repo>/...@latest`；**别 import agentkit 门面**（已废弃）。

| tier | 模块 | 一句话 | heavyDeps（import 即拖入） | 深入 |
|---|---|---|---|---|
| tier0 | [`guardx`](https://github.com/oagentkit/guardx) | 脱敏/出口围栏/权限判定/沙箱档位/跨进程协议 | — | `guardx/AGENTS.md` |
| tier0 | [`runtime`](https://github.com/oagentkit/runtime) | agent 进程运行时——流式节控/泛型事件总线/子进程环境/热插拔/钩子 | — | `runtime/AGENTS.md` |
| tier0 | [`textx`](https://github.com/oagentkit/textx) | 文本/差异/会话/报告纯函数集 | — | `textx/AGENTS.md` |
| tier1 | [`promptx`](https://github.com/oagentkit/promptx) | system prompt 分段组装/运行中转向/文件读后写一致性 | — | `promptx/AGENTS.md` |
| tier2 | [`llm`](https://github.com/oagentkit/llm) | 模型客户端/降级链/预算/成本/阶段路由/调用观测 | eino、go-openai、openai | `llm/AGENTS.md` |
| tier2 | [`mcpkit`](https://github.com/oagentkit/mcpkit) | MCP 客户端/工具池/建连单飞 | mcp-go、jsonschema、eino | `mcpkit/AGENTS.md` |
| tier2 | [`obs`](https://github.com/oagentkit/obs) | 观测客户端 + 审计留痕 | — | `obs/AGENTS.md` |
| tier2 | [`queue`](https://github.com/oagentkit/queue) | DB 即队列 + Postgres 选主 Lease | pq、sqlite | `queue/AGENTS.md` |
| tier2 | [`rag`](https://github.com/oagentkit/rag) | 知识检索——向量化/混合召回/重排 | eino、v2 | `rag/AGENTS.md` |
| tier2 | [`skillkit`](https://github.com/oagentkit/skillkit) | SKILL.md 渐进披露 + 领域包契约 + 装配血缘 | eino、yaml.v3、v3 | `skillkit/AGENTS.md` |
| tier2 | [`toolgate`](https://github.com/oagentkit/toolgate) | 工具优先级/并发调度/操作审计门/不可绕过的调用链 | eino、yaml.v3 | `toolgate/AGENTS.md` |
| tier2 | [`tools`](https://github.com/oagentkit/tools) | CLI 编码 agent 统一调用/公开检索/git 沙箱/SAST 扫描/Shell 静态裁决 | eino、v3 | `tools/AGENTS.md` |
| tier3 | [`agent`](https://github.com/oagentkit/agent) | ReAct 运行样板/反思循环/意图路由/多专家黑板/派发守卫/澄清 | eino、openai、yaml.v3 | `agent/AGENTS.md` |
