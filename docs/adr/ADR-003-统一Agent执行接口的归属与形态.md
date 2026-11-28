# ADR-003 · 统一 Agent 执行接口的归属与形态

| 项 | 内容 |
|---|---|
| **状态** | 已采纳（Accepted） |
| **日期** | 2026-11-28 |
| **背景** | oagentkit 演进评审 2.3-问题一/三（Agent 层没有接口面，「一个 Agent 是什么」无从谈起；缺统一执行语义）+ 2.4-阶段一「优先稳定 Agent 接口」。全矩阵 grep `type Agent interface` 为零，两套可运行入口形态各异。issue `oagentkit/agent#174`。 |
| **决策** | ① **归属**：窄接口放 `oagentkit/agent` 新子包 **`iface`**（被依赖方——依赖方向决定，§二三方案对比；实现侧 `iface/doc.go` 独立推出同一结论并以编译期断言 `var _ iface.Agent = (*agentrun.Agent)(nil)` 锚定）。② **形态**：`iface.Agent{ Run(ctx, Input) (Result, error) }`——只定语义不定装配；`Result.Status` 是宿主状态词表**透传**（词表单源 kernel/contract，本包不定义任何状态常量）；事件以 **`Input.OnEvent` 调用方回调**形态收（原生词表透传、不翻译、不代 recover、不支持必须显式拒绝——不建第三份传输，§三）。③ **adapter 分落两仓**：`agentrun.Agent` 在 `oagentkit/agent`，agentloop 与 contract 边界适配在 `yi-nology/goagent-runtime/bridge/agentiface`（只有它同时 import 两边）；**不强迁存量**，原签名不动。④ 定级 **Experimental**（ADR-002 判据）。 |
| **后果** | 消费方写泛化代码只依赖 `iface.Agent` 一个面；三入口语义差异被对照表钉死而不是散在各自文档；agent 仓新增包触发 `verify_api_lifecycle` 覆盖双向判据（不登记即红）；跨仓词表对账由 runtime 侧 bridge 测试常绿守着；实现与本 ADR 互为锚点（实现注释引用本对照表，本 ADR §四引用实现映射规则）。 |
| **依据** | 三入口 2026-11-28 逐仓实测（§一，含评审快照勘误）；go.mod 依赖边实况（ADR-001 §一，思想层零依赖 kernel、runtime→agent 已存在）；runtime `ADR-0010` 已背书「同一入口形态 `Run(ctx, input) → Result`、状态词表归 kernel contract」——本 ADR 是该形态在 Agent 层的类型化；kernel `contract/status.go` 13 值词表单源实测；`verify_import_direction` 模块级 `dependsOn` 守卫（runtime→agent 已声明）。 |

> **一句话结论**：接口放**被依赖方**（agent 仓 `iface` 子包），形态收窄到
> 「一次输入一次输出」，事件走调用方回调的原生词表透传——统一的是执行语义的
> **契约面**，不是再造一份传输或词表。

---

## 一、三入口实况对照（2026-11-28 逐仓实测）

| 面 | `agent/agentrun.Run` | `goagent-runtime/agentloop.Loop.Run` | `goagent-kernel/contract` |
|---|---|---|---|
| **参数** | `(ctx, cfg Config, query string, opts ...RunOption)`——**配置随调用**，每次执行独立装配 | `(ctx, sessionID, user string)`——会话在 `Loop` 构造时装配，调用只带会话锚点 + 用户输入 | **无 Run 函数**：`RunContext`/`StepSink` 由宿主构造注入（编排运行上下文与环节推进出口） |
| **返回** | `(string, error)`——拼接终稿文本，无结构化状态 | `(*Result, error)`——`RunID / Status / Final / Rounds / ToolCalls / Uncertain / EmitFailed / PendingCallID` | 无返回值；状态经 `RunContext.Status/SetStatus` 迁移，环节经 `StepSink.Open/Finish` 落账 |
| **事件** | `WithOnEvent(func(Event))` RunOption（可多传、末个生效；**回调 panic 不代 recover**）；事件常量 snake_case（`reasoning`/`text`/`tool_call`…） | `Config.Emitter.Publish(ctx, sessionID, caseID, stepID, typ, level, payload)`——`eventbus.DurablePublisher` 兼容的**持久发布面**（embedded 广播 / standalone 落库）+ `Bus` embedded + `OnEmitError` + `EmitFailed` 计数 | **无事件面**——事件词表单源 `kernel/eventbus`（机制层 snake_case），传输归 `runtime/hookx`/`notifyx`（ADR-001 事件机制行） |
| **取消** | `ctx` 透传 eino 执行链 | `ctx` + 快照序号闸（并发后到者拿 `ErrStaleSeq` 明确拒绝）+ 审批挂起（`waiting_approval` → `Approve` 落决策） | 无执行可取消；宿主经 `SetStatus` 做状态迁移，终态判定 `IsTerminalStatus` |

