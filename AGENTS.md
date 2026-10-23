# matrix-tools —— 矩阵的机器可读单一数据源与门禁工具链

> 本仓是 oagentkit 矩阵的**单一数据源**（`modules.json`）+ **生成器** + **守卫**。
> 改错一个包名，下游是「静默失效」而不是「报错」——所以本仓的纪律比别仓更严。

## 本仓包含

```
modules.json         单一数据源：13 个模块 + 包清单 + 分层 + 依赖声明 + 元信息
versions.json        生成物：机器可读版本矩阵（由 gen_versions.py 生成，勿手改）
scripts/             工具链（见下）
.github/workflows/   本仓自己的 CI
```

## 三层架构（2026-10-21 起）

守卫脚本按职责分三层，**这是本仓最重要的结构约定**：

| 层 | 文件 | 职责 | 能不能共享 |
|---|---|---|---|
| **事实层** | `matrix_facts.py`、`repo_meta.py`、`matrix_paths.py` | 只取事实：go.mod 说什么、真实 import 是谁、有哪些 tag | **可以**共享——事实没有判定 |
| **判定层** | `verify_*.py`、`check_*.py`、`scan_*.py` | 各自独立判定不变量 | **不能**共享判定逻辑 |
| **渲染层** | `judge_report.py` | 红/黄/绿三档的统一输出 | 可以共享——渲染没有判定 |

### 为什么这么切

矩阵已经踩过两次「共享判断逻辑 ⇒ 共同盲区」：

1. `verify_breaking_index.py` 复用了**生成器** `breaking_index.py` 的解析函数——
   生成器漏收的形态它跟着一起漏，于是「矩阵继续说无破坏性变更」。
2. CHANGELOG 逐字节门禁比的是「生成器丢 body 之后的结果」——
   门禁全绿而发布说明已经残缺（实测 7 仓 773 处正文缺失）。

所以：**事实可以共享，判定不能共享。**
守卫之间互相 import 判定函数，是审查时第一条要拦的东西。

### 耦合必须披露

守卫 import **生成器**（`gen_*` / `breaking_index` / `rewrite_imports`）时，
必须在 `scripts/invariants.json` 的对应不变量的 `sharedWith` 里登记并写明理由。
`verify_inventory.py` 会把未披露的耦合判红。

（`gen_floor_bumps` 也在这个名单里——floorBumps 的判据是「相邻两 tag 的 go.mod 差异」，
独立 oracle 必须自己判，一 import 就共享了判定。）

披露是最低限度的防线——自动判定「这次 import 是否影响了判定」要读代码语义，
不可靠。所以由人 review 理由。

## 硬规则

### 1. 每条不变量都要登记

新增守卫的流程，顺序**不能反**：

1. 在 `scripts/invariants.json` 登记：claim + judges + ci（文件/ job / 触发条件）
2. 写判据脚本
3. 接进 CI
4. 跑 `python3 scripts/verify_inventory.py --root <矩阵根>` 通过

先接线再登记，登记就变成了事后追认——那正是我们要消灭的形态。

**登记的三件事现在都受机器校验**（`verify_inventory.py`）：

| 登记项 | 校验内容 | 判红条件 |
|---|---|---|
| `judges` | 判据脚本存在、已被登记的 CI 文件引用 | 脚本不存在 / 没被引用 |
| `ci[].job` | job 名在 workflow 里存在 | 改名后没同步 ⇒ **判黄** |
| `ci[].triggers` | trigger 存在于 workflow 级 `on:`，且不被该 job 的 `if:` 排除 | 登记了但不会跑 ⇒ **判红** |

⚠️ 触发条件这一腿此前**从未被校验过**（`triggers` 在表里出现 31 次却一次没被读过）——
实测抽掉 workflow 的 `schedule` 触发后门禁仍报「红 0 / 黄 0」，
而 `schedule` 恰恰是跨仓审计唯一的自愈入口（matrix-tools#76）。

⚠️ `if:` 表达式只静态认 `github.event_name` 的 `==` / `!=` 形式（含 `||` / `&&` 组合）；
认不出来的（`always()` 等）⇒ **判黄不判红**——判不了 ≠ 没问题，但也不是确认违规
（硬规则 3）。

### 2. 守卫必须自证还活着

每条守卫都要有 `--selftest`，而且自测必须**带反证样本**：
拿一个**故意造坏**的被检对象跑一遍，要求它报出来。

「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西。

