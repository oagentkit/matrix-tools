# oagentkit 子包速查（13 模块全包面）

> 由 gen_skill.py 从 modules.json 的 packages/pkgDesc 渲染；API 以 `go doc github.com/oagentkit/<repo>/<pkg>` 为准。

### guardx（tier0）

| 子包 | 用途 |
|---|---|
| `fence` | 中和不可信文本（LLM 产出、用户输入）中的 markdown 结构，防提示词注入 |
| `logredact` | 日志/审计载荷凭据脱敏：连接串/URL/头中的凭据落盘前打码 |
| `egress` | 出口围栏：LLM 可控 URL 的字面量层 SSRF 防护 |
| `egressproxy` | 进程级出口强制：只监听回环的本地 HTTP 代理（含 CONNECT 隧道），「声明了受限出口」的兑现件——子进程出站流量经目标准入（白/黑名单 + IP 围栏 + 解析钉住）后才能出网 |
| `permgate` | 工具调用许可判定链：谁在什么模式下对什么输入得到什么许可 |
| `injectband` | 系统带外注入的防伪包裹通道：给系统提示打来源标记，并转义 body 内伪造的同形标签（含大小写/空白变体），防身份伪造。与 fence 互补——fence 封数据侧越权，injectband 封系统侧伪造 |
| `invoke` | 工具调用统一契约 + 不可绕过的调用链（构造即保证）；框架无关，不携带 eino / policy 类型 |
| `toolfail` | 工具调用失败的分类与处置契约：类别 → 可重试性/退避/需人工/重发安全/面向模型的处置指引。不可分类即不可重试；重发安全与重试价值是两个正交维度 |
| `sandbox` | 执行沙箱的档位语义与后端协议：Level（none/process/os/machine，序即强弱）+ Provider + Registry。要求档位是下限，选不到满足者即拒绝、绝不降级 |
| `wire` | 跨进程调用协议：帧格式（4B 大端长度 + JSON 负载，不用行分隔——工具输出可能含换行）+ 两条通道（启动通道 Starter/Runner 一次执行、调用通道 Server/Client N 次工具调用）；失败分类跨进程同源（取 toolfail 的分类）、预算权威在父侧、在途调用有硬上限、取消传播靠管道关闭 |

### runtime（tier0）

| 子包 | 用途 |
|---|---|
| `pacer` | 流式增量节流器：LLM token 分片攒够窗口再发，避免逐条发布洪泛事件通道 |
| `progress` | 泛型事件总线 `Bus[T]`（发布订阅 + 慢消费者丢弃计数） |
| `procx` | 子进程最小环境构造 `ChildEnv` + 执行封装，供 spawn 外部进程的包共用 |
| `hotplug` | 运行时插拔与原子热替换：`Plugboard` 启停视图 + `Holder[T]` 快照持有点 |
| `hookx` | 外部 hook 拦截协议：在关键拦截点派生子进程，stdin 喂 JSON |
| `notifyx` | 生命周期事件通知出口（notify sink）：归一化事件契约可靠外发（webhook/HTTP），Emit 永不阻塞主流程，丢弃计数 + OnDrop 可观测 |
| `notifyx/hook` | 挂点侧官方 shim：平台 shell 协议载荷归一化为 notifyx.Payload，映射表/ack 轮询/跨进程节流单源，cmd/notifyx-hook 供 hooks 一行接入 |

### textx（tier0）

| 子包 | 用途 |
|---|---|
| `textutil` | 按行边界把文本切成 ≤target(rune) 的增量片，凑片不跨行 |
| `diffx` | unified diff 解析纯函数 |
| `codeintel` | 轻量代码符号索引 |
| `compact` | 会话压缩策略面：auto-compact 判定（阈值/熔断/token 三轨）+ 压缩/恢复 |
| `conversation` | 多轮会话历史原语：滚动窗口切分、历史渲染 |
| `reportutil` | 评审/评测报告后处理：severity 归并、sampling、stats、Fingerprint 精确指纹 |

### promptx（tier1）