### 快照勘误（红线 3「先验证再动手」抓到的）

评审快照称第三入口是「`contract.Run/Step/Interrupt/Resume`——纯接口契约面
（Stable）」。**2026-11-28 实测（含 `git log -S` 全史检索）：contract 包从未
有过这些函数**——它只有 `RunContext`/`StepSink` 接口与词表判定函数
（`AllStatuses`/`ClassifyStatus`/`IsKnownStatus`/`IsTerminalStatus`）。
这不是缺失而是设计：kernel 是判据层（kernel ADR-0011「Kernel ≠ Framework」），
**没有可运行入口**。因此真实格局是「**两套可运行入口 + 一套词表契约**」，
本 ADR 的对照与 adapter 按此执行。

## 二、归属裁决：放 `oagentkit/agent`（`iface` 子包）

三个候选逐一过依赖方向（ADR-001 §一的 go.mod 实况）：

| 候选 | 判定 | 理由 |
|---|---|---|
| **`oagentkit/agent` 新子包 `iface`** ✅ | 采用 | agent 是**被依赖方**：`runtime→agent` 边已存在（`roundguard` 4 处，已登记 `dependsOn`），agentrun 就住在本仓——原生实现零跨仓；思想层自述「Agent 的执行面长什么样」与其职责（智能行为与决策编排）相容。实现侧 `iface/doc.go` 独立论证同一结论：接口放装配层则思想层的**自我描述**反向依赖装配层（tier 3 → tier 5 违反分层），且编译期断言 `var _ iface.Agent = (*agentrun.Agent)(nil)` 写不出来——实现关系无从查证 |
| `yi-nology/goagent-runtime` | ❌ | `runtime→agent` 已存在，接口放 runtime 则 agentrun 要实现它必须 `agent→runtime`——**反向必成环**（Go module 直接拒绝） |
| `yi-nology/goagent-kernel/contract` 扩面 | ❌ | 思想层**零依赖 kernel** 是 ADR-001 钉死的红线（`verify_import_direction` 反向判红）；接口放 kernel 等于让 agent 为自己的接口面引 kernel |
| 新开独立小仓 | ❌ | 模块准入三问（oekit#344 同型判据）：使用场景已被 agent 覆盖、无独立生命周期，不独立成仓 |

**为什么是子包不是根包**：agent 仓根包不存在（各能力平铺子包：agentrun/
blackboard/dispatch/…），新子包与既有组织方式一致；`iface` 命名沿用矩阵先例
`mcpkit/iface`（协议中立面）。

## 三、形态裁决：窄接口收什么、不收什么

实现签名（`oagentkit/agent/iface`，以实现为锚）：

```go
type Input struct {
    Query     string         // 本轮输入；空串合法性由实现方裁
    SessionID string         // 会话锚点；空 = 一次性执行（agentrun 天然为空）
    OnEvent   func(Event)    // 过程事件回调（可 nil）；不代 recover；不支持必须显式拒绝
}

type Result struct {
    Status string // 宿主状态词表透传（kernel contract 词表；本包不定义状态常量）
    Final  string // 终答文本（Status=succeeded 时有值）；失败信息由 error 承载
}

type Event struct {          // 原生词表透传信封，不做翻译层
    Type, Text, Tool, CallID string
    Payload                  map[string]any
}

type Agent interface {
    Run(ctx context.Context, in Input) (Result, error)
}
```

**收的**：

- **一次输入一次输出**——`Run(ctx, Input) (Result, error)`。三入口里最窄的
  共同语义就是「给上下文和输入，拿回一次执行的结果」；这正是 runtime
  `ADR-0010` 已背书的入口形态（「共享的是语义与读面，不是控制流抽象」）在
  Agent 层的类型化。Config 装配、会话装配全部留在各入口构造器
  （**只定语义不定装配**）。
- **`Result.Status` 是透传不是定义**：词表单源是 kernel `contract/status.go`
  的 13 值表（ADR-001 生命周期机制行），本包**不定义任何状态常量**——另立
  常量集就是词表分叉（contract#354/#357 收编过的形态）。空串 = 入口未提供
  状态。adapter 层允许出现词表字面量做映射（薄、单点常量块），**跨仓对账由
  runtime 侧 bridge 测试承担**（§四）——agent 仓自己不引 kernel，也不养
  第二份词表定义。
- **事件以 `Input.OnEvent` 调用方回调形态收**（本 ADR 与实现对齐后的裁决，
  取代评审倾向的独立订阅面）：回调由**调用方提供、适配器接线到入口原生面**
  （agentrun `WithOnEvent` / agentloop `Emitter`）——不建第三份传输；
  `Event.Type` 沿用**入口原生词表**不做翻译（两套词表对照即 §一事件行），
  需要统一词表的消费方按 runtime ADR-0010/事件分层单源走 kernel eventbus。
  两条纪律与各入口既有约定逐字对齐：回调 panic **不代 recover**（与
  `agentrun.WithOnEvent` 同约）；**不支持事件翻译的实现必须显式拒绝**
  （返回错误），不得静默丢弃——静默丢事件等于对消费方撒谎「没有事件」。

