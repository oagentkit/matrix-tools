#!/usr/bin/env python3
"""模块仓文档漂移检测（只报告，永不覆盖）。

为什么需要它（2026-10-18 实测）：
    `gen_docs.py` 生成各仓 README.md / AGENTS.md 的骨架，但生成之后有人在
    仓库里**手写补充了大量内容**——例如 queue/AGENTS.md 的「选主语义（正确性红线）」
    一整节、textx/README.md 的包级细节。实测 13 个仓 26 个文档里有 10 个已与
    生成器输出不一致，且**仓库版本才是更丰富的那个**。

    这意味着 `gen_docs.py --out-root` 直接指向真实仓目录会**抹掉这些手写内容**。
    本脚本只把差异报出来，让人自己决定，不提供 --fix。

    另一个方向也存在：modules.json 更新后（如 llm 新增 runtime 依赖），
    生成器会比仓库新——那才是「该重新生成」的情形。

用法:
    verify_repo_docs.py --mirror /Users/zhangyi/oagentkit
    verify_repo_docs.py --mirror ... --repo textx --verbose
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("README.md", "AGENTS.md")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", required=True, help="各仓工作目录的父目录")
    ap.add_argument("--repo", action="append", help="只查指定仓（可多次）")
    ap.add_argument("--verbose", action="store_true", help="打印 diff")
    ap.add_argument("--max-lines", type=int, default=12)
    args = ap.parse_args()

    mirror = Path(args.mirror).resolve()
    gen_docs = HERE / "gen_docs.py"
    if not gen_docs.is_file():
        print(f"❌ 找不到 {gen_docs}")
        return 1

    # 1) 在临时目录里生成一份参照。仓列表取 modules.json 的 modules（权威），
    #    不用「目录里有 go.mod」这种猜法——否则 facade/infra 仓会被误算进来。
    td = Path(tempfile.mkdtemp())
    matrix = HERE.parent / "modules.json"
    if args.repo:
        repos = args.repo
    elif matrix.is_file():
        import json
        repos = [m["repo"] for m in json.loads(matrix.read_text("utf-8"))["modules"]]
    else:
        repos = sorted(p.name for p in mirror.iterdir()
                       if p.is_dir() and (p / "go.mod").is_file()
                       and not p.name.startswith("."))
    for r in repos:
        (td / r).mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(gen_docs), "--out-root", str(td)],
                   capture_output=True, text=True)

    # 2) 比对
    drift: list[tuple[str, str, int, int]] = []
    for r in repos:
        for f in FILES:
            a, b = td / r / f, mirror / r / f
            if not b.is_file():
                continue
            if a.is_file() and a.read_bytes() == b.read_bytes():
                continue
            n = 0
            if a.is_file():
                out = subprocess.run(["diff", "-u", str(b), str(a)],
                                     capture_output=True, text=True).stdout
                n = sum(1 for l in out.splitlines()
                        if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
                if args.verbose:
                    print(f"\n──── {r}/{f} ────")
                    c = 0
                    for line in difflib.unified_diff(
                            b.read_text("utf-8").splitlines(),
                            a.read_text("utf-8").splitlines(),
                            "committed", "generated", lineterm="", n=0):
                        if line.startswith(("+++", "---", "@@")):
                            continue
                        print("  " + line[:160])
                        c += 1
                        if c >= args.max_lines:
                            print("  …")
                            break
            drift.append((r, f, n, len(a.read_text("utf-8").splitlines())
                          if a.is_file() else 0))

    total = len(repos) * len(FILES)
    print(f"\n检查 {len(repos)} 个仓 × {len(FILES)} 个文档 = {total}，"
          f"漂移 {len(drift)} 个")
    if not drift:
        print("✅ 全部与生成器一致")
        return 0

    print("\n⚠️  以下文档与 gen_docs.py 的输出不一致：")
    for r, f, n, glen in drift:
        print(f"   {r:<12}{f:<12}差异 {n:>4} 行（生成物 {glen} 行）")

    print("\n⚠️  注意：多数情况下**仓库版本更丰富**（含手写的工程纪律，如 queue 的选主红线）。")
    print("   gen_docs.py 只生成骨架，直接覆盖会抹掉手写内容——本脚本因此不提供 --fix。")
    print("   处理方向：把差异并回 modules.json 让生成器产出同样内容，或把生成范围限定到骨架段落。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
