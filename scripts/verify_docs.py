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
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--llms", required=True, help="仓库内已提交的 llms.txt 路径")
    ap.add_argument("--fix", action="store_true", help="用生成结果覆盖 llms.txt")
    args = ap.parse_args()

    committed = Path(args.llms)
    if not committed.is_file():
        print(f"❌ 找不到 llms.txt: {committed}")
        return 1

    with tempfile.TemporaryDirectory() as td:
        fresh = Path(td) / "llms.txt"
        r = subprocess.run(
            [sys.executable, str(HERE / "gen_llms_txt.py"),
             "--matrix", args.matrix, "--out", str(fresh)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("❌ gen_llms_txt.py 执行失败：")
            print(r.stderr)
            return 1
        print(r.stdout.strip())

        a = fresh.read_text("utf-8").splitlines()
        b = committed.read_text("utf-8").splitlines()

        if a == b:
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
