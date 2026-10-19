# matrix-tools

oagentkit 模块矩阵的自动化仓 —— 生成、审计、改写、发布。

**`modules.json` 是整个矩阵的唯一数据源**：13 个模块的仓名、tier、包含的包、
依赖地板、dependsOn、GitHub description/topics、以及各仓的工程红线条目都在这里。
改矩阵改这个文件，本仓的脚本与各仓的文档生成器都从它读取。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/gen_versions.py` | 从 GitHub tag 生成机器可读的 `versions.json`（latest / matrixDeps / tier / 包清单 / `fromLegacy`） |
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
python3 scripts/gen_versions.py --org oagentkit

# 校验矩阵（拆分前/拆分后都要跑，任一失败 exit 1）
python3 scripts/verify_split.py --root <agentkit 单仓路径>

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
