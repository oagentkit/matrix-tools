# 执行语义七词总表（消费方视角对照）

> 写给**要在自己产品里编排 Agent 的宿主开发者**：Run / Turn / Step /
> ToolCall / Handoff / Interrupt / Resume 七个词各自是什么、谁有权触发、
> 幂等性怎么判、跨仓现有实现在哪——一张表判完，不再逐仓翻注释。
>
> **本表是消费方视角对照，不是词表单源**：各词的判定单源在 kernel 各包
> （状态词表 `contract/status.go`、环节状态 `statemachine`、回执词表
> `delegate`、交接状态 `handoff`、事件词表 `eventbus`）与 guardx/toolfail
> （失败分类）——本表**引用不重定义**，与单源冲突时以单源为准。
> 形态照 kernel `docs/error-semantics.md` 惯例；锚由
> `scripts/verify_semantic_anchors.py` 机器校验（锚表任一条在代码中消失
> ⇒ 红——**先改表再删测试**，不是测试删了表还挂着）。
>
> 实测日期 **2026-11-28**（issue `oagentkit/agent#175`；开单快照 2026-11-27
> 的抽查经逐词 grep 复核，勘误见 §八）。

## 判定方式

- **每词六维**：定义 / 状态迁移 / 触发权 / 幂等性 / 跨仓实现对照 / 契约测试锚。
- **锚是可执行事实**：锚表（§九）每行 = `仓 + 路径 + 测试函数`，由
  `verify_semantic_anchors.py` 逐条断言 `func <符号>` 在该文件真实存在；
  文件读不到 = 判不了 ⇒ 红（#79 同款：判不了 ≠ 通过）。
- **不一致零悬置**（§七）：每条不一致只有「对齐」或「有意分歧（记 ADR）」
  两个出口，不允许「知道不一致但放着」。

---

## 一、Run — 一次完整执行任务

| 维度 | 内容 |
|---|---|
| 定义 | 给定输入与上下文，驱动 Agent 走到一个终态的完整过程 |
| 状态迁移 | 会话轴词表（`contract/status.go` 单源）：`created → running → succeeded / failed / cancelled / timed_out`（过程态含 `waiting_approval`） |
| 触发权 | 宿主 / 装配层调用入口；会话制入口须持 `sessionID`，一次性入口无会话概念 |
| 幂等性 | 同会话并发 Run 由**快照序号闸**裁决：后到者拿 `ErrStaleSeq` 明确拒绝，不合并执行、不混写快照（`checkpoint` 单源） |
| 跨仓实现对照 | `agent/agentrun.Run`（函数式，配置随调用，返回 `string`）· `goagent-runtime/agentloop.Loop.Run`（结构体方法，返回 `*Result`）· `goagent-runtime/runtime.Session.Run`（会话对象方法，复用 agentloop.Result）· **`agent/iface.Agent`**（统一读面 `Run(ctx, Input) (Result, error)`，ADR-003 裁决）· kernel `contract` **非入口**（`RunContext` 状态读面 + `StepSink` 推进面，宿主驱动） |

## 二、Turn — 一次模型决策与响应周期

| 维度 | 内容 |
|---|---|
| 定义 | 一次「模型看到上下文 → 产出响应」的决策周期；Run 由若干 Turn 组成 |
| 状态迁移 | 无独立状态机——由**事件括号**表达：`turn_started → turn_ended` 必须成对（`eventbus` 词表 + `watchdog` INV3 轮级括号不变量） |
| 触发权 | 编排引擎内部推进（回合循环）；宿主不直接建 Turn |
| 幂等性 | 括号配对判据：未开先关 / 开而不关 / 配对错位即告警（watchdog 三态）；在途 Turn 的取消走 `drain.Turns`（`Active` 计数 + `CancelAll`，快照后逐个调、持锁调取消即死锁——包注释判据） |
| 跨仓实现对照 | kernel `eventbus` 事件对 + `watchdog` 括号不变量 · kernel `drain.Turns`（在途轮能力面：计数 + 批量取消）· runtime `trace.TurnTrace`（观测投影，非控制对象）· agentloop `Result.Rounds`（已执行回合计数读面） |

