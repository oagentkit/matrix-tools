# oagentkit 子包速查（16 模块全包面）

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
| `netguard` | 出站 URL 守卫（SSRF 防护）：凡服务端代发 HTTP 的出口统一判定；IP 策略依据单源 egress，不私补网段表 |
| `rbac` | 角色能力矩阵机制：角色→能力集查表、只读档由矩阵推导、授权面指纹；词表是宿主数据，本包只收机制 |
| `ticket` | 一次性短时授权票据：签发/消费/过期/用途绑定，消费即作废（用过即焚）；机制与策略分离 |

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
| `gotyped` | go/types 精确调用边抽取——嵌套模块隔离 go/packages 重依赖（x/tools 等），根模块 tier 0 地板零抬升；产出 caller→callee 结构化边，替换词法面同名假边 |
| `budget` | — |
| `spill` | — |
| `offload` | — |

### toolname（tier0）

| 子包 | 用途 |
|---|---|
| `toolname` | 工具名 → 程序内标识符：IsJSIdent 判定原样可用、Normalize 给出规整名（数字开头补 _、前导非法字符跳过、已开始后记 _） |

### promptx（tier1）

| 子包 | 用途 |
|---|---|
| `sysprompt` | system prompt 分段组装：Section 化收集 → stable/dynamic 分离 |
| `steering` | 运行中转向队列：turn 执行中用户插入的新输入按投递语义生效 |
| `filestate` | 读后写一致性状态：编辑/覆写文件前必须读过，且读后文件未被改动 |
| `pending` | — |
| `sessionref` | — |
| `memorypipe` | — |

### llm（tier2）

| 子包 | 用途 |
|---|---|
| `llm` | 模型客户端：多模型降级链、任务级预算、成本记账、分阶段路由、主备切换 |
| `llm/port` | 窄接口层（不引 eino）：Complete(ctx,req) 形态的降级链/预算/成本记账；无 eino 的内核（预算/成本/价表/熔断）由根包下沉而来，两条路径共用同一份实现 |
| `llm/wrap` | 调用包装与限流适配 |
| `llm/llmtest` | 模型层契约测试套件 |
| `obsx` | eino callbacks 体系的 LLM 调用追踪 + token 用量采集 + 分阶段打标（自 obs/obsx 迁入）＋ Langfuse Public API 只读客户端（自 obs/langfuse 迁入，读写同域） |
| `llm/decision` | System One 协议判定面客户端（非自回归判定模型）：choice/score/noul 三原语 + 校准置信口径（本地算，两端同义） |
| `conformance` | — |

### mcpkit（tier2）

| 子包 | 用途 |
|---|---|
| `mcp` | MCP 客户端/工具池：工具目录、建连单飞、自愈与租约管理 |
| `iface` | MCP 工具调用面的协议中立声明：失败分类学（ErrorKind）、结果分类载体（ClassifiedError）与工具能力接口（RawNamer/OutputSchemer）——只依赖标准库，供不要 mcp-go 的消费方使用 |
| `conformance` | — |

### obs（tier2）

| 子包 | 用途 |
|---|---|
| `audit` | 敏感操作的结构化审计留痕（自 guardx/audit 迁入）：字段值统一经 logredact 打码 |

### queue（tier2）

| 子包 | 用途 |
|---|---|
| `worker` | 任务表驱动的工作池（DB 即队列）+ `LeaseStore`/`TaskQueue` 存储接口与参考实现 re-export |
| `worker/pglease` | `worker.LeaseStore` 的 PostgreSQL 实现（条件 UPSERT 一行即成） |
| `sqllease` | `worker.LeaseStore` 的 database/sql 参考实现（PG/MySQL/SQLite 三方言） |
| `sqlqueue` | `worker.TaskQueue` 的 database/sql 参考实现（三方言） |
| `outbox` | 统一副作用 outbox 队列：宿主按 kind 注册退避规格与处理器，机制面只认规格不认业务（与 sqlqueue 的任务认领划清边界） |

