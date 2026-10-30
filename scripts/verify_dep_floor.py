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

## 三个载体，只有一个被守（实测 2026-10-24）

「本仓有哪些重量级第三方依赖」这件事，在矩阵里有**三个载体**：

| 载体 | 怎么产生的 | 谁在守 |
|---|---|---|
| `versions.json` 的 `depFloor` | **自动**从各仓 go.mod 读（`gen_versions.py`） | ✅ 本脚本的 `reconcile_dep_floor` |
| `modules.json` 的 `heavyDeps` | **手写** | ❌ 此前无人守 |
| 各仓 README 的「外部依赖地板」小节 | **手写** | ❌ 此前无人守 |

⇒ 后两者会**静默漂移**。实测 5 个仓的 `modules.json.heavyDeps` 与 go.mod 对不上：
`obs` 仍登记着已随 `obsx` 迁走的 `eino`；`skillkit` / `tools` / `agent` **漏登记**；
`llm` 登记的是 `github.com/cloudwego/eino-ext` —— 这个模块路径**根本不在 llm 的模块图里**
（llm 直接依赖的是 `github.com/cloudwego/eino-ext/components/model/openai`，
根模块 `eino-ext` 连 go.sum 都没有）。

本脚本新增 `reconcile_heavy_deps` 守**第二个**载体。
第三个载体（README 小节）要解析散文、且涉及「测试依赖算不算外部依赖」这类判断
（`queue` 的 `lib/pq` 只在测试里用），留作另案 —— ���看 issue 里的取舍。

## 两种口径：`floor_of` 与 `third_party_of` 不是一回事

- `floor_of` = **消费方模块图继承到的东西**（direct + indirect，含 `oekit/logx`
  —— 它不在基座白名单里，因为 logx 带 hertz）
  ⇒ 门禁 `tier0/tier1` 与 `versions.json.depFloor` 用这个口径；
- `third_party_of` = **第三方直接依赖**（排除全部 `oekit/*` 第一方基座与 `oagentkit/*` 矩阵内仓）
  ⇒ `modules.json.heavyDeps` 用这个口径
  （基座是「第一方基座」，不是「外部依赖」——各仓 README 的「外部依赖地板」小节也是这么写的）。

**为什么一个全量、一个只收 direct**（2026-10-30 口径修正）：
两者回答的是**不同的问题**——
`floor_of` 答「**消费方** import 它会继承到什么」（Go 的依赖面按 module 结算，
indirect 同样进消费方模块图）⇒ 必须全量；
`third_party_of` 答「**本仓自己**主动拉了哪些重第三方」（用来判层位是否合理、
对账各仓 README 的「外部依赖地板」小节）⇒ 只看主动选择的那几条。

修正前 `floor_of` 只收 direct，而它自己的字段说明写着「整条 require 就进消费方模块图」
—— **说明与实现互相矛盾**。实测 toolgate 真实第三方依赖 36 个、字段只记 2 个，
而矩阵自己的 breakingIndex 写着「让消费方白吃 34 个第三方模块」
⇒ 同一个事实，矩阵说 34，机器可读字段说 2。修正后 tier0/tier1 仍全空，红线不受影响。

两个口径各有消费者 ⇒ 各写一份实现、各自写清 docstring；
**不要把一个口径悄悄套到另一个载体上**（那会把「登记口径」与「门禁口径」搅在一起，
到时连「红的是哪一条」都说不清）。

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
import matrix_facts  # noqa: E402  事实层：仓清单取数（与 verify_dep_floors 共用）
import repo_meta  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"
DEFAULT_VERSIONS = HERE.parent / "versions.json"

FIRST_PARTY_PREFIX = "github.com/oagentkit/"