## 三、Step — 执行节点

| 维度 | 内容 |
|---|---|
| 定义 | 执行的最小落账单元：可包含 ToolCall / LLM 调用 / Handoff / Interrupt 或宿主自定义操作（七词是词汇不是继承层级——总纲 6.1 判词） |
| 状态迁移 | 环节状态词表（`statemachine.StepStatus` 单源，与 `contract` 同取值空间 #354/#357）：12 值 + `CanTransition` / `MustTransition` 判据；非法迁移拒绝（`StateError` 处理方向：拒绝） |
| 触发权 | 编排引擎经 `contract.StepSink`（`Open(name, label) → StepRef` / `Finish(ref, status)`）推进——**引擎不直接碰存储**，换宿主不改编排逻辑 |
| 幂等性 | `StepRef` 由宿主分配、回传给 `Finish`——重复 Finish 同一 ref 由宿主幂等实现吸收；环节状态迁移经 `MustTransition` 校验，重复迁移非法即拒 |
| 跨仓实现对照 | kernel `statemachine`（枚举 + 迁移判据）· kernel `contract.StepRef/StepSink`（推进出口）· runtime W1 `Session/SessionStatus`（会话级读面，环节级在其下） |

## 四、ToolCall — 工具调用

| 细度 | 内容 |
|---|---|
| 定义 | 一次「Agent 请求执行工具」的调用单元，含请求（参数完整、副作用未施加）与收口（outcome + 结果）两半 |
| 状态迁移 | 判定环（`guardx/invoke` 单源）：`Call → Stage 通过/拒绝 → Result(Outcome, Verdict)`；调度面 `toolgate/toolsched`（并发配额、ctx 取消时跳过未开始项） |
| 触发权 | 回合循环内由模型产出触发；执行前经 guardx 判定环（拒绝即不施加副作用），并发度经 toolsched 裁决 |
| 幂等性 | **事件配对语义**：`tool_call` 是执行前请求事件、`tool_result` 是执行后收口事件，两侧配对（agentloop `TestToolEventPairing` 判据）；结果未知（Uncertain）**先对账禁止盲目重试**（`invocationtest` 裁决面） |
| 跨仓实现对照 | `guardx/invoke`（Call/Result/Verdict/Stage 判定环）· `toolgate/toolsched`（Call/Item/Schedule 并发调度）· `mcpkit` 信封（协议层 ErrorKind）· 事件面：agentrun `tool_call`/`tool_result`（snake_case 原生）vs agentloop `agent.tool_call`/`agent.tool_result`（应用层 `agent.` 前缀）——**有意分层**，见 §七-3 |

## 五、Handoff — 任务交接

| 维度 | 内容 |
|---|---|
| 定义 | 把任务从一个执行方转交另一个执行方的全过程：**能不能派**（守卫）→ **协商**（报价/路由）→ **交接台挂单/领取/回执** |
| 状态迁移 | 交接单状态（`handoff.State` 单源）：`offered → accepted → closed`，旁路终态 `expired`（到点未领或领而未回执）/ `withdrawn`（源方收回，重交=新一代）；回执状态 `delegate.Status`：`completed / failed / refused`（handoff.ReceiptStatus 是其**类型别名**，#358 收编） |
| 触发权 | 源方 Agent 发起（派发守卫先过）；接收方原子 `Take` 完成负责人转移；宿主经 `EdgeSource` 提供拓扑边（编译期校验的 allow 矩阵，运行期只读） |
| 幂等性 | 领取并发判据：N 接收方同起跑线抢同一单，**赢家恰好 1 个**（`handoff/contention` 判据长在包里，宿主存储实现跑过即验契约）；回执幂等经 `Ledger` 原子写 + Token 铸造 |
| 跨仓实现对照 | `agent/dispatch`（**能不能派**：`Guard`/`EdgeSource`/`DenyError`，拒绝原因 `not_allowed`/`depth_exceeded`/`self_dispatch`）· kernel `delegate`（**协商**：`Capability/Request/Offer/Route` + `Receipt`）· kernel `handoff`（**交接台**：`Desk/Ledger/State` + 领取并发判据）——三处分工是 ADR-001 任务调度机制行的裁决，不是重复实现 |

