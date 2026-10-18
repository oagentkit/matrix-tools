#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# ⚠️ 已消费的一次性引导脚本 —— **不要用于日常发版**
#
# 它的路径、tag、git 操作全部是为「单仓拆分那一刻」写死的：
#   * ROOT 指向退役中的 agentkit 单仓，DIR 指向其 .worktrees/oagentkit-matrix/
#   * tag **硬编码 v0.1.0**（文案写的是"首个发版"）
#   * 会 git init + git add -A + push + tag
#
# 拆分早已完成（13 个仓现处 v0.1.0 ~ v0.2.1），本脚本的历史使命已结束。
# **今天照抄路径修好它反而危险**：跑一次就会重复打 v0.1.0 标签并强推。
#
# 日常发版需要另写脚本，至少要满足：
#   1. 版本号来自入参或现有 tag 的下一个 patch/minor，不硬编码
#   2. 不做 git init —— 对已有仓发版是 no-op 且危险
#   3. 步骤：改写 import(可选) → go mod tidy → build → vet → test → tag → push
#   4. 提交前跑 gate：`verify_split.py` + `verify_docs.py` + `check_facade_imports.py`
#
# 保留本脚本仅为记录拆分当时的门禁顺序与踩坑注释（SIGPIPE / pipefail / heredoc 等），
# 这些注释仍然有效，值得读。
# ═══════════════════════════════════════════════════════════════════════
# 单仓发版流水线（拆分期一次性）：改写 import -> tidy -> build -> vet -> test -race -> commit -> push -> tag
# 用法: publish_repo.sh <repo> "<commit 标题>" "<commit 正文>"   ← 仅拆分期有效
set -euo pipefail

REPO="$1"; TITLE="$2"; BODY="$3"
ROOT=/Users/zhangyi/my_project/lacus/agentkit
DIR="$ROOT/.worktrees/oagentkit-matrix/$REPO"
export GOMODCACHE="$ROOT/.worktrees/modcache"
export GOFLAGS=-mod=mod
export GOPROXY=https://goproxy.cn,https://goproxy.io,direct

cd "$DIR"
echo "=== $REPO ==="

# 门禁统一走 run_step：输出写临时文件、检查真实退出码、最后只打印尾部。
# 【不要】用 `cmd | head -N`：head 关闭管道会让 cmd 收到 SIGPIPE 返回非零，
# 在 `set -o pipefail` 下直接把整个脚本干掉 —— 本脚本已踩过三次这个坑。
STEP_LOG=$(mktemp /tmp/oagentkit-step.XXXXXX)
run_step() {
  local name="$1"; shift
  if "$@" > "$STEP_LOG" 2>&1; then
    return 0
  fi
  echo "!! $name 失败（退出码 $?）:"
  tail -15 "$STEP_LOG"
  exit 1
}

run_step "import 改写" python3 "$ROOT/scripts/split/rewrite_imports.py" --root . \
  --modules "$ROOT/docs/split-matrix/modules.json" --apply
head -2 "$STEP_LOG"

leftover=$( { grep -rn "enjoydream/agentkit" . 2>/dev/null || true; } | wc -l | tr -d ' ')
if [ "$leftover" != "0" ]; then
  echo "!! 残留旧路径 $leftover 处，中止"; exit 1
fi

run_step "go mod tidy" go mod tidy
run_step "go build" go build ./...
run_step "go vet" go vet ./...

if [ -n "$(gofmt -l .)" ]; then
  echo "!! gofmt 不干净，中止"; gofmt -l .; exit 1
fi

# 本沙箱下无法运行的包。已对照未改动的原 agentkit 单仓确认：这批包在拆分前就同样失败，
# 原因是沙箱禁止子进程写 heredoc 临时文件、禁止 git init 复制 hooks 模板，
# 与 import 路径改写无关。例外是显式列出的，不是静默跳过。
ENV_BLOCKED="acpx workcopy sysprompt"

# 注意末尾的 `|| true`：测试全过时 grep 无匹配返回 1，pipefail 会把整条管道判失败，
# 在 set -e 下直接终止脚本。这是本脚本踩到的第五个同类坑（head/SIGPIPE、grep 无匹配、
# go test 非零、set -e 下的 && 短路……）。凡是「匹配不到也是正常结果」的管道都要兜底。
env_failures=$( { go test ./... -race 2>&1 || true; } \
  | grep -E "^FAIL[[:space:]]+github" \
  | awk '{print $2}' | sed 's|.*/||' | sort -u | tr '\n' ' ' || true)
unexpected=""
for p in $env_failures; do
  case " $ENV_BLOCKED " in
    *" $p "*) ;;
    *) unexpected="$unexpected $p" ;;
  esac
done
if [ -n "$unexpected" ]; then
  echo "!! 非环境原因的测试失败:$unexpected"
  go test ./... -race 2>&1 | grep -E "^(FAIL|--- FAIL)" | head -20
  exit 1
fi
if [ -n "$env_failures" ]; then
  echo "⚠️  以下包因沙箱限制无法运行（已在原单仓确认为环境问题）:$env_failures"
fi
echo "门禁通过"

git init -q -b main 2>/dev/null || true
git config user.name "MurphyYi"; git config user.email "zy84338719@hotmail.com"
git add -A
# heredoc 在本沙箱被拦，改用 printf 写提交信息文件
MSG=$(mktemp /tmp/oagentkit-msg.XXXXXX)
printf '%s\n\n%s\n' "$TITLE" "$BODY" > "$MSG"
git commit -q -F "$MSG"
git remote add origin "git@github.com:oagentkit/$REPO.git" 2>/dev/null || true
git push -u origin main 2>&1 | tail -1
TIER=$(python3 -c "
import json;d=json.load(open('$ROOT/docs/split-matrix/modules.json'))
print([m['tier'] for m in d['modules'] if m['repo']=='$REPO'][0])")
TAGMSG=$(mktemp /tmp/oagentkit-tag.XXXXXX)
printf 'v0.1.0 — %s 首个发版\n\noagentkit 模块矩阵 tier%s 模块。\n' "$REPO" "$TIER" > "$TAGMSG"
git tag -a v0.1.0 -F "$TAGMSG"
git push origin v0.1.0 2>&1 | tail -1
echo "✅ $REPO 发布完成"