# 第一方基座前缀（oekit 矩阵）。基座是「第一方基座」，**不是**「外部依赖」——
# 各仓 README 的「外部依赖地板」小节都不列它 ⇒ `third_party_of` 排除整棵前缀。
BASE_PREFIX = "github.com/oekit/"

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
    """重量级第三方依赖（地板清单）——本仓地板口径的唯一实现。

    排除第一方矩阵内仓（看层位，不看地板）与基座白名单；
    **direct 与 indirect 都收**。

    ## 为什么 indirect 也要收（2026-10-30 口径修正）

    Go 的依赖面按 **module** 结算：import 本仓任一包，本仓 go.mod 里的
    **整条 require** 都进消费方的模块图 —— 间接依赖同样进，
    消费方构建时也要下载、也要链接、也要吃它的传递依赖。
    所以「这个包会把多少重依赖拖进我的项目」这个问题，
    答案必须是**整条 require**，不是「我自己直接拉的那几条」。

    实测（修正前）：toolgate 的 go.mod 有 36 个第三方依赖（33 个是 indirect），
    而 `versions.json` 的 depFloor 只记 **2** 个 —— 下游拿到的信息少了 17 倍。
    同仓的 breakingIndex 早就写着「它在带 eino 家族的重仓里让消费方
    **白吃 34 个第三方模块**」，而机器可读的那份字段写的是 2 ⇒
    **同一个事实，矩阵说 34，字段说 2**。

    修正后各仓 tier0/tier1 仍是空的（实测 guardx/runtime/textx/promptx
    四个仓在新口径下都是 0 条）⇒ 地板红线不受影响，只是把「说了什么」
    改成「事实是什么」。

    gen_versions.py 写 versions.json 的 depFloor 也走这里——同一份实现，
    免得「生成器算一套、门禁算另一套」，两套各写一遍就会各漂各的。
    """
    floor: list[str] = []
    for dep, indirect in reqs:
        if dep.startswith(FIRST_PARTY_PREFIX):
            continue
        if dep in ALLOWED:
            continue
        floor.append(dep)
    return floor


def third_party_of(reqs: list[tuple[str, bool]]) -> list[str]:
    """各仓 go.mod 的**第三方直接依赖**（「外部依赖地板」口径）。

    与 `floor_of` 的区别（两个口径各有消费者，别互相套用）：

    - `floor_of` = 消费方模块图**继承到**的东西（含 indirect）⇒ 含 `oekit/logx`
      （它不在基座白名单里，因为 logx 带 hertz）⇒ 门禁 `tier0/tier1` 用它，
      `versions.json.depFloor` 也用它；
    - `third_party_of` = **第三方**（非第一方基座、非矩阵内仓）的**直接**依赖
      ⇒ `modules.json.heavyDeps` 用它 —— 基座是「第一方基座」，不是「外部依赖」。

    ## 为什么 heavyDeps 还是只收直接依赖（2026-10-30）

    heavyDeps 回答的是另一个问题：**「这个仓自己主动拉了哪些重第三方」**
    —— 用来判层位是否合理（tier2 才允许有）、以及各仓 README 的
    「外部依赖地板」小节要对账。它是**本仓的主动选择**，不是消费方的继承面。

    而 depFloor 回答的是**消费方**的问题：「我 import 它会继承到什么」。
    Go 的依赖面按 module 结算 ⇒ indirect 同样进消费方模块图 ⇒ 必须全量。

    ⇒ 两个口径都是对的，只是问的问题不同；**别互相套用**。
    """
    out: list[str] = []
    for dep, indirect in reqs:
        if dep.startswith(FIRST_PARTY_PREFIX) or dep.startswith(BASE_PREFIX):
            continue
        if not indirect:
            out.append(dep)
    return out