**不收的（及理由）**：

- **不收独立订阅方法 / 事件总线**——订阅面进接口方法就要定义返回通道与
  生命周期（谁关、缓冲多大），那是传输语义，归 `runtime/hookx`/`notifyx`；
  `Input.OnEvent` 的回调形态避开这一切。
- **不收 `PendingApproval` / `Approve` 等控制方法**——挂起决策与恢复是装配
  层与机制层的面（agentloop `Approve`、kernel `checkpoint`），收进接口会让
  「一个 Agent」背上整个生命周期；挂起时的丰富读面（`PendingCallID` /
  `RunID` / `Rounds` / `Uncertain`）经**适配器的原生句柄**取，窄面只保
  `Status`+`Final`。
- **不收 Usage/Rounds/ToolCalls 等观测字段**——同理：窄面收了就要为
  agentrun 的 string 出口编造字段；需要观测的消费方用原生类型。

## 四、adapter 落点（不强迁存量）

| adapter | 落点 | 映射要点（以实现为锚） |
|---|---|---|
| **agentrun** | `oagentkit/agent`（`agentrun.Agent` 结构体，`agentrun→iface` 方向自然） | `Input.SessionID` 忽略（无会话概念，不报错）；`Input.OnEvent` 追加为 `WithOnEvent`（**末个生效**，故窄接口输入面优先于 `Opts` 预设回调）；`statusOf`：`DeadlineExceeded→timed_out`、`Canceled→cancelled`、其余→`failed`、成功→`succeeded`（全部 ∈ contract 13 值，常量块逐字对齐 + 注释声明跨仓对账点）；失败**原样返回 error**（失败信息不塞 Result） |
| **agentloop + contract 边界** | `yi-nology/goagent-runtime/bridge/agentiface`（只有它同时 import agent 与 kernel） | agentloop 侧：`Input.SessionID` 空则用构造期默认会话，`*Result` 的 `Status/Final` 透传（Loop 的 Status 本就是 contract 词表）；contract 边界：词表对账——agentrun adapter 的字面量映射与 agentloop 的透传值**逐一过 `contract.IsKnownStatus`**，跨仓词表一致性常绿即本 ADR「透传不定义」的机器证明 |

**不强迁存量**：`agentrun.Run` 与 `Loop.Run` 原签名不动，`iface.Agent` 是
增量面（拿窄面的消费方按需取用）；两个 adapter 均随所在包定级
Experimental（ADR-002），晋级按三维判据走。

## 五、定级与门禁

1. `agent/api-lifecycle.yaml` 登记 `iface`（Experimental，reason 注明
   「agent#174 新面，未过一个 minor 无 breaking 窗口」）+ 包注释
   `// 生命周期: Experimental——…` 标记（已随 `iface/doc.go` 落）——
   `verify_api_lifecycle` 覆盖双向判据：**新增包不登记即红**（ADR-002 §二，
   清单已放跑不掉）。
2. `verify_import_direction`：`runtime→agent` 模块级 `dependsOn` 已声明，
   新增 `agentrun`/`iface` 子包 import 不产生新模块边；反向红线（思想层
   零依赖 kernel）不受影响——iface 与 agentrun adapter 均不引 kernel。
3. runtime 侧 bridge 测试进 `goagent-runtime` 既有测试面。

## 关联

- issue：`oagentkit/agent#174`（本 ADR 的执行单）、`oagentkit/matrix-tools#254`（演进总纲）
- 边界前置：`ADR-001`（三层边界与八机制归属——本 ADR 的依赖方向与词表/事件单源引用源）、`ADR-002`（api-lifecycle 判据——定级走它）
- 形态背书：`yi-nology/goagent-runtime` `docs/adr/ADR-0010`（执行接口两模型分立、共享语义不成模型——`Run(ctx, input) → Result` 同一入口形态与词表归 kernel 的出处）
- runtime 侧镜像：`yi-nology/goagent-runtime#39`（W2 Loop 与 Pipeline 边界）、#38（对象模型——本面不重复其 Session/Run/Turn 建模）
- 词表单源：`yi-nology/goagent-kernel` `contract/status.go`（13 值）、`contract` 包 `RunContext`/`StepSink`
- 实现锚点：`oagentkit/agent/iface`（doc.go 含本裁决的实现侧论证）、`agentrun/iface.go`（agentrun adapter）、`yi-nology/goagent-runtime/bridge/agentiface`（agentloop + contract 边界 adapter）