## 六、Interrupt — 中断与人工介入

| 维度 | 内容 |
|---|---|
| 定义 | 用户/宿主主动中止在途执行，以及审批等人工介入对执行的暂停与恢复点 |
| 状态迁移 | 会话轴终态 `cancelled`（用户取消）/ `interrupted`；介入过程态 `waiting_approval → approved / rejected`（词表单源 `contract/status.go`） |
| 触发权 | **用户取消唯一身份**：宿主用 `context.WithCancelCause(contract.ErrUserCancelled)` 表达——全仓唯一哨兵，deadline 路由按 cause 三分（用户取消 / 超时 / 关停） |
| 幂等性 | 取消是**幂等收口**：重复取消不重复触发终态迁移；审批挂起以 `PendingCallID` 为锚，`Approve` 落一次决策（agentloop 面）；分类判据 `roundguard.Classify` 决定中断后回合级走法（工具在途 → 续跑收口 vs 强制终稿） |
| 跨仓实现对照 | kernel `contract.ErrUserCancelled`（唯一身份）· kernel `deadline.Route`（三分路由）· `agent/roundguard.Classify`（回合级分类）· runtime 取消链（W3：取消路径 Abandon 扣闸，见 runtime#40/#533） |

## 七、Resume — 恢复

| 维度 | 内容 |
|---|---|
| 定义 | 中断/失败后从断点继续执行；结论未知的调用先对账再决定重做 |
| 状态迁移 | 断点读写经 `checkpoint`：快照 `Commit` 严格递增，序号回退被 `ErrStaleSeq` 拒（**恢复点不是回退**——被拒的重试永久拒，调用方丢弃该次落库）；恢复后状态从快照态续跑 |
| 触发权 | 恢复面（`checkpoint`/`reconcile`）驱动；两 worker 抢同一任务由单调闸裁决（恰好一个赢家） |
| 幂等性 | **对账优先**：结论未知（Uncertain）的调用先裁决再动作，不许盲目重做（`invocationtest` 三态：可重做 / 须对账 / 拒绝）；`TestFaultResumeInterrupted*` 系列钉住中断点前后两种恢复形态 |
| 跨仓实现对照 | kernel `checkpoint`（Snapshot/Commit/ErrStaleSeq 单调闸 + 故障注入恢复测试）· kernel `invocationtest`（对账 conformance 套件，宿主存储跑过即验）· kernel `reconcile.RecoverInterrupted`（恢复入口）· agentloop `TestResumeAfterModelDeath`（模型死亡后续跑）|

### 超时三轴对照（`oagentkit/guardx#222` 交付，判据单源 `guardx/toolfail` + `kernel/deadline`）

