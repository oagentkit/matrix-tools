# matrix-tools

oagentkit 模块矩阵的自动化仓 —— 生成、审计、改写、发布。

**`modules.json` 是整个矩阵的唯一数据源**：13 个模块的仓名、tier、包含的包、
依赖地板、dependsOn、GitHub description/topics、以及各仓的工程红线条目都在这里。
改矩阵改这个文件，本仓的脚本与各仓的文档生成器都从它读取。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/gen_versions.py` | 从 GitHub tag 生成机器可读的 `versions.json`（latest / matrixDeps / tier / 包清单 / `fromLegacy` / **breakingIndex** / 嵌套模块版本线） |
| `scripts/breaking_index.py` | **破坏性变更抽取规则本体**：从各仓 CHANGELOG.md 抽出 breaking 条目（三种判定依据 + 负向样本自测）。`gen_versions.py` 与 `verify_breaking_index.py` 共用 |
| `scripts/repo_meta.py` | **远端取数层**：最新 tag / 文件内容 / 目录树 / module 路径的统一取法与判定（含 tag 过滤自测） |
| `scripts/verify_breaking_index.py` | **对账门禁**：矩阵 `breakingIndex` ↔ 各仓 CHANGELOG（漏收 / 过期 / 内容漂移 / 取不到即红） |
| `scripts/verify_import_paths.py` | **对账门禁**：矩阵 `importPath` ↔ 各仓**真实** go.mod 与包目录（旧路径 / 幽灵包 / 模块改名 / 取不到即红） |
| `scripts/verify_dep_floor.py` | **依赖地板门禁 + 地板对账**：① tier0/tier1 不得有重量级第三方直接依赖（零依赖核心不许白吃它）；② 矩阵 `depFloor` ↔ 各仓**真实** go.mod 的实际地板（漏登记 / 多登记 / 内容漂移 / 取不到即红）。`--org` 联网读远端 go.mod，`--root` 读本地，`--selftest` 离线自测 |
| `scripts/verify_changelog_at_tag.py` | **对账门禁**：tag ↔ CHANGELOG 版本节双向一一对应 + 每个已发布 tag 的快照里必须含自己那段（存量欠账走基线记账） |
| `scripts/verify_changelog_coverage.py` | **覆盖门禁（独立 oracle）**：直接扫**原始 git 历史**算「应当出现的条目与正文」，与提交版 CHANGELOG 比对（**条目级 + body 级**）。不复用生成器的解析函数——生成器丢 body 时门禁必须还能报出来（issue matrix-tools#41） |
| `scripts/verify_split.py` | 矩阵三项硬校验：覆盖度 / 分层方向 / 依赖闭包 |
| `scripts/matrix_paths.py` | **旧→新 import 路径构造规则的唯一事实源**。`rewrite_imports.py` / `gen_versions.py` / `gen_llms_txt.py` 全部 import 它，规则改一处即可 |
| `scripts/rewrite_imports.py` | **唯一的 import 改写引擎**：旧 module 路径 → 新矩阵路径。只改 import 块内引号字符串，未登记的包直接报错退出（`--check` / `--apply` / `--print-rules` / `--manual`） |
| `scripts/gen_docs.py` | 从 `modules.json` 生成各仓的 `README.md` 与 `AGENTS.md` 的**骨架**（见下方警告） |
| `scripts/create_repos.py` | 按矩阵建仓（带 description 与 topics，幂等，默认 dry-run） |
| `scripts/publish_repo.sh` | 单仓发版流水线：改写 → tidy → build → vet → test -race → commit → push → tag |
| `scripts/gen_llms_txt.py` | 从 `modules.json` 生成 `llms.txt`（面向 LLM 的模块索引） |
| `scripts/verify_docs.py` | **防漂移**：重新生成 `llms.txt` 与提交版比对，不一致 exit 1（`--fix` 直接覆盖） |
| `scripts/verify_repo_docs.py` | **防抹除**：检测各仓 README/AGENTS 与 `gen_docs.py` 输出的差异（只报告，不提供 `--fix`） |
| `scripts/check_facade_imports.py` | **退役守卫**：扫描下游是否仍 import 门面仓/旧 module，命中 exit 1 |
| `scripts/verify_dep_floors.py` | **地板漂移报告器**：各仓 `go.mod` 的第一方依赖地板 vs 远端最新 tag（`--enforce` 发版前自检 / `--json` 机器可读 / `--selftest` 离线自测）。**默认恒 exit 0**——漂移是常态，硬门禁会逼出发版跑步机 |
| `scripts/publish_repo.sh` | ⚠️ **已消费**，拆分期的一次性引导脚本（硬编码 v0.1.0 tag），保留仅为记录门禁顺序与踩坑注释 |

> ⚠️ `gen_docs.py` 只生成各仓文档的**骨架**。生成后有人手写补充了大量内容
> （实测 13 仓 26 个文档里 10 个已与生成器输出不一致，且仓库版本更丰富）。
>
> **脚本已改为默认不覆盖**：只有带 `<!-- oagentkit:generated:begin/end -->`
> 标记的文档才会被更新标记之间的内容；无标记的文档会被**跳过并报告**，
> 因为整文件覆盖会抹掉手写内容。
>
> 首次给某个仓「开口子」用 `--adopt`：它把现有内容整体保留为手写区，
> 在文末追加生成区。此后每次生成只更新生成区，手写区永不触碰。
>
> ```bash
> # ① 补标记（一次性，不丢内容）
> python3 scripts/gen_docs.py --out-root <各仓父目录> --adopt
> # ② 之后正常生成，只更新生成区
> python3 scripts/gen_docs.py --out-root <各仓父目录>
> # ③ 想看还有哪些仓没开口子
> python3 scripts/verify_repo_docs.py --mirror <各仓父目录>
> ```
>
> `--adopt` 可能让与 `modules.json` 已有的内容重复一遍（重复是可修的，丢失不可逆——
> 这个取舍是有意的）。

## 用法

```bash
# 刷新版本矩阵（结构变更或发版后重跑）
# --root 指向各仓本地目录的父目录 → breakingIndex 从各仓 CHANGELOG 抽取
python3 scripts/gen_versions.py --org oagentkit --root /path/to/oagentkit

