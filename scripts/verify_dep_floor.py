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
DEFAULT_VERSIONS = HERE.parent / "versions.json"

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


def floor_of(reqs: list[tuple[str, bool]]) -> list[str]:
    """重量级第三方**直接**依赖（地板清单）——本仓地板口径的唯一实现。

    排除第一方矩阵内仓（看层位，不看地板）与基座白名单；只收直接依赖，
    indirect 不算（间接依赖是传递进来的，不是本仓自己拉的）。

    gen_versions.py 写 versions.json 的 depFloor 也走这里——同一份实现，
    免得「生成器算一套、门禁算另一套」，两套各写一遍就会各漂各的。
    """
    floor: list[str] = []
    for dep, indirect in reqs:
        if dep.startswith(FIRST_PARTY_PREFIX):
            continue
        if dep in ALLOWED:
            continue
        if not indirect:
            floor.append(dep)
    return floor


def check_module(repo: str, tier: int | None,
                 reqs: list[tuple[str, bool]]) -> tuple[list[str], list[str]]:
    """→ (问题列表, 地板清单[仅直接依赖])

    tier=None 表示「不按层位判红，只记账」——门面仓（agentkit）没有层位概念，
    它只作为地板的**事实陈述**被记录与对账。
    """
    problems: list[str] = []
    floor = floor_of(reqs)
    if tier is not None and tier <= 1:
        for dep, _ in reqs:
            if dep.startswith(FIRST_PARTY_PREFIX) or dep in ALLOWED:
                continue
            problems.append(
                f"{repo}(tier{tier}) 依赖重量级第三方 `{dep}` —— "
                f"tier0/tier1 的地板只能是 {sorted(ALLOWED)}（零依赖核心会白吃它）")
    elif tier is not None and not HEAVY_OK_AT_TIER2:
        for dep, _ in reqs:
            if dep.startswith(FIRST_PARTY_PREFIX) or dep in ALLOWED:
                continue
            problems.append(f"{repo}(tier{tier}) 依赖 `{dep}`")
    return problems, floor


def reconcile_dep_floor(repo: str, actual: list[str], recorded: list[str] | None) -> list[str]:
    """versions.json 登记的 depFloor vs go.mod 实际算出的地板。

    与 verify_breaking_index.py 同级：矩阵说的地板必须等于事实源的地板。
    漏登记 / 多登记 / 内容不一致 / 仓整个没登记 → 一律红。
    """
    if recorded is None:
        return [f"{repo}: versions.json 里没有这个仓的 depFloor 登记 —— 矩阵漏了这个仓？"]
    if set(recorded) != set(actual):
        missing = sorted(set(actual) - set(recorded))
        extra = sorted(set(recorded) - set(actual))
        return [f"{repo}: versions.json 的 depFloor 与 go.mod 不一致"
                f"（矩阵缺 {missing or '无'} / 多 {extra or '无'}）—— 重跑 gen_versions.py"]
    return []


