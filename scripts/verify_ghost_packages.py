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

判定：某仓目录下含 *.go 的子目录集合 vs modules.json 登记的包名集合，双向比对。

退出码: 0 = 一致；1 = 有幽灵包或缺包。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# 非包目录：含 .go 文件才算包，这些名字即使有 .go 也不是矩阵包
SKIP = {
    "docs", "doc", "testdata", "test", "examples", "example", "cmd",
    "internal", "scripts", "tools", "ci", "hack", "build", "deploy",
    "site", "web", "api", "config", "third_party", "vendor", "node_modules",
    "gen", "proto", "migrations",
}


def has_go(dirpath: Path) -> bool:
    try:
        return any(f.suffix == ".go" and not f.name.endswith("_test.go")
                   for f in dirpath.iterdir() if f.is_file())
    except OSError:
        return False


def actual_packages(repo_dir: Path) -> set[str]:
    out: set[str] = set()
    # module 根自己就可能是包 —— 路径规则在「包名末段==仓名」时要求包落 module 根
    # （llm / rag 两个仓）。只扫子目录会把它们误报成「登记了但仓里没有」。
    if has_go(repo_dir):
        out.add(repo_dir.name)
    for p in repo_dir.iterdir():
        if not p.is_dir() or p.name in SKIP or p.name.startswith((".", "_")):
            continue
        if has_go(p):
            out.add(p.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="各仓工作目录的父目录")
    ap.add_argument("--repos-dir", default="",
                    help="各仓实际所在子目录（相对 --root）。CI 里各仓 checkout 到 "
                         "matrix/<repo>/，本地是平铺的 <repo>/ —— 不指定则按平铺处理")
    ap.add_argument("--matrix", default=None, help="modules.json 路径（默认自动定位）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    matrix = Path(args.matrix) if args.matrix else here.parent / "modules.json"
    if not matrix.is_file():
        print(f"❌ 找不到 modules.json: {matrix}")
        return 1

    data = json.loads(matrix.read_text(encoding="utf-8"))
    root = Path(args.root).expanduser().resolve()
    # 各仓可能平铺在 root 下，也可能统一放在 root/<repos-dir>/ 下。
    # 两种都认：先试平铺，再试子目录——否则传错参数会「全部跳过」并显示全绿，
    # 那是最糟的失效方式（看起来在检查，其实什么都没查）。
    base = (root / args.repos_dir) if args.repos_dir else root
    if args.repos_dir and not base.is_dir():
        print(f"❌ --repos-dir 指定的目录不存在: {base}")
        return 1

    declared: dict[str, set[str]] = {}
    for m in data["modules"]:
        declared[m["repo"]] = {p.rsplit("/", 1)[-1] for p in m["packages"]}

    print(f"矩阵登记 {len(declared)} 个仓 / "
          f"{sum(len(v) for v in declared.values())} 包")
    print(f"各仓目录: {base}\n")

    checked = 0
    bad: list[str] = []
    for repo, pkgs in sorted(declared.items()):
        d = base / repo
        if not d.is_dir():
            print(f"  {repo:<12} ⏭  工作目录不存在（跳过）：{d}")
            continue
        checked += 1
        actual = actual_packages(d)
        ghost = actual - pkgs
        missing = pkgs - actual
        if ghost or missing:
            bad.append(repo)
            print(f"  {repo:<12} ⚠️")
            if ghost:
                print(f"      👻 幽灵包（仓里有、矩阵没登记）: {sorted(ghost)}")
            if missing:
                print(f"      ❌ 登记了但仓里没有: {sorted(missing)}")
        elif args.verbose:
            print(f"  {repo:<12} ✅ {len(actual)} 包一致")

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
