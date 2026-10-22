#!/usr/bin/env python3
"""幽灵包检测：仓里存在、但 modules.json 未登记的包。

背景（2026-10-18 实测）：
    `guardx/injectband` 提交时带了完整代码 + 测试 + README 段落，
    CI 也是绿的（`go test ./...` 自动覆盖新包），**唯独没进 modules.json**。

    后果不是「编译不过」，而是它变成了**矩阵外的存在**：
      - versions.json / llms.txt 的包清单里没有它 → 消费方根本看不到这个包
      - gen_docs.py 不会为它生成文档
      - 门禁的分层不变量、依赖闭包校验对它**完全失效**（它不在矩阵里）
    而这一切全绿 —— 这正是它危险的地方：CI 绿 ≠ 矩阵完整。

    `verify_split.py` 的第 1 项（覆盖度）本来管这事，但它要求 agentkit 单仓布局，
    跑不了「13 个独立仓 + 一份矩阵文件」的新形态。所以这里另起一个。

用法:
    verify_ghost_packages.py --root <各仓父目录> --matrix modules.json
    verify_ghost_packages.py --root ... --verbose
    verify_ghost_packages.py --selftest

判定：某仓目录下含 *.go 的**包目录集合** vs modules.json 登记的包名集合，双向比对。

退出码: 0 = 一致；1 = 有幽灵包或缺包。

⚠️ **口径必须与 CONTRIBUTING 一致**：「`go list ./...` 的每个包都登记在 modules.json」。
   `go list ./...` 会把三种形态都算成包，而它们全都**编译/测试全绿**：
     ① 仓根的直接子目录         （旧实现能看见）
     ② 嵌套包 repo/foo/bar      （旧实现看不见）
     ③ 测试专用目录（只有 _test.go）（旧实现看不见）
   实测（2026-10-22）：旧实现漏掉 ②③，两种形态 `go build ./... && go test ./...` 都绿，
   即「幽灵包存在于 CI 完全看不见」——这正是本守卫要防的东西。
   所以这里按 `go list ./...` 的口径取数：递归 + 任何 .go 文件（含 _test.go）。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

# 非包目录：含 .go 文件才算包，这些名字即使有 .go 也不是矩阵包。
# 注意：SKIP 现在**按路径段**生效——嵌套包 repo/cmd/foo 里的 cmd 段同样豁免。
SKIP = {
    "docs", "doc", "testdata", "test", "examples", "example", "cmd",
    "internal", "scripts", "tools", "ci", "hack", "build", "deploy",
    "site", "web", "api", "config", "third_party", "vendor", "node_modules",
    "gen", "proto", "migrations",
}


def has_go(dirpath: Path) -> bool:
    """目录是否是一个 Go 包。

    口径对齐 `go list ./...`：**任何** .go 文件都算，包括只有 _test.go 的
    测试专用目录（go list 把它列为包，go test 也会跑它）。
    旧实现排除 _test.go，于是这类目录对门禁完全不可见。
    """
    try:
        return any(f.suffix == ".go" for f in dirpath.iterdir() if f.is_file())
    except OSError:
        return False


def skip_dir(name: str) -> bool:
    """该目录名是否应当整棵跳过（SKIP 名单 / 隐藏目录 / 下划线开头）。"""
    return name in SKIP or name.startswith((".", "_"))


def actual_packages(repo_dir: Path) -> set[str]:
    """仓内所有包目录的**相对路径**（相对仓根，用 posix 分隔）。

    - 仓根本身就可能是包 —— 路径规则在「包名末段==仓名」时要求包落 module 根
      （llm / rag 两个仓）。只扫子目录会把它们误报成「登记了但仓里没有」。
    - 递归查找：嵌套包（repo/foo/bar）也是包（`go list ./...` 认它）。
    - SKIP 名单按**路径段**豁免：`cmd/`、`internal/` 整棵不算库包。
    """
    out: set[str] = set()
    if has_go(repo_dir):
        out.add(repo_dir.name)
    for p in repo_dir.rglob("*"):
        if not p.is_dir():
            continue
        rel = p.relative_to(repo_dir)
        if any(skip_dir(seg) for seg in rel.parts):
            continue
        if has_go(p):
            out.add(rel.as_posix())
    return out


def declared_packages(matrix: Path) -> dict[str, set[str]]:
    """modules.json → {仓名: 登记的包名集合}。"""
    data = json.loads(matrix.read_text(encoding="utf-8"))
    return {m["repo"]: {p.rsplit("/", 1)[-1] for p in m["packages"]}
            for m in data["modules"]}


def judge(base: Path, declared: dict[str, set[str]]
          ) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]],
                     int, list[str]]:
    """判定本体。返回 (不一致的仓, 幽灵包, 缺失包, 实际检查到的仓数, 跳过的仓)。

    抽成独立函数是为了能离线自测——判据必须能对着**故意造坏**的样本跑。
    """
    bad: list[str] = []
    ghosts: dict[str, list[str]] = {}
    missings: dict[str, list[str]] = {}
    checked = 0
    skipped: list[str] = []
    for repo, pkgs in sorted(declared.items()):
        d = base / repo
        if not d.is_dir():
            skipped.append(repo)
            continue
        checked += 1
        actual = actual_packages(d)
        # 比对按**包名末段**（矩阵登记的就是末段），幽灵包按相对路径报出，
        # 嵌套的幽灵要看得见是哪一层。
        actual_names = {rel.rsplit("/", 1)[-1] for rel in actual}
        ghost = sorted(rel for rel in actual if rel.rsplit("/", 1)[-1] not in pkgs)
        missing = sorted(pkgs - actual_names)
        if ghost or missing:
            bad.append(repo)
            if ghost:
                ghosts[repo] = ghost
            if missing:
                missings[repo] = missing
    return bad, ghosts, missings, checked, skipped


def selftest() -> int:
    """反证自测：拿**故意造坏**的被检对象跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西
    （matrix-tools/AGENTS.md 硬规则 2）。所以样本必须覆盖：

      ① 幽灵包（仓里有、矩阵没登记）      → 必须报出
      ② 嵌套幽灵包（repo/foo/bar）        → 必须报出（旧实现漏）
      ③ 测试专用包（只有 _test.go）        → 必须报出（旧实现漏）
      ④ 登记了但仓里没有                   → 必须报出
      ⑤ 一个仓都没查到                     → 必须判红（不得「全部跳过显示全绿」）
      ⑥ 全合规                            → 必须不报
      ⑦ SKIP 名单里的目录（cmd/…）         → 必须**不**报（不得误报）
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        matrix = base / "modules.json"
        matrix.write_text(json.dumps({"modules": [
            {"repo": "alpha", "packages": ["alpha", "alpha/core"], "tier": 0},
            {"repo": "beta", "packages": ["beta/core"], "tier": 0},
        ]}, ensure_ascii=False), encoding="utf-8")

        def mk(repo: str, rel: str, body: str = "package p\n", fname: str = "p.go"):
            d = base / repo / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / fname).write_text(body, encoding="utf-8")
            return d

        # alpha：完全合规（仓根也有 .go —— 路径规则允许包落 module 根）
        mk("alpha", ".", "package alpha\n", "a.go")
        mk("alpha", "core")
        # beta：合规包 + 四种异常
        mk("beta", "core")
        mk("beta", "ghostplain")                              # ① 普通幽灵包
        mk("beta", "ghostnest/sub", "package sub\n", "s.go")   # ② 嵌套幽灵包
        mk("beta", "ghosttest", "package ghosttest\n", "t_test.go")  # ③ 测试专用包
        # ⑦ SKIP 名单里的目录：有 .go 但不是库包，不得误报
        mk("beta", "cmd/tool", "package main\n", "m.go")
        # ④ 登记了但仓里没有：给 matrix 加一个第三仓（gamma），但仓目录不存在
        matrix.write_text(json.dumps({"modules": [
            {"repo": "alpha", "packages": ["alpha", "alpha/core"], "tier": 0},
            {"repo": "beta", "packages": ["beta/core"], "tier": 0},
            {"repo": "gamma", "packages": ["gamma/core"], "tier": 0},
        ]}, ensure_ascii=False), encoding="utf-8")

        declared = declared_packages(matrix)
        bad, ghosts, missings, checked, skipped = judge(base, declared)

        # ① 普通幽灵包
        check("① 普通幽灵包被报出", "ghostplain" in ghosts.get("beta", []), True)
        # ② 嵌套幽灵包（旧实现漏）
        check("② 嵌套幽灵包被报出", "ghostnest/sub" in ghosts.get("beta", []), True)
        # ③ 测试专用包（旧实现漏）
        check("③ 测试专用包被报出", "ghosttest" in ghosts.get("beta", []), True)
        # ④ 登记了但仓里没有 → gamma 目录不存在 ⇒ 跳过，不算 missing
        check("④ 仓不存在的仓被跳过", "gamma" in skipped, True)
        check("④ 仓不存在的仓不算缺失", "gamma" in missings, False)
        # ⑦ SKIP 名单不得误报
        check("⑦ cmd/ 目录不误报", "cmd/tool" in ghosts.get("beta", []), False)
        check("⑦ cmd/ 目录不计入缺失", "cmd" in missings.get("beta", []), False)
        # ⑥ alpha 完全合规
        check("⑥ 合规仓不报", "alpha" in bad, False)

        # ④ 反向样本：把 beta/core 删掉 ⇒ 「登记了但仓里没有」必须报出
        (base / "beta" / "core" / "p.go").unlink()
        (base / "beta" / "core").rmdir()
        bad2, _, missings2, _, _ = judge(base, declared)
        check("④ 登记了但仓里没有 → 报出", "core" in missings2.get("beta", []), True)
        check("④ 该仓判为不一致", "beta" in bad2, True)

        # ⑤ 一个仓都没查到 ⇒ 不得显示全绿（main 里判红）
        empty = base / "empty"
        empty.mkdir()
        bad3, _, _, checked3, _ = judge(empty, declared)
        check("⑤ 一个仓都没查到 → checked==0", checked3, 0)
        check("⑤ 一个仓都没查到 → 判为不一致", bool(bad3) or checked3 == 0, True)

    if fails:
        print("❌ verify_ghost_packages 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_ghost_packages 自测通过"
          "（幽灵包 / 嵌套幽灵包 / 测试专用包 / 缺包 必报；合规与 SKIP 名单不误报）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="各仓工作目录的父目录")
    ap.add_argument("--repos-dir", default="",
                    help="各仓实际所在子目录（相对 --root）。CI 里各仓 checkout 到 "
                         "matrix/<repo>/，本地是平铺的 <repo>/ —— 不指定则按平铺处理")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认自动定位）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root:
        print("❌ 缺少 --root（或用 --selftest）")
        return 1

    here = Path(__file__).resolve().parent
    matrix = Path(args.matrix) if args.matrix else here.parent / "modules.json"
    if not matrix.is_file():
        print(f"❌ 找不到 modules.json: {matrix}")
        return 1

    declared = declared_packages(matrix)
    root = Path(args.root).expanduser().resolve()
    # 各仓可能平铺在 root 下，也可能统一放在 root/<repos-dir>/ 下。
    # 两种都认：先试平铺，再试子目录——否则传错参数会「全部跳过」并显示全绿，
    # 那是最糟的失效方式（看起来在检查，其实什么都没查）。
    base = (root / args.repos_dir) if args.repos_dir else root
    if args.repos_dir and not base.is_dir():
        print(f"❌ --repos-dir 指定的目录不存在: {base}")
        return 1

    print(f"矩阵登记 {len(declared)} 个仓 / "
          f"{sum(len(v) for v in declared.values())} 包")
    print(f"各仓目录: {base}\n")

    bad, ghosts, missings, checked, skipped = judge(base, declared)
    for repo in skipped:
        print(f"  {repo:<12} ⏭  工作目录不存在（跳过）：{base / repo}")
    for repo in bad:
        print(f"  {repo:<12} ⚠️")
        if repo in ghosts:
            print(f"      👻 幽灵包（仓里有、矩阵没登记）: {ghosts[repo]}")
        if repo in missings:
            print(f"      ❌ 登记了但仓里没有: {missings[repo]}")
    if not bad and args.verbose:
        for repo in sorted(declared):
            d = base / repo
            if d.is_dir():
                print(f"  {repo:<12} ✅ {len(actual_packages(d))} 包一致")

    # 一个仓都没查到 = 参数传错，不是「一致」。必须显式失败，
    # 否则这条门禁会变成又一个「一直绿但从没生效过」的摆设。
    if checked == 0:
        print(f"\n❌ 一个仓都没检查到（共 {len(declared)} 个登记仓全部找不到目录）。")
        print(f"   --root={root}  --repos-dir={args.repos_dir!r}")
        print("   请确认各仓的实际 checkout 路径。本地通常是平铺的 <repo>/，"
              "CI 里是 matrix/<repo>/。")
        return 1

    if bad:
        print(f"\n❌ {len(bad)} 个仓不一致：{', '.join(bad)}\n")
        print("幽灵包是「矩阵外的存在」：")
        print("  · 消费方从 versions.json / llms.txt 里看不到它")
        print("  · gen_docs.py 不会为它生成文档")
        print("  · 门禁的分层不变量与依赖闭包校验对它完全失效")
        print("  · 而 CI 仍然是绿的 —— 绿不代表矩阵完整")
        print("\n修法：把它补进 modules.json（packages + pkgDesc，必要时 tier/dependsOn），")
        print("     或从仓里删掉。补完后跑 gen_versions.py / gen_llms_txt.py 重新生成产物。")
        return 1

    print("✅ 全部一致，无幽灵包")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
