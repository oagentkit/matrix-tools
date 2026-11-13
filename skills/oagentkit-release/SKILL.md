---
name: oagentkit-release
description: >-
  oagentkit 矩阵发版/tag/CHANGELOG/矩阵同步操作手册（release.py prewrite→tag→sync-matrix 流水线）。
  触发场景：给 oagentkit 各仓发新版、打 tag、预写 CHANGELOG 段、发版后同步矩阵源与站点，
  或排查发版门禁失败、prewrite 被重生成吞段、地板时效拦截。仅适用 oagentkit org。
---

# oagentkit 发版操作手册

> 权威定义在 `matrix-tools/scripts/release.py`（顺序敏感的前置条件全写成代码）——本 skill 是
> 提炼不是替代。所有命令在 `~/XiaomiMiMoProjects/oagentkit-matrix/matrix-tools` 仓根执行。
> 「用 oagentkit 包写代码」用 **oagentkit** skill；issue 扫描复用 **oekit-sweep**（工具通用）。

## 流水线（四子命令，顺序敏感不可跳步）

**`--root` 是全局 flag，必须放在子命令之前**（默认 `.` = matrix-tools 自己，会报「不是 git 仓」）：

```bash
MT=~/XiaomiMiMoProjects/oagentkit-matrix/matrix-tools
ROOT=~/XiaomiMiMoProjects/oagentkit-matrix
python3 $MT/scripts/release.py --root $ROOT prewrite --repo <repo> --version vX.Y.Z  # ① 预写待发版段（只改 CHANGELOG，自动开分支）
python3 $MT/scripts/release.py --root $ROOT tag      --repo <repo> --version vX.Y.Z  # ② 提交 CHANGELOG → 打 annotated tag（同提交）
python3 $MT/scripts/release.py --root $ROOT sync-matrix                            # ③ 刷新矩阵源 versions.json → 自动拷站点副本（副本仓按「源先副本后」另行提交）
python3 $MT/scripts/release.py --root $ROOT doctor   --repo <repo> --version vX.Y.Z  # ⓪ 发版前体检（只读，随时可跑）
```

- prewrite 自动开分支 `chore/changelog-prewrite-<version>`；tag 步在**同一提交**上打 annotated tag
- tag 步带「地板时效」检查：那一刻依赖的 oekit/* 与本矩阵模块必须是最新已发布版本——
  落后即拦；`--skip-floor-check "理由"` 显式逃生（理由留痕，tag 不可重打）
- **本机 golangci-lint 必须与 CI 同版**（v2.14.0，go-ci.yml default 同源，
  oagentkit/matrix-tools#212）：v2.15 起 errcheck 检查 error 型 channel 接收、
  与 staticcheck S1005 互斥——本机 2.15 判红而 CI 2.14 判绿的假红卡发版。
  doctor 已实弹比对（装了但版本不符 ⇒ 红）；brew 装的 2.15 在 PATH 前面时，
  `go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0`
  装到 ~/go/bin 或 ~/.local/bin 并前置 PATH

## 两类历史事故（本流水线存在的理由）

1. **重生成吞待发版段**：`gen_changelogs.py` 无参重生成只按已存在 tag 切段，预写段会被删掉
   （实测 agent v0.10.1，距上次重生成 17 分钟，4 道门禁同时全绿没发现）——
   **prewrite 之后到 tag 之前，禁止跑 gen_changelogs.py 重生成**
2. **矩阵源没刷新**：真实 tag 已新、versions.json 的 latest 停在旧版——sync-matrix 是 tag 后
   必跑步，跳过 = 站点说谎且无门禁发现

## 三门禁（回执 / CHANGELOG / 欠账基线）

- **回执**：issue 关单回执按 CHANGELOG **版本节**定位本条修复——版本节必须点名 issue 号；
  Release 正文里的「合入记录」回执读不到
- **CHANGELOG**：tag 必须有对应版本节（tag 步前置检查）；锚文本形态各仓不统一，replace 后必须验证
- **欠账基线**：发版后 `sync-matrix` 未跑 = 欠账；多仓连发按 tier 从小到大（tier0 → tier3），
  基础层先发否则下游全卡

## 依赖地板

- bump 一条命令：`python3 scripts/bump_dep_floors.py --apply`（全矩阵追平）
- 新 tag 被 go proxy 缓存卡：`GOPRIVATE=github.com/oagentkit/*` 直连绕过（proxy 版本列表滞后）
- 脏 go.mod 要收尾：tidy 干净才算完

## 并发会话纪律（14 个 @mk worktree 车道常态并行）

- 动手前 `git ls-remote --tags origin` + `gh pr list` 查撞车；别人收编的仓让道
- **有子代理活跃的仓禁 `reset --hard`**；本地多车道用 worktree（`<repo>@mk` 形态）
- 版本号被抢发 ⇒ 改发下一版，绝不重打（tag ruleset 保护不可删改）
- 发版前后跑 `python3 scripts/selftest_all.py`（判据自测）确认工具链健康

## 门面仓特例

- agentkit 门面仓发版 = 别名同步（上游加导出符号要同步 alias.go + modules.json heavyDeps 登记）；
  新包**不进门面**只在 modules.json 登记 + llms.txt——门面已废弃，只做存量兼容
- 上游模块发版后门面跟进：跑 facade 相关校验（check_facade_imports.py）
