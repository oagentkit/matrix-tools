---
name: oagentkit
description: >-
  在 Go 业务代码中使用/重构时选用 oagentkit 框架包的选型与用法入口（13 模块 + agentkit 废弃门面）。
  触发场景：建 agent（ReAct/反思/路由/多专家）、模型调用（降级链/预算/阶段路由）、工具体系
  （MCP/调度/审计门/调用契约）、安全围栏（提示词注入/出口 SSRF/脱敏/许可沙箱）、RAG、DB 队列、
  prompt 组装、SKILL 技能装配、观测审计、文本处理，或不确定该用哪个模块/子包时。
  含场景选型表、tier/heavyDeps 红线、48 子包速查。发版/tag/CHANGELOG 用 oagentkit-release；
  issue 扫描复用 oekit-sweep（--org oagentkit）。
---

# oagentkit 包选型与用法入口

> oagentkit = Open Agent Kit：13 模块（tier0-3 自底向上）+ 已废弃 agentkit 门面。
> 本 skill 由 `matrix-tools/scripts/gen_skill.py` 渲染——场景表为手写数据、模块/子包索引为
> modules.json 渲染产物；修订改 gen_skill.py 数据区（或 modules.json），勿手改本文件。

## 使用流（四步）

1. **选型**：场景表定位模块/子包；先看 tier 与 heavyDeps（import 即拖依赖面）
2. **拿准签名**：`go doc github.com/oagentkit/<repo>/<pkg>` 或读包 doc.go——禁止凭记忆猜 API
3. **套红线**：下方速览；per-repo 硬规则在该仓 AGENTS.md（agentNotes 的单源在 modules.json）
4. **深入**：同目录 `modules.md`（tier 索引）/ `pkgs.md`（全包速查）→ 该仓 AGENTS.md

## 场景选型表

### Agent 应用骨架

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 建 ReAct agent（运行样板/反思循环） | `agent`（agentrun/reflection）+ `runtime` + `llm` | agentrun 是组装样板；反思循环 reflection；进程骨架（节控/事件总线/钩子）由 runtime 供 | panic 不能只转 error：`RunSafe*` 收进全局 PanicHandler 后返回的 `panic: X` 无哨兵类型——丢结构化上下文前先确认可接受（llm AGENTS） |
| 意图路由 / 多专家协作 | `agent` router/blackboard/dispatch/clarify | router 意图路由、blackboard 多专家黑板、dispatch 派发守卫、clarify 澄清 | — |
| agent 进程运行时（节控/热插拔/子进程） | `runtime`（pacer/progress/procx/hotplug/hookx/notifyx） | 流式节控 pacer、泛型事件总线 hookx/notifyx、子进程环境 procx（配 guardx/egressproxy 强制出口） | — |

### 模型调用

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 模型客户端 / 降级链 / 预算 | `llm`（llm/llm/wrap） | 多后端 port 抽象 + 降级链 wrap；成本与预算面在根包 | 降级链每一跳必须记录原因（成本/限流/超时/内容策略），不得静默换模型；预算判定必须在调用前——事后扣减等于没有预算（llm AGENTS） |
| 阶段路由 / 调用观测 | `llm`（llm/decision、obsx） | decision 按阶段选模型；obsx 调用观测（v0.12.0 自 obs/obsx 迁入） | — |
| 模型调用的测试替身 | `llm`（llm/llmtest） | 单测不打真端点；契约与真实现同源 | — |

### 工具体系

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| MCP 工具接入（客户端/工具池/建连单飞） | `mcpkit`（mcp） | mcp-go 重依赖隔离在此仓 | mcpkit 的**信封包装是特性不是缺陷**——工具结果带元数据信封，别在调用方剥掉（跨仓集成契约） |
| 工具优先级 / 并发调度 / 审计门 | `toolgate`（toolprior/toolsched/policy） | policy 是不可绕过调用链的裁定者；yaml 配置热更 | — |
| 工具调用统一契约（框架无关） | `guardx`（invoke/toolfail） | invoke 不携带 eino/policy 类型（可在任何层用）；toolfail 失败分类 | 不可分类即不可重试；重发安全与重试价值是两个正交维度（toolfail 契约） |
| CLI 编码 agent 调用 / 公开检索 / git 沙箱 / SAST | `tools`（acpx/websearch/workcopy/sast/bashguard/bgjob） | acpx 统一调用 CLI agent；bashguard Shell 静态裁决；bgjob 后台任务 | — |