| 轴 | 定义 | 触发者 | ctx 形态（身份怎么认） | 调用方处置 |
|---|---|---|---|---|
| **① 模型超时** | 单次模型尝试（含流式空闲）超预算 | llm resilient `timeoutProvider` 为每次尝试建 attempt ctx | `context.WithTimeout` → 裸 `DeadlineExceeded`（**有意无专属哨兵**：模型超时是瞬态，`llm.IsTransientStreamError` 判 `Canceled`=false / `DeadlineExceeded`=true；已登记文案白名单 `transientErrorReasons` 子串兜底） | **自动**：瞬态 → 恢复循环重开流（`MaxStreamRecoveryRetries`）；取消显式排除（不空转） |
| **② 工具超时** | 单次工具执行超预算 | 宿主/toolgate 给工具的 ctx；gate 等待期另有口径 | 执行超时 = 工具错误链里的 `DeadlineExceeded`——进 toolfail **须带 `Failure{ClassTransient}` 载体**（裸 `DeadlineExceeded` → `ClassUnknown` = 不可分类即不可重试，保守）；**gate 等待期超时 = `Skipped`（从未执行）不是 `Err`**（toolgate#121/#124：记 Err 会触发必然再被取消的退避风暴） | 重试可（ClassTransient），**默认不可盲目重发**（`SafeToResubmit=false`——服务端可能已执行），guidance=「先确认上一次调用是否已经生效」（toolfail 契约测试钉） |
| **③ 会话墙钟** | 整轮编排的总预算 | 宿主/装配层建会话 ctx | `context.WithTimeoutCause(..., contract.ErrSessionDeadline)`（**专属 cause**；`kernel/deadline.Route` 臂序硬约束：接管 > 用户取消 > 墙钟 > 停机——写反则会话超时被判成停机、永久滞留 running） | 落 `timed_out` 终态；整轮不重试（是否重启由宿主裁） |
| （对照轴）**用户取消** | 用户主动中止 | 宿主取消 API | `context.WithCancelCause(contract.ErrUserCancelled)`（**全仓唯一身份**，#170/#333/#355 事故教训）；裸 cancel 与停机在 Cause 上不可分——要区分就必须给专属 cause | 落 `cancelled`；**不**当超时处理（显式意图优先于墙钟） |

**身份判据总结**：三轴的区分**不靠文案靠值**——①②共享 `DeadlineExceeded` 这个
底层值，靠**载体与触发点**分轴（attempt ctx vs 工具链 + toolfail 类别载体）；
③与取消走 **ctx cause 专属值**。同一字面量不同值的事故形态（文本相同、
`errors.Is` 互不认领）由两仓对偶契约测试回归钉住：
`llm/timeout_axis_test.go`（轴① + 取消不串 + 文本复制件不认领）与
`guardx/toolfail/timeout_axis_test.go`（轴②载体/重发安全 + 取消不进瞬时类 +
伪造哨兵不认领）——依赖方向决定两仓不能互 import，契约以「同一判据面两处
各钉一半」成立，两仓 CI 各自常绿（锚见 §十）。

**墙钟轴扩展评估（结论：oagentkit 侧不新增墙钟哨兵）**：
会话墙钟的唯一身份已单源在 kernel `contract.ErrSessionDeadline`（cause 形态，
`deadline.Route` 臂序判据齐）。消费方向上——runtime（装配层）已依赖 kernel，
直接透传 cause 即可，无需第二份哨兵；agent（思想层）**零依赖 kernel 是红线**
（ADR-001），且思想层只拥有 attempt 级（轴①）超时，会话轴本就不归它管；
llm 同理（只到模型轴）。若未来 oagentkit 侧出现「不经过 kernel 的会话级预算」
需求，那说明宿主用错了层——正确解是把预算建到装配层 ctx 上（用 kernel 的
cause），不是在思想层复制一个墙钟错误。**记此结论替代 ADR**（负向结论，
无新面可裁）。

**RetryHint 收编判据（给 `oagentkit/llm#234`，收编实现归该单）**：
① 模型超时 → `ClassTransient`（可重试）；② 取消 → 不进瞬时类（`ClassUnknown`
或显式不重试——违背意图的重试是事故）；③ 同字面量不同值回归随收编迁移
（`errors.Is` 认领靠值不靠文本）；④ 不得改变 `IsTransientStreamError` 的
取消排除语义。收编 PR 引用本节为判据（互链）。

### 与 runtime W1 对象模型的对照（互认列）

