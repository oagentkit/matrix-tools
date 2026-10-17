# matrix-tools

oagentkit 模块矩阵的自动化仓 —— 生成、审计、改写、发布。

**`modules.json` 是整个矩阵的唯一数据源**：13 个模块的仓名、tier、包含的包、
依赖地板、dependsOn、GitHub description/topics、以及各仓的工程红线条目都在这里。
改矩阵改这个文件，本仓的脚本与各仓的文档生成器都从它读取。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/gen_versions.py` | 从 GitHub tag 生成机器可读的 `versions.json`（latest / matrixDeps / tier / 包清单） |
| `scripts/verify_split.py` | 矩阵三项硬校验：覆盖度 / 分层方向 / 依赖闭包 |
| `scripts/rewrite_imports.py` | 旧 module 路径 → 新矩阵路径的 import 改写（只改 import 块内引号字符串 + 自动 gofmt 重排） |
| `scripts/gen_docs.py` | 从 `modules.json` 生成各仓的 `README.md` 与 `AGENTS.md` |
| `scripts/create_repos.py` | 按矩阵建仓（带 description 与 topics，幂等，默认 dry-run） |
| `scripts/publish_repo.sh` | 单仓发版流水线：改写 → tidy → build → vet → test -race → commit → push → tag |

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
- module 路径固定三层：`github.com/oagentkit/{repo}/{pkg}` —— 包名一律保留，只加模块前缀，
  所以 import 改写是纯前缀映射。`llm/llm`、`rag/rag` 这类双层目录是刻意的，
  对标 oekit 的 `oekit/hertz/hertz`。
- 改 module 路径必须同步：全部 import、go.mod、README 安装行、llms.txt。

## License

MIT