### rag（tier2）

| 子包 | 用途 |
|---|---|
| `knowledge/rag` | 知识检索：Embedding 接口、向量后端可插拔、混合召回与重排 |
| `wiki` | 持久 wiki 的写入面：页面 upsert/删除/乐观版本 + 增量摄取与结构检查；不做检索（读侧索引由宿主交给 Indexer 接缝） |
| `wiki/fsstore` | wiki.Store 的文件系统参考实现：一页一个 markdown（元数据 YAML front-matter），同目录临时文件 + Rename 原子替换 |
| `milvus` | 向量后端（Milvus）实现：rag/milvus 子包——**只有它链向量 sdk**，根包零向量依赖（本地索引/混合召回调用方不必拖进 milvus 传递链） |
| `ontology` | 实体/关系本体层：Schema/Type/Field 与 Triple 契约 + Validate 判据面 + Store 接口（内存参考实现）——不做抽取、不建图库，契约与领域无关 |
| `embedtest` | — |

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
| `cmdpolicy` | 命令级放行裁决与事实面：命令解析后判定放行/拒绝，裁决与事实同仓；只收机制，业务词表由宿主注入 |

### agent（tier3）

| 子包 | 用途 |
|---|---|
| `iface` | — |
| `agentrun` | ReAct 运行样板与计划先行（PlanAndExecute）编排 |
| `reflection` | 反思循环 |
| `router` | 显式意图路由 |
| `blackboard` | 无中心多专家协作黑板 |
| `dispatch` | 通用派发守卫：allow 矩阵 + 深度上限 + 自派发拒绝 |
| `clarify` | 澄清与标准化词表内核 |
| `roundguard` | — |

### goagent-kernel（tier4）

| 子包 | 用途 |
|---|---|
| `archcheck` | 架构门禁执行体（可被宿主复用：policy 声明面→逐条判定） |
| `backend` | 任务式执行后端契约（EgressEnforcer/Capable/Spec） |
| `checkpoint` | 编排续跑契约：进度快照 + CAS + 「续跑而非重放」恢复判定 |
| `commitgate` | 环节终态独占提交闸（凭令牌放闸/释放） |
| `contract` | 编排引擎↔宿主状态的唯一边界（纯接口面，零实现） |
| `controlplane` | 介入契约：任务查看/运行状态/失败诊断/人工介入 + Store 原子提交 |
| `coord` | Board/Store 适配形态约定（控制面/集群面共用） |
| `cost` | 运行成本治理口径面 |
| `deadline` | 会话级墙钟锚点（首轮认领写一次、续跑轮沿用） |
| `delegate` | 异构委托契约面 |
| `delta` | 增量节流的编排层：(kind,iteration) 键控 + 确定性收口（底座=runtime/pacer） |
| `dispatch` | 改派失败面：三档分类 + 两级判定 + 改派环（可达性归上游 agent/dispatch） |
| `drain` | 优雅排水：轮 ctx 与根 ctx 取消脱钩 + 在途轮收敛 |
| `egress` | 出口策略：三档单调可比 + 兑现判据 + 强度（declared/enforced 分开） |
| `eval` | 录制/重放/差异（轨迹回放判定） |
| `eventbus` | 进程内有损广播 + durable 发布面 + event-tail 补灌（Store 由宿主实现） |
| `finalize` | 中止→终态四分路由 |
| `gatecheck` | 审批层×沙箱层联合判据（两层都同意才放行） |
| `guardset` | 守卫装配契约：多守卫合成一条拒绝（接上游 guardx/invoke） |
| `handoff` | 跨进程交接契约（交接点/幂等键/回执地址/时限四问） |
| `hookengine` | hook 匹配/超时/fail-closed 裁决判据（传输协议归上游 runtime/hookx） |
| `invocation` | 工具调用账本：崩溃后调用身份存续（planned/failed 终态） |
| `invocation/invocationtest` | — |
| `metrics` | 内核指标口径契约面（名称/类型/维度/基数上界单一来源） |
| `ownership` | 跨区域所有权权威（提交栅栏纯判据） |
| `preset` | 具名不可变组合件（egress+gatecheck 预设） |
| `protocol` | 统一报告协议：Report 契约 + 五级容错解析 + 白名单校验 + Markdown 渲染 |
| `ptc` | 程序化工具编排沙箱：模型写程序→VM 执行→经 bindings 调宿主工具（三重预算） |
| `raceprobe` | race 门禁失败形态判定（纯函数层） |
| `reconcile` | 启动对账/归因收敛 |
| `retryplan` | 重试处置决策：幂等键重放 vs 原样重发的编排级合成（判据=上游 toolfail） |
| `scheduling` | Agent 调度判据面：优先级/加权公平/配额/背压（纯函数，不做队列不做执行） |
| `sideeffect` | 副作用「至多一次」跨进程契约：幂等键 + 租约 + 栅栏令牌 + 结果未知态 |
| `sidetest` | sideeffect.Store 共享契约测试套件（宿主存储实现跑同一套断言） |
| `skilldisclose` | 技能披露三信号确定性挑选（与 skillkit/skill 邻接） |
| `statemachine` | 环节状态机：条件迁移的纯函数件 |
| `steer` | 运行中接管五件套 |
| `taskboard` | 多 agent 任务板：DAG 依赖 + 认领租约 + 接管代次（boardtest 契约套件随行） |
| `taskboard/boardtest` | — |
| `tenant` | 多租户判据面：隔离/配额/权限上下文（数据面归宿主） |
| `tooldisclose` | 渐进式工具披露：确定性打分检索换完整 schema + 两类失败二分执行门禁 |
| `trajectory` | 事件→轨迹行的投影判据（实测/估算不混用） |
| `watchdog` | 被动配对不变量监视（只告警不裁决） |
| `worker` | 任务驱动循环：注册→心跳→领取→续期→恢复→退出 |
| `worktree` | 工作区冲突判据（声明面） |
| `worker/faultmatrix` | 故障注入矩阵本体（NetworkPolicy/PG HA 等真环境判据；CI 提供环境后由宿主测试入口执行） |