### 安全围栏（guardx）

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 提示词注入防护（数据侧越权） | `guardx/fence` | 中和 LLM 产出/用户输入中的 markdown 结构；系统侧伪造配 injectband | 围栏判定**单源在 oekit/pkg**（跨 org 契约）——guardx 消费单源，别在调用方重写判定（跨仓集成契约） |
| 系统提示防伪（来源标记） | `guardx/injectband` | 系统带外注入打来源标记 + 转义 body 内伪造同形标签（含大小写/空白变体）；与 fence 互补 | — |
| 出口围栏 / 进程级出口强制 | `guardx`（egress/egressproxy） | egress 字面量层 URL 校验；egressproxy 只听回环的本地代理——子进程出站经目标准入才能出网（配 runtime/procx） | — |
| 日志/审计凭据脱敏 | `guardx/logredact` | 连接串/URL/头中凭据落盘前打码；规则顺序敏感 | 规则秩序有契约（收窄/RawNamer 等）——改规则前读 guardx AGENTS，别按直觉排序 |
| 工具许可判定 / 沙箱档位 | `guardx`（permgate/sandbox） | permgate：谁在什么模式下对什么输入得到什么许可；sandbox 执行档位 | — |

### 知识 · 任务 · 技能

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| RAG（向量化/混合召回/重排） | `rag`（knowledge/rag） | milvus SDK 重依赖隔离在此 | — |
| DB 即队列 / PG 选主 | `queue`（worker/sqlqueue/pglease/sqllease） | worker 消费骨架 + pglease Postgres Lease 主备单活 | — |
| system prompt 组装 / 运行中转向 | `promptx`（sysprompt/steering/filestate） | 分段组装 sysprompt、运行中转向 steering、文件读后写一致性 filestate | — |
| SKILL.md 渐进披露 / 装配血缘 | `skillkit`（skill/pack/lineage） | skill 渐进披露、pack 领域包契约、lineage 装配血缘 | — |

### 观测 · 文本基础

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| Langfuse 观测 / 审计留痕 | `obs`（langfuse/audit） | 模型调用观测走 llm/obsx（已迁入 llm），obs 仓是客户端与审计 | — |
| 文本/差异/代码索引/会话压缩/报告指纹 | `textx`（textutil/diffx/codeintel/compact/conversation/reportutil） | tier0 纯函数零依赖；compact 三轨 auto-compact 阈值不得写死在实现（有边界测试契约） | — |

## 红线速览（出处标注；per-repo 全量在该仓 AGENTS.md）

- **直接 import `github.com/oagentkit/<repo>/<pkg>`——agentkit 门面已废弃**（存量迁移兼容层，只做别名转发；新代码进门面 = 给退役层续命）
- **import 前看 tier/heavyDeps**（modules.json）：tier0 零重量级三方；带 eino/milvus/mcp-go 的仓是 tier2——只要接口不要实现时别 import 实现仓
- 围栏判定单源在 oekit/pkg（跨 org 契约）：guardx 消费单源，调用方不重写判定
- mcpkit 信封包装是特性：工具结果带元数据信封，别在调用方剥掉
- logredact 规则秩序有契约（收窄/RawNamer）：改规则前读 guardx AGENTS
- 降级链每一跳记录原因，不得静默换模型；预算判定在调用前（llm）
- 工具失败不可分类即不可重试（guardx/toolfail）
- panic 收编先确认丢上下文可接受：RunSafe* 无哨兵类型（llm）

## 流程任务（另装的兄弟 skill）

- 发版 / tag / CHANGELOG / 矩阵同步 → **oagentkit-release**（release.py prewrite→tag→sync-matrix 流水线）
- issue 扫描 / 清零 → **oekit-sweep**（工具通用：`issue_sweep.py --org oagentkit`）

## 深入入口

- 模块索引：同目录 `modules.md` · 子包速查：同目录 `pkgs.md`
- 下游/远端 AI：<https://oagentkit.github.io/llms.txt>
- 门面迁移：agentkit 仓 MIGRATION.md（旧 import → 新前缀映射）
