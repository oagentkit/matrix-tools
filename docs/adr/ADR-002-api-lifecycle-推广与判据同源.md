# ADR-002 · api-lifecycle 推广与判据同源

| 项 | 内容 |
|---|---|
| **状态** | 已采纳（Accepted） |
| **日期** | 2026-11-28 |
| **背景** | oagentkit 演进评审 2.3-问题五（生态兼容性需要稳定契约：模型/工具/存储/Embedding/追踪后端的替换成本可控）+ 2.4-阶段一「能力稳定化」（LLM / Tool / MCP / Skill / Agent / 生命周期六类接口优先稳定）。kernel 已有 `api-lifecycle.yaml` + `make apilifecheck` 跑熟的定级机制，oagentkit 各仓**全部没有**——「改了算不算 breaking」只靠 CHANGELOG 事后记。issue `matrix-tools#256`。 |
| **决策** | ① **判据同源**：不另写一份——分级判定（什么算 breaking / 三级承诺 / 晋级判据 / 清单格式）一律引用 kernel `docs/api-compat.md`，两 org 同源；差异记在本 ADR §二。② **实现单源**：判定实现只此一份 `matrix-tools/scripts/verify_api_lifecycle.py`，矩阵侧 `--root` 扫全仓、各仓 CI 用 `--repo` 调用——**各仓只接线不复制判据**。③ **接入 opt-in 分批**：先接口面 7 仓，其余见 §三；没放清单报「未接入」信息，**清单一放全套判定立即生效**。④ **level 初值策略**：默认 Experimental，Stable 只给已有代码级 Stable 标记或有晋级证据的包（§四）。 |
| **后果** | 已接入仓新增包未登记 ⇒ `matrix-local-guards` 红；点名接口有机器可读定级；kernel 与 oagentkit 两 org 引用**同一份**判据文档，改判据只改一处。 |
| **依据** | kernel `docs/api-compat.md`（origin/main：2026-11-28 起晋级判据为三维，随 kernel#540 落地）；kernel `cmd/apilifecheck` 判定形态实测（50 包 = Stable 15 / Experimental 35，包级标记 `// 生命周期: <级别>`）；本守卫对 kernel 真仓实测零红（判据同源的实弹校验）。 |

> **一句话结论**：定级的**判据**读 kernel 的文档、**实现**只写 matrix-tools 一份、
> **接入**按批次 opt-in——推广不是把 apilifecheck 抄 16 份。

---

## 一、判据同源（引用，不复制）

以下四面一律以 kernel `docs/api-compat.md` 为准，本仓不另立口径：

1. **什么算 breaking**（四类命中任一即 breaking）：改签名 / 改语义 / 改默认值 /
   删导出符号；新增导出符号、新增包、修 bug、性能优化、内部重构不算。
2. **三个级别的承诺**：Stable（additive-only）/ Experimental（不承诺兼容）/
   Deprecated（必须给替代符号）。
3. **晋级判据**（2026-11-28 起三维，kernel#540）：一个 minor 无 breaking 窗口 +
   真实宿主接入证据 + 真实故障测试证据；不齐步走。
4. **清单格式**：`version / packages[{pkg, level, since, reason}]`，
   `reason` 必填（防免死金牌）。

**包级不是符号级**：新增导出符号不单独判级，它继承所在包的级别
（api-compat.md 的刻意设计——符号级标记会让每个导出都变成一处要维护的注释）。

## 二、与 kernel 实现的差异（本 ADR 记录的全部差异）

| 面 | kernel | 推广版 | 为什么 |
|---|---|---|---|
| 实现语言 | Go（`cmd/apilifecheck`，go/ast 精确解析 File.Doc 与符号存在性） | Python 词法近似（`package` 行回溯连续 `//` 行 = 包注释；正则找导出声明） | 判据本体要能在 matrix-tools 的守卫体系里被 `judge_report`/selftest/`verify_inventory` 复用；近似边界在脚本 docstring 诚实声明，selftest 带正反例钉形态。**同仓同判据不复制**：kernel 侧照跑它自己的 Go 版本 |
| 触发位置 | kernel 仓 `make unit/check`（推送前本地即说话） | 矩阵侧 `matrix-local-guards` 克隆后 `--root` 扫全仓；各仓 CI 用 `--repo` 接线 | 判据单源在 matrix-tools（与 `verify_ghost_packages` / `verify_import_direction` 同族）；各仓薄调用层零改动 |
| 覆盖范围 | kernel 仓内**全量强制** | **opt-in 分批**：没放清单报「未接入」信息、退出 0；清单一放全套判定生效 | 50 包可以一次登记，15 仓 × 上百包不能一次铺——分批是推广策略不是判定松紧（放了就跑不掉） |
| `follows_upstream` 字段 | 支持（门面包跟上游级别，当前 0 条使用） | 接受并跳过其校验 | 保持清单互通；agentkit 门面本轮不接入（SKIP_DIRS） |
| `docsblock` / `MATURITY.md` 盘点 | kernel 仓内 docs 对账 + 晋级盘点表（maturity.go） | **不推广**（本轮） | 晋级盘点是 kernel 仓内治理面；oagentkit 各仓的晋级证据走三维判据时再接 |