def recorded_floor(versions: dict, repo: str) -> list[str] | None:
    """versions.json 里某仓登记的 depFloor；没登记返回 None（≠ 空列表）。"""
    for m in versions.get("modules", []):
        if m["repo"] == repo:
            return m.get("depFloor")
    for f in versions.get("facades", []):
        if f["repo"] == repo:
            return f.get("depFloor")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="tier0/tier1 的依赖地板门禁 + 地板对账")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--versions", default=str(DEFAULT_VERSIONS),
                    help="矩阵登记的 depFloor（对账时比对它与各仓 go.mod 的实际地板）")
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

    # 要检查的仓：矩阵模块（有层位）+ 门面仓（无层位，只记账与对账）。
    # 门面仓也要进对账：它的 depFloor 同样写在 versions.json 里，同样不能是
    # 「没人守的字段」。
    targets: list[tuple[str, int | None]] = [(m["repo"], m["tier"]) for m in data["modules"]]
    for r in data.get("infraRepos", []):
        if r.get("goModule"):
            targets.append((r["repo"], None))

    versions: dict = {}
    vpath = Path(args.versions)
    if not vpath.is_file():
        print(f"❌ 找不到 versions.json: {vpath}")
        return 1
    versions = json.loads(vpath.read_text(encoding="utf-8"))

    problems: list[str] = []
    checked = 0
    report: list[tuple[str, int | None, list[str]]] = []
    for repo, tier in targets:
        if args.root:
            p = Path(args.root) / repo / "go.mod"
            text = p.read_text(encoding="utf-8") if p.is_file() else None
        else:
            text = repo_meta.fetch_file(org, repo, "go.mod")
        if text is None:
            problems.append(f"{repo}: 取不到 go.mod（判不了，不能当成「没有地板」）")
            continue
        checked += 1
        probs, floor = check_module(repo, tier, requires(text))
        problems.extend(probs)
        report.append((repo, tier, floor))
        # 对账：矩阵登记的地板必须等于 go.mod 的实际地板
        problems.extend(reconcile_dep_floor(repo, floor, recorded_floor(versions, repo)))

    if checked == 0:
        print("❌ 一个仓都没检查到（拒绝判定：避免「全绿但什么都没查」）")
        return 1

    print("各仓的重量级第三方地板：")
    for repo, tier, floor in sorted(report, key=lambda x: (x[1] if x[1] is not None else 99, x[0])):
        tier_txt = f"tier{tier}" if tier is not None else "门面"
        print(f"  {tier_txt:<7} {repo:<11} {', '.join(floor) if floor else '—（无）'}")
    print()

    if problems:
        print(f"❌ 依赖地板检查失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print("\n修法：")
        print("  1. 该依赖不该在这里 → 把它所在的重包迁到带该依赖的重仓（tier2 起），")
        print("     或让轻包迁到零地板的仓；")
        print("  2. 该依赖确实该在这里且是轻量的 → 登记进 scripts/verify_dep_floor.py 的")
        print("     ALLOWED 白名单并写明 reason（豁免必须显式列举）；")
        print("  3. 矩阵登记与实际不一致 → 跑 gen_versions.py 重生成 versions.json，")
        print("     并把 matrix-tools/versions.json 与站点 docs/public/versions.json 一起提交。")
        return 1

    print(f"✅ {checked} 个仓的依赖地板合规（tier0/tier1 无重量级第三方直接依赖），"
          f"且与 versions.json 登记一致")
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

    # tier=None（门面仓）：不按层位判红，但地板照记
    probs, floor = check_module("facade", None, [("github.com/cloudwego/eino", False),
                                                 ("github.com/oagentkit/obs", False)])
    check("门面不判层位", probs, [])
    check("门面地板照记", floor, ["github.com/cloudwego/eino"])

    # 地板口径唯一实现：gen_versions.py 也走它（免得两份各算各的）
    check("floor_of 与 check_module 同口径", floor_of([("github.com/cloudwego/eino", False),
                                                       ("github.com/oekit/pkg", False),
                                                       ("github.com/oagentkit/obs", False)]),
          ["github.com/cloudwego/eino"])

    # ── 对账：矩阵登记的 depFloor vs 实际地板 ──
    check("对账一致", reconcile_dep_floor("x", ["github.com/cloudwego/eino"],
                                          ["github.com/cloudwego/eino"]), [])
    check("对账空地板一致", reconcile_dep_floor("x", [], []), [])
    check("对账缺登记即红", len(reconcile_dep_floor("x", [], None)), 1)
    check("对账漏登记即红", len(reconcile_dep_floor("x", ["github.com/cloudwego/eino"], [])), 1)
    check("对账多登记即红", len(reconcile_dep_floor("x", [], ["github.com/cloudwego/eino"])), 1)
    check("对账内容漂移即红",
          len(reconcile_dep_floor("x", ["github.com/cloudwego/eino"], ["github.com/oekit/logx"])), 1)

    # recorded_floor：从 modules / facades 两处都能取到，取不到返回 None（≠ 空列表）
    v = {"modules": [{"repo": "a", "depFloor": ["x"]}],
         "facades": [{"repo": "b", "depFloor": []}]}
    check("取 modules 的登记", recorded_floor(v, "a"), ["x"])
    check("取 facades 的登记", recorded_floor(v, "b"), [])
    check("没登记返回 None", recorded_floor(v, "zzz"), None)

    if fails:
        print("❌ verify_dep_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_dep_floor 自测通过（tier0/1 红 / tier2 放行但记账 / logx 不在白名单 / "
          "depFloor 对账：漏登记·多登记·漂移即红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
