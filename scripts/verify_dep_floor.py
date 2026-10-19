#!/usr/bin/env python3
"""依赖地板门禁：**tier0/tier1 的仓不得有重量级第三方依赖**。

## 为什么要有这条

Go 的依赖面按 **module** 结算：消费方 import 某仓的**任意一个包**，都会把该仓的
整条 `require` 拉进自己的模块图。于是「一个仓里既有重依赖包、又有轻包」=
**轻包被动白吃重依赖**。

实测（2026-10-19，本矩阵）：

| import 的包 | 包自身的 import 面 | 消费方模块图里多出来的第三方模块 |
|---|---|---|
| `toolgate/invoke` | 只有 `oekit/pkg/errors` | **34 个**（eino + sonic + mcp-go 全家） |
| `obs/langfuse` | 只有 `oekit/pkg/httpjson` | **27 个**（eino + sonic） |
| `guardx/logredact` | 纯 stdlib | **0 个** |

第三行是这个门禁想守住的状态：轻包自己干净，被所在仓的地板拖累的那两行不干净。

## 判据

1. **tier ≤ 1 的仓**：直接 require 只能是
   - 第一方矩阵内仓（`github.com/oagentkit/*`），且
   - 基座白名单：`github.com/oekit/pkg`、`github.com/oekit/concurrency`

   出现其他任何依赖 → **红**。

   基座白名单为什么只有这两个：`oekit/pkg` 与 `oekit/concurrency` 是纯 stdlib 邻接；
   而 `oekit/logx` 会带 `oekit/core` → `hertz`（HTTP 框架），它属于重量级，
   要引得进白名单并写明理由。

2. **tier ≥ 2 的仓**：允许重地板（那是它们的定位：集成层 = 重第三方地板），
   但地板清单会写进 `versions.json` 的 `depFloor` 字段与 llms.txt，消费者可据此判断。

## 豁免

`tier ≤ 1` 确需某个第三方时，把它加进 `ALLOWED` 并**写明 reason** ——
豁免必须显式列举，不豁免就会红。隐式放行等于没有门禁。

用法:
    verify_dep_floor.py --root /path/to/oagentkit   # 读本地各仓 go.mod
    verify_dep_floor.py --org oagentkit             # 联网读各仓 go.mod
    verify_dep_floor.py --selftest                  # 离线自测

退出码: 0 = 合规；1 = 有越层地板 / 取不到（判不了即红）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"

FIRST_PARTY_PREFIX = "github.com/oagentkit/"

# 基座白名单：纯 stdlib 邻接的 oekit 仓。
# ⚠️ `github.com/oekit/logx` **不在**这里：它带 `oekit/core` → `hertz`（HTTP 框架），
# 属于重量级依赖。要引它必须显式登记并写明理由。
ALLOWED: dict[str, str] = {
    "github.com/oekit/pkg": "纯函数基座（字符串/编码/数学），零第三方传递依赖",
    "github.com/oekit/concurrency": "并发原语（async/singleflight/breaker），零第三方传递依赖",
}

# tier ≥ 2 允许的重量级依赖 —— 它们的定位就是集成层。
HEAVY_OK_AT_TIER2 = True

_REQUIRE_RE = re.compile(r"^\s*([\w.\-]+(?:\.[\w\-]+)*/[\w.\-/]+)\s+(v[\w.\-+]+)")


def requires(gomod_text: str) -> list[tuple[str, bool]]:
    """go.mod 文本 → [(module path, 是否 indirect)]（去重、保序）。

    只认 require 块内的行；单行 `require x v1` 也认。注释行与 module 行不含
    module path，不会误命中。

    **indirect 也要收**：它表示这个模块确实在仓的模块图里（传递依赖被写进了
    go.mod），地板判定不能只看见「自己挑的那几条」。
    """
    out: list[tuple[str, bool]] = []
    in_block = False
    for raw in gomod_text.splitlines():
        body = raw.split("//", 1)[0].strip()
        indirect = "// indirect" in raw
        if not body:
            continue
        if body == "require (":
            in_block = True
            continue
        if in_block and body == ")":
            in_block = False
            continue
        if body.startswith("require "):
            body = body[len("require "):]
        elif not in_block:
            continue
        m = _REQUIRE_RE.match(body)
        if m and not any(p == m.group(1) for p, _ in out):
            out.append((m.group(1), indirect))
    return out


def check_module(repo: str, tier: int, reqs: list[tuple[str, bool]]) -> tuple[list[str], list[str]]:
    """→ (问题列表, 地板清单[仅直接依赖])"""
    problems: list[str] = []
    floor: list[str] = []
    for dep, indirect in reqs:
        if dep.startswith(FIRST_PARTY_PREFIX):
            continue                      # 第一方矩阵内仓：看层位，不看地板
        if dep in ALLOWED:
            continue                      # 基座白名单
        if not indirect:
            floor.append(dep)
        if tier <= 1:
            problems.append(
                f"{repo}(tier{tier}) 直接依赖重量级第三方 `{dep}` —— "
                f"tier0/tier1 的地板只能是 {sorted(ALLOWED)}（零依赖核心会白吃它）")
        elif not HEAVY_OK_AT_TIER2:
            problems.append(f"{repo}(tier{tier}) 依赖 `{dep}`")
    return problems, floor


def main() -> int:
    ap = argparse.ArgumentParser(description="tier0/tier1 的依赖地板门禁")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--root", help="各仓本地目录的父目录")
    ap.add_argument("--org", help="联网读各仓 go.mod")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.org:
        print("❌ 必须给 --root 或 --org")
        return 1

    data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = args.org or data.get("defaultOrg")

    problems: list[str] = []
    checked = 0
    report: list[tuple[str, int, list[str]]] = []
    for m in data["modules"]:
        repo = m["repo"]
        if args.root:
            p = Path(args.root) / repo / "go.mod"
            text = p.read_text(encoding="utf-8") if p.is_file() else None
        else:
            text = repo_meta.fetch_file(org, repo, "go.mod")
        if text is None:
            problems.append(f"{repo}: 取不到 go.mod（判不了，不能当成「没有地板」）")
            continue
        checked += 1
        probs, floor = check_module(repo, m["tier"], requires(text))
        problems.extend(probs)
        report.append((repo, m["tier"], floor))

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    print("各仓的重量级第三方地板：")
    for repo, tier, floor in sorted(report, key=lambda x: (x[1], x[0])):
        print(f"  tier{tier} {repo:<11} {', '.join(floor) if floor else '—（无）'}")
    print()

    if problems:
        print(f"❌ 依赖地板检查失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：")
        print("  1. 该依赖不该在这里 → 把它所在的重包迁到带该依赖的重仓（tier2 起），")
        print("     或让轻包迁到零地板的仓；")
        print("  2. 该依赖确实该在这里且是轻量的 → 登记进 scripts/verify_dep_floor.py 的")
        print("     ALLOWED 白名单并写明 reason（豁免必须显式列举）。")
        return 1

    print(f"✅ {checked} 个仓的依赖地板合规（tier0/tier1 无重量级第三方直接依赖）")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # require 解析
    gomod = """module github.com/oagentkit/demo