| 子包 | 用途 |
|---|---|
| `sysprompt` | system prompt 分段组装：Section 化收集 → stable/dynamic 分离 |
| `steering` | 运行中转向队列：turn 执行中用户插入的新输入按投递语义生效 |
| `filestate` | 读后写一致性状态：编辑/覆写文件前必须读过，且读后文件未被改动 |

### llm（tier2）

| 子包 | 用途 |
|---|---|
| `llm` | 模型客户端：多模型降级链、任务级预算、成本记账、分阶段路由、主备切换 |
| `llm/port` | 窄接口层（不引 eino）：Complete(ctx,req) 形态的降级链/预算/成本记账；无 eino 的内核（预算/成本/价表/熔断）由根包下沉而来，两条路径共用同一份实现 |
| `llm/wrap` | 调用包装与限流适配 |
| `llm/llmtest` | 模型层契约测试套件 |
| `obsx` | eino callbacks 体系的 LLM 调用追踪 + token 用量采集 + 分阶段打标（自 obs/obsx 迁入） |
| `llm/decision` | System One 协议判定面客户端（非自回归判定模型）：choice/score/noul 三原语 + 校准置信口径（本地算，两端同义） |

### mcpkit（tier2）

| 子包 | 用途 |
|---|---|
| `mcp` | MCP 客户端/工具池：工具目录、建连单飞、自愈与租约管理 |

### obs（tier2）

| 子包 | 用途 |
|---|---|
| `langfuse` | Langfuse Public API 只读客户端（v3 self-hosted 契约面） |
| `audit` | 敏感操作的结构化审计留痕（自 guardx/audit 迁入）：字段值统一经 logredact 打码 |

### queue（tier2）

| 子包 | 用途 |
|---|---|
| `worker` | 任务表驱动的工作池（DB 即队列）+ `LeaseStore`/`TaskQueue` 存储接口与参考实现 re-export |
| `worker/pglease` | `worker.LeaseStore` 的 PostgreSQL 实现（条件 UPSERT 一行即成） |
| `sqllease` | `worker.LeaseStore` 的 database/sql 参考实现（PG/MySQL/SQLite 三方言） |
| `sqlqueue` | `worker.TaskQueue` 的 database/sql 参考实现（三方言） |

### rag（tier2）

| 子包 | 用途 |
|---|---|
| `knowledge/rag` | 知识检索：Embedding 接口、向量后端可插拔、混合召回与重排 |

### skillkit（tier2）

| 子包 | 用途 |
|---|---|
| `skill` | SKILL.md 渐进披露：元数据常驻、正文按需加载；多根技能库与版本判定 |
| `pack` | 领域包（expert/pack 目录布局）的契约面 |
| `lineage` | 装配血缘：expert → skill → MCP server 三类资产节点的依赖图 |

### toolgate（tier2）

| 子包 | 用途 |
|---|---|
| `toolprior` | 工具优先级三层约束（收紧语义，下层已设的限制不得放宽） |
| `toolsched` | 注解驱动的工具并发调度与去重 |
| `policy` | 操作审计门：敏感工具调用的策略判定 |

### tools（tier2）

| 子包 | 用途 |
|---|---|
| `acpx` | CLI 编码 agent 统一调用（claude / codex / gemini / kimi / mcode 等） |
| `websearch` | 公开资料检索抽象，接口可插拔（缺省 SearXNG 自建实例） |
| `workcopy` | Git 工作副本沙箱：隔离环境 + merge-base 校验 |
| `sast` | 确定性 SAST 前置扫描（不执行被扫代码） |
| `bashguard` | Shell 命令静态风险裁决（自 guardx/bashguard 迁入）：经真 shell AST 判定只读性，不执行命令 |
| `bgjob` | 远程后台作业 marker 协议原语：经宿主既有 SSH 会话起「脱离会话的长任务」，退出码走 marker 文件做终态探测与收尾（tools#106） |

### agent（tier3）

| 子包 | 用途 |
|---|---|
| `agentrun` | ReAct 运行样板与计划先行（PlanAndExecute）编排 |
| `reflection` | 反思循环 |
| `router` | 显式意图路由 |
| `blackboard` | 无中心多专家协作黑板 |
| `dispatch` | 通用派发守卫：allow 矩阵 + 深度上限 + 自派发拒绝 |
| `clarify` | 澄清与标准化词表内核 |