# 校验矩阵（拆分前/拆分后都要跑，任一失败 exit 1）
python3 scripts/verify_split.py --root <agentkit 单仓路径>

# 矩阵作为「机器可读权威源」的三条对账门禁
python3 scripts/verify_breaking_index.py --org oagentkit   # breakingIndex ↔ CHANGELOG
python3 scripts/verify_import_paths.py  --org oagentkit   # importPath ↔ 真实 go.mod / 包目录
python3 scripts/verify_dep_floor.py     --org oagentkit   # depFloor ↔ 真实 go.mod 地板 + tier0/1 地板为空
# 三条的离线自测（守卫必须能证明自己还活着）
python3 scripts/verify_breaking_index.py --selftest
python3 scripts/verify_import_paths.py  --selftest
python3 scripts/verify_dep_floor.py     --selftest

# 改写 import
python3 scripts/rewrite_imports.py --org oagentkit --print-rules   # 看映射
python3 scripts/rewrite_imports.py --org oagentkit --check         # 只报告
python3 scripts/rewrite_imports.py --org oagentkit --apply         # 执行

# 重新生成各仓文档
python3 scripts/gen_docs.py            # 全部
python3 scripts/gen_docs.py llm rag    # 指定仓

# 建仓（默认 dry-run）
python3 scripts/create_repos.py
python3 scripts/create_repos.py --apply
```

## CHANGELOG 门禁

各仓的 `CHANGELOG.md` 由 `gen_changelogs.py` 从 git 历史生成。守它的是**三道**门禁，
各管一件事：

| 门禁 | 守什么 | 判据 |
|---|---|---|
| `gen_changelogs.py --check` | **提交版跟上了 git 历史吗** | 两级覆盖：重生成结果里的每个版本段、每条条目、每行正文都必须在提交版里（**提交版可以更丰富**） |
| `verify_changelog_coverage.py` | **发布说明的内容完整吗** | 独立 oracle：扫原始 git 历史算「应当出现的条目与正文」，与提交版比对 |
| `verify_changelog_at_tag.py` | **tag 与版本节对得上吗** | 双向一一对应 + 每个已发布 tag 的快照里必须含自己那段 |

### 为什么 `--check` 不再逐字节相等

逐字节相等会**逼人删掉手写发布说明**：手写的迁移步骤、补充说明在重生成结果里没有
对应行，于是「不一致」⇒ 人只能把手写内容删掉，或者干脆不写。而发布说明的价值恰恰
常常在这些**生成器写不出来**的内容上（issue matrix-tools#41）。

放宽到「覆盖」之后：

- 提交版**可以更丰富**（多出来的手写内容不算问题）；
- **缺内容仍然是红的**（漏条目 / 漏正文 / 缺版本段都红）；
- **顺序仍然守**（按顺序做子序列匹配）⇒「段落张冠李戴」「条目顺序错乱」仍会被发现；
- `--check --strict` 保留**逐字节相等**模式（守住格式一致性），默认不启用。

⚠️ 这道门禁只看「内容在不在」，**生成器自己算错**它看不见——那正是独立 oracle
（`verify_changelog_coverage.py`）存在的理由：它直接扫原始 git 历史，
完全不经过生成器的解析逻辑。实测 2026-10-20：7 个仓合计 **773 处正文缺失**
（旧产物里 body 整段消失、条目却都在），逐字节门禁一条都没报出来。

### 门禁范围：谁造成漂移谁负责

org 级全量审计**不能**跑在每个 PR 上——任一模块欠账会堵住所有无关 PR
（实测 `oagentkit/.github#15` 被「各仓 CHANGELOG 未过期」卡了两天）。