> **这一条现在是机器可执行的不变量**（`verify_inventory.py` 判红，2026-10-22 起）：
> 每个**已登记**的判据脚本必须 ① 支持 `--selftest` ② 该自测在 CI 里被真的调用过
> （`matrix-tools-ci.yml` 的 `guards` job）。缺任一条 ⇒ 登记表门禁当场判红。
> 此前这条规则**只是文档**——实测 27 个登记判据里有 4 个根本没有 `--selftest`
> （`verify_ghost_packages` / `scan_stale_refs` / `verify_docs` / `verify_repo_docs`），
> 它们处于「一直绿、从没生效过」的高危形态且无人知晓。
> 新增守卫时**先写 `--selftest`**，否则会被这条门禁挡下。

### 3. 判不了 ≠ 没问题

取数失败（网络挂了、仓不存在、解析失败）必须与「事实确实如此」**分开**：

- 事实层返回 `None` / 状态标记，不返回空值；
- 判定层把「判不了」判成**黄灯**（可疑），不是绿灯。

历史上 `verify_dep_floors.py` 出现过 owner/repo 拼错，
于是每个依赖都查不到 tag、报告里全是「判不了」却照样 exit 0——
**一直绿、从没生效过**。现在这类情况一律黄灯或红灯。

### 4. 报告分三档

| 档 | 含义 | 默认行为 |
|---|---|---|
| 红 | 确认违规 | 退出码非 0 |
| 黄 | 可疑 / 判不了 / 待确认 | 不失败，但必须出现在报告与产物里 |
| 绿 | 确认通过 | — |

「查得太松」（没人查 latest）与「查得太紧」（CI 假红）此消彼长，
两档模型只能二选一。黄档让「可疑」本身变成可观测的量。

### 5. 取数一律走事实层

「取某仓的最新 tag」「取某文件的内容」「列仓里的路径」——
这些在多个脚本里各写一份时，同一类 bug 要修多次，
而且已经修漏过一次（嵌套模块 tag `audit/v0.1.0` 混进主模块 latest）。

所以：取数走 `repo_meta` / `matrix_facts`，不要自己拼 `gh api` 或 `git` 命令。

### 6. 门禁自测要能区分新旧产物

oracle 类门禁的自测，拿**有盲区的旧产物**跑一遍必须报出缺失；
否则 oracle 也在同一处盲区里，两边一起绿而内容在丢。

### 7. 生成物不手改

`versions.json`、`llms.txt`、各仓 `CHANGELOG.md`、站点 `docs/modules/*.md`
都是生成物。改了生成器就要**重生成全矩阵**，不是重生成报红的那几个——
改动必然影响所有用该生成器的仓，不存在「只影响一部分」。

### 8. 预写段不手改，且重生成不许吞它

各仓 `CHANGELOG.md` 的待发版段由 `gen_changelogs.py --next-version` 生成。

⚠️ **无参重生成会吞掉待发版段**——生成器只按**已存在的 tag** 切段。
（2026-10-21 起已在生成器里加保护：无参重生成会自动保留已有的待发版段。）

预写段必须**单独成一个提交**，且那个提交**只改 CHANGELOG.md**：
混入别的文件会被 squash 合成一个提交，生成器按「实际改了哪些文件」
剔除零信息量提交的判据就失效了。

### 9. floorBumps：「地板追平」是下游的隐式破坏性变更

`versions.json` 的 `floorBumps` 登记每一版**抬了依赖地板**的事实。
判据是**结构性**的：相邻两个 root tag 处的 go.mod `require` 差异
（版本抬高或依赖新增）——**与提交说明的措辞无关**。

它对下游的意义是「这一版会让你的构建红，除非你跑 `go mod tidy`」。
实测报错是 `go: updates to go.mod needed`——**不提真正原因**。

**为什么不并进 `breakingIndex`**：那里存的是**迁移说明**（「import 路径从 X 改为 Y」），
而地板抬高的修法是「跑个 tidy」——那是构建卫生，不是迁移。混进去之后，
下游读它就分不出「改哪行 import」与「跑个 tidy」。
（issue matrix-tools#68：此前「地板追平」既不进索引、又被 oracle 的标记行判据误伤，
索引内容取决于作者怎么措辞——不是事实。）

**谁刷新它**：`gen_floor_bumps.py`，需要各仓的**完整 tag 历史**
（浅克隆会算出假的「没抬地板」——门禁把它判红，不当绿灯）。
`gen_versions.py` 在没有 `--root` 时只**搬运**既有值：它算不出 tag 历史，
若直接不写这个字段，`release.py sync-matrix` 重生成 versions.json 时
就会**把它抹掉**（抹掉即等于删登记）。对账由 `verify_floor_bumps.py`
独立重算事实完成——它与生成器判据完全解耦。

