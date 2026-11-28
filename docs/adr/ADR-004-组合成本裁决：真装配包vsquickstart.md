# ADR-004 · 组合成本裁决：真装配包 vs quickstart

| 项 | 内容 |
|---|---|
| **状态** | 已采纳（Accepted） |
| **日期** | 2026-11-28 |
| **背景** | oagentkit 演进评审 2.3-问题二：13+ 模块独立发版后组合成本升高，普通 Agent 开发者要的是 `agent := runtime.New(...); agent.Run(ctx, input)` 而不是自己拼十几个包。issue `oagentkit/matrix-tools#257`（裁决单，不是实现单）。 |
| **决策** | 选 **(a) 真装配包**，落点 **`yi-nology/goagent-runtime`**（不开新仓、不做转发门面）：五形态样例（runtime#46 / PR#53 已交付）作为 preset 的**种子与验收场**，逐步毕业为 runtime 内的版本化 preset 构造面；(b) 的教学面（五形态样例 + 场景选型表 + llms 拖累面）**已被采纳并持续存在**，但不作为生产组合的载体。 |
| **后果** | ① 版本策略：preset 跟随 runtime 独立 tag，**零新增版本线**（runtime 本是 tier5 全地板跑步机，preset 不引入第二条跑步机）；② conformance 三件：五形态样例 `go run` 进 runtime CI（当前仅 `go build ./...` 编译覆盖，执行面是首个落地项）、preset 与样例互为锚（preset 出厂即样例可跑）、扩展点套件复用 `oagentkit/llm#241` 的 conformance 模式；③ heavyDeps 展示位**已由 #258/PR#264 交付**（场景表「import 拖累面」列三出口同源 + 站点 scenarios 页 + 模块页地板行），本单验收第 4 条核销；④ 后续实现单另开（preset 毕业 + examples 执行面 CI），本单只定路。 |
| **依据** | 四维度评估（§二）；ADR-003「只定语义不定装配，装配留各入口构造器」；runtime 实况（NewEmbedded/NewStandalone + W4 NewSession 入口 + 五形态样例 PR#53）；agentkit v1.9.0 门面退役记录（红线：转发≠装配）；`modules.json` tier/heavyDeps 机读面与 #258/PR#264 场景表拖累面。 |

> **一句话结论**：组合知识进**版本化装配代码**（runtime preset，真装配、同仓同 tag、可 conformance），
> 不进文档；quickstart 样例与场景表作为教学面继续存在——学在样例，用在 preset。

---

## 一、两候选与实况盘点（2026-11-28）

| 候选 | 定义（issue #257 原文） | 实况 |
|---|---|---|
| **(a) 真装配包** | 预设组合放 goagent-runtime（tier5 装配层）或 agent 仓新开装配包；有独立版本、有 conformance、**代码是真装配不是转发** | 雏形已在：`NewEmbedded/NewStandalone` + W4 `NewSession→Run`（runtime#41）+ 五形态样例矩阵（runtime#46 / PR#53：simple/workflow/react/multiagent/longrunning 各带 ≤10 行 README） |
| **(b) quickstart + 脚手架** | 五形态 quickstart + 场景表直查，组合逻辑留在文档 | 教学面已在：五形态样例（PR#53）+ 场景选型表三出口同源（mt#258 / PR#264：skill / llms.txt / 站点 scenarios 页，含「import 拖累面」列） |

两者的**教学面已经合流**（样例+场景表同时服务两边）；本裁决分岔的只剩一件事：**生产上「用哪套组合」的权威载体是代码还是文档**。

## 二、四维度评估（issue 指定）