### goagent-runtime（tier5）

| 子包 | 用途 |
|---|---|
| `chat` | 供应商中立模型窄接口（单方法非流式契约保底 + Delta 流式承载 + DeltaAggregator 归并器） |
| `pipeline` | 工具注册与副作用施加映射（幂等键/资源互斥/重试安全声明 → kernel sideeffect） |
| `agentloop` | 可恢复回合循环：预算→模型→审批→施加→Joint 联合提交→账本续跑 |
| `adjudicate` | 副作用未知态的周期裁决框架（枚举→grace→Resolver 链→kernel Runner.Reconcile） |
| `storage` | 存储判据面 Provider 聚合契约 + 内存实现（零驱动边） |
| `storage/sqlitestore` | standalone 形态 SQLite Provider 绑定（驱动边只在此包） |
| `sqliteadapter` | SQLite(WAL) 参考实现：11 面统一门面（kernel 契约套件同源） |
| `pgstore` | PostgreSQL 参考实现：11 面统一门面 + Outbox 事件面 + SIGKILL 故障恢复 e2e |
| `runtime` | 模板装配（NewEmbedded 内存 / NewStandalone SQLite） |
| `cluster` | 多副本模板装配（PG + worker 池：认领/租约/接管代次/跨副本事件面） |
| `observe` | eventbus → 宿主回调薄适配 |
| `containerbackend` | 容器执行后端参考实现（Docker/K8s Job）——宿主抄它不 import 它 |
| `bridge/llmchat` | 钦定适配器：oagentkit/llm → chat.Model（嵌套模块，LLM SDK 边只在 bridge） |
| `bridge/einotool` | 钦定适配器：eino InvokableTool → pipeline.Tool（副作用闸字段不猜） |
| `trace` | — |
| `agentiface` | — |
| `preset` | — |
