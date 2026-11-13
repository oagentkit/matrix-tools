#!/usr/bin/env python3
"""渲染 oagentkit AI 入口 skill（skills/oagentkit）——同一份 modules.json 两种消费。

定位（与 oekit 的 gen_skill.py 同型，2026-11-12 移植）：skill 回答「我要做 X，
该用哪个 oagentkit 模块/子包、怎么组合、踩哪条红线」。因此：

  - 场景选型表（SCENARIOS）：**唯一手写新增知识**——跨模块选型判据
    （建 agent 的模块组合、工具契约选 invoke 还是 mcpkit、围栏三件套分工…）
  - 模块索引 / 子包速查：渲染自 modules.json（repo/tier/desc/packages/heavyDeps/pkgDesc）
  - 红线：oagentkit 没有全矩阵统一硬规则块（各仓 AGENTS.md per-repo agentNotes），
    红线速览按「跨仓契约 + 各仓 agentNotes 精华」手写，条条标注出处仓

oagentkit 特有的头号判据：**agentkit 门面已废弃**——新代码直接 import
github.com/oagentkit/<repo>/<pkg>，存量迁移才走门面别名。

用法：
  python3 scripts/gen_skill.py           # 渲染 skills/oagentkit/{SKILL.md,modules.md,pkgs.md}
  python3 scripts/gen_skill.py --check   # 只比对不写（漂移退出码 1）

渲染产物**勿手改**；修订入口：场景表改本文件数据区，模块事实改 modules.json。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
MT_ROOT = HERE.parent
SKILL_DIR = MT_ROOT / "skills" / "oagentkit"
MODULES_JSON = MT_ROOT / "modules.json"

# 入口冒烟守卫声明（verify_cli_smoke.py 真跑一次的参数）：--check 是只读正常调用
SMOKE_ARGS = ["--check"]


def load_modules() -> dict:
    return json.loads(MODULES_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 手写数据区（修订场景判据改这里；mods 只填 repo 名，selftest 按 modules.json 校验死键）
# ---------------------------------------------------------------------------

# 场景选型表：scene / use / tips / redline / mods（repo 名集合，校验 ∈ modules）
SCENARIOS: list[tuple[str, list[dict]]] = [
    ("Agent 应用骨架", [
        {"scene": "建 ReAct agent（运行样板/反思循环）",
         "use": "`agent`（agentrun/reflection）+ `runtime` + `llm`",
         "tips": "agentrun 是组装样板；反思循环 reflection；进程骨架（节控/事件总线/钩子）由 runtime 供",
         "redline": "panic 不能只转 error：`RunSafe*` 收进全局 PanicHandler 后返回的 `panic: X` 无哨兵类型——丢结构化上下文前先确认可接受（llm AGENTS）",
         "mods": ["agent", "runtime", "llm"]},
        {"scene": "意图路由 / 多专家协作",
         "use": "`agent` router/blackboard/dispatch/clarify",
         "tips": "router 意图路由、blackboard 多专家黑板、dispatch 派发守卫、clarify 澄清",
         "mods": ["agent"]},
        {"scene": "agent 进程运行时（节控/热插拔/子进程）",
         "use": "`runtime`（pacer/progress/procx/hotplug/hookx/notifyx）",
         "tips": "流式节控 pacer、泛型事件总线 hookx/notifyx、子进程环境 procx（配 guardx/egressproxy 强制出口）",
         "mods": ["runtime"]},
    ]),
    ("模型调用", [
        {"scene": "模型客户端 / 降级链 / 预算",
         "use": "`llm`（llm/llm/wrap）",
         "tips": "多后端 port 抽象 + 降级链 wrap；成本与预算面在根包",
         "redline": "降级链每一跳必须记录原因（成本/限流/超时/内容策略），不得静默换模型；预算判定必须在调用前——事后扣减等于没有预算（llm AGENTS）",
         "mods": ["llm"]},
        {"scene": "阶段路由 / 调用观测",
         "use": "`llm`（llm/decision、obsx）",
         "tips": "decision 按阶段选模型；obsx 调用观测（v0.12.0 自 obs/obsx 迁入）",
         "mods": ["llm"]},
        {"scene": "模型调用的测试替身",
         "use": "`llm`（llm/llmtest）",
         "tips": "单测不打真端点；契约与真实现同源",
         "mods": ["llm"]},
    ]),
    ("工具体系", [
        {"scene": "MCP 工具接入（客户端/工具池/建连单飞）",
         "use": "`mcpkit`（mcp）",
         "tips": "mcp-go 重依赖隔离在此仓",
         "redline": "mcpkit 的**信封包装是特性不是缺陷**——工具结果带元数据信封，别在调用方剥掉（跨仓集成契约）",
         "mods": ["mcpkit"]},
        {"scene": "工具优先级 / 并发调度 / 审计门",
         "use": "`toolgate`（toolprior/toolsched/policy）",
         "tips": "policy 是不可绕过调用链的裁定者；yaml 配置热更",
         "mods": ["toolgate"]},
        {"scene": "工具调用统一契约（框架无关）",
         "use": "`guardx`（invoke/toolfail）",
         "tips": "invoke 不携带 eino/policy 类型（可在任何层用）；toolfail 失败分类",
         "redline": "不可分类即不可重试；重发安全与重试价值是两个正交维度（toolfail 契约）",
         "mods": ["guardx"]},
        {"scene": "CLI 编码 agent 调用 / 公开检索 / git 沙箱 / SAST",
         "use": "`tools`（acpx/websearch/workcopy/sast/bashguard/bgjob）",
         "tips": "acpx 统一调用 CLI agent；bashguard Shell 静态裁决；bgjob 后台任务",
         "mods": ["tools"]},
    ]),
    ("安全围栏（guardx）", [
        {"scene": "提示词注入防护（数据侧越权）",
         "use": "`guardx/fence`",
         "tips": "中和 LLM 产出/用户输入中的 markdown 结构；系统侧伪造配 injectband",
         "redline": "围栏判定**单源在 oekit/pkg**（跨 org 契约）——guardx 消费单源，别在调用方重写判定（跨仓集成契约）",
         "mods": ["guardx"]},
        {"scene": "系统提示防伪（来源标记）",
         "use": "`guardx/injectband`",
         "tips": "系统带外注入打来源标记 + 转义 body 内伪造同形标签（含大小写/空白变体）；与 fence 互补",
         "mods": ["guardx"]},
        {"scene": "出口围栏 / 进程级出口强制",
         "use": "`guardx`（egress/egressproxy）",
         "tips": "egress 字面量层 URL 校验；egressproxy 只听回环的本地代理——子进程出站经目标准入才能出网（配 runtime/procx）",
         "mods": ["guardx"]},
        {"scene": "日志/审计凭据脱敏",
         "use": "`guardx/logredact`",
         "tips": "连接串/URL/头中凭据落盘前打码；规则顺序敏感",
         "redline": "规则秩序有契约（收窄/RawNamer 等）——改规则前读 guardx AGENTS，别按直觉排序",
         "mods": ["guardx"]},
        {"scene": "工具许可判定 / 沙箱档位",
         "use": "`guardx`（permgate/sandbox）",
         "tips": "permgate：谁在什么模式下对什么输入得到什么许可；sandbox 执行档位",
         "mods": ["guardx"]},
    ]),
    ("知识 · 任务 · 技能", [
        {"scene": "RAG（向量化/混合召回/重排）",
         "use": "`rag`（knowledge/rag）",
         "tips": "milvus SDK 重依赖隔离在此",
         "mods": ["rag"]},
        {"scene": "DB 即队列 / PG 选主",
         "use": "`queue`（worker/sqlqueue/pglease/sqllease）",
         "tips": "worker 消费骨架 + pglease Postgres Lease 主备单活",
         "mods": ["queue"]},
        {"scene": "system prompt 组装 / 运行中转向",
         "use": "`promptx`（sysprompt/steering/filestate）",
         "tips": "分段组装 sysprompt、运行中转向 steering、文件读后写一致性 filestate",
         "mods": ["promptx"]},
        {"scene": "SKILL.md 渐进披露 / 装配血缘",
         "use": "`skillkit`（skill/pack/lineage）",
         "tips": "skill 渐进披露、pack 领域包契约、lineage 装配血缘",
         "mods": ["skillkit"]},
    ]),
    ("观测 · 文本基础", [
        {"scene": "Langfuse 观测 / 审计留痕",
         "use": "`obs`（langfuse/audit）",
         "tips": "模型调用观测走 llm/obsx（已迁入 llm），obs 仓是客户端与审计",
         "mods": ["obs", "llm"]},
        {"scene": "文本/差异/代码索引/会话压缩/报告指纹",
         "use": "`textx`（textutil/diffx/codeintel/compact/conversation/reportutil）",
         "tips": "tier0 纯函数零依赖；compact 三轨 auto-compact 阈值不得写死在实现（有边界测试契约）",
         "mods": ["textx"]},
    ]),
]

# 红线速览（条条标出处；跨仓契约放最前）
REDLINES: list[str] = [
    "**直接 import `github.com/oagentkit/<repo>/<pkg>`——agentkit 门面已废弃**（存量迁移兼容层，只做别名转发；新代码进门面 = 给退役层续命）",
    "**import 前看 tier/heavyDeps**（modules.json）：tier0 零重量级三方；带 eino/milvus/mcp-go 的仓是 tier2——只要接口不要实现时别 import 实现仓",
    "围栏判定单源在 oekit/pkg（跨 org 契约）：guardx 消费单源，调用方不重写判定",
    "mcpkit 信封包装是特性：工具结果带元数据信封，别在调用方剥掉",
    "logredact 规则秩序有契约（收窄/RawNamer）：改规则前读 guardx AGENTS",
    "降级链每一跳记录原因，不得静默换模型；预算判定在调用前（llm）",
    "工具失败不可分类即不可重试（guardx/toolfail）",
    "panic 收编先确认丢上下文可接受：RunSafe* 无哨兵类型（llm）",
]

SKILL_DESCRIPTION = """\
在 Go 业务代码中使用/重构时选用 oagentkit 框架包的选型与用法入口（13 模块 + agentkit 废弃门面）。
触发场景：建 agent（ReAct/反思/路由/多专家）、模型调用（降级链/预算/阶段路由）、工具体系
（MCP/调度/审计门/调用契约）、安全围栏（提示词注入/出口 SSRF/脱敏/许可沙箱）、RAG、DB 队列、
prompt 组装、SKILL 技能装配、观测审计、文本处理，或不确定该用哪个模块/子包时。
含场景选型表、tier/heavyDeps 红线、48 子包速查。发版/tag/CHANGELOG 用 oagentkit-release；
issue 扫描复用 oekit-sweep（--org oagentkit）。"""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _tier_label(t: int) -> str:
    return {0: "tier0 基础层（零重量级三方）", 1: "tier1 能力层",
            2: "tier2 集成层（重第三方隔离在此）", 3: "tier3 编排层"}[t]


def _module_table() -> str:
    mods = load_modules()["modules"]
    out = ["| tier | 模块 | 一句话 | heavyDeps（import 即拖入） | 深入 |", "|---|---|---|---|---|"]
    for m in sorted(mods, key=lambda x: (x["tier"], x["repo"])):
        hd = "、".join(d.split("/")[-1] for d in m.get("heavyDeps", [])) or "—"
        out.append(f"| {_tier_label(m['tier']).split()[0]} | [`{m['repo']}`](https://github.com/oagentkit/{m['repo']}) "
                   f"| {m['desc']} | {hd} | `{m['repo']}/AGENTS.md` |")
    return "\n".join(out)


def _pkg_tables() -> str:
    mods = load_modules()["modules"]
    out = []
    for m in sorted(mods, key=lambda x: (x["tier"], x["repo"])):
        out.append(f"### {m['repo']}（tier{m['tier']}）\n")
        out.append("| 子包 | 用途 |")
        out.append("|---|---|")
        pd = m.get("pkgDesc", {})
        for p in m["packages"]:
            d = pd.get(p, "—")
            out.append(f"| `{p}` | {d} |")
        out.append("")
    return "\n".join(out)


def render_modules() -> str:
    return "\n".join([
        "# oagentkit 模块索引（tier 分层）",
        "",
        "> 由 gen_skill.py 从 modules.json 渲染——模块集合以 modules.json 为真相源，本文件勿手改。",
        "深入阅读 = 该仓 AGENTS.md（per-repo 硬规则 agentNotes）→ 包 doc.go。",
        "",
        "安装：按需 `go get github.com/oagentkit/<repo>/...@latest`；**别 import agentkit 门面**（已废弃）。",
        "",
        _module_table(),
        "",
    ])


def render_pkgs() -> str:
    return (
        "# oagentkit 子包速查（13 模块全包面）\n\n"
        "> 由 gen_skill.py 从 modules.json 的 packages/pkgDesc 渲染；API 以 `go doc github.com/oagentkit/<repo>/<pkg>` 为准。\n\n"
        f"{_pkg_tables()}"
    )


def render_skill() -> str:
    fm = (
        "---\n"
        "name: oagentkit\n"
        "description: >-\n"
        + "".join(f"  {ln}\n" for ln in SKILL_DESCRIPTION.splitlines())
        + "---\n\n"
    )
    sc = []
    for group, rows in SCENARIOS:
        sc.append(f"### {group}\n")
        sc.append("| 我要做什么 | 用什么 | 组合提示 | 红线 |")
        sc.append("|---|---|---|---|")
        for r in rows:
            sc.append(f"| {r['scene']} | {r['use']} | {r.get('tips', '—')} | {r.get('redline', '—')} |")
        sc.append("")
    body = f"""# oagentkit 包选型与用法入口