def check_module(repo: str, tier: int | None,
                 reqs: list[tuple[str, bool]]) -> tuple[list[str], list[str]]:
    """→ (问题列表, 地板清单[整条 require 的重量级第三方])

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


def recorded_heavy(data: dict, repo: str) -> tuple[bool, list[str] | None]:
    """→ (是不是矩阵模块, 该仓登记的 `heavyDeps`)

    **不是**矩阵模块（如门面 `agentkit`）⇒ `(False, None)` ⇒ **不参与这条对账** ——
    门面在 `modules.json` 的 `infraRepos` 里、不是 `modules[]` 的一条，
    本来就没有 `heavyDeps` 字段（它的地板由 `versions.json` 的 `facades[].depFloor` 记，
    那份已由 `reconcile_dep_floor` 守着）。

    是矩阵模块但**没有** `heavyDeps` 字段 ⇒ `(True, None)` ⇒ 判红（漏登记）。
    """
    for m in data.get("modules", []):
        if m.get("repo") == repo:
            hd = m.get("heavyDeps")
            return True, (list(hd) if isinstance(hd, list) else None)
    return False, None


def reconcile_heavy_deps(repo: str, actual: list[str],
                         recorded: list[str] | None) -> list[str]:
    """`modules.json` 登记的 `heavyDeps` vs go.mod 实际算出的第三方直接依赖。

    与 `reconcile_dep_floor` 同级：**矩阵登记的必须等于事实源算出来的**。
    漏登记 / 多登记 / 内容不一致 ⇒ 一律红。

    为什么这条必须存在：`modules.json.heavyDeps` 是**手写**字段，
    而同义的 `versions.json.depFloor` 是**自动生成**且已被对账 ——
    有人据此以为「地板这件事有门禁」，其实手写的那一份没人守。
    """
    if recorded is None:
        return [f"{repo}: modules.json 里没有这一仓的 heavyDeps —— "
                f"矩阵漏登记了？（该仓 go.mod 实测第三方直接依赖：{actual or '无'}）"]
    if set(recorded) != set(actual):
        missing = sorted(set(actual) - set(recorded))
        extra = sorted(set(recorded) - set(actual))
        return [f"{repo}: modules.json 的 heavyDeps 与 go.mod 不一致"
                f"（矩阵漏 {missing or '无'} / 多 {extra or '无'}）"
                f"—— 登记必须等于 go.mod 实测的第三方直接依赖"]
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
    #
    # ⚠️ 取数走**事实层**（matrix_facts.go_module_targets）——与
    # verify_dep_floors.py 共用同一份「哪些仓是 Go module」的判定，
    # 结构上就不可能两条守卫的覆盖面不一致（此前各写各的谓词：goModule vs
    # topics 含 deprecated/facade，改数据源一个字段就可能让某个仓从
    # 其中一条的视野里消失，而两条各自看起来都正常）。
    targets: list[tuple[str, int | None]] = matrix_facts.Matrix(
        path=Path(args.matrix), data=data).go_module_targets()

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
        reqs = requires(text)
        probs, floor = check_module(repo, tier, reqs)
        problems.extend(probs)
        report.append((repo, tier, floor))
        # 对账：矩阵登记的地板必须等于 go.mod 的实际地板
        problems.extend(reconcile_dep_floor(repo, floor, recorded_floor(versions, repo)))
        # 对账：**手写**的 heavyDeps 必须等于 go.mod 实测的第三方直接依赖
        # （只对矩阵模块做对账；门面在 infraRepos 里，本来就没有 heavyDeps 字段）
        is_module, recorded = recorded_heavy(data, repo)
        if is_module:
            problems.extend(reconcile_heavy_deps(repo, third_party_of(reqs), recorded))

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
        print("  4. modules.json 的 heavyDeps 与 go.mod 不一致 → 把 modules.json 里该仓的")
        print("     heavyDeps 改成 go.mod 实测的**第三方直接依赖**（排除 oekit 第一方基座、")
        print("     排除 oagentkit 矩阵内仓、排除 indirect）；它同时驱动 gen_docs.py 生成的")
        print("     README「外部依赖地板」小节 ⇒ 登记错了，生成出来的文档也是错的。")
        return 1

    print(f"✅ {checked} 个仓的依赖地板合规（tier0/tier1 无重量级第三方直接依赖），"
          f"且与 versions.json 的 depFloor、modules.json 的 heavyDeps 登记均一致")
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

    # ── 口径修正（2026-10-30）：indirect 必须进地板 ──
    #
    # 为什么这一条曾长期没有断言：修正前 floor_of 只收 direct，
    # 而自测样本里**每一个** indirect 标记都是 False ⇒
    # 「indirect 算不算地板」这个决定口径的分支，从来没被测过。
    # 于是口径与自己的字段说明（"整条 require 就进消费方模块图"）长期矛盾，
    # 实测 toolgate 的真实第三方依赖 36 个、对外字段只记 2 个。
    check("indirect 也进地板（口径修正的核心断言）",
          floor_of([("github.com/cloudwego/eino", True)]),
          ["github.com/cloudwego/eino"])
    check("direct + indirect 一起进地板",
          floor_of([("github.com/cloudwego/eino", False),
                    ("github.com/bytedance/sonic", True)]),
          ["github.com/cloudwego/eino", "github.com/bytedance/sonic"])
    # indirect 同样会触发 tier0 红线（它同样进消费方模块图）
    probs, floor = check_module("x", 0, [("github.com/cloudwego/eino", True)])
    check("tier0 带 indirect 的 eino 也被抓", len(probs), 1)
    check("tier0 indirect 的地板被记", floor, ["github.com/cloudwego/eino"])
    # 但 heavyDeps 口径（third_party_of）仍是「只收直接依赖」——两者别互相套用
    check("third_party 仍只收直接依赖（不随 floor_of 改口径）",
          third_party_of([("github.com/cloudwego/eino", False),
                          ("github.com/bytedance/sonic", True)]),
          ["github.com/cloudwego/eino"])

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

    # ── 第三种口径：第三方直接依赖（heavyDeps 用它）──
    # 与 floor_of 的区别：**排除全部 oekit 基座**（基座不是「外部依赖」），
    # 且排除矩阵内仓；indirect 同样不算。
    check("third_party 排除 oekit 基座",
          third_party_of([("github.com/oekit/logx", False),
                          ("github.com/oekit/pkg", False),
                          ("github.com/cloudwego/eino", False)]),
          ["github.com/cloudwego/eino"])
    check("third_party 排除矩阵内仓",
          third_party_of([("github.com/oagentkit/obs", False),
                          ("github.com/cloudwego/eino", False)]),
          ["github.com/cloudwego/eino"])
    check("third_party 排除 indirect",
          third_party_of([("github.com/cloudwego/eino", True),
                          ("github.com/cloudwego/eino-ext", False)]),
          ["github.com/cloudwego/eino-ext"])
    check("third_party 与 floor_of 口径不同",
          third_party_of([("github.com/oekit/logx", False)]), [])
    check("floor_of 收 logx 而 third_party 不收（两者口径确实不同）",
          floor_of([("github.com/oekit/logx", False)]),
          ["github.com/oekit/logx"])

    # ── 对账：modules.json 手写的 heavyDeps vs go.mod 实测 ──
    check("heavyDeps 对账一致",
          reconcile_heavy_deps("x", ["github.com/cloudwego/eino"],
                               ["github.com/cloudwego/eino"]), [])
    check("heavyDeps 对账空一致", reconcile_heavy_deps("x", [], []), [])
    check("heavyDeps 缺登记即红", len(reconcile_heavy_deps("x", [], None)), 1)
    check("heavyDeps 漏登记即红",
          len(reconcile_heavy_deps("x", ["github.com/cloudwego/eino"], [])), 1)
    check("heavyDeps 多登记即红",
          len(reconcile_heavy_deps("x", [], ["github.com/cloudwego/eino"])), 1)
    check("heavyDeps 路径漂移即红",
          len(reconcile_heavy_deps("x", ["github.com/cloudwego/eino-ext/components/model/openai"],
                                   ["github.com/cloudwego/eino-ext"])), 1)

    # recorded_heavy：从 modules.json 取登记。
    # ⚠️ 返回 (是不是矩阵模块, 登记值) —— 门面在 infraRepos 里、不是 modules[] 的一条 ⇒
    # 不参与对账（否则会被误报成「漏登记」）。
    md = {"modules": [{"repo": "a", "heavyDeps": ["x"]},
                      {"repo": "b", "heavyDeps": []},
                      {"repo": "c"}]}
    check("取 heavyDeps 登记", recorded_heavy(md, "a"), (True, ["x"]))
    check("取空的 heavyDeps 登记", recorded_heavy(md, "b"), (True, []))
    check("模块缺 heavyDeps 字段 → (True, None)", recorded_heavy(md, "c"), (True, None))
    check("非矩阵模块（如门面）→ (False, None)", recorded_heavy(md, "zzz"), (False, None))

    if fails:
        print("❌ verify_dep_floor 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_dep_floor 自测通过（tier0/1 红 / tier2 放行但记账 / logx 不在白名单 / "
          "depFloor 对账：漏登记·多登记·漂移即红 / "
          "third_party 口径排除基座·矩阵内仓·indirect / "
          "heavyDeps 对账：漏登记·多登记·路径漂移即红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