| 七词 | runtime W1（yi-nology/goagent-runtime#38）对象面 |
|---|---|
| Run | `Session.Run(ctx, user) (*agentloop.Result, error)` + `SessionStatus`（状态读面走 contract 词表） |
| Turn | 无一等 Go 类型——语义由 eventbus 事件括号 + `trace.TurnTrace` 观测投影承载（与本表 §二一致） |
| Step | 环节级在 `SessionStatus` 之下，落账仍走 kernel `StepSink`（不另建状态面——#38「读模型不另造状态面」纪律） |
| ToolCall | agentloop 事件对 + `Result.Uncertain` 读面 |
| Handoff / Interrupt / Resume | 装配层消费 kernel 单源（dispatch/delegate/handoff、checkpoint），不复制判定 |

---

## 八、开单快照勘误（红线 3「先验证再动手」，2026-11-28 逐词 grep）

1. **`kernel contract.Run/Step/Interrupt/Resume` 不存在**（`git log -S` 全史
   检索）——contract 是状态词表 + `RunContext`/`StepSink` 边界接口，无可运行
   入口（kernel ADR-0011 Kernel≠Framework 的设计）。详见 ADR-003 §一勘误。
2. **`kernel drain.Turn` 非数据对象**——实际是 `drain.Turns` 接口（在途轮
   计数 + 批量取消能力面）。
3. **runtime W1 无 `Turn`/`Step`/`Worker` 一等类型**——W1 落的是 `Session`
   对象与 `SessionStatus`；Turn/Step 语义由事件与 kernel 单源承载（互认列见上）。
4. **Handoff 词表字面量分叉（completed vs done）已闭环**——kernel #358 把
   `handoff.ReceiptStatus` 收编为 `delegate.Status` 类型别名，
   `receipt_vocab_test.go` 带反向变异断言（见 §九锚）。

## 九、不一致清单（0 悬置——对齐或有意分歧，二选一）

| # | 不一致 | 处置 | 依据 |
|---|---|---|---|
| 1 | Run 三套返回值语义（`string` vs `*Result` vs contract 状态读面） | **有意分歧**：原生返回各自保留（一次性/会话制/状态边界三形态本就不同），统一**读面**由 `agent/iface.Agent` 承载——`Result.Status/Final` 窄面透传，丰富面经原生句柄取 | `matrix-tools` ADR-003 §一/§三（已合）；跨仓词表对账由 runtime `bridge/agentiface` 测试常绿守 |
| 2 | Handoff 多套 API（`agent/dispatch` vs kernel `delegate`/`handoff`） | **有意分工 + 词表已对齐**：dispatch=能不能派（守卫）、delegate=协商、handoff=交接台（领取/回执）——ADR-001 任务调度机制行裁决；回执词表分叉已由 #358 别名收编（锚 `TestReceiptVocabularySharedWithDelegate`） | ADR-001 §二；kernel #358 |
| 3 | ToolCall 事件词表两套（`tool_call` vs `agent.tool_call`） | **有意分层**：机制层 snake_case（kernel/eventbus）+ 应用层 `agent.` 前缀（runtime）——事件命名法已裁，词表对照不翻译 | runtime ADR-0011（事件命名法与 trace 身份面裁决） |
| 4 | 开单快照三处类型名失准（contract.Run / drain.Turn / W1 三对象） | **实况勘误记档**（§八）——按实测写表，不按快照 | 红线 3 |

## 十、契约测试锚（机器校验面）

> `verify_semantic_anchors.py` 逐行断言 `仓/路径/符号` 三元组真实存在；
> 校验失败 = 红。**先改本表再动测试**（锚是测试的索引，不是装饰）。

