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


def generate_for(repos: list[str], gen_root: Path, gen_script: Path | None = None) -> None:
    """在 gen_root 里生成参照文档（gen_docs.py 的骨架）。

    先把仓目录建出来——gen_docs.py 只为**已存在**的工作目录生成。

    `gen_script` 只给自测用（传一个必定失败的脚本，验证「生成器坏了」会被
    明确失败而不是当成漂移）——**绝不改动仓库里的真 gen_docs.py**。

    ⚠️ **必须检查生成器的退出码**（实测 2026-10-31）：
    旧实现不检查 ⇒ 生成器崩了（语法错、modules.json 坏了、磁盘满…）时
    生成目录是**空的** ⇒ 后续每个文档都记「漂移」但**差异行数 = 0**
    （因为参照文件不存在 ⇒ 算不出差异）⇒
    **「生成器坏了」与「文档确实漂移」在报告里长得一模一样**，
    而报告器恰恰是给人看方向用的 ⇒ 这个区分必须保住。
    """
    for r in repos:
        (gen_root / r).mkdir(parents=True, exist_ok=True)
    script = gen_script or (HERE / "gen_docs.py")
    p = subprocess.run([sys.executable, str(script), "--out-root", str(gen_root)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"gen_docs.py 执行失败（退出码 {p.returncode}）——"
            f"这不是「文档漂移」，是**生成器坏了**，报告无法判定：\n"
            f"{(p.stderr or p.stdout or '').strip()[:2000]}")


# 注：原先的 compare_repo() 已并入 judge()——它与 judge 各算一遍「缺文档就跳过」的
# 口径，留着两份就多一处可能漂移的地方（且它不统计「实际检查量」）。


def judge(mirror: Path, repos: list[str],
          gen_script: Path | None = None) -> tuple[list[tuple[str, str, int, int]], dict]:
    """判定本体。返回 (漂移列表, 统计)。

    漂移列表：[(仓, 文件, 差异行数, 生成物行数)]
    统计：{"checked": 实际比对的文档数, "missing": 仓里没有该文档的数量,
          "expected": 应比对的文档数（仓数 × 文件数）}

    `gen_script` 只给自测用（注入一个必定失败的生成器，验证「生成器坏了」
    会被明确失败而不是当成漂移）——不改动仓库里的真 gen_docs.py。

    抽成独立函数是为了能离线自测——判据必须能对着**故意造坏**的样本跑
    （matrix-tools/AGENTS.md 硬规则 2）。

    ## 为什么统计里要带「实际检查量」（实测 2026-10-31）

    旧实现直接报「检查 仓数 × 2 = 26 个文档」，但**仓里没有该文档时是 `continue` 跳过**
    ⇒ 实际比对的可能只有 13 个 ⇒ 报告口径与事实不符（说 26 查了 13）。

    「仓里没有文档」**不算漂移**（生成器会给新仓造骨架，仓里没有的文档说明它
    不是本守卫的靶子）——但它必须**单独可见**：缺失同样是「文档层没到位」，
    静默跳过就变成了「判不了 ≠ 没问题」。
    """
    with tempfile.TemporaryDirectory() as td:
        gen_root = Path(td)
        generate_for(repos, gen_root, gen_script=gen_script)
        drift: list[tuple[str, str, int, int]] = []
        checked = 0
        missing = 0
        for r in repos:
            for f in FILES:
                b = mirror / r / f
                if not b.is_file():
                    missing += 1
                    continue
                checked += 1
                a = gen_root / r / f
                if a.is_file() and a.read_bytes() == b.read_bytes():
                    continue
                n = 0
                if a.is_file():
                    res = subprocess.run(["diff", "-u", str(b), str(a)],
                                         capture_output=True, text=True).stdout
                    n = sum(1 for l in res.splitlines()
                            if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
                glen = len(a.read_text("utf-8").splitlines()) if a.is_file() else 0
                drift.append((r, f, n, glen))
    stats = {"checked": checked, "missing": missing, "expected": len(repos) * len(FILES)}
    return drift, stats


def exit_code_for(n_drift: int, fail_on_drift: bool) -> int:
    """本守卫的退出码语义（**报告器**：恒不失败，除非显式要求）。

    抽成纯函数是为了让「契约」也能被自测钉住——否则「报告器却 return 1」
    这种矛盾只能靠人读代码发现（实测 2026-10-31：现场已经被人工调和过一次，
    因为手动跑它的人拿到 exit 1，而 CI 靠 continue-on-error 兜住所以不红）。
    """
    return 1 if (fail_on_drift and n_drift > 0) else 0


def selftest() -> int:
    """反证自测：拿**故意造坏**的文档跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设（matrix-tools/AGENTS.md 硬规则 2）。样本覆盖：

      ① 文档与生成器逐字节一致   → 必须不报
      ② 文档被改过（多一行）      → 必须报出，且能算出差异行数
      ③ 文档被整个换掉            → 必须报出
      ④ 仓里没有该文档            → 不计漂移，但**单独计数**（2026-10-31）
      ⑤ 仓目录不存在              → 同上
      ⑥ **生成器坏了**            → 必须明确失败，不得当成漂移（2026-10-31）

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
        drift, _st = judge(mirror, [repo])
        check("① 一致不报", drift, [])

        # ② 多一行 → 报出，且差异行数 > 0
        (mirror / repo / "README.md").write_bytes(original + "\n手写补充的一行\n".encode("utf-8"))
        drift2, _st = judge(mirror, [repo])
        check("② 改过要报出", len(drift2), 1)
        check("② 报出的是 README.md", drift2[0][1] if drift2 else None, "README.md")
        check("② 差异行数为正", drift2[0][2] > 0 if drift2 else False, True)

        # ③ 整个换掉 → 报出
        (mirror / repo / "README.md").write_bytes("完全不是生成物\n".encode("utf-8"))
        drift3, _st = judge(mirror, [repo])
        check("③ 换掉要报出", len(drift3), 1)

        # ④ 仓里没有该文档 → 不计漂移，但**单独计数**（实测 2026-10-31）
        #    旧实现静默 continue ⇒ 报告说「检查 26 个」实际只查了 13 个 ⇒
        #    口径与事实不符，且「缺失」这件事完全不可见。
        (mirror / repo / "README.md").unlink()
        drift4, st4 = judge(mirror, [repo])
        check("④ 缺文档不计漂移", drift4, [])
        # 自测的 mirror 里原本只造了 README.md ⇒ 删掉它后**两个**文档都缺
        check("④ 缺文档被单独计数", st4["missing"], 2)
        check("④ 实际检查量不含缺失的", st4["checked"], 0)
        check("④ 应检查量 = 仓数 × 文件数", st4["expected"], 2)

        # ⑤ 仓目录不存在 → 跳过（且计入 missing）
        drift5, st5 = judge(mirror, ["nosuchrepo"])
        check("⑤ 仓不存在跳过", drift5, [])
        check("⑤ 仓不存在计入缺失", st5["missing"], 2)

        # ⑥ 生成器坏了 ⇒ 必须**明确失败**，且与「文档漂移」可区分（实测 2026-10-31）
        #    旧实现不检查 gen_docs.py 的退出码 ⇒ 生成目录是空的 ⇒ 每个文档都记
        #    「漂移」且**差异行数 = 0** ⇒ 「生成器坏了」与「文档漂移」长得一模一样。
        #    ⚠️ 用**注入**一个必定失败的脚本，**绝不改动仓库里的真 gen_docs.py**。
        broken = td / "broken_gen_docs.py"
        broken.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        raised = False
        try:
            judge(mirror, [repo], gen_script=broken)
        except RuntimeError:
            raised = True
        check("⑥ 生成器坏了必须明确失败（不得当成漂移）", raised, True)

        # ⑦ **退出码契约**（报告器：恒不失败，除非显式要求）
        #    实测 2026-10-31：旧实现有漂移就 return 1，与登记表的「报告器」矛盾 ⇒
        #    手动跑它的人拿到 exit 1（现场已人工调和过一次），而 CI 靠
        #    continue-on-error 兜住所以不红 ⇒ 契约不一致长期没人察觉。
        check("⑦ 有漂移但默认不失败（报告器语义）", exit_code_for(5, False), 0)
        check("⑦ 无漂移不失败", exit_code_for(0, False), 0)
        check("⑦ 显式 --fail-on-drift 才失败", exit_code_for(5, True), 1)
        check("⑦ 显式要求但无漂移仍不失败", exit_code_for(0, True), 0)

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
          "（漂移必报；一致不报；缺文档/缺仓不计漂移但单独计数；"
          "生成器坏了明确失败；报告器退出码契约；只报告不覆盖）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", help="各仓工作目录的父目录")
    ap.add_argument("--repo", action="append", help="只查指定仓（可多次）")
    ap.add_argument("--verbose", action="store_true", help="打印 diff")
    ap.add_argument("--max-lines", type=int, default=12)
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    ap.add_argument("--fail-on-drift", action="store_true",
                    help="把「有漂移」升级为退出码非 0（默认是**报告器**：恒不失败，"
                         "产物留档；见 scripts/invariants.json 的 repo-docs 登记）")
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

    try:
        drift, stats = judge(mirror, repos)
    except RuntimeError as e:
        # 生成器坏了 ⇒ **判不了**，不是「有漂移」。必须明确失败（≠ 绿）。
        print(f"\n❌ {e}")
        return 1

    print(f"\n检查 {len(repos)} 个仓 × {len(FILES)} 个文档："
          f"实际比对 {stats['checked']} 个"
          + (f"，仓里缺失 {stats['missing']} 个（缺失不计漂移，但单独列出）"
             if stats["missing"] else "")
          + f"，漂移 {len(drift)} 个")
    if not drift:
        print("✅ 全部与生成器一致")
        return 0

    print("\n⚠️  以下文档与 gen_docs.py 的输出不一致：")
    for r, f, n, glen in drift:
        print(f"   {r:<12}{f:<12}差异 {n:>4} 行（生成物 {glen} 行）")
        if args.verbose:
            # 临时目录用完即删（旧的 mkdtemp 从不清理 ⇒ 每跑一次泄漏一个目录）
            with tempfile.TemporaryDirectory() as tdir:
                gen_root = Path(tdir)
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
    # **报告器语义**：默认恒不失败（与 scripts/invariants.json 的 repo-docs 登记一致）。
    #
    # 旧实现这里 return 1 ⇒ 与登记表「报告器：恒不失败」矛盾
    # （CI 靠 continue-on-error 兜住，所以 CI 不红；但任何手动跑它的人拿到的是
    #  exit 1 ⇒ 现场已经被人工调和过一次，见 .oagentkit-cron-state.json 的存档）。
    # ⇒ 契约与实现对齐：默认 0；要硬失败请显式 --fail-on-drift。
    rc = exit_code_for(len(drift), args.fail_on_drift)
    if rc == 0:
        print("   （本守卫是**报告器**：默认恒不失败，产物留档；要硬失败请加 --fail-on-drift）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