### 10. 欠账台账：「条目不可退休」≠「危害仍在」

`scripts/changelog_tag_debt.json` 记的是「已发布 tag 的 CHANGELOG **快照**里缺该 tag
自己的版本段」。两件事必须分开：

- **条目**（永远不可退休）：org 有 `protect-tags` ruleset ⇒ tag 不可改不可删
  ⇒ 该 tag 处的 CHANGELOG.md 内容不可变 ⇒ 那条快照永远缺自己那一段
  ⇒ 基线里的每一条都永远判 stale、**永远不能移出基线**。
- **危害**（可以消除）：下游读到的**最新 CHANGELOG** 里缺那个版本的段。
  后续任何一次重生成 CHANGELOG 的发版都会把它补上。

⇒ 所以「补发补丁 tag」**消除的是危害、不是条目**。把它写成「根治办法」会让人
以为这批欠账可还——实际它永远清不掉。报告里也**不要**把它写成「待修」。

危害是否已消由 `scripts/verify_debt_mitigation.py` **独立重算**：
判据 = 该仓 main 的 CHANGELOG 是否含该版本段（结构信号，与措辞无关）。

⚠️ **判定不采信基线文件里写的状态**（`mitigation.entries` 只是给人读的账本）：
文件声称「已缓解」而重算为「未缓解」⇒ **判红**（那是企图豁免）——
共享判定 = 共同盲区，那个字段自己就会变成新的可写豁免。

基线内 stale 只警告不挡 ⇒ 谁往基线里加一条新欠账，它就变成「已知」而只留一行警告
⇒ **新增欠账被基线吸收、无人可见**。这条洞由上面那条门禁兜住：
新增的、未缓解的欠账一律判红。

### 11. 仓级文档的依赖红线必须与 go.mod 相容

各仓 `AGENTS.md` / `README.md` 里写的依赖零性红线（「零第三方依赖」/
「`require` 段必须为空」/「零外部依赖」…），**必须与该仓 `go.mod` 的实际
require 段相容**。由 `scripts/verify_repo_dep_claims.py` 判定。

实测 2026-10-24：`guardx/AGENTS.md` 写着「本仓不得有外部依赖
（`require` 段必须为空）」，而它的 go.mod 实测是
`require github.com/oekit/pkg v0.22.1` ⇒ **那句话是假的**；同一文件第 44 行
又把 `oekit/pkg` 当「外部依赖」⇒ **两条指令互斥**，无论怎么解读都自相矛盾。

三个本该抓住它的机制逐个失效：`verify_repo_docs.py` 是报告器（恒不失败）、
`gen_docs.py` 因这 13 个仓的 README/AGENTS.md **没有生成区标记**而一律跳过、
`verify_repo_tiers.py` 只判层位数字不判依赖红线 ⇒ 仓级依赖红线**完全裸奔**。

三条纪律：

1. **写依赖红线要把语义钉死**。矩阵里「外部依赖」有两个读法：「一条 require
   都没有」与「没有第三方依赖」——第一方基座 `github.com/oekit/*`（姊妹矩阵）
   算哪一种？`modules.json` 的 tier 0 定义与 `verify_dep_floor.py` 的基座白名单
   都把它算作**允许**的依赖 ⇒ 该写「**零第三方依赖**」，并列出允许的依赖
   （样板：`obs/README.md` 的「外部依赖地板」一节）。
   ⇒ 没钉死语义 ⇒ 门禁**判黄**（「核对不了」），CI 用 `--fail-on-yellow` 升级为失败。
2. **只判本仓自述，不判跨仓提及与否定句**（与 `verify_repo_tiers.py` 同款边界）。
3. **require 段口径含 indirect**：文档自己的理由就是「import 某个包会把
   **整个模块的 require** 拉进消费方的模块图」——间接依赖同样进模块图。

## 发版

发版动作已脚本化（`scripts/release.py`），顺序依赖由代码保证：