| 词 | 仓 | 路径 | 符号 |
|---|---|---|---|
| Run | agent | agentrun/agentrun_test.go | TestRunSimpleReply |
| Run | agent | agentrun/agentrun_test.go | TestRunReActLoopWithToolCall |
| Run | goagent-runtime | agentloop/loop_test.go | TestCompleteLoop |
| Run | goagent-runtime | runtime/session_test.go | TestSessionRunEndToEnd |
| Run | goagent-kernel | contract/contract_test.go | TestIsTerminalStatus |
| Turn | goagent-kernel | watchdog/turn_test.go | TestTurnBracketCleanPairing |
| Turn | goagent-kernel | drain/session_test.go | TestDrainSessionWaitsForConvergence |
| Step | goagent-kernel | statemachine/step_test.go | TestLegalTransitions |
| Step | goagent-kernel | statemachine/step_test.go | TestIllegalTransitions |
| Step | goagent-kernel | contract/contract_test.go | TestMinimalHostSatisfiesContract |
| ToolCall | goagent-runtime | agentloop/loop_tool_events_test.go | TestToolEventPairing |
| ToolCall | guardx | invoke/outcome_test.go | TestOutcomeRefusalReachesRecord |
| ToolCall | toolgate | toolsched/issue_124_test.go | TestExecuteCtxCancelWhileWaitingOnGateSkips |
| Handoff | agent | dispatch/dispatch_test.go | TestGuardAssert |
| Handoff | goagent-kernel | delegate/vocab_test.go | TestAllStatusesMatchesValid |
| Handoff | goagent-kernel | handoff/receipt_vocab_test.go | TestReceiptVocabularySharedWithDelegate |
| Interrupt | goagent-kernel | deadline/route_test.go | TestRouteMultipleUserCancelSentinels |
| Interrupt | agent | roundguard/classify_test.go | TestClassifyToolCallsContinues |
| Interrupt | agent | agentrun/contract_interrupt_resume_test.go | TestContractUserCancelLandsCancelledNotShutdown |
| Interrupt | agent | agentrun/contract_interrupt_resume_test.go | TestContractDispatchFailureReturnsToParentTerminal |
| Resume | goagent-kernel | checkpoint/faultinjection_test.go | TestFaultResumeInterruptedThenResumes |
| Resume | goagent-kernel | invocation/invocationtest/invocationtest_test.go | TestConformanceOnMemoryStore |
| Resume | goagent-runtime | agentloop/loop_test.go | TestResumeAfterModelDeath |
| Resume | agent | agentrun/contract_interrupt_resume_test.go | TestContractInterruptResumeSkipsCompletedSteps |
| Timeout | llm | timeout_axis_test.go | TestModelTimeoutIsTransient_Issue222 |
| Timeout | llm | timeout_axis_test.go | TestUserCancelNotModelTimeout_Issue222 |
| Timeout | llm | timeout_axis_test.go | TestSameTextDifferentValue_Issue222 |
| Timeout | guardx | toolfail/timeout_axis_test.go | TestToolTimeoutIdentity_Issue222 |
| Timeout | guardx | toolfail/timeout_axis_test.go | TestUserCancelNotTimeout_Issue222 |
| Timeout | guardx | toolfail/timeout_axis_test.go | TestSameTextDifferentValue_Issue222 |

## 关联

- 执行单：`oagentkit/agent#175`（本表的交付单）、总纲 `oagentkit/matrix-tools#254`（阶段二出口条件）
- 单源（引用不重定义）：`yi-nology/goagent-kernel` `contract/status.go`、`statemachine`、`delegate`、`handoff`、`eventbus`、`checkpoint`；`oagentkit/guardx/toolfail`（失败分类）
- 超时三轴：`oagentkit/guardx#222`（兄弟单，本表列引）
- 中断/恢复行为契约：`oagentkit/agent#176`（行为测试单）
- 派发调度归属：`oagentkit/agent#177`（归属表单）；kernel ADR-0009 / matrix-tools ADR-001
- 统一执行接口：`matrix-tools` ADR-003（Run 统一读面裁决）
- 对象模型互认：`yi-nology/goagent-runtime#38`（W1）
- 事件命名法：`yi-nology/goagent-runtime` `docs/adr/ADR-0011`
- 形态惯例：`yi-nology/goagent-kernel` `docs/error-semantics.md`
