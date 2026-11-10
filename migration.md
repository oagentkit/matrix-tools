# oagentkit 矩阵包搬迁映射（migration.md）

> 消费方升级依赖时按此页查「哪个包搬去哪了」，替代逐仓 CHANGELOG 考古（agent#68）。
>
> **搬迁纪律**（自 agent#68 起）：跨模块搬包时，旧路径保留同签名的 `// Deprecated:` 转发壳**至少一个 minor**，下个 minor 再删；CHANGELOG 条目固定 `MovedFrom / MovedTo / 兼容期截止版本` 三行格式。

| 旧路径 | 新路径 | 搬迁版本 | 兼容壳 |
|---|---|---|---|
| `github.com/oagentkit/guardx/audit` | `github.com/oagentkit/obs/audit` | guardx v0.7.0（2026-10-19） | 无（当时纪律未立；路径已冻结） |
| `github.com/oagentkit/guardx/bashguard` | `github.com/oagentkit/tools/bashguard` | guardx v0.7.0（2026-10-19） | 无（同上；`audit/v0.1.0`、`bashguard/v0.1.0` 两个嵌套 module tag 已冻结） |
| `github.com/oagentkit/toolgate/invoke` | `github.com/oagentkit/guardx/invoke` | toolgate v0.12.6 | **已删除**——壳随 toolgate v0.15.0~v0.16.1 提供，兼容期截止 toolgate v0.16.0，**已于 toolgate v0.17.0 删除**（toolgate#93）；机器可读登记见 `modules.json` 的 `compatShells` 段 |
| `github.com/oagentkit/obs/obsx` | `github.com/oagentkit/llm/obsx` | llm v0.12.0 迁入 / obs 同期迁出（llm CHANGELOG「obsx 已于 v0.12.0 从 obs/obsx 迁入本仓」） | 无（当时纪律未立） |
| `github.com/oagentkit/agentkit/*`（单仓门面，48 包） | 13 个拆分模块仓各自路径 | agentkit v1.0.0 起（过渡门面仍全量转发，退役条件见 agentkit MIGRATION.md） | 门面本身即兼容层 |

## 注意

- **import 路径不是纯前缀映射**：旧单仓 `agentkit/<包>` 迁到新矩阵时中间层目录会被丢掉（如 `agentkit/notifyx/hook` → `github.com/oagentkit/runtime/hook`…**错误示例**，真实映射见站点 `llms.txt` 的 import 对照段）。按「仓库名 + 完整旧路径」拼会直接构建断裂。
- 嵌套 module tag（`audit/v0.1.0` 等）是历史遗留，那条版本线**已冻结**，不再发版。
- 新增搬迁条目时同步更新本表与对应仓的 CHANGELOG 三行条目；页由人肉维护（矩阵内无机器单源），machine-readable 的包清单以 `modules.json` 为准——带转发壳的搬迁还须把「壳 + 兼容期截止版本」登记进 `modules.json` 的 `compatShells` 段（toolgate#93 起），到期未删由门禁判红。
