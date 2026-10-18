#!/usr/bin/env python3
"""从 modules.json 生成各仓的 README.md 与 AGENTS.md。

为什么生成而不是手写：13 个仓的文档有 90% 是同构的（分层声明、硬规则、依赖表、
「不属于本仓」表），手写必然漂移。仓特有的纪律走 modules.json 的 agentNotes 字段。

⚠️ **本脚本只生成骨架，直接覆盖会抹掉手写内容。**
   生成之后有人在真实仓里补了大量手写段落——例如 queue/AGENTS.md 的
   「选主语义（正确性红线）」整节、textx/README.md 的包级细节。
   实测 13 仓 26 个文档里 10 个已与本脚本输出不一致，且**仓库版本才是更丰富的那个**。

   动手前先跑 `verify_repo_docs.py --mirror <各仓父目录>` 看漂移清单：
   - 差异方向是「生成物更新」→ 可以放心重新生成
   - 差异方向是「仓库更丰富」→ 只能把内容并回 modules.json，不能直接覆盖

   根因是生成范围与手写范围共用同一个文件、且没有分段标记。
   根治办法是把 agentNotes / pkgDesc 补全让生成器产出同样内容，
   或引入「生成区」标记把生成范围限定到骨架段落。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 生成区标记：只有这两个标记之间的内容由本脚本接管，标记之外的手写内容永不触碰。
# 这是「gen_docs.py 会抹掉工程纪律」这个地雷的根治手段——
# 在标记引入之前，脚本默认**拒绝**整文件覆盖，而不是默默把人手写的内容清空。
BEGIN = "<!-- oagentkit:generated:begin -->"
END = "<!-- oagentkit:generated:end -->"


def merge_into(target: Path, body: str, *, force: bool) -> bool:
    """把生成内容写进 target 的生成区，返回是否写入。

    - target 不存在          → 整体写入（带标记）
    - target 已有生成区标记  → 只替换标记之间的内容，手写区原样保留
    - target 存在但无标记    → 拒绝（除非 force），因为整文件覆盖会抹掉手写内容
    """
    if not target.is_file():
        target.write_text(f"{BEGIN}\n{body}\n{END}\n", encoding="utf-8")
        return True

    cur = target.read_text("utf-8")
    if BEGIN not in cur or END not in cur:
        if not force:
            return False
        target.write_text(f"{BEGIN}\n{body}\n{END}\n", encoding="utf-8")
        return True

    i, j = cur.index(BEGIN), cur.index(END)
    new = cur[:i] + f"{BEGIN}\n{body}\n{END}" + cur[j + len(END):]
    if new != cur:
        target.write_text(new, encoding="utf-8")
    return True


def default_matrix() -> Path:
    """定位 modules.json。

    脚本从退役中的 agentkit 单仓搬到 matrix-tools 时，这里曾按旧布局算成
    `<repo>/docs/split-matrix/modules.json`，导致脚本在新家直接 FileNotFoundError
    （连 --help 都崩，因为常量在 argparse 之前求值）。

    两个布局都认，避免再被搬一次就断：
      1) matrix-tools 根（本仓）
      2) agentkit 单仓旧布局 docs/split-matrix/
    """
    for cand in (HERE.parent / "modules.json",
                 HERE.parent.parent / "docs/split-matrix/modules.json"):
        if cand.is_file():
            return cand
    return HERE.parent / "modules.json"


TIER_ROLE = {
    0: "基础层，零仓内依赖或仅依赖同仓包",
    1: "能力层，依赖 tier0",
    2: "集成层，依赖 tier0/tier1 + 重第三方地板",
    3: "编排层，依赖 llm 等 tier2 门面",
}

COMMON_RULES = """### 分层与依赖

- 依赖严格自底向上，**禁止反向依赖**。本仓是 tier {tier}，只允许依赖 tier ≤ {tier} 的仓。
{extra_dep}
- 改 module 路径必须同步：全部 import、go.mod、README 安装行。

### 错误处理

- 一律使用 `github.com/oekit/pkg/errors`；业务错误用 BizError。
- 所有 error 禁止 `_` 忽略。

### 并发安全

- 禁止裸 `go func`：用 `github.com/oekit/concurrency/async` 的 GoSafe 系列（panic 被 recover）。
- 共享状态用 mutex / atomic 保护；包级全局可变状态用 `atomic.Pointer[T]`。

### API 设计

- 函数选项模式命名 `XxxOption`（禁止裸 `Option`，多包 co-import 会冲突）。
- 导出符号 PascalCase + godoc 注释；包文档放 `doc.go`。
"""


def dep_line(m: dict, all_mods: dict) -> str:
    deps = m.get("dependsOn", [])
    if not deps:
        return "- 本仓无矩阵内依赖，可被任意层引用。"
    return "- 本仓依赖的矩阵内仓：" + "、".join(
        f"`{d}`" for d in sorted(deps)
    ) + "。"


def build_readme(m: dict, all_mods: dict) -> str:
    repo = m["repo"]
    pkgs = m["packages"]
    ext = m.get("heavyDeps", [])
    rows = "\n".join(
        f"| `{p.rsplit('/', 1)[-1]}` | {m.get('pkgDesc', {}).get(p, '')} |" for p in pkgs
    )
    notes = m.get("agentNotes", "")
    return f"""# {repo}