go 1.26.0

require (
\tgithub.com/oagentkit/textx v0.6.0
\tgithub.com/oekit/pkg v0.15.1 // indirect
\tgithub.com/cloudwego/eino v0.9.21
)

require github.com/oekit/concurrency v0.17.1
"""
    check("解析 require", [p for p, _ in requires(gomod)],
          ["github.com/oagentkit/textx", "github.com/oekit/pkg",
           "github.com/cloudwego/eino", "github.com/oekit/concurrency"])
    check("indirect 标记", dict(requires(gomod))["github.com/oekit/pkg"], True)
    check("module 行不算 require", requires("module github.com/oagentkit/x\n"), [])

    # tier0：只允许第一方 + 基座白名单
    probs, floor = check_module("x", 0, [("github.com/oagentkit/textx", False), ("github.com/oekit/pkg", False)])
    check("tier0 干净", probs, [])
    check("tier0 地板为空", floor, [])
    probs, floor = check_module("x", 0, [("github.com/cloudwego/eino", False)])
    check("tier0 带 eino 被抓", len(probs), 1)
    check("tier0 地板被记", floor, ["github.com/cloudwego/eino"])
    # logx 不在白名单 → tier0 里也要红（它带 hertz）
    probs, _ = check_module("x", 0, [("github.com/oekit/logx", False)])
    check("tier0 带 logx 被抓", len(probs), 1)

    # tier2：允许重地板，但地板要记进报告
    probs, floor = check_module("x", 2, [("github.com/cloudwego/eino", False), ("github.com/oekit/pkg", False)])
    check("tier2 允许重地板", probs, [])
    check("tier2 地板被记", floor, ["github.com/cloudwego/eino"])

    if fails:
        print("❌ verify_dep_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_dep_floor 自测通过（tier0/1 红 / tier2 放行但记账 / logx 不在白名单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
