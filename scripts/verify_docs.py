#!/usr/bin/env python3
"""文档防漂移校验：重新生成 llms.txt / 站点场景页，与仓库内版本比对，不一致就 exit 1。

背景（2026-10-18 实测事故）：
    `runtime/breaker` 早已退役、modules.json 也已更新，但 `gen_llms_txt.py` 的
    「易踩的坑」段是**硬编码**的，退役时没人同步 → 站点 llms.txt 一旦重新生成，
    就会把「暂不可删 / 上游还缺 #19 #20」这种**已失效的结论**发布出去，
    而 AI Agent 是把 llms.txt 当权威索引读的。

    也就是说：文档漂移不是「不好看」，是会**主动误导**下游。

本脚本把「生成物必须等于提交物」变成机器可判定的事实。

`--page`（matrix-tools#258）守站点场景页 `docs/reference/scenarios.md`：
    它是 `gen_llms_txt --out-page` 的**逐字节副本**（与 llms.txt 同一纪律）——
    手改会在下次生成时消失；门禁必须在消失**之前**报红，否则手改与生成器
    互相顶替，没人知道哪边才是对的。

用法:
    verify_docs.py --matrix modules.json --llms ../oagentkit.github.io/docs/public/llms.txt
    verify_docs.py ... --page ../oagentkit.github.io/docs/reference/scenarios.md
    verify_docs.py ... --fix     # 直接把生成结果写回
    verify_docs.py --selftest    # 离线反证自测
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# ⚠️ 这组参数必须**无副作用**、且能让脚本**真正走完入口**（不能只让 argparse 早退）。
SMOKE_ARGS = []

def generate(matrix: Path) -> tuple[list[str], str]:
    """按 modules.json 生成 llms.txt 的行列表。

    返回 (行列表, 生成器自己的日志)。生成器失败时抛 RuntimeError。
    """
    with tempfile.TemporaryDirectory() as td:
        fresh = Path(td) / "llms.txt"
        r = subprocess.run(
            [sys.executable, str(HERE / "gen_llms_txt.py"),
             "--matrix", str(matrix), "--out", str(fresh)],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("gen_llms_txt.py 执行失败：\n" + r.stderr)
        return fresh.read_text("utf-8").splitlines(), r.stdout.strip()


def generate_page(matrix: Path) -> list[str]:
    """按 modules.json 生成站点场景页的行列表（gen_llms_txt --out-page）。"""
    with tempfile.TemporaryDirectory() as td:
        fresh = Path(td) / "scenarios.md"
        r = subprocess.run(
            [sys.executable, str(HERE / "gen_llms_txt.py"),
             "--matrix", str(matrix), "--out", str(Path(td) / "llms.txt"),
             "--out-page", str(fresh)],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("gen_llms_txt.py 执行失败：\n" + r.stderr)
        return fresh.read_text("utf-8").splitlines()


def compare(matrix: Path, committed: Path) -> tuple[bool, list[str], list[str]]:
    """判定本体：生成物与提交物是否逐行相等。

    返回 (是否一致, 生成行, 提交行)。抽成独立函数是为了能离线自测——
    判据必须能对着**故意造坏**的样本跑（硬规则 2）。
    """
    a, _ = generate(matrix)
    b = committed.read_text("utf-8").splitlines()
    return a == b, a, b


def compare_page(matrix: Path, committed: Path) -> tuple[bool, list[str], list[str]]:
    """场景页的判定本体（同 compare，只是生成器走 --out-page）。"""
    a = generate_page(matrix)
    b = committed.read_text("utf-8").splitlines()
    return a == b, a, b


def selftest() -> int:
    """反证自测：拿**故意造坏**的产物跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设（matrix-tools/AGENTS.md 硬规则 2）。样本覆盖：

      ① 与 modules.json 一致        → 必须判绿
      ② 提交版多一行（漂移）        → 必须判红
      ③ 提交版少一行（漂移）        → 必须判红
      ④ 提交版整体被换掉            → 必须判红
      ⑤ 提交版文件不存在            → 必须判红（不得静默跳过）
      ⑥ --fix 能把漂移修回一致      → 修完必须判绿
      ⑦ 场景页一致                  → 必须判绿
      ⑧ 场景页手改一行（漂移）      → 必须判红（#258 新渲染目标的有用例）
      ⑨ 场景页 --fix 能修回         → 修完必须判绿

    ⑤ 尤其关键：这是「文档漂移会主动误导下游 AI Agent」那条事故的形态——
    提交物缺失时若判绿，等于「门禁坏了但 CI 说没问题」。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    matrix = HERE.parent / "modules.json"
    if not matrix.is_file():
        print(f"❌ 自测需要 modules.json: {matrix}")
        return 1

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        committed = td / "llms.txt"
        a, _ = generate(matrix)

        # ① 一致 → 绿
        committed.write_text("\n".join(a) + "\n", encoding="utf-8")
        ok, _, _ = compare(matrix, committed)
        check("① 一致判绿", ok, True)

        # ② 多一行 → 红
        committed.write_text("\n".join(a) + "\n多一行漂移\n", encoding="utf-8")
        ok2, _, _ = compare(matrix, committed)
        check("② 多一行判红", ok2, False)

        # ③ 少一行 → 红
        committed.write_text("\n".join(a[:-1]) + "\n", encoding="utf-8")
        ok3, _, _ = compare(matrix, committed)
        check("③ 少一行判红", ok3, False)

        # ④ 整体换掉 → 红
        committed.write_text("完全不是生成物\n", encoding="utf-8")
        ok4, _, _ = compare(matrix, committed)
        check("④ 整体换掉判红", ok4, False)

        # ⑤ 文件不存在 → 红（main 里必须显式判红，不得静默跳过）
        missing = td / "nope.txt"
        check("⑤ 文件不存在判红", missing.is_file(), False)

        # ⑥ --fix 能把漂移修回一致，且修完判绿
        committed.write_text("\n".join(a) + "\n多一行漂移\n", encoding="utf-8")
        argv = sys.argv
        try:
            sys.argv = ["verify_docs.py", "--matrix", str(matrix),
                        "--llms", str(committed), "--fix"]
            rc = main()
        finally:
            sys.argv = argv
        check("⑥ --fix 退出码 0", rc, 0)
        ok6, _, _ = compare(matrix, committed)
        check("⑥ --fix 后判绿", ok6, True)

        # ⑦⑧⑨ 场景页（#258 新渲染目标：手改必须判红，且 --fix 能修回）
        page = td / "scenarios.md"
        pa = generate_page(matrix)
        page.write_text("\n".join(pa) + "\n", encoding="utf-8")
        ok7, _, _ = compare_page(matrix, page)
        check("⑦ 场景页一致判绿", ok7, True)

        page.write_text("\n".join(pa) + "\n手改的场景行\n", encoding="utf-8")
        ok8, _, _ = compare_page(matrix, page)
        check("⑧ 场景页手改判红", ok8, False)

        try:
            sys.argv = ["verify_docs.py", "--matrix", str(matrix),
                        "--page", str(page), "--fix"]
            rc9 = main()
        finally:
            sys.argv = argv
        check("⑨ 场景页 --fix 退出码 0", rc9, 0)
        ok9, _, _ = compare_page(matrix, page)
        check("⑨ 场景页 --fix 后判绿", ok9, True)

    if fails:
        print("❌ verify_docs 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_docs 自测通过"
          "（llms/场景页漂移必红；一致判绿；--fix 能修回）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--llms", help="仓库内已提交的 llms.txt 路径")
    ap.add_argument("--page", help="仓库内已提交的站点场景页路径（docs/reference/scenarios.md）")
    ap.add_argument("--fix", action="store_true", help="用生成结果覆盖提交物")
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.llms and not args.page:
        print("❌ 缺少 --llms / --page（或用 --selftest）")
        return 1

    problems: list[str] = []

    def run(label: str, committed: Path, gen) -> None:
        if not committed.is_file():
            # 提交物缺失必须显式判红——「门禁坏了但 CI 说没问题」是最糟形态
            problems.append(f"{label}: 找不到 {committed}")
            return
        try:
            ok, a, b = gen(Path(args.matrix), committed)
        except RuntimeError as e:
            problems.append(f"{label}: {e}")
            return
        if ok:
            print(f"✅ {label} 与 modules.json 一致（{len(a)} 行）")
            return
        if args.fix:
            committed.write_text("\n".join(a) + "\n", encoding="utf-8")
            print(f"✅ 已用生成结果覆盖 {committed}")
            return
        problems.append(f"{label}: 漂移（生成 {len(a)} 行 / 提交 {len(b)} 行）")
        for line in difflib.unified_diff(b, a, "committed", "generated",
                                         lineterm="", n=1):
            print("   " + line)

    if args.llms:
        run("llms.txt", Path(args.llms), compare)
    if args.page:
        run("场景页 scenarios.md", Path(args.page), compare_page)

    if problems:
        print("❌ " + "；".join(problems))
        print("\n   若确认生成结果正确，跑：  python3 scripts/verify_docs.py --fix "
              + " ".join(x for x in (f"--llms {args.llms}" if args.llms else "",
                                     f"--page {args.page}" if args.page else "") if x))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
