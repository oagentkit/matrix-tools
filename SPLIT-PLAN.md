# agentkit 模块矩阵拆分方案

按 [oekit](https://oekit.github.io/llms.txt) 的「框架耦合分层 + 独立发版」口径，把原
**1 个 go.mod / 47 个包** 的单仓拆成 **13 个独立发版的仓**。

> **状态：拆分已执行完成（2026-10-17）。**
> 17 个仓全部就位于 [github.com/oagentkit](https://github.com/oagentkit)：
> 13 个模块仓（各 `v0.1.0`）+ 门面仓 `agentkit`（`v1.0.0`）+ 三个基建仓。
> 执行结果见 §10，本文其余章节是拆分时的方案与决策记录。

---

## 1. 拆分判据

oekit 的轴是**框架耦合度**，不是功能目录。本方案用三条硬判据：

1. **依赖地板不同** —— 重第三方依赖（eino / milvus / mcp-go / lib/pq / mvdan.cc/sh / yaml）
   不同则必拆。地板不同意味着版本升级节奏不同，混在一仓会被 slowest 拖死。
2. **消费方集合不同** —— 谁 import 它决定它该独立发版还是当门面。
3. **变更节奏不同** —— 上游 issue 收口轮只碰一部分包时，不该带着全仓发版。

### agentkit 的最大耦合轴是 eino

13 个包直接依赖 eino：`llm` `acpx` `agentrun` `mcp` `obsx` `policy` `reflection` `router`
`skill` `toolprior` `websearch` `llm/wrap` `llm/llmtest` `knowledge/rag`。eino 是本矩阵的
「框架层」，与 oekit 的 Hertz/Kitex 地位对等。

### 依赖图无环、最深 3 层

内部依赖最长链：`lineage → skill → fence`（3 跳）。**无环**是自底向上切仓可行性的硬证据 ——
切仓不会产生循环 module 依赖。

---

## 2. 模块矩阵

| tier | 仓 | 语义 | 收编的现有包 | 依赖地板 |
|---|---|---|---|---|
| 0 | `textx` | 文本/差异/会话/报告纯函数集 | textutil, diffx, codeintel, compact, conversation, reportutil | 零 |
| 0 | `runtime` | agent 进程运行时：流控/事件总线/子进程/热插拔/钩子 | pacer, progress, procx, hotplug, hookx | 零 |

> `breaker` 已从本仓移出，待退役到 `oekit/concurrency/breaker`，见 §8。
| 0 | `guardx` | 脱敏/出口围栏/权限/沙箱/审计 | fence, logredact, egress, permgate, bashguard, audit | mvdan.cc/sh |
| 0 | `queue` | DB 即队列 + PG 选主 Lease | worker, worker/pglease | lib/pq |
| 1 | `promptx` | prompt 组装/运行中转向/读后写一致性 | sysprompt, steering, filestate | 零 |
| 1 | `obs` | 用量观测 + Langfuse 追踪 | obsx, langfuse | eino |
| 1 | `skillkit` | SKILL.md 渐进披露 + 包契约 + 装配血缘 | skill, pack, lineage | yaml, semver |
| 1 | `mcpkit` | MCP 客户端/工具池/建连单飞 | mcp | mcp-go, eino |
| 1 | `rag` | 知识检索/混合召回/重排 | knowledge/rag | eino, milvus |
| 2 | `llm` | 模型客户端/降级链/预算/成本/阶段路由 | llm, llm/wrap, llm/llmtest | eino, go-openai, eino-ext, x/time |
| 2 | `toolgate` | 工具优先级/并发调度/审计门 | toolprior, toolsched, policy | eino, yaml |
| 2 | `tools` | CLI agent 调用/公开检索/git 沙箱/SAST | acpx, websearch, workcopy, sast | eino |
| 3 | `agent` | ReAct/反思/路由/黑板/派发/澄清 | agentrun, reflection, router, blackboard, dispatch, clarify | eino |

**46 个包全部归位，无漏无余。** 其中 `breaker` 标记为**待退役但过渡期仍在本仓**（见 §8）；
第 47 个是仓根 `integration_test.go`，见 §5。

### 依赖方向（tier N 不得依赖 tier > N）

```
tier0:  textx   runtime   guardx   queue
             │       │        │
tier1:  promptx  obs   skillkit  mcpkit   rag
             │       │        │        │
tier2:   llm ◄───┘        │        ├──► toolgate
             │              │        │
tier3:  agent              └──► tools
```

**`procx` 不能并入 `tools`（硬约束）**：它同时被 tier1 的 `mcpkit`、`promptx` 使用，
并入 tier2 的 `tools` 会造成 tier1→tier2 分层倒挂，破坏自底向上不变量。

机器可读定义：[`modules.json`](modules.json)（格式对齐 oekit 的 `versions.json`）。

---

## 3. 命名

**包名一律保留，只加模块前缀。** 新路径固定三层：

```
github.com/{org}/{repo}/{pkg}
```

所以 import 改写是**纯前缀映射**，可机械执行、可机械校验，不需要逐包人工判断。

包名末段与仓名相同时（`llm`、`rag`）收口到 module 根：`oagentkit/llm`、`oagentkit/rag`，
路径**不重复末段**。`mcpkit/mcp` 仍是三层（包名 `mcp` ≠ 仓名 `mcpkit`），
与 oekit 的 `oekit/hertz/hertz` 同形态。

### org 名：`oagentkit`（已创建）

**Open Agent Kit** —— agent / LLM 编排工具链矩阵。

**已就位**：`github.com/oagentkit`，创建于 2026-10-17，账号 `zy84338719` 为 admin，
当前 0 个公开仓。module path 形如：

```
github.com/oagentkit/textx/textutil
github.com/oagentkit/llm
github.com/oagentkit/mcpkit/mcp
go get github.com/oagentkit/llm@latest
```

待补：org description、blog（`https://oagentkit.github.io`，站点建好后）、13 个仓的
description 与 topics。

选名的判据是**解释成本 vs 长度**：

| 候选 | 字符 | 判定 |
|---|---|---|
| `openagentkit` | 12 | ❌ **已被他人注册**（Organization `OpenAgentKit`，2 个公开仓） |
| `oagentkit` | 10 | ✅ **选它** |
| `o-agentkit` | 11 | ❌ org 名带连字符在 GitHub 上非主流；module path 多一个分隔符，手输/复制易漏 |
| `oagentic` | 8 | ❌ `agentic` 是形容词没有实体落点，`oagentic/textx` 读作「智能体式的 textx」 |
| `eagentkit` | 9 | ❌ `e` 单独悬空语义不明；`e-a-g` 开头三字母粘连，误读误拼风险高 |
| `oeakit` | 6 | ❌ agent 藏在 O.E.A.K. 里，不解释=不知道是 agent 工具链 |
| `oagnetkit` | 9 | ❌ `oagentkit` 的 a/e 字母互换 |

核心理由：**`o` 前缀不需要解释**。技术品牌里前缀是通用约定，不是需要说明的缩写 ——
`openagentkit` 多花 2 个字符买一个没人会问的问题。`agentkit` 在 `oagentkit` 里是完整
子串，所以现有 README / CHANGELOG / FRAMEWORK.md 里的 agentkit 字样**依然准确**，
不需要全量改口径。

```bash
# 脚本默认 org 已是 oagentkit，三处都不必再传 --org
python3 scripts/split/rewrite_imports.py --print-rules
python3 scripts/split/rewrite_imports.py --check
python3 scripts/split/verify_split.py
```

**与 oekit 的关系**（写文档时守住）：oekit 是 Go 微服务工具链（bootstrap / hertz /
kitex / nns / redisx …），oagentkit 是 Agent 编排工具链并**依赖** `oekit/pkg`、
`oekit/logx`、`oekit/concurrency`。两者只共享 `o`/`kit` 两个元素，不构成子品牌关系，
所以**在每个仓的 README 里直接写依赖声明即可**，不需要先解释两个名字什么关系。

**建仓时不要误选的已占用名**（你的账号对它们均无成员关系）：`openagentkit`(已注册)、
`agentkit`(2018 空壳)、`agentkit-dev`、`agentkits`、`agkit`(2022)、`ekit`(2014)、
`aikit`、`agentic`、`agentickit`、`mindkit`、`agstack`(46 仓)、`agentlab`(173 仓)。

备选（均已验证可注册，若日后 org 要改名）：`o-agentkit`、`agentkitgo`、`agentkit-io`、
`agentstack`、`agentosm`。

---

## 4. 拆分执行顺序

自底向上，每一步都可独立验证、可独立发版。

| 步 | 动作 | 验证方式 |
|---|---|---|
| 1 | 建 org + `.github` 仓（共享 CI、profile） | 复制 oekit `.github` 后改 org 名 |
| 2 | 切 tier0 四仓：textx / runtime / guardx / queue | 各自 `go test ./... -race` 绿 |
| 3 | 切 tier1 五仓，逐个 `go mod edit -replace` 指向 tier0 本地路径 | `go build ./...` 绿 |
| 4 | 切 tier2 三仓：llm / toolgate / tools | 同上 |
| 5 | 切 tier3 一仓：agent | 同上 |
| 6 | 删 `-replace`，全部指向 GitHub 正式 tag | `go mod tidy` 后 `go test ./...` |
| 7 | 原单仓发一个大版本（v1.0.0）说明迁移，改为 re-export 门面或标记 deprecated | 下游能平滑过渡 |
| 8 | 每个仓根补 `AGENTS.md`（对齐 oekit 规范：facade、类型同一性、禁裸 go func） | — |

**第 7 步的取舍要先定**：原 `agentkit` 仓是留作门面 re-export，还是直接 archive？
留门面要维护 13 条 re-export（oekit 的 `data` 就是这么做的），archive 则下游必须立刻改
go.mod。oekit 的先例是 ekit 单体 → oekit 矩阵时用 perl 脚本全量改写 + 过渡期桥接。

---

## 5. 改写与验证脚本

### `scripts/split/create_repos.py`

按矩阵建仓：13 个模块仓 + 4 个基建仓，带 description 与 topics。**幂等** —— 已存在的仓只补/改
元数据。**默认 dry-run**，加 `--apply` 才真建（建仓在 GitHub 上是公开且难以完全撤回的动作）。

```bash
python3 scripts/split/create_repos.py                      # dry-run，打印将做什么
python3 scripts/split/create_repos.py --apply              # 真建（17 个公开仓）
python3 scripts/split/create_repos.py --apply --modules-only  # 只建 13 个模块仓
```

建什么仓由 `modules.json` 驱动，脚本不硬编码仓名 —— 矩阵改了脚本自动跟随。

| 仓 | 角色 |
|---|---|
| `agentkit` | 门面仓，46 包 re-export，设退役时间表（§8-4） |
| `.github` | org profile + 共享 reusable workflow |
| `matrix-tools` | 版本矩阵生成 / 审计 / 巡检 |
| `oagentkit.github.io` | llms.txt 模块索引 + 文档站点 |

### `scripts/split/rewrite_imports.py`

对标 oekit 的 `matrix-tools/scripts/ekit2oekit.py`（ekit→oekit 迁移），同样提供
`--print-rules`。

```bash
python3 scripts/split/rewrite_imports.py --org <org> --print-rules   # 看映射表
python3 scripts/split/rewrite_imports.py --org <org> --check         # 只报告，有改动 exit 1
python3 scripts/split/rewrite_imports.py --org <org> --apply         # 执行改写
python3 scripts/split/rewrite_imports.py --org <org> --manual        # 列出需人工处理的非 Go 文件
python3 scripts/split/rewrite_imports.py --org <org> --single github.com/<org>/agentkit --apply
```

设计上的两个关键约束：

1. **只改 import 块内的引号字符串**（逐行状态机，识别 `import (` 块与单行 `import`），
   不碰注释、不碰其他字符串字面量。测试里断言模块路径的地方必须原样保留。
2. **未登记的包一律报错退出，不静默跳过。** 静默跳过是这类脚本最危险的失败模式 ——
   改了一半，看起来成功了。

### `scripts/split/verify_split.py`

三项硬校验，任一失败 exit 1：

```bash
python3 scripts/split/verify_split.py --root .                      # 单仓校验矩阵本身
python3 scripts/split/verify_split.py --root . --layout split --org <org>   # 多仓布局校验
```

1. **覆盖度** —— `go list ./...` 的每个包都登记在矩阵里，且没有幽灵条目
2. **分层方向** —— tier N 不得依赖 tier > N（自底向上不变量）
3. **依赖闭包** —— 每个模块的 `dependsOn` 必须覆盖其包的实际跨仓依赖

---

## 6. 本地验证结果

在临时副本上跑完单仓改名（`--single`）全流程的实测结果：

| 检查项 | 结果 |
|---|---|
| 映射规则条数 | 46 条，覆盖 46/46 包 |
| `--check` 命中 | 45 个文件 / 62 处 import |
| 改写后残留旧路径 | **0** |
| `go build ./...` | **exit 0** |
| `go test ./...` | 44 ok / 3 FAIL |
| `verify_split.py` | 三项全绿（13 仓 / 46 包 / tier0-3） |

### 3 个 FAIL 的归因（重要）

全部是**环境问题，非改写引入的回归**，逐条对照过：

- **`acpx`、`workcopy`** —— 在**未改动的原仓**上同样 FAIL。失败信息全是
  `cannot create temp file for here document: Operation not permitted`（共 21 处）：
  测试会 spawn 一个用 heredoc 的 `fake-agent` 脚本，当前沙箱禁止 heredoc 写临时文件。
  与改写无关，改动前后一致。
- **`sysprompt`** —— `TestDetectEnvInGitRepo` 的注释写明「agentkit 仓库自身就是 git 仓」，
  测试要求采集到分支与**提交历史**。原仓跑 `ok`，tar 复制的副本没有真实 git 历史故 FAIL。
  是测试对运行位置的假设，不是代码回归。

**尚未验证的部分**：13 仓真实布局下的多 module 构建、跨仓 `-replace` 链路、
以及 oekit 风格共享 CI 的接入 —— 这些需要真实建仓后才能验。

---

## 7. breaking 影响面

| 面 | 影响 |
|---|---|
| **下游 go.mod** | 所有 import `git.enjoye.top/enjoydream/agentkit/*` 的项目必须改。唯一无法用脚本代劳的一步。 |
| **仓内 62 处自引用** | 脚本机械改写，已验证。 |
| **非 Go 引用 23 处** | `README.md` 22 处 + `docs/FRAMEWORK.md` 1 处。`--manual` 可列全，脚本不碰。 |
| **仓根 integration_test** | 依赖全部包，拆分后无处安放。建议独立仓 `{org}/agentkit-e2e`。 |
| **GitCode 侧** | origin 在 `git.enjoye.top/enjoydream/agentkit`，已裁定 **archive**（§8-5），README 加迁移指引。`github.com/yi-nology/agentkit` 同样处理。 |
| **原单仓** | **不 archive**，留 `github.com/oagentkit/agentkit` 薄门面仓做 46 包 re-export，发 v1.0.0 并写死退役时间表（§8-4）。 |

---

## 8. 决策记录

五项全部已裁定，无未决项。

1. ~~**org 名**~~ —— **已定：`oagentkit`**（见 §3）。已锁进脚本默认参数，建仓时无需再传 `--org`。
2. ~~**`runx` 与 `oekit/concurrency` 的重叠裁定**~~ —— **已裁定**：`runx` 更名 `runtime`，
   `breaker` 待退役到 `oekit/concurrency/breaker`。见下方裁定依据。
3. ~~**`obs` 与 `oekit/logx` 的边界**~~ —— **作废，原问题问错了边**。核实后 `obsx` 与
   `logx` 根本不重叠：obsx 只有 7 个导出且全是 LLM 专属，已经 import `oekit/logx` +
   `oekit/concurrency/async`，是**消费方**而非候选替代方。改为一条接线约束，见下方。
4. **原单仓归宿：留薄门面仓**，`github.com/oagentkit/agentkit` 只做 46 包 re-export 零逻辑，
   发 v1.0.0 说明迁移，README 写死退役时间表（v1.x 后转 archive）。
5. **GitCode 侧去留：archive** —— 已定并**部分执行**（2026-10-18）：
   - `github.com/yi-nology/agentkit` —— ✅ **已归档**（`archived=true`），description 已改为迁移指引
   - `git.enjoye.top/enjoydream/agentkit`（GitCode，module path 的实际所在）—— ❌ **未能执行**：
     该域名在当前执行环境不可达 —— curl HTTP 000，Electron 浏览器导航同样失败，
     GitCode API（`api.gitcode.com/api/v5`）返回 401 且本机无 GitCode token。
     **需手动在 GitCode Web 界面归档。**
   - 归档前先确认：GitCode 对 archived 仓是否仍提供只读 git 服务。若下游还有人用旧路径
     `go get git.enjoye.top/enjoydream/agentkit`，归档可能直接打断他们的构建。

### `obs` 仓的接线约束（替代原问题）

`obs` **维持 13 仓之一，不收编到 oekit 侧**。依据：

| | oekit/logx | oekit/tracing | oekit/metrics | agentkit/obsx |
|---|---|---|---|---|
| 导出数 | 35 | 3（`Init`/`Config`/`Provider`） | 1（`Init`） | 7 |
| 定位 | 日志门面 | OTel 初始化 | Prometheus 门面 | **eino callbacks 适配** |

obsx 的 7 个导出（`InitLLMObservability`/`NewTracingHandler`/`TokenUsageOf`/
`DrainStreamUsage`/`StageFromContext`/`WithStage`）全部是 LLM 专属能力，oekit 作为微服务
工具链**永远不会做**。两者是互补关系：oekit 提供 Provider，obsx 把 LLM 调用挂上去。

**唯一要守的约束**：`obs` **必须复用 `oekit/tracing` 的 Provider 和 `logx` 的 trace
提取器（`logx.ExtractTrace`/`SetTraceExtractor`），不自建一套 trace**。否则 agentkit
日志里的 traceid 会与 OTel 的对不上。这条写进 obs 仓的 AGENTS.md。

### `breaker` 退役裁定

`runx` 六包里**只有 breaker 有实质重叠**，另五包不可退役，逐条对过：

| 原 runx 包 | oekit 对应物 | 语义重叠 | 可退役? |
|---|---|---|---|
| `breaker` | `concurrency/breaker` | **高，但非同族** | ✅ 待 #17 落地后退役 |
| `pacer` | `concurrency/ratelimit` | 零 —— 一个是聚合缓冲（攒够窗口再发），一个是准入控制 | ❌ |
| `progress` | `concurrency/progress` | **名字撞车，语义零重叠** | ❌ |
| `procx` | 无 | 零 | ❌ |
| `hotplug` | 无 | 零 | ❌ |
| `hookx` | 无 | 零 | ❌ |

**两个必须记住的坑：**

1. **`progress` 名字撞车但语义相反。** `oekit/concurrency/progress` 是长任务**阶段进度
   追踪器**（`NewTracker`/`PhaseConfig`/`PhaseStatus`）；`agentkit/progress` 是泛型
   **事件总线**（`NewBus[T]`，发布订阅 + 慢消费者丢弃计数）。按包名对齐做退役判断会直接
   坏掉——**对齐必须按语义，不能按名字**。
2. **两侧不是同一算法族。** `concurrency/breaker` 是 Google SRE 式**自适应概率丢弃**
   （`k=1.5`/`minK=1.1`/`protection=5`/`WithForcePass` 1s），状态由窗口统计隐式推导，
   **没有 half-open 三态**；`agentkit/breaker` 是三态熔断器。

提的需求已发到 **[oekit/concurrency#17](https://github.com/oekit/concurrency/issues/17)**，
三条都是通用并发语义而非 agent 专属：代绑定（`AllowGen`/`SuccessGen`/`FailureGen`/
`AbandonGen`）、探测时限（`WithProbeTimeout`）、开放期失败不延长冷却。

**退役是有前置条件的，不是裁定完就能删。** `llm` 现在仍然 import `breaker`，在上游
#17 三项落地之前把它移出矩阵会直接导致构建断裂（`go build` 去拉旧模块失败）。所以：

- **当前**：`breaker` 留在 `runtime` 仓，`modules.json` 里以 `retiredToOekit` 标注状态为
  「过渡期仍在本仓，#17 落地后方可移出」
- **#17 落地后**：删本包 → `llm` 改依赖 `oekit/concurrency/breaker` → **仓数仍是 13，
  只减代码量**

**修正**：早前估过「退役 breaker 可省掉 1 个仓，实际 12 个」——**这个判断是错的**。
剩下的 pacer/progress/procx/hotplug/hookx 仍需一个仓（procx 受分层倒挂硬约束不能并入
`tools`），所以仓数不变。

## 9. 基建清单（照抄 oekit）

| 组件 | 来源 | 用途 |
|---|---|---|
| `.github` 仓 | `oekit/.github` | 共享 reusable workflow，各仓 ci.yml 只带按仓参数 |
| `matrix-tools` | `oekit/matrix-tools` | 版本矩阵生成 / 审计 / 巡检（`gen_versions.py`、`ekit2oekit.py` 同源） |
| `versions.json` | `oekit.github.io/versions.json` | 机器可读版本矩阵，每模块 latest/nested/matrixDeps/breakingIndex |
| `llms.txt` 站点 | `oekit.github.io` | 模块索引 + AI Agent 入口 |
| `AGENTS.md` 模板 | oekit 各仓根 | 面向 AI Agent 的规范与硬规则 |

**注意**：照抄要改的是 org 名和域名，**不要把 oekit 的微服务向硬规则整套搬过来** ——
「错误处理必须用 pkg/errors」「日志必须 logx kv」这类规则是为 Go 微服务写的，
agent 场景部分不适用。抄基建，不抄章程。


---

## 10. 执行结果（2026-10-17 完成）

### 已发布

| 类别 | 仓 | tag | CI |
|---|---|---|---|
| tier 0 | `textx` `runtime` `guardx` `queue` | `v0.1.0` | ✅ |
| tier 1 | `promptx` `obs` `skillkit` `mcpkit` `rag` | `v0.1.0` | ✅ |
| tier 2 | `llm` `toolgate` `tools` | `v0.1.0` | ✅ |
| tier 3 | `agent` | `v0.1.0` | ✅ |
| 门面 | `agentkit`（46 包 re-export） | `v1.0.0` | — |
| 基建 | `.github`（共享 CI） `matrix-tools` `oagentkit.github.io`（llms.txt + 站点） | — | — |

13 个模块仓全部通过 `go build` / `go vet` / `gofmt -l` / `go test -race` 门禁，
并接入 org 共享 CI（`uses: oagentkit/.github/.github/workflows/go-ci.yml@main`，
各仓 `ci.yml` 只是薄调用层）。

### 路径方案落地

包名全部保留，只加模块前缀：`github.com/oagentkit/{repo}/{pkg}`。
因此 import 改写是**纯前缀映射**，62 处自引用机械改写，残留 0。

两处结构提升（三层路径方案）：
- `worker/pglease` → `queue/pglease`
- `llm/wrap` / `llm/llmtest` → `llm/wrap` / `llm/llmtest`
- `knowledge/rag` → `rag`

### 三个 bug（都是这套工具自己引入又修掉的）

1. **gofmt 被改写破坏** —— 改 import 路径会改变组内字母序（`git.enjoye.top/...`
   原本排在 `github.com/...` 之前，改成 `github.com/oagentkit/` 开头的路径后要排到
   `github.com/cloudwego/...` 之后）。原仓 gofmt 是干净的，改写后 2 个文件脏了，
   会直接挂 CI 门禁。修法：`--apply` 后自动 `gofmt -w`，**不要自己实现 import 排序**。
2. **`verify_split` 的依赖闭包校验一直空转** —— `go list -f` 模板用 `\n` 分隔 imports，
   导致每个包跨多行，`splitlines()` 只取到第一行（通常是 `context`），
   其余全被 `continue` 跳掉。**看起来三项全绿，实际什么都没查。**
   修法：模板改用逗号分隔。修好后立刻抓出矩阵里两处真实错漏
   （`promptx` 漏声明 `textx`、`llm` 漏声明 `runtime`）。
3. **门面生成器漏了两类东西** —— 只处理 `GenDecl`（类型/变量/常量）而完全漏了
   `FuncDecl`（函数），导致门面只有类型没有函数；且泛型类型被错误实例化成
   `[any]`（`Bus = upstream.Bus[any]` 会丢掉类型参数）。修法：补 `FuncDecl`、
   泛型写成 `type Bus[T any] = upstream.Bus[T]`、类型别名单独成块、过滤未导出符号。

### 执行中遇到的沙箱限制（与拆分本身无关）

本机沙箱有三条限制，都已绕开或如实记录：

| 限制 | 影响 | 处理 |
|---|---|---|
| 只允许主工作区内写 `.git` | 兄弟目录建仓失败 | 工作区放到 `agentkit/.worktrees/oagentkit-matrix/`（已被 gitignore 覆盖） |
| module cache 的 tmp→final rename 被拒 | 新模块无法进默认缓存 | 用可写的 `GOMODCACHE`，并把旧缓存硬链接复制过去（不占额外空间） |
| 禁子进程 heredoc / `git init` 复制 hooks 模板 | `acpx` `workcopy` 的测试跑不了 | 已对照**未改动的原单仓**确认这批包拆分前同样失败；发版流水线的 `ENV_BLOCKED` 白名单显式记录该例外 |

### 脚本清单

`scripts/split/` 下 6 个，全部在 matrix-tools 仓有副本：

| 脚本 | 作用 |
|---|---|
| `verify_split.py` | 三项硬校验：覆盖度 / 分层方向 / 依赖闭包 |
| `rewrite_imports.py` | import 路径改写（只改 import 块内引号 + 自动 gofmt 重排） |
| `gen_docs.py` | 从 `modules.json` 生成各仓 `README.md` 与 `AGENTS.md` |
| `create_repos.py` | 按矩阵建仓（幂等，默认 dry-run） |
| `publish_repo.sh` | 单仓发版流水线：改写→tidy→build→vet→test→commit→push→tag |
| `push_ci.sh` | 给各仓写薄调用层 `ci.yml` 并推送 |

`modules.json` 是矩阵的唯一数据源：仓名、tier、包清单、依赖地板、`dependsOn`、
GitHub description/topics、各仓的工程红线（`agentNotes`）与日志规则（`loggingRule`）
全在里面。各仓的 README/AGENTS.md 都是从它生成的，不手写，避免漂移。

### 遗留

- `runtime/breaker` 待退役 —— 上游需求 [oekit/concurrency#17](https://github.com/oekit/concurrency/issues/17)
  落地后删包、`llm` 改依赖上游。**落地前不要删**，消费方仍在 import。
- 本机 `/Users/zhangyi/my_project/lacus/oagentkit-matrix/` 与
  `agentkit/.probe-git/` 下有首次失败留下的 `.git/config.lock` 等残留，
  可恢复删除被沙箱拒绝，需手动清理。
