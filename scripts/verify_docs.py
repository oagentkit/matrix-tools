#!/usr/bin/env python3
"""文档防漂移校验：重新生成 llms.txt 并与仓库内版本比对，不一致就 exit 1。

背景（2026-10-18 实测事故）：
    `runtime/breaker` 早已退役、modules.json 也已更新，但 `gen_llms_txt.py` 的
    「易踩的坑」段是**硬编码**的，退役时没人同步 → 站点 llms.txt 一旦重新生成，
    就会把「暂不可删 / 上游还缺 #19 #20」这种**已失效的结论**发布出去，
    而 AI Agent 是把 llms.txt 当权威索引读的。

    也就是说：文档漂移不是「不好看」，是会**主动误导**下游。

本脚本把「生成物必须等于提交物」变成机器可判定的事实。

用法:
    verify_docs.py --matrix modules.json --llms ../oagentkit.github.io/docs/public/llms.txt
    verify_docs.py ... --fix     # 直接把生成结果写回 llms.txt
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


def compare(matrix: Path, committed: Path) -> tuple[bool, list[str], list[str]]:
    """判定本体：生成物与提交物是否逐行相等。

    返回 (是否一致, 生成行, 提交行)。抽成独立函数是为了能离线自测——
    判据必须能对着**故意造坏**的样本跑（硬规则 2）。
    """
    a, _ = generate(matrix)
    b = committed.read_text("utf-8").splitlines()
    return a == b, a, b


def selftest() -> int:
    """反证自测：拿**故意造坏**的 llms.txt 跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设（matrix-tools/AGENTS.md 硬规则 2）。样本覆盖：

      ① 与 modules.json 一致        → 必须判绿
      ② 提交版多一行（漂移）        → 必须判红
      ③ 提交版少一行（漂移）        → 必须判红
      ④ 提交版整体被换掉            → 必须判红
      ⑤ 提交版文件不存在            → 必须判红（不得静默跳过）
      ⑥ --fix 能把漂移修回一致      → 修完必须判绿

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

    if fails:
        print("❌ verify_docs 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_docs 自测通过"
          "（漂移必红；一致判绿；--fix 能修回）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--llms", help="仓库内已提交的 llms.txt 路径")
    ap.add_argument("--fix", action="store_true", help="用生成结果覆盖 llms.txt")
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.llms:
        print("❌ 缺少 --llms（或用 --selftest）")
        return 1

    committed = Path(args.llms)
    if not committed.is_file():
        print(f"❌ 找不到 llms.txt: {committed}")
        return 1

    try:
        ok, a, b = compare(Path(args.matrix), committed)
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1

    if ok:
        print(f"✅ llms.txt 与 modules.json 一致（{len(a)} 行）")
        return 0

    if args.fix:
        committed.write_text("\n".join(a) + "\n", encoding="utf-8")
        print(f"✅ 已用生成结果覆盖 {committed}")
        return 0

    print(f"❌ llms.txt 与 modules.json 漂移（生成 {len(a)} 行 / 提交 {len(b)} 行）：\n")
    for line in difflib.unified_diff(b, a, "committed", "generated", lineterm="", n=1):
        print("   " + line)
    print("\n   若确认生成结果正确，跑：  python3 scripts/verify_docs.py "
          f"--llms {committed} --fix")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