{m['ghDescription']} —— oagentkit 模块矩阵 **tier {m['tier']}**（{TIER_ROLE[m['tier']]}）。

```
go get github.com/oagentkit/{repo}
```

| 包 | 职责 |
|---|---|
{rows}

## 定位

**tier {m['tier']}** — {TIER_ROLE[m['tier']]}。

{dep_line(m, all_mods)}

## 外部依赖地板

{chr(10).join(f'- `{d}`' for d in ext) if ext else '- 零外部依赖。'}

## 与 oekit 的关系

本矩阵（oagentkit，Agent/LLM 编排工具链）**依赖** oekit（Open Easy Kit，Go 微服务工具链）：
`oekit/pkg` / `oekit/concurrency` / `oekit/logx` 等。oekit 是另一套独立矩阵，不是子品牌。

## 开发

```bash
go test ./... -race
go vet ./...
gofmt -l .
```

规范见 [AGENTS.md](AGENTS.md)。

## License

MIT
{notes}"""


def build_agents(m: dict, all_mods: dict) -> str:
    repo = m["repo"]
    pkgs = "\n".join(f"`{p}/`" for p in m["packages"])
    logging_rule = (
        "- **本仓不引入日志依赖。** tier0 加日志等于抬地板；需要观测输出时返回 error 或交给调用方。"
        if not m.get("loggingRule")
        else m["loggingRule"]
    )
    return f"""# AGENTS.md — oagentkit/{repo}

> {m['ghDescription']}
> module：`github.com/oagentkit/{repo}` · org：[oagentkit = Open Agent Kit](https://github.com/oagentkit)
> 矩阵分层：**tier {m['tier']}**（{TIER_ROLE[m['tier']]}）

## 本仓包含

```
{pkgs}
```

## 硬规则

{COMMON_RULES.format(tier=m['tier'], extra_dep=dep_line(m, all_mods))}
### 日志

{logging_rule}

## 仓特有纪律

{m.get('agentNotes', '（无）')}

## 明确不属于本仓的

| 能力 | 归属 |
|---|---|
| 文本/差异/会话/报告 | `github.com/oagentkit/textx` |
| 事件总线/流控/子进程/热插拔/钩子 | `github.com/oagentkit/runtime` |
| 脱敏/出口围栏/权限/沙箱解析/审计 | `github.com/oagentkit/guardx` |
| DB 队列与选主 | `github.com/oagentkit/queue` |
| 通用 errors/stringx/encoding/mathx | `github.com/oekit/pkg` |
| goroutine 池/单飞/重试/令牌桶 | `github.com/oekit/concurrency` |
"""


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(default_matrix()),
                    help="modules.json 路径（默认自动定位）")
    ap.add_argument("--out-root", default="/Users/zhangyi/oagentkit",
                    help="各仓工作目录的父目录（生成 README.md / AGENTS.md 写到这里）")
    ap.add_argument("--dry-run", action="store_true", help="只报告将要写哪些文件")
    ap.add_argument("--force", action="store_true",
                    help="允许整文件覆盖无标记的文档（会抹掉手写内容，默认禁止）")
    ap.add_argument("--adopt", action="store_true",
                    help="给现有文档补上生成区标记：把当前内容整体当作手写区，"
                         "在末尾追加生成区（不丢内容；下一次起只更新生成区）")
    ap.add_argument("repos", nargs="*", help="只生成指定仓（默认全部）")
    args = ap.parse_args()

    data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    all_mods = {m["repo"]: m for m in data["modules"]}
    targets = args.repos or list(all_mods)
    out_root = Path(args.out_root)
    written = skipped = adopted = 0
    for repo in targets:
        m = all_mods.get(repo)
        if not m:
            print(f"矩阵里没有 {repo}", file=sys.stderr)
            return 1
        d = out_root / repo
        if not d.is_dir():
            print(f"跳过 {repo}: 工作目录不存在（{d}）")
            continue
        if args.dry_run:
            print(f"  [dry-run] 将生成 {repo}/README.md 与 AGENTS.md（当前是否存在："
                  f"README={'是' if (d / 'README.md').is_file() else '否'}, "
                  f"AGENTS={'是' if (d / 'AGENTS.md').is_file() else '否'}）")
            continue

        for fname, builder in (("README.md", build_readme), ("AGENTS.md", build_agents)):
            target = d / fname
            body = builder(m, all_mods)
            if args.adopt:
                if target.is_file() and BEGIN not in target.read_text("utf-8"):
                    target.write_text(
                        target.read_text("utf-8").rstrip() + "\n\n"
                        + f"{BEGIN}\n{body}\n{END}\n", encoding="utf-8")
                    adopted += 1
                    print(f"  已加生成区标记 {repo}/{fname}（原内容全部保留在手写区）")
                continue
            if not merge_into(target, body, force=args.force):
                skipped += 1
                print(f"  ⚠️  跳过 {repo}/{fname}：无生成区标记，"
                      f"整文件覆盖会抹掉手写内容（--force 强制覆盖 / --adopt 先补标记）")
                continue
            written += 1

    print(f"\n写入 {written} 个，跳过 {skipped} 个，补标记 {adopted} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
