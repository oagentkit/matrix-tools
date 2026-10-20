#!/usr/bin/env python3
"""门面仓的 re-export 覆盖率校验。

## 为什么要有这条守卫

门面仓 `oagentkit/agentkit` 把各模块仓的包做别名转发，让下游**只需改 import 前缀、
无需改调用点**。但它**没有任何自动检查**说「矩阵新增了一个包，门面有没有跟上」——
实测 2026-10-19：矩阵已 47 个包，门面只有 45 个，缺 `toolgate/invoke` 与
`guardx/injectband`。后果不是编译失败，而是**走门面的下游根本够不到这两个包**：
`go get github.com/oagentkit/agentkit/invoke` 直接 404。

这与 `verify_ghost_packages.py` 是**反方向**的缺口：
  - verify_ghost_packages：仓里有、矩阵没登记 → 矩阵外的幽灵包
  - 本脚本：矩阵里有、门面没转发 → 门面上的覆盖空洞
两者都缺，就是「矩阵不完整」在门面侧的表现。

## 门面已废弃，但这条守卫必须继续跑

门面仓 `oagentkit/agentkit` 的 README 明写「本仓已废弃：只保留兼容转发，
不是开发目标，新代码不要引它」，v1.x 退役条件**已满足**。
本守卫**不因此停跑**：它保护的是**存量下游**——已经按门面 import 的消费方
依赖门面把既有包**完整**转发过去。门面缺一个包（存量下游编译不过），
或多一个陈旧转发（把已退役/已改名的 API 又引回来），都是真实的破坏。

废弃说的是「不再接受新功能」，不是「已转发的兼容面可以烂掉」。
门面的「不得再长出新包」由另一条守卫守：`verify_facade_frozen.py`（冻结基线快照）。

## 判据

1. **矩阵的每个包，门面都要有对应目录**（含 alias.go）
2. **门面的每个目录，矩阵里都要有**（否则是陈旧转发，指向已退役/已改名的包）
3. **每个 alias.go 的 upstream import 路径，其模块段必须在矩阵的仓集合里**
   （转发到一个矩阵外的仓，等于把幽灵包又引回来）
4. **门面的包数 == 矩阵的包数**（交叉校验，防止前三条被绕过）

包名取矩阵 `packages` 条目的**末段**（`knowledge/rag` → `rag`、`llm/llmtest` →
`llmtest`、`worker/pglease` → `pglease`）。因为拆分后仓根也可能是包
（`github.com/oagentkit/rag` 本身就是包），从矩阵条目推不出真实 import 路径，
而门面目录名就是包名——按名字集合比对才是可靠的。

## 退役豁免

包退役时（例如 `breaker` → `oekit/concurrency/breaker`）把它加进 `RETIRED`，
门面上对应的目录也应一并删掉。豁免是**显式列举**的，不是「不在矩阵里就算过」——
否则「忘了从门面删」和「忘了从门面加」会长得一模一样。

用法:
    verify_facade_packages.py --facade <门面仓路径>
    verify_facade_packages.py --facade <路径> --json
    verify_facade_packages.py --selftest

退出码: 0 = 覆盖完整；1 = 有缺口或陈旧转发；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 已退役、矩阵里不再有、门面上也不该有的包。
# 形如 {模块段: {包名, ...}}；模块段用于校验「曾属哪个仓」。
RETIRED: dict[str, set[str]] = {
    "runtime": {"breaker"},
}

_ALIAS_IMPORT_RE = re.compile(r'import\s+upstream\s+"(?P<path>[^"]+)"')
_IMPORT_PATH_RE = re.compile(
    r"^github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)(?P<rest>/.*)?$"
)
# MIGRATION.md 的映射行：| `旧包` | `新模块` | `新 import 路径` |
# 退役行的第二列是 **已退役**（不是反引号包名），所以第二列用宽松匹配。
_MIGRATION_ROW_RE = re.compile(
    r"^\|\s*`(?P<old>[a-z0-9/]+)`\s*\|\s*(?P<mid>[^|]+?)\s*\|\s*`(?P<new>[^`]+)`\s*\|",
    re.M,
)
_RETIRED_MARK = "已退役"


def matrix_packages(matrix_path: Path) -> dict[str, set[str]]:
    """modules.json → {仓名: {包名, ...}}（包名取 entries 末段）。"""
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for m in data.get("modules", []):
        repo = m["repo"]
        names = out.setdefault(repo, set())
        for entry in m.get("packages", []):
            names.add(entry.rstrip("/").split("/")[-1])
    return out


def facade_packages(facade_path: Path) -> dict[str, str]:
    """门面仓 → {包名: upstream import 路径}（只认含 alias.go 的目录）。"""
    found: dict[str, str] = {}
    for alias in sorted(facade_path.glob("*/alias.go")):
        text = alias.read_text(encoding="utf-8")
        m = _ALIAS_IMPORT_RE.search(text)
        if not m:
            continue
        found[alias.parent.name] = m.group("path")
    return found


def migration_map(facade_path: Path) -> dict[str, dict]:
    """MIGRATION.md 的映射表 → {包名(末段): {module, path, retired, old}}。

    ## 为什么按**末段**归集

    旧单仓里包路径是嵌套的（`llm/llmtest`、`worker/pglease`、`knowledge/rag`），
    而门面仓的**目录名是末段**（`llmtest/`、`pglease/`、`rag/`）——
    拆分时的 import 改写规则就是「包名一律保留，只加模块前缀」。
    所以比对必须按末段，��按整条旧路径。

    ## 为什么要有这个解析

    迁移映射表是**下游唯一的迁移指引**（「照表逐包替换」）。
    实测 2026-10-21：门面的 `port` 包（转发 llm/port 的 51 个符号）
    早就存在，MIGRATION.md 却**漏了这一行**——下游照表逐包替换会漏掉它，
    而 llm 是 tier2 重依赖仓，漏掉等于把重依赖拉回来。

    而当时**没有任何门禁**读 MIGRATION.md：
    `verify_facade_packages.py` 只比目录与 modules.json，`verify_facade_symbols.py`
    只比符号。于是这行漏了多久就错了多久。
    """
    out: dict[str, dict] = {}
    mig = facade_path / "MIGRATION.md"
    if not mig.is_file():
        return out
    text = mig.read_text(encoding="utf-8")
    for m in _MIGRATION_ROW_RE.finditer(text):
        mid = m.group("mid").strip().strip("`").strip()
        old = m.group("old")
        out[old.split("/")[-1]] = {
            "module": mid,
            "path": m.group("new"),
            "retired": _RETIRED_MARK in m.group("mid"),
            "old": old,
        }
    return out


def check_migration_map(facade: dict[str, str], migration: dict[str, dict]) -> list[dict]:
    """迁移映射表 ↔ 门面实际转发的一致性。返回问题列表。

    三条判据：
    1. 门面的每个包，MIGRATION.md 里都要有迁移行（否则下游不知道往哪迁）
    2. MIGRATION.md 的每个**活包**行，门面上都要有对应转发（否则是空指引）
    3. 行的目标 import 路径要与 alias.go 的 upstream 一致（否则表与实现漂移）
    """
    problems: list[dict] = []
    if not migration:
        problems.append({"kind": "no-map", "package": "-", "repo": "-",
                         "detail": "门面仓没有 MIGRATION.md，或表里一行映射都解析不到"})
        return problems

    for name, path in sorted(facade.items()):
        row = migration.get(name)
        if row is None:
            problems.append({"kind": "map-missing", "package": name, "repo": "-",
                             "detail": f"门面包 {name} 在 MIGRATION.md 里没有迁移行"
                                       f"（下游照表逐包替换会漏掉它）"})
            continue
        if row["retired"]:
            problems.append({"kind": "map-retired", "package": name, "repo": "-",
                             "detail": f"门面包 {name} 仍转发着，但 MIGRATION.md 把它标成已退役"})
            continue
        if row["path"] != path:
            problems.append({"kind": "map-drift", "package": name, "repo": "-",
                             "detail": f"MIGRATION.md 说 {name} → {row['path']}，"
                                       f"而 alias.go 实际转发 {path}"})

    for old, row in sorted(migration.items()):
        if row["retired"]:
            continue
        if old not in facade:
            problems.append({"kind": "map-orphan", "package": old, "repo": "-",
                             "detail": f"MIGRATION.md 有 {row.get('old', old)} 的映射，"
                                       "但门面上没有对应转发"})
    return problems


def check(matrix_pkgs: dict[str, set[str]], facade: dict[str, str]) -> list[dict]:
    """返回问题列表；空列表 = 覆盖完整。"""
    problems: list[dict] = []

    all_matrix: dict[str, str] = {}          # 包名 → 所属仓
    for repo, names in matrix_pkgs.items():
        for n in names:
            all_matrix[n] = repo

    retired_all = {n for names in RETIRED.values() for n in names}

    # 1. 矩阵有、门面没有
    for name, repo in sorted(all_matrix.items()):
        if name in retired_all:
            continue
        if name not in facade:
            problems.append({"kind": "missing", "package": name, "repo": repo,
                             "detail": f"矩阵有 {repo}/{name}，门面没有对应目录"})

    # 2. 门面有、矩阵没有（陈旧转发）
    #    注意：退役包（如 breaker）**不在** matrix_names 里 —— 它已退役，
    #    门面若还留着它，正是陈旧转发，要报出来。豁免只作用于「缺失」方向。
    matrix_names = set(all_matrix)
    for name, path in sorted(facade.items()):
        if name not in matrix_names:
            problems.append({"kind": "stale", "package": name, "repo": "-",
                             "detail": f"门面有 {name}，矩阵里已无此包（upstream={path}）"})

    # 3. upstream 的模块段必须在矩阵仓集合里
    #    这里**不能**把 org 写死成 oagentkit 再去匹配：转发到一个完全不同的 org
    #    （github.com/someone-else/...）时，写死的正则会整个匹配不上、问题被
    #    静默跳过——那正是「幽灵包被引回门面」的最坏形态。必须解析出 org 再判。
    known_repos = set(matrix_pkgs)
    for name, path in sorted(facade.items()):
        m = _IMPORT_PATH_RE.match(path)
        if not m:
            problems.append({"kind": "off-matrix", "package": name, "repo": "-",
                             "detail": f"门面 {name} 的 upstream 路径无法解析：{path}"})
            continue
        org, repo = m.group("org"), m.group("repo")
        if org != "oagentkit" or repo not in known_repos:
            problems.append({"kind": "off-matrix", "package": name, "repo": f"{org}/{repo}",
                             "detail": f"门面 {name} 转发到矩阵外的仓 {org}/{repo}（{path}）"})

    # 4. 数量交叉校验
    #    期望值 = 矩阵包数 **减去仍在矩阵里的退役包数**。
    #    退役包早已从矩阵移除（如 breaker），不在 all_matrix 里，减它就减多了。
    expected = len([n for n in all_matrix if n not in retired_all])
    if len(facade) != expected:
        problems.append({"kind": "count", "package": "-", "repo": "-",
                         "detail": f"门面包数 {len(facade)} != 矩阵应覆盖 {expected}"})

    return problems


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    """离线自测。判据必须是纯函数——否则自测就得联网，覆盖不到任何东西。"""
    failures: list[str] = []

    def check_ok(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    matrix = {
        "textx": {"textutil", "diffx"},
        "llm": {"llm", "llmtest"},
        "rag": {"rag"},
        "guardx": {"fence", "injectband"},
        "toolgate": {"invoke", "policy"},
    }

    # 1. 完整覆盖 → 无问题
    facade_full = {
        "textutil": "github.com/oagentkit/textx/textutil",
        "diffx": "github.com/oagentkit/textx/diffx",
        "llm": "github.com/oagentkit/llm",
        "llmtest": "github.com/oagentkit/llm/llmtest",
        "rag": "github.com/oagentkit/rag",
        "fence": "github.com/oagentkit/guardx/fence",
        "injectband": "github.com/oagentkit/guardx/injectband",
        "invoke": "github.com/oagentkit/toolgate/invoke",
        "policy": "github.com/oagentkit/toolgate/policy",
    }
    check_ok("完整覆盖无问题", check(matrix, facade_full), [])

    # 2. **线上真实形态**：缺 invoke 与 injectband（2026-10-19 实测）
    facade_gap = dict(facade_full)
    del facade_gap["invoke"]
    del facade_gap["injectband"]
    probs = check(matrix, facade_gap)
    kinds = {(p["kind"], p["package"]) for p in probs}
    check_ok("缺包被判出", ("missing", "invoke") in kinds, True)
    check_ok("缺包被判出2", ("missing", "injectband") in kinds, True)
    check_ok("缺包时数量也不对", ("count", "-") in kinds, True)
    check_ok("只报缺失不误报", all(p["kind"] == "missing" or p["kind"] == "count" for p in probs), True)

    # 3. 陈旧转发：门面有、矩阵没有
    facade_stale = dict(facade_full)
    facade_stale["breaker"] = "github.com/oagentkit/runtime/breaker"
    kinds = {(p["kind"], p["package"]) for p in check(matrix, facade_stale)}
    check_ok("陈旧转发被判出", ("stale", "breaker") in kinds, True)

    # 4. 转发到矩阵外的仓（幽灵包引回门面）
    facade_off = dict(facade_full)
    facade_off["textutil"] = "github.com/someone-else/textx/textutil"
    kinds = {(p["kind"], p["package"]) for p in check(matrix, facade_off)}
    check_ok("矩阵外转发被判出", ("off-matrix", "textutil") in kinds, True)

    # 5. 退役豁免：breaker 已从矩阵移除 → **不要求**门面有它（缺失方向豁免）
    check_ok("退役包不报缺失",
             any(p["kind"] == "missing" and p["package"] == "breaker"
                 for p in check(matrix, facade_stale)), False)
    #    但门面若还留着它 → 陈旧转发，必须报（豁免只作用于缺失方向）
    kinds = {(p["kind"], p["package"]) for p in check(matrix, facade_stale)}
    check_ok("退役包残留被判陈旧", ("stale", "breaker") in kinds, True)

    # 6. 负向样本：把「缺包」判据写反的实现（比如只比数量不比集合）应当红
    #    —— 上面 case 2 已经要求同时报出具体包名，数量对但集合缺的情况必须被抓到
    facade_same_count = dict(facade_full)
    facade_same_count.pop("invoke")
    facade_same_count["policy"] = "github.com/oagentkit/toolgate/policy"  # 数量不变
    probs = check(matrix, facade_same_count)
    check_ok("数量相同但集合缺也要报",
             any(p["kind"] == "missing" and p["package"] == "invoke" for p in probs), True)

    # 7. 迁移映射表 ↔ 实际转发的一致性
    full_map = {
        "textutil": {"module": "textx", "path": "github.com/oagentkit/textx/textutil", "retired": False},
        "diffx": {"module": "textx", "path": "github.com/oagentkit/textx/diffx", "retired": False},
        "llm": {"module": "llm", "path": "github.com/oagentkit/llm", "retired": False},
        "llmtest": {"module": "llm", "path": "github.com/oagentkit/llm/llmtest", "retired": False},
        "rag": {"module": "rag", "path": "github.com/oagentkit/rag", "retired": False},
        "fence": {"module": "guardx", "path": "github.com/oagentkit/guardx/fence", "retired": False},
        "injectband": {"module": "guardx", "path": "github.com/oagentkit/guardx/injectband", "retired": False},
        "invoke": {"module": "toolgate", "path": "github.com/oagentkit/toolgate/invoke", "retired": False},
        "policy": {"module": "toolgate", "path": "github.com/oagentkit/toolgate/policy", "retired": False},
    }
    check_ok("映射表完整时无问题", check_migration_map(facade_full, full_map), [])

    # 7a. **线上真实形态**：门面有 port，MIGRATION.md 漏了它
    facade_with_port = dict(facade_full)
    facade_with_port["port"] = "github.com/oagentkit/llm/port"
    kinds = {(p["kind"], p["package"]) for p in check_migration_map(facade_with_port, full_map)}
    check_ok("门面包缺迁移行被判出", ("map-missing", "port") in kinds, True)

    # 7b. 映射表指向门面没有的包（空指引）
    orphan = dict(full_map)
    orphan["ghost"] = {"module": "llm", "path": "github.com/oagentkit/llm/ghost", "retired": False}
    kinds = {(p["kind"], p["package"]) for p in check_migration_map(facade_full, orphan)}
    check_ok("空映射被判出", ("map-orphan", "ghost") in kinds, True)

    # 7c. 表与实现漂移：行的目标路径与 alias.go 不一致
    drifted = dict(full_map)
    drifted["textutil"] = {"module": "textx", "path": "github.com/oagentkit/textx/textutil2",
                            "retired": False}
    kinds = {(p["kind"], p["package"]) for p in check_migration_map(facade_full, drifted)}
    check_ok("表与实现漂移被判出", ("map-drift", "textutil") in kinds, True)

    # 7d. 退役行不该要求门面有对应转发
    with_retired = dict(full_map)
    with_retired["breaker"] = {"module": "已退役", "path": "github.com/oekit/concurrency/breaker",
                                "retired": True}
    kinds = {(p["kind"], p["package"]) for p in check_migration_map(facade_full, with_retired)}
    check_ok("退役行不误报", any(k[0] in ("map-orphan", "map-missing") for k in kinds), False)

    if failures:
        print("❌ verify_facade_packages 自测失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ verify_facade_packages 自测通过（缺包 / 陈旧转发 / 矩阵外转发 / 退役豁免 / 负向样本）")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="校验门面仓的 re-export 覆盖率")
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--facade", help="门面仓本地路径")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.facade:
        print("❌ 需要 --facade <门面仓路径>")
        return 2

    matrix_path = Path(args.matrix)
    facade_path = Path(args.facade)
    if not matrix_path.is_file():
        print(f"❌ 找不到矩阵文件: {matrix_path}")
        return 2
    if not facade_path.is_dir():
        print(f"❌ 找不到门面仓: {facade_path}")
        return 2

    matrix_pkgs = matrix_packages(matrix_path)
    facade = facade_packages(facade_path)
    problems = check(matrix_pkgs, facade)
    # 迁移映射表的一致性：下游唯一的迁移指引，必须与实际转发一一对应
    problems += check_migration_map(facade, migration_map(facade_path))

    if args.as_json:
        print(json.dumps({"facade_packages": len(facade),
                          "matrix_packages": sum(len(v) for v in matrix_pkgs.values()),
                          "problems": problems}, ensure_ascii=False, indent=2))
    else:
        all_names = {n for names in matrix_pkgs.values() for n in names}
        retired_all = {n for names in RETIRED.values() for n in names}
        # 期望值 = 矩阵包数减去**仍在矩阵里的**退役包数。
        # 退役包早已从矩阵移除（breaker），不在 all_names 里，减它就减多了。
        expected = len([n for n in all_names if n not in retired_all])
        print(f"门面包数 {len(facade)}，矩阵应覆盖 {expected}")
        if not problems:
            print("✅ 门面的 re-export 覆盖完整，无缺口、无陈旧转发")
        else:
            print(f"\n❌ {len(problems)} 处问题：\n")
            for p in problems:
                print(f"   [{p['kind']:<9}] {p['package']:<14} {p['detail']}")
            print("\n修法：缺包 → 照门面既有 alias.go 模式补目录；"
                  "陈旧转发 → 删掉对应目录并从 MIGRATION.md 移除该行。")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