> oagentkit = Open Agent Kit：13 模块（tier0-3 自底向上）+ 已废弃 agentkit 门面。
> 本 skill 由 `matrix-tools/scripts/gen_skill.py` 渲染——场景表为手写数据、模块/子包索引为
> modules.json 渲染产物；修订改 gen_skill.py 数据区（或 modules.json），勿手改本文件。

## 使用流（四步）

1. **选型**：场景表定位模块/子包；先看 tier 与 heavyDeps（import 即拖依赖面）
2. **拿准签名**：`go doc github.com/oagentkit/<repo>/<pkg>` 或读包 doc.go——禁止凭记忆猜 API
3. **套红线**：下方速览；per-repo 硬规则在该仓 AGENTS.md（agentNotes 的单源在 modules.json）
4. **深入**：同目录 `modules.md`（tier 索引）/ `pkgs.md`（全包速查）→ 该仓 AGENTS.md

## 场景选型表

{chr(10).join(sc)}
## 红线速览（出处标注；per-repo 全量在该仓 AGENTS.md）

{chr(10).join(f"- {r}" for r in REDLINES)}

## 流程任务（另装的兄弟 skill）

- 发版 / tag / CHANGELOG / 矩阵同步 → **oagentkit-release**（release.py prewrite→tag→sync-matrix 流水线）
- issue 扫描 / 清零 → **oekit-sweep**（工具通用：`issue_sweep.py --org oagentkit`）

