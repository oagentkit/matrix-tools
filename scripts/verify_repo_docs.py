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
    verify_repo_docs.py --selftest
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("README.md", "AGENTS.md")


def generate_for(repos: list[str], gen_root: Path) -> None:
    """在 gen_root 里生成参照文档（gen_docs.py 的骨架）。

    先把仓目录建出来——gen_docs.py 只为**已存在**的工作目录生成。
    """
    for r in repos:
        (gen_root / r).mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(HERE / "gen_docs.py"), "--out-root", str(gen_root)],
                   capture_output=True, text=True)


def compare_repo(repo: str, mirror: Path, gen_root: Path) -> list[tuple[str, int, int]]:
    """比对一个仓的两个文档。返回 [(文件名, 差异行数, 生成物行数)]。

    仓里没有这个文件 ⇒ 跳过（不算漂移：生成器会给新仓造骨架，
    但仓里没有的文档说明它不是本守卫的靶子）。
    """
    out: list[tuple[str, int, int]] = []
    for f in FILES:
        a, b = gen_root / repo / f, mirror / repo / f
        if not b.is_file():
            continue
        if a.is_file() and a.read_bytes() == b.read_bytes():
            continue
        n = 0
        if a.is_file():
            res = subprocess.run(["diff", "-u", str(b), str(a)],
                                 capture_output=True, text=True).stdout
            n = sum(1 for l in res.splitlines()
                    if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
        glen = len(a.read_text("utf-8").splitlines()) if a.is_file() else 0
        out.append((f, n, glen))
    return out


def judge(mirror: Path, repos: list[str]) -> list[tuple[str, str, int, int]]:
    """判定本体。返回 [(仓, 文件, 差异行数, 生成物行数)]。

    抽成独立函数是为了能离线自测——判据必须能对着**故意造坏**的样本跑
    （matrix-tools/AGENTS.md 硬规则 2）。
    """
    with tempfile.TemporaryDirectory() as td:
        gen_root = Path(td)
        generate_for(repos, gen_root)
        drift: list[tuple[str, str, int, int]] = []
        for r in repos:
            for f, n, glen in compare_repo(r, mirror, gen_root):
                drift.append((r, f, n, glen))
    return drift


def selftest() -> int:
    """反证自测：拿**故意造坏**的文档跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设（matrix-tools/AGENTS.md 硬规则 2）。样本覆盖：

      ① 文档与生成器逐字节一致   → 必须不报
      ② 文档被改过（多一行）      → 必须报出，且能算出差异行数
      ③ 文档被整个换掉            → 必须报出
      ④ 仓里没有该文档            → 必须跳过（不算漂移）
      ⑤ 仓目录不存在              → 必须跳过（不算漂移）

    另外守住本守卫最核心的承诺：**只报告，永不覆盖**——
    跑完之后被检文档必须逐字节不变（手写内容被抹掉是它存在的理由）。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    matrix = HERE.parent / "modules.json"
    if not matrix.is_file():
        print(f"❌ 自测需要 modules.json: {matrix}")
        return 1
    repo = json.loads(matrix.read_text("utf-8"))["modules"][0]["repo"]

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mirror = td / "mirror"
        # 先生成参照，再把它当成「仓里的文档」——逐字节一致是基线
        gen_root = td / "gen"
        generate_for([repo], gen_root)
        src = gen_root / repo / "README.md"
        if not src.is_file():
            print(f"❌ 自测无法生成 {repo}/README.md 参照")
            return 1
        original = src.read_bytes()

        (mirror / repo).mkdir(parents=True)
        (mirror / repo / "README.md").write_bytes(original)

        # ① 一致 → 不报
        drift = judge(mirror, [repo])
        check("① 一致不报", drift, [])

        # ② 多一行 → 报出，且差异行数 > 0
        (mirror / repo / "README.md").write_bytes(original + "\n手写补充的一行\n".encode("utf-8"))
        drift2 = judge(mirror, [repo])
        check("② 改过要报出", len(drift2), 1)
        check("② 报出的是 README.md", drift2[0][1] if drift2 else None, "README.md")
        check("② 差异行数为正", drift2[0][2] > 0 if drift2 else False, True)

        # ③ 整个换掉 → 报出
        (mirror / repo / "README.md").write_bytes("完全不是生成物\n".encode("utf-8"))
        drift3 = judge(mirror, [repo])
        check("③ 换掉要报出", len(drift3), 1)

        # ④ 仓里没有该文档 → 跳过
        (mirror / repo / "README.md").unlink()
        drift4 = judge(mirror, [repo])
        check("④ 缺文档跳过", drift4, [])

        # ⑤ 仓目录不存在 → 跳过
        drift5 = judge(mirror, ["nosuchrepo"])
        check("⑤ 仓不存在跳过", drift5, [])

        # 核心承诺：只报告，永不覆盖
        (mirror / repo / "README.md").write_bytes(original + "\n手写补充的一行\n".encode("utf-8"))
        before = (mirror / repo / "README.md").read_bytes()
        judge(mirror, [repo])
        after = (mirror / repo / "README.md").read_bytes()
        check("只报告不覆盖：被检文档逐字节不变", after == before, True)

    if fails:
        print("❌ verify_repo_docs 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_repo_docs 自测通过"
          "（漂移必报；一致/缺文档/缺仓不报；只报告不覆盖）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", help="各仓工作目录的父目录")
    ap.add_argument("--repo", action="append", help="只查指定仓（可多次）")
    ap.add_argument("--verbose", action="store_true", help="打印 diff")
    ap.add_argument("--max-lines", type=int, default=12)
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.mirror:
        print("❌ 缺少 --mirror（或用 --selftest）")
        return 1

    mirror = Path(args.mirror).resolve()
    gen_docs = HERE / "gen_docs.py"
    if not gen_docs.is_file():
        print(f"❌ 找不到 {gen_docs}")
        return 1

    # 仓列表取 modules.json 的 modules（权威），
    # 不用「目录里有 go.mod」这种猜法——否则 facade/infra 仓会被误算进来。
    matrix = HERE.parent / "modules.json"
    if args.repo:
        repos = args.repo
    elif matrix.is_file():
        repos = [m["repo"] for m in json.loads(matrix.read_text("utf-8"))["modules"]]
    else:
        repos = sorted(p.name for p in mirror.iterdir()
                       if p.is_dir() and (p / "go.mod").is_file()
                       and not p.name.startswith("."))

    drift = judge(mirror, repos)

    total = len(repos) * len(FILES)
    print(f"\n检查 {len(repos)} 个仓 × {len(FILES)} 个文档 = {total}，"
          f"漂移 {len(drift)} 个")
    if not drift:
        print("✅ 全部与生成器一致")
        return 0

    print("\n⚠️  以下文档与 gen_docs.py 的输出不一致：")
    for r, f, n, glen in drift:
        print(f"   {r:<12}{f:<12}差异 {n:>4} 行（生成物 {glen} 行）")
        if args.verbose:
            gen_root = Path(tempfile.mkdtemp())
            generate_for([r], gen_root)
            b = mirror / r / f
            a = gen_root / r / f
            if a.is_file():
                c = 0
                for line in difflib.unified_diff(
                        b.read_text("utf-8").splitlines(),
                        a.read_text("utf-8").splitlines(),
                        "committed", "generated", lineterm="", n=0):
                    if line.startswith(("+++", "---", "@@")):
                        continue
                    print(f"    {r}/{f}: {line[:160]}")
                    c += 1
                    if c >= args.max_lines:
                        print("    …")
                        break

    print("\n⚠️  注意：多数情况下**仓库版本更丰富**（含手写的工程纪律，如 queue 的选主红线）。")
    print("   gen_docs.py 只生成骨架，直接覆盖会抹掉手写内容——本脚本因此不提供 --fix。")
    print("   处理方向：把差异并回 modules.json 让生成器产出同样内容，或把生成范围限定到骨架段落。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