除上表外**无差异**：词表、`since` 形态、reason 必填、Deprecated-replacement
规则、覆盖双向、标记对账——全部同源。

## 三、推广批次（opt-in 节奏）

| 批次 | 仓 | 状态 |
|---|---|---|
| 0（起点） | `yi-nology/goagent-kernel` | 已有（50 包，本守卫实测零红） |
| 1（本单） | 接口面 7 仓：`llm` `mcpkit` `guardx` `skillkit` `agent` `runtime` + `promptx` | 本单登记（`promptx` 因点名的生命周期接口 `steering` 而进第一批） |
| 2（后续） | `textx` `toolname` `queue` `obs` `rag` `toolgate` `tools` `goagent-runtime` | 纯函数仓与装配仓，随各自波次接入 |
| 豁免 | `agentkit`（已退役门面）、`matrix-tools`、`.github`、站点仓 | 非模块仓，不进清单 |

批次推进的机器面：`verify_api_lifecycle.py --root` 的「已接入 N / 扫描 M」
note 就是进度表——M−N 就是剩余欠账，不需要另立台账。

## 四、level 初值策略与点名接口定级

**初值策略**：默认 `Experimental`。`Stable` 只在两种情况下给——
（a）代码里**已有** `// 生命周期: Stable` 标记（既有承诺，如 `guardx/ticket`）；
（b）有晋级三维证据的包（本批没有——各仓此前从未承诺过定级，不追认）。
这是「不齐步走」的反面应用：**不齐着升，也不齐着降**，初值如实反映现状。

**评审点名的五类接口**（验收 4）：

| 接口类 | 包 | 定级 | 依据 |
|---|---|---|---|
| LLM 接口 | `llm/llm/port`（Completer 窄接口） | Experimental | 未过一个 minor 无 breaking 窗口（晋级条件一） |
| Tool 接口 | `guardx/invoke`、`guardx/toolfail` | Experimental | 同上；`guardx/ticket` 另按既有标记记 Stable |
| MCP 接口 | `mcpkit/iface`（协议中立面） | Experimental | 同上 |
| Skill 接口 | `skillkit/skill`（Provider / Lister / AliasResolver） | Experimental | 同上 |
| 生命周期接口 | `runtime/hookx`、`promptx/steering` | Experimental | 同上 |
| Agent 接口 | **当前不存在**（全矩阵无 `type Agent interface`） | 待 `agent#174` 裁决 | 裁决出统一入口后再定级，不抢跑 |

## 五、门禁接线

1. `scripts/invariants.json` 登记 `api-lifecycle-registration`（oracle independent：
   只读各仓源码 + `api-lifecycle.yaml`，不 import 生成器）；
2. `scripts/verify_api_lifecycle.py`：清单自洽 + 覆盖双向 + 标记对账，
   `--selftest` 九例反证（新增包无定级 / 标记三态 / 幽灵登记 / 未接入不红 /
   CLI subprocess 无崩溃），SMOKE_ARGS 指向 `scripts/fixtures/api_lifecycle_repo`；
3. CI：`matrix-local-guards`（克隆全仓后 `--root .` 真判定）+ `selftest` 分片
   （AST 自动发现）；
4. `verify_inventory` 通过。

## 关联

- issue：`oagentkit/matrix-tools#256`（本 ADR 的执行单）、`#254`（演进总纲）
- 判据源：`yi-nology/goagent-kernel` `docs/api-compat.md`、`cmd/apilifecheck`
  （仓内 Makefile target，不 import——判定各管各的仓）
- 配对单：`yi-nology/goagent-kernel#525`（机制成熟度三维判据，已随 #540 落地——
  本 ADR §一引用其结论，不重复做 kernel 仓内盘点）
- 接口面后续：`oagentkit/agent#174`（统一 Agent 接口裁决后补定级）