范围按 **PR 触及范围**裁剪：

- **PR 事件** → 只校验「PR 触及的仓」= **PR 的 base 仓**（GitHub 的 PR 是单仓的：
  PR 能改动的文件全部落在它自己的 base 仓里），且用 **PR head** 的内容校验
  （只裁剪不换 ref 解决不了问题：合入前各仓 main 上还是旧产物）；
- **非 PR 事件**（push / 定时 / 手动）→ 全矩阵（**周期性全量审计兜底**，不可省）。

```bash
# 独立 oracle：本地各仓（--root 的每个子目录是一个 git 仓）
python3 scripts/verify_changelog_coverage.py --root /path/to/oagentkit
python3 scripts/verify_changelog_coverage.py --root ... --repo agent --repo rag

# 联网：按需把各仓克隆下来再校验
python3 scripts/verify_changelog_coverage.py --org oagentkit

# CI 模式：按 GitHub 事件上下文裁剪范围（PR → 只查本 PR 触及的仓）
python3 scripts/verify_changelog_coverage.py --ci --org oagentkit

# 离线自测
python3 scripts/verify_changelog_coverage.py --selftest
```

## 依赖地板漂移

「子依赖发版时只动了自己的 `go.mod`、没回头抬本仓 `require`」是一类会复发的漂移：
后果不是编译失败，而是 MVS 被旧地板压住——以本仓为入口的消费方自己 require 了新版，
最终拿到的仍是旧版本。

`oagentkit/agent#8` 与 `oagentkit/toolgate#10` 是同一类漂移的两份报告。

```bash
# 报告全矩阵漂移（默认恒 exit 0，只让漂移可见）
python3 scripts/verify_dep_floors.py

# 只看一个仓 / 查某个已发布 tag 的地板
python3 scripts/verify_dep_floors.py --repo agent
python3 scripts/verify_dep_floors.py --ref v0.9.0

# 发版前自检：有漂移则 exit 1
python3 scripts/verify_dep_floors.py --enforce

# 守卫自测（离线，不联网）
python3 scripts/verify_dep_floors.py --selftest
```

**为什么不做成硬门禁**：任何一次子仓发版都会让所有下游变落后。硬门禁等于要求每个仓
在每次子仓发版后都跟着发一版，长期只会训练出「红了就加豁免」的习惯。所以判定放在
`--enforce` 供发版 runbook 手动用，CI 里跑的是**报告器**（`dep-floors` job，
`continue-on-error`，且不挂 `pull_request`）。

> ⚠️ 取最新 tag 用 `gh api repos/{owner}/{repo}/tags`，**不要用 `go list -m -versions`**：
> 后者报的是 proxy 的缓存目录而非远端现实（实测 `oagentkit/agent` 会列出远端已删的
> v0.1.0～v0.5.0，`oekit/pkg` 会列出不存在的 v0.9.2）。要确认真相用
> `git ls-remote --tags`。

## verify_split 校验什么

1. **覆盖度** —— `go list ./...` 的每个包都登记在 `modules.json`，且无幽灵条目；
   待退役包必须显式登记（否则报错，不静默跳过）。
2. **分层方向** —— tier N 的模块不得依赖 tier > N（自底向上不变量）。
3. **依赖闭包** —— 每个模块的 `dependsOn` 必须覆盖其包在仓内的全部实际跨仓依赖。

⚠️ 第 3 项的实现有个坑值得记住：`go list -f` 的模板必须用**逗号**分隔 imports。
用 `\n` 分隔会让每个包跨多行，`splitlines()` 只取到第一行（通常是 `context`），
依赖闭包校验会**静默空转**，看起来全绿实则什么都没查。

## 命名与分层约定

- org：`github.com/oagentkit`（Open Agent Kit），与 `github.com/oekit`（Open Easy Kit，
  Go 微服务工具链）是**两套独立矩阵**；oagentkit **依赖** oekit 的 `pkg`/`concurrency`/`logx`。
- module 路径固定三层：`github.com/oagentkit/{repo}/{pkg 末段}`。**注意这不是纯前缀映射**——
  旧单仓 `agentkit/<包>` 的中间层目录会被丢掉（那层组织职责由仓承担）：
  `agentkit/knowledge/rag` → `oagentkit/rag`，
  `agentkit/worker/pglease` → `oagentkit/queue/pglease`。
  **唯一的例外**：包名末段与仓名相同时（`llm`、`rag`）收口到 module 根，
  `agentkit/llm` → `oagentkit/llm`，**不重复成 `llm/llm`**——
  Go 的惯例本就是包名不重复 import 路径末段。
  规则实现见 `scripts/matrix_paths.py`，完整对照跑 `rewrite_imports.py --print-rules`。
- 改 module 路径必须同步：全部 import、go.mod、README 安装行、llms.txt。

## License

MIT