| 维度 | (a) 真装配包 | (b) quickstart 文档 | 胜方 |
|---|---|---|---|
| **学习成本** | 生产一行 `preset.X(cfg)`；学习仍靠样例（样例不因选 (a) 被削） | 复制 30 行样例起步，读代码学 | 平——教学面已合流；生产面 (a) |
| **版本耦合（地板跑步机）** | runtime 已是 tier5 全矩阵地板跑步机——preset 同仓同 tag，**零新增耦合**；若开新装配仓才会引入第二条跑步机（本裁决明确不开） | 零耦合，但地板变化时样例/文档靠人肉跟 | (a) 零新增 + 有 conformance 兜底；(b) 的「零耦合」实为「零守护」 |
| **门面退役教训的适用边界** | 门面死于**纯转发 + 自己的版本滞后**（v1.9.0 48 包删除）。preset 是真装配代码：与实现同仓、同 tag、同 CI，不存在转发滞后——**教训禁止的是转发形态，不禁止装配**（issue 对 (a) 的定义本身就是「真装配不是转发」） | 文档不构成转发，但样例会随实现漂移而无人判红 | (a)（同仓即无滞后面）；(b) 的漂移风险由 conformance 缺位放大 |
| **「Agent Developer Infrastructure」判词** | 「标准执行原语 + 可组合接口」——preset = 原语的**标准组合**，组合知识进版本化代码正是「基础设施」；不绑模型/工具仍是组合不是框架（kernel#528 边界管内核，runtime 本职就是装配） | 组合知识散在文档，无「标准」可言——上层可以自己拼，但平台没给出被测过的标准答案 | (a) |

## 三、不选 (b) 作为生产载体的理由

(b) 的教学价值**已全额采纳**（样例矩阵 + 场景表 + 拖累面列，PR#53/PR#264），本裁决不削 Teaching 面。但 (b) 作为**生产组合的载体**有三处结构性缺口：

1. **无 conformance**：文档组合没有「换实现仍全绿」的判据面（对照 llm#241 扩展点套件的模式——文档给不出套件）；
2. **复制漂移**：用户复制样例后，地板/语义演进无任何门禁提示他那份拷贝已过期；
3. **回答不了验收问题**：「生产上用哪套组合、它被谁测过」——(b) 的答案永远是「看文档自己拼」。

## 四、(a) 的版本策略与 conformance 要求（issue 任务 2 成文）

**版本策略**：
- preset 住在 `yi-nology/goagent-runtime` 模块内（子包形态，实现单定名），**跟随 runtime 独立 tag**；
- 不开新仓、不建独立版本线——地板变更走 runtime 既有 bump_dep_floors 跑步机，**零新增耦合**；
- preset 自身 breaking 随 runtime minor 走（api-lifecycle 定级：preset 面起步 Experimental，按 ADR-002 判据晋级）。

**conformance 要求（三件，缺一不给出厂）**：
1. **执行面**：五形态样例 `go run` 常绿进 runtime CI（现状仅 `go build ./...` 编译覆盖——执行面是实现单的第一项）；
2. **互为锚**：每个 preset 出厂必须有对应形态样例直接调用它（样例是 preset 的最小验收用例，preset 改坏 ⇒ 样例红）；
3. **扩展点套件**：换模型/工具实现的 conformance 复用 `oagentkit/llm#241` 的套件模式（suite 形态，落地时双向引用）。

## 五、heavyDeps 展示位（issue 任务 4——已交付，核销）

| 面 | 落点 | 状态 |
|---|---|---|
| 机读 | `modules.json` `heavyDeps`（单源） | 既有 |
| 场景表 | skill / llms.txt / 站点 `reference/scenarios` 页三出口同源「import 拖累面」列 | **#258 / PR#264 已合** |
| 模块页 | 站点模块页「第三方地板」行 + 首页矩阵表地板列 | 既有（gen_module_pages） |
| 五形态入口 | llms.txt 场景段互链 runtime examples（组合成本↔样例直达） | **PR#264 已合** |

## 关联

- 总纲：oagentkit/matrix-tools#254（阶段三出口条件）
- 裁决单：oagentkit/matrix-tools#257（本 ADR 的验收主体）
- 前置裁决：ADR-003（统一 Agent 执行接口——只定语义不定装配，装配留入口构造器）
- 验证场：yi-nology/goagent-runtime#46 / PR#53（五形态样例矩阵）、oagentkit/matrix-tools#258 / PR#264（场景表 + 拖累面）
- 红线依据：oagentkit/agentkit v1.9.0 门面退役（转发≠装配）
- 后续：实现单（preset 毕业 + examples 执行面 CI + llm#241 套件引用）另开