```bash
# 1. 预写待发版段（只改 CHANGELOG.md，单独提交，推分支开 PR）
python3 scripts/release.py --root <矩阵根> prewrite --repo agent --version v0.10.1

# 2. PR 合并后，在 main 上打 tag（前置条件不满足会拒绝执行）
python3 scripts/release.py --root <矩阵根> tag --repo agent --version v0.10.1

# 3. 刷新矩阵源并同步站点副本（过去这一步不在任何 runbook 里）
python3 scripts/release.py --root <矩阵根> sync-matrix

# 4. 刷新 floorBumps（**每个仓发版后都要做**）
#    它登记「这一版抬了依赖地板」——下游据此知道要跑 go mod tidy。
#    需要各仓的完整克隆（<各仓克隆父目录> 下应有 14 个 Go 仓）。
cd <矩阵根>/matrix-tools
python3 scripts/gen_floor_bumps.py --root <各仓克隆父目录> \
        --versions versions.json --write
```

发版前体检（只读）：

```bash
python3 scripts/release.py --root <矩阵根> doctor --repo agent --version v0.10.1
```

## 本仓自己的 CI

`.github/workflows/matrix-tools-ci.yml`：

| job | 守什么 |
|---|---|
| `Scripts` | 语法 + `--help` 冒烟 + 路径规则自洽 |
| `Guards self-test` | 每条守卫的自测（带反证样本） |
| `changelog-coverage` | CHANGELOG 覆盖（独立 oracle，PR 只查本 PR 触及的仓） |
| `matrix-truth` | breakingIndex / import 路径 / dep 地板 / Go 地板 / 欠账基线缓解状态（联网对账） |
| `floor-bumps` | floorBumps 登记 ↔ 各仓 tag 历史的 go.mod 差异（需完整克隆） |
| `release-floor` | 打 tag 时刻的地板时效 |
| `facade-symbols` | 门面符号级转发完整性 |
| `facade-frozen` | 门面包集合冻结 |
| `dep-floors` | 依赖地板**报告器**（恒不失败，不挂 PR） |
| `drift` | versions.json 与 modules.json 一致 + llms.txt 可确定性重生成 |
| `matrix-local-guards` | 层级方向 / 门面规则 / 仓文档 / **各仓 AGENTS.md 层位声明** / **各仓依赖零性声明**（需要各仓源码） |
| `pending-release` | 预写段可观测性（需完整历史，只在 push/定时跑） |
| `latest-field` | latest 字段 ↔ 远端最新 tag |
| `inventory` | 不变量登记表（承诺 ↔ 判据 ↔ 接线 ↔ 触发条件） |
| `receipts` | issue 回执落地（被机器自动关闭的 issue 必须留下带锚点的回执） |

⚠️ 跨仓一致性门禁有「顺序窗口」：本仓推了生成物而站点副本未跟上时，
`.github` 仓的门禁会红，且红因与那个 PR 无关。按「谁造成漂移谁负责」处理。

### 回执门禁的范围与归属地（issue matrix-tools#90）

「**跨仓硬失败必须有唯一归属地**」——一个仓的欠账不该把别的仓的 CI 弄红。

| 事件 | 范围（`verify_receipts.py --ci --scope-mode repo`） | 谁负责 |
|---|---|---|
| PR 事件 | **PR 的 base 仓**（＝issue 所在仓） | 该仓自己 |
| push / 手动 | **调用方本仓** | 该仓自己 |
| schedule（每日 06:17） | **全矩阵** | matrix-tools（跨仓硬失败的唯一归属地） |

- 各模块仓的回执检查由 `.github` 的**可复用工作流**下发（`receipts-enabled` 开关 +
  `receipts-scope` 口径）⇒ **每个仓自己 issue 的回执由它自己负责**，
  别的仓的缺口不再把无关仓的 PR 弄红；
- 本仓（matrix-tools）的 `receipts` job 是**硬失败**（不再 `continue-on-error`）：
  判据准了、范围有了归属地，结论就该落到 CI 上；
- ⚠️ `schedule` 事件**永远是全矩阵**，哪怕在 `repo` 模式下——
  那是周期性全量审计的唯一入口，收窄它＝跨仓欠账再也没人抓（静默失效）；
- **容忍窗**：回执是**合并之后**才补的（要等 CI、要打 tag）⇒ 刚关单的缺口在巡检口径下
  判**黄**（「可能还在路上」）而不是红；**PR 事件严格**（容忍窗 0）——
  那时看到的缺口一定是上一轮合并留下的。假红会让人习惯性忽略红标。

## 不属于本仓

- 各模块仓的代码与测试
- 门禁规则本体（那是 `oagentkit/.github` 的 `.github/workflows/go-ci.yml`）
- 文档站内容（`oagentkit/oagentkit.github.io`）