## 深入入口

- 模块索引：同目录 `modules.md` · 子包速查：同目录 `pkgs.md`
- 下游/远端 AI：<https://oagentkit.github.io/llms.txt>
- 门面迁移：agentkit 仓 MIGRATION.md（旧 import → 新前缀映射）
"""
    return fm + body


# ---------------------------------------------------------------------------
# 写盘 / 校验
# ---------------------------------------------------------------------------

def write_all() -> list[str]:
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in (("SKILL.md", render_skill()), ("modules.md", render_modules()),
                          ("pkgs.md", render_pkgs())):
        (SKILL_DIR / name).write_text(content, encoding="utf-8")
        written.append(str(SKILL_DIR / name))
    return written


def check_drift() -> list[str]:
    drift = []
    expected = {"SKILL.md": render_skill(), "modules.md": render_modules(), "pkgs.md": render_pkgs()}
    for name, exp in expected.items():
        p = SKILL_DIR / name
        if not p.exists():
            drift.append(f"skills/oagentkit/{name} 缺失（跑 python3 scripts/gen_skill.py）")
        elif p.read_text(encoding="utf-8") != exp:
            drift.append(f"skills/oagentkit/{name} 与 gen_skill.py 渲染不一致（重跑生成器）")
    return drift


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        raise SystemExit(0)
    if "--check" in argv:
        d = check_drift()
        for line in d:
            print(f"❌ {line}")
        raise SystemExit(1 if d else 0)
    for p in write_all():
        print(f"✓ {p}")
