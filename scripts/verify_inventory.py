#!/usr/bin/env python3
"""不变量登记表的守门人：确保「承诺 ↔ 判据 ↔ 接线」三者齐全且不互相撒谎。

## 守的是什么

矩阵反复出现的失败形态是同一个：**文档承诺由脚本强制校验 ≠ 实际有脚本在强制校验**。

实测过的三个：profile README 说「tier 不变量由 verify_split.py 强制校验」——
那个脚本没被任何 CI 引用、数据路径也已失效；
门面 README 说「不接受任何新功能」——只有事后快照守卫，而快照把违规吸收了；
`versions.json` 的 `latest`——drift 门禁明确忽略它，而全 CI 无人在查。

所以本脚本查四件事：

1. **每条不变量的判据脚本都存在**（登记了却没写，或写完被删）。
2. **每条不变量的判据脚本都被登记的 CI 文件引用**（登记了却没接线 = 承诺是假的）。
3. **每个守卫脚本都被登记**（写了却没登记 = 死代码，或忘了接进 CI）。
4. **独立 oracle 不得悄悄 import 生成器**——耦合必须显式登记并写明理由。

## 第 4 条为什么用「登记制」而不是自动判定

「import 了生成器」不等于「判定逻辑被共享」——
`verify_pending_release.py` 用 `gen_changelogs.tags_in_order` 取事实，
但它的**判定**（预写段是否丢失）与生成器的**渲染**无关。

自动判定「这次 import 是否影响了判定」需要读代码语义，不可靠。
所以本脚本强制的是**披露**：耦合必须写进登记表并给出理由，由人 review。
没披露的耦合 ⇒ 红灯。

## 通用性

登记表是数据（`scripts/invariants.json`），本脚本不绑定具体矩阵；
CI 文件路径、脚本名、job 名都从登记表读。

用法:
    verify_inventory.py
    verify_inventory.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402

# 守卫类脚本的文件名前缀——只有这些必须被登记为不变量。
GUARD_PREFIXES = ("verify_", "check_", "scan_")

# **共享层**：只取事实 / 只渲染，**不含判定**。守卫 import 它们不构成
# 「与被检对象共享判断逻辑」，因此不需要逐条披露。
#
# 为什么单独分出来：矩阵踩过两次「共享判断逻辑 ⇒ 共同盲区」的坑
# （门禁复用生成器的解析函数、逐字节门禁比的是生成器丢 body 之后的结果）。
# 解法不是「一律不许 import」，而是**把共享面分成两层**：
#   - 取数层/渲染层：共享是安全的（事实没有判定，渲染没有判定）；
#   - 生成器/判据层：共享是危险的，必须逐条披露并写明理由。
SHARED_LAYER = {
    "matrix_facts",   # 事实层：go.mod / import 图 / tag / git 历史
    "judge_report",   # 渲染层：红黄绿三档的格式化
    "repo_meta",      # 取数层：gh api 取 tag / 文件 / 树
    "matrix_paths",   # import 路径规则本体（自带独立断言 test_matrix_paths.py）
}

# **生成器/判据层**：import 它们可能把「判定逻辑」也共享出去，
# 必须逐条披露在登记表的 sharedWith 里并写明理由。
GENERATOR_STEMS = {
    "gen_changelogs", "gen_versions", "gen_llms_txt", "gen_docs",
    "breaking_index", "rewrite_imports",
    # floorBumps 的抽取规则本体：判据是「相邻两 tag 的 go.mod 差异」，
    # 独立 oracle（verify_floor_bumps.py）必须自己判——它一 import 就共享了判定。
    "gen_floor_bumps",
}

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)\s+import|import\s+([A-Za-z_][\w]*))",
                         re.M)


def imported_modules(script: Path) -> set[str]:
    """脚本 import 的本地模块名（排除标准库）。

    判据只看**同目录的本地模块**——那是矩阵内部的耦合面。
    标准库与第三方包不在这个仓库里，不构成「与被检对象共享逻辑」。
    """
    if not script.is_file():
        return set()
    text = script.read_text(encoding="utf-8", errors="ignore")
    local = {p.stem for p in script.parent.glob("*.py")}
    out = set()
    for m in _IMPORT_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name and name in local:
            out.add(name)
    return out


def selftest_invoked(ci_text: str, script: str) -> bool:
    """CI 里是否有一步**以 --selftest 调用**了这个判据脚本。

    按**行**判定：同一个文件里既有普通调用又有 --selftest 调用时，
    只有后者算数（普通调用是门禁本身，不是自测）。
    """
    for line in ci_text.splitlines():
        if f"scripts/{script}" in line and "--selftest" in line:
            return True
    return False


def judge(registry: dict, scripts_dir: Path, workflows: dict[str, str],
          report: Report) -> None:
    invariants = registry.get("invariants", [])
    retired = {r.get("script") for r in registry.get("retired", [])}
    tools = set(registry.get("notInvariants", {}).get("tools", []))

    if not invariants:
        report.red("invariants.json", "登记表是空的——等于没有登记任何不变量")
        return

    # 1 + 2：判据脚本存在且被登记的 CI 文件引用
    for inv in invariants:
        inv_id = inv.get("id", "?")
        for judge_script in inv.get("judges", []):
            if not (scripts_dir / judge_script).is_file():
                report.red(inv_id, f"判据脚本 {judge_script} 不存在",
                           "登记表与实际脚本对不上——要么补上脚本，要么撤掉这条登记")
                continue
            for ci in inv.get("ci", []):
                wf_name = ci.get("file", "")
                wf_path = workflows.get(wf_name)
                if wf_path is None:
                    report.red(inv_id, f"登记的 CI 文件 {wf_name} 找不到",
                               "路径相对矩阵根目录；确认该文件存在")
                    continue
                content = Path(wf_path).read_text(encoding="utf-8", errors="ignore")
                # 精确匹配「scripts/<name>」——用子串匹配会把
                # check_facade_release.py 误判成 release.py 被引用。
                if f"scripts/{judge_script}" not in content:
                    report.red(
                        inv_id,
                        f"判据脚本 {judge_script} 没有被 {wf_name} 引用——承诺是假的",
                        f"在 {wf_name} 里加一步：python3 scripts/{judge_script} …")
                else:
                    report.info(inv_id, f"{judge_script} 已在 {wf_name} 接线")
            # job 名存在性
            for ci in inv.get("ci", []):
                wf_path = workflows.get(ci.get("file", ""))
                if wf_path is None:
                    continue
                content = Path(wf_path).read_text(encoding="utf-8", errors="ignore")
                job = ci.get("job")
                if job and f"{job}:" not in content:
                    report.yellow(inv_id, f"登记的 job 名 {job} 在 {ci.get('file')} 里找不到",
                                  "可能是 job 已改名——登记表要跟着改")

        # 3：独立 oracle 不得悄悄 import 生成器
        declared = {s.get("module") for s in inv.get("sharedWith", [])}
        for judge_script in inv.get("judges", []):
            script = scripts_dir / judge_script
            if not script.is_file():
                continue
            coupled = imported_modules(script) & GENERATOR_STEMS
            undisclosed = coupled - declared
            if undisclosed:
                report.red(
                    inv_id,
                    f"{judge_script} import 了未登记的耦合模块：{sorted(undisclosed)}",
                    "耦合必须写进登记表的 sharedWith 并写明理由——"
                    "「门禁与被检对象共享判断逻辑」是矩阵踩过两次的坑，"
                    "披露是最低限度的防线")

    # 4：硬规则 2——每条守卫都要能自证还活着（AGENTS.md 硬规则 2）
    #
    # 「每条守卫都要有 --selftest，而且自测必须带反证样本」这条规则此前**只是文档**：
    # 登记表门禁只查「脚本存在 / 被 CI 引用 / job 名存在 / 耦合披露 / 守卫必须登记」，
    # 没有一条判「判据脚本支持 --selftest」——实测 27 个登记判据里有 4 个根本没有
    # --selftest 开关（verify_ghost_packages / scan_stale_refs / verify_docs /
    # verify_repo_docs），它们处于「一直绿、从没生效过」的高危形态且无人知晓。
    #
    # 所以这里把它变成**机器可执行的不变量**：
    #   ① 判据脚本必须支持 --selftest；
    #   ② 该自测必须在本 org 的 CI 里真的被调用一次（否则是「写了但没接线」）。
    ci_text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore")
                        for p in workflows.values())
    for inv in invariants:
        inv_id = inv.get("id", "?")
        for judge_script in inv.get("judges", []):
            script = scripts_dir / judge_script
            if not script.is_file():
                continue
            src = script.read_text(encoding="utf-8", errors="ignore")
            if "--selftest" not in src:
                report.red(
                    inv_id,
                    f"{judge_script} 没有 --selftest——硬规则 2 要求每条守卫都能自证还活着",
                    f"给这个守卫加 --selftest，并带上**故意造坏**的样本"
                    f"（「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西）")
                continue
            if not selftest_invoked(ci_text, judge_script):
                report.red(
                    inv_id,
                    f"{judge_script} 的 --selftest 没有接进任何 CI——写了但没接线",
                    f"在 CI 的 guards job 里加一步："
                    f"python3 scripts/{judge_script} --selftest")

    # 5：每个守卫脚本都必须被登记
    registered: set[str] = set()
    for inv in invariants:
        registered.update(inv.get("judges", []))
    for script in sorted(scripts_dir.glob("*.py")):
        name = script.name
        if not name.startswith(GUARD_PREFIXES):
            continue
        if name in registered or name in retired:
            continue
        if name in tools:
            continue
        report.red(
            name,
            "守卫脚本没有任何不变量登记它——它是死代码，或忘了接进 CI",
            "要么为它写一条登记（claim + judges + ci），要么登记进 retired 说明被谁取代，"
            "要么删掉它——「写了但没接线」的门禁会给人虚假的安全感")


def selftest() -> int:
    """反证自测：登记表的失配都必须被抓出来。

    覆盖六类（每一类都带**故意造坏**的样本）：
      ① 未登记的守卫脚本        → 判红
      ② 未披露的生成器耦合      → 判红
      ③ 判据脚本没有 --selftest → 判红（硬规则 2）
      ④ --selftest 没接进 CI   → 判红（硬规则 2）
      ⑤ 全部合规                → 不报
      ⑥ 补齐后                  → 转绿
    """
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        scripts = root / "scripts"
        scripts.mkdir()
        # 合规的判据脚本：有 --selftest 开关
        (scripts / "verify_ok.py").write_text(
            "import sys\nfrom pathlib import Path\nimport json\n"
            "ap = None  # --selftest\n", encoding="utf-8")
        (scripts / "verify_coupled.py").write_text(
            "import gen_changelogs\nfrom matrix_facts import git\n"
            "ap = None  # --selftest\n", encoding="utf-8")
        # 反证样本：没有 --selftest 开关的判据脚本
        (scripts / "verify_noselftest.py").write_text("print('x')\n", encoding="utf-8")
        # 反证样本：有 --selftest，但 CI 里没调用它
        (scripts / "verify_unwired.py").write_text(
            "ap = None  # --selftest\n", encoding="utf-8")
        (scripts / "verify_orphan.py").write_text(
            "print('x')\nap = None  # --selftest\n", encoding="utf-8")
        (scripts / "gen_changelogs.py").write_text("x = 1\n", encoding="utf-8")

        wf = root / "ci.yml"
        wf.write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_ok.py --selftest\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n",
            encoding="utf-8")

        registry = {
            "invariants": [
                {"id": "ok", "judges": ["verify_ok.py"], "oracle": "independent",
                 "sharedWith": [], "ci": [{"file": "ci.yml", "job": "a"}]},
                {"id": "coupled", "judges": ["verify_coupled.py"], "oracle": "independent",
                 "sharedWith": [], "ci": [{"file": "ci.yml", "job": "a"}]},
                {"id": "noselftest", "judges": ["verify_noselftest.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a"}]},
                {"id": "unwired", "judges": ["verify_unwired.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a"}]},
            ],
            "retired": [],
            "notInvariants": {"tools": ["gen_changelogs.py"]},
        }
        reg_path = root / "invariants.json"
        reg_path.write_text(json.dumps(registry), encoding="utf-8")

        rep = Report(title="不变量登记表")
        judge(registry, scripts, {"ci.yml": str(wf)}, rep)
        subjects = {f.subject for f in rep.reds}
        check("未登记的守卫被抓出", "verify_orphan.py" in subjects, True)
        check("未披露的耦合被抓出", "coupled" in subjects, True)
        check("已登记且合规的不报", "ok" in subjects, False)
        # ③ 硬规则 2：没有 --selftest 的判据脚本必须判红
        check("没有 --selftest 的判据被抓出", "noselftest" in subjects, True)
        # ④ 硬规则 2：--selftest 没接进 CI 必须判红
        check("--selftest 未接线的判据被抓出", "unwired" in subjects, True)

        # 反证：把 --selftest 的 CI 接线去掉 ⇒ 必须转红（证明这条判定有区分力）
        wf.write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n",
            encoding="utf-8")
        rep_x = Report(title="不变量登记表")
        judge(registry, scripts, {"ci.yml": str(wf)}, rep_x)
        check("抽掉 --selftest 接线 → 判红", rep_x.has_red(), True)

        # 披露之后应当转绿（并把两个反证样本补齐）
        registry["invariants"][1]["sharedWith"] = [
            {"module": "gen_changelogs", "why": "只取事实"},
            {"module": "matrix_facts", "why": "只取事实"},
        ]
        # 把孤儿脚本登记进不变量**并且**接进 CI（两者缺一都会红）
        registry["invariants"][0]["judges"].append("verify_orphan.py")
        registry["invariants"][0]["ci"].append({"file": "ci.yml", "job": "a"})
        # 反证样本补齐：给它们加上 --selftest 开关并接进 CI
        (scripts / "verify_noselftest.py").write_text(
            "ap = None  # --selftest\n", encoding="utf-8")
        wf.write_text("jobs:\n  a:\n    steps:\n"
                      "      - run: python3 scripts/verify_ok.py\n"
                      "      - run: python3 scripts/verify_ok.py --selftest\n"
                      "      - run: python3 scripts/verify_coupled.py --selftest\n"
                      "      - run: python3 scripts/verify_orphan.py --selftest\n"
                      "      - run: python3 scripts/verify_noselftest.py --selftest\n"
                      "      - run: python3 scripts/verify_unwired.py --selftest\n",
                      encoding="utf-8")
        rep2 = Report(title="不变量登记表")
        judge(registry, scripts, {"ci.yml": str(wf)}, rep2)
        check("披露后转绿", rep2.has_red(), False)

    if fails:
        print("❌ verify_inventory 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_inventory 自测通过"
          "（未登记 / 未接线 / 未披露耦合 / 无 --selftest / --selftest 未接线 五类检出）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="不变量登记表的守门人")
    ap.add_argument("--registry", default=str(HERE / "invariants.json"))
    ap.add_argument("--root", default=str(HERE.parent.parent),
                    help="矩阵根目录（用于解析登记的 CI 文件路径）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    reg_path = Path(args.registry)
    if not reg_path.is_file():
        print(f"❌ 找不到登记表 {reg_path}")
        return 2
    registry = json.loads(reg_path.read_text(encoding="utf-8"))

    root = Path(args.root).resolve()
    scripts_dir = reg_path.parent

    workflows: dict[str, str] = {}
    for name in ("matrix-tools/.github/workflows/matrix-tools-ci.yml",
                 ".github/.github/workflows/repo-ci.yml"):
        p = root / name
        if p.is_file():
            workflows[name] = str(p)

    report = Report(title="不变量登记表")
    judge(registry, scripts_dir, workflows, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
