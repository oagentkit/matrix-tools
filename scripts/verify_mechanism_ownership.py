#!/usr/bin/env python3
"""八机制归属表守卫：`modules.json` 的 `mechanisms` 字段 + 撞名仓 `qualifier` 消歧。

## 守的是哪条缺口（matrix-tools#255，评审 2.3-问题一）

三层（Agent / Runtime / Kernel）各自实现生命周期、超时、重试……时，
多套相似机制就会冒出来。**根因是归属只写在 ADR 散文里——没有判定**，
所以「新增一个机制包」时没有任何东西拦你先在错误的层写一份。

本守卫把归属变成机器可判的三件事（全部只读 `modules.json`，不 import 生成器）：

1. **表自洽**：八机制 id 齐全不重复；`owners/claims/consumers/keywords/note`
   齐全；`owners/claims` 指向真实存在的 `repo/pkg`；`consumers` 指向真实仓；
   `exempt` 必须带 `reason`（豁免是记录一次判断，不是免死金牌）。
2. **关键词网**：包路径（`repo/pkg` 小写）命中某机制的 `keywords` ⇒
   该包必须在该机制的 `owners/claims/exempt` 里登记归属，否则**判红**。
   —— 这就是验收里的「新增机制包若八机制表中无归属 ⇒ 判红」。
3. **撞名消歧**：一个仓名是另一个的后缀（`runtime` ⊂ `goagent-runtime`）
   ⇒ 双方都必须带非空 `qualifier`。命名撞车走**消歧字段**而不是改名
   （改名 = 跨 org breaking + 历史 tag/文档全改；ADR §三裁决）。

## 与其它守卫的分工（判定不共享）

- `verify_import_direction.py` 对账 `dependsOn` ↔ 真实 import（声明层）；
  本守卫**不碰 import 图**——`consumers` 是 ADR 落笔时的 grep 实测快照，
  机器面只校验「指向的仓存在」（防笔误），import 对账仍归 import direction。
- `scan_stale_refs.py` 扫文档里的死包路径；本守卫扫数据源里的活包归属。

## 关键词网的边界（诚实声明）

关键词按**包名**匹配——抓「名字长得像机制」的包，抓不住名字不带词的语义重复
（那是 review 的活）。所以：漏报靠关键词维护，误报靠 `exempt` 带理由豁免。
判据只在 modules.json 自身上做（oracle independent，无需克隆矩阵）。

用法:
    verify_mechanism_ownership.py --matrix modules.json
    verify_mechanism_ownership.py --selftest

退出码: 0 = 干净（黄不计）；1 = 有红。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402

DEFAULT_MATRIX = HERE.parent / "modules.json"

# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# ⚠️ 这组参数必须**无副作用**、且能让脚本**真正走完入口**（不能只让 argparse 早退）。
SMOKE_ARGS = ["--matrix", "@repo/matrix-tools/modules.json"]

# 评审承诺的八机制。删一个 ⇒ 红（改承诺面要过 review，不许静默缩表）。
# 加机制本身不红（新机制走 info），但新增后关键词网自动生效。
EXPECTED_MECHANISM_IDS = (
    "lifecycle", "timeout", "retry", "failure",
    "state", "scheduling", "events", "recovery",
)

REQUIRED_TEXT = ("name", "note")


# ---------------------------------------------------------------- 判定


def _inventory(data: dict) -> dict[str, set[str]]:
    """modules.json → {仓: packages 集合}（org 不参与判定，仓名在矩阵内唯一）。"""
    out: dict[str, set[str]] = {}
    for m in data.get("modules") or []:
        repo = m.get("repo") or ""
        if repo:
            out[repo] = set(m.get("packages") or [])
    return out


def _pkg_exists(pkg: str, inv: dict[str, set[str]]) -> bool:
    if "/" not in pkg:
        return False
    repo, _, rest = pkg.partition("/")
    return rest in inv.get(repo, set())


def judge(data: dict, report: Report,
          expected_ids: tuple[str, ...] = EXPECTED_MECHANISM_IDS) -> None:
    inv = _inventory(data)
    if not inv:
        report.red("modules.json", "modules 为空——判定面（包清单）不存在")
        return

    mechs = data.get("mechanisms")
    if not isinstance(mechs, list) or not mechs:
        report.red("mechanisms", "modules.json 缺 mechanisms 八机制归属表（字段缺失或为空）",
                   "归属不能只活在 ADR 散文里——补 mechanisms 字段，见 docs/adr/ADR-001")
        return

    # -- 1. 表自洽 -------------------------------------------------------
    seen: set[str] = set()
    registered: dict[str, set[str]] = {}   # 机制 id → 已登记包（小写 repo/pkg）
    for mech in mechs:
        mid = mech.get("id") or "?"
        if not isinstance(mid, str) or not mid or mid == "?":
            report.red("mechanisms", f"机制条目缺 id：{mech!r}",
                       "每条机制必须有稳定 id（gen/守卫都按 id 引用）")
            continue
        if mid in seen:
            report.red(mid, "机制 id 重复——归属表按 id 单点引用，重复即歧义",
                       "删掉重复条目，或合并两条机制")
        seen.add(mid)

        for field in REQUIRED_TEXT:
            if not isinstance(mech.get(field), str) or not mech.get(field, "").strip():
                report.red(mid, f"缺 {field}（非空字符串）",
                           "归属表每条都要能自解释：name 是机制名，note 是归属理由/边界")

        owners = mech.get("owners")
        if not isinstance(owners, list) or not owners:
            report.red(mid, "owners 为空——没有单源的机制不进表",
                       "每个判据面标注唯一单源仓（repo/pkg + scope 说明判据面）")
            owners = []

        claims = mech.get("claims") or []
        exempt = mech.get("exempt") or []
        consumers = mech.get("consumers") or []
        keywords = mech.get("keywords") or []

        if not isinstance(keywords, list) or not keywords or \
                not all(isinstance(k, str) and k.strip() for k in keywords):
            report.red(mid, "keywords 为空或含空值——关键词网张不开",
                       "至少一个包名关键词（小写），否则该机制的归属网永远不生效")

        reg: set[str] = set()
        for o in owners:
            if not isinstance(o, dict):
                report.red(mid, f"owners 条目必须是对象：{o!r}")
                continue
            scope = o.get("scope")
            pkg = o.get("pkg")
            if not isinstance(scope, str) or not scope.strip():
                report.red(mid, "owners[].scope 为空——判据面没说清是哪个面",
                           "scope 写清这个单源裁的是哪个判据（例：可重试性判定）")
            if not isinstance(pkg, str) or not _pkg_exists(pkg, inv):
                report.red(mid, f"owner 包不存在：{pkg!r}",
                           "包必须真实出现在 modules.json 的 packages 清单里（防陈旧引用）")
            elif isinstance(pkg, str):
                reg.add(pkg.lower())

        for c in claims:
            if not isinstance(c, str) or not _pkg_exists(c, inv):
                report.red(mid, f"claim 包不存在：{c!r}",
                           "claims 同样只许指向矩阵内真实存在的包")
            else:
                reg.add(c.lower())

        for e in exempt:
            if not isinstance(e, dict):
                report.red(mid, f"exempt 条目必须是对象：{e!r}")
                continue
            pkg = e.get("pkg")
            if not isinstance(pkg, str) or not _pkg_exists(pkg, inv):
                report.red(mid, f"exempt 包不存在：{pkg!r}")
            else:
                reg.add(pkg.lower())
            if not isinstance(e.get("reason"), str) or not e.get("reason", "").strip():
                report.red(mid, f"exempt 缺 reason：{pkg!r}",
                           "豁免必须写清为什么它词面命中但不属于本机制（防免死金牌）")

        for c in consumers:
            if not isinstance(c, str) or c not in inv:
                report.red(mid, f"consumer 仓不存在：{c!r}",
                           "consumers 指向矩阵内的仓名（import 对账归 verify_import_direction）")

        registered[mid] = reg

    # -- 2. 关键词网（包名命中 ⇒ 必须登记归属）----------------------------
    for mech in mechs:
        mid = mech.get("id") or "?"
        kws = [k.lower() for k in (mech.get("keywords") or []) if isinstance(k, str)]
        if mid not in registered or not kws:
            continue
        reg = registered[mid]
        for repo, pkgs in sorted(inv.items()):
            for pkg in sorted(pkgs):
                full = f"{repo}/{pkg}".lower()
                hit = next((k for k in kws if k in full), None)
                if hit and full not in reg:
                    report.red(
                        f"{repo}/{pkg}",
                        f"命中机制 {mid} 的关键词「{hit}」但未登记归属",
                        f"加进 mechanisms[{mid}] 的 claims（同机制）"
                        f"或 exempt 写 reason（词面命中但不同机制）")

    # -- 3. 八机制承诺 ---------------------------------------------------
    for mid in expected_ids:
        if mid not in seen:
            report.red(mid, "评审承诺的机制被删——八机制归属表必须齐全",
                       "删机制/改承诺面要过 review；只许增不许静默减")
    extra = [m.get("id") for m in mechs if m.get("id") not in expected_ids]
    if extra:
        report.info("mechanisms", f"超出八机制承诺的新增条目：{sorted(x for x in extra if x)}",
                    "新增机制请同步更新本脚本的 EXPECTED_MECHANISM_IDS（改承诺面留痕）")

    # -- 4. 撞名仓 qualifier 消歧 ----------------------------------------
    repos = sorted(inv)
    for i, a in enumerate(repos):
        for b in repos[i + 1:]:
            if not (b.endswith(a) or a.endswith(b)):
                continue
            for r, other in ((a, b), (b, a)):
                mod = next((m for m in (data.get("modules") or [])
                            if m.get("repo") == r), {})
                q = mod.get("qualifier")
                if not isinstance(q, str) or not q.strip():
                    report.red(
                        r,
                        f"与 {other} 构成仓名后缀撞名，但缺 qualifier 消歧字段",
                        "短名撞车走消歧字段不改名（ADR §三裁决）——"
                        "两个仓都要写 qualifier，gen_llms_txt 会渲染出来")


# ---------------------------------------------------------------- 自测


def _fixture() -> dict:
    """合法基线：八机制齐全、关键词命中的包都有归属、撞名对都带 qualifier。"""
    def mech(mid: str, **over) -> dict:
        base = {
            "id": mid,
            "name": mid,
            "owners": [{"scope": "fixture 判据面", "pkg": "demo/core"}],
            "claims": [],
            "consumers": ["runtime"],
            "keywords": [mid],
            "exempt": [],
            "note": "fixture",
        }
        base.update(over)
        return base

    return {
        "modules": [
            {"repo": "runtime", "packages": ["pacer"],
             "qualifier": "进程运行时机制件（tier0）——非 goagent-runtime"},
            {"repo": "goagent-runtime", "packages": ["agentloop"],
             "qualifier": "Agent 应用运行时装配层（tier5）——非 oagentkit/runtime"},
            {"repo": "demo", "packages": ["core", "retryx"]},
        ],
        "mechanisms": [
            mech("lifecycle"),
            mech("timeout"),
            mech("retry", keywords=["retry"], claims=["demo/retryx"]),
            mech("failure"),
            mech("state"),
            mech("scheduling"),
            mech("events"),
            mech("recovery"),
        ],
    }


def _run_judge(data: dict, expected=EXPECTED_MECHANISM_IDS) -> Report:
    rep = Report(title="八机制归属")
    judge(data, rep, expected_ids=expected)
    return rep


def _red_subjects(rep: Report) -> set[str]:
    return {f.subject for f in rep.reds}


def selftest() -> int:
    """反证自测：每种违规形态都必须让自测转红（有区分力的断言）。"""
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # ① 合法基线 ⇒ 零红
    base = _fixture()
    rep = _run_judge(base)
    check("合法基线零红", rep.reds, [])

    # ② 无归属的新机制包 ⇒ 红，且点名到包
    import copy
    data = copy.deepcopy(base)
    for m in data["mechanisms"]:
        if m["id"] == "retry":
            m["claims"] = []
    rep = _run_judge(data)
    check("无归属机制包判红", rep.has_red(), True)
    check("红点名到未归属包", "demo/retryx" in _red_subjects(rep), True)

    # ③ owner 包陈旧（不存在）⇒ 红
    data = copy.deepcopy(base)
    data["mechanisms"][0]["owners"][0]["pkg"] = "demo/nothere"
    rep = _run_judge(data)
    check("陈旧 owner 判红", any("owner 包不存在" in f.message for f in rep.reds), True)

    # ④ 缺 mechanisms 字段 ⇒ 红（表不能只活在散文里）
    data = copy.deepcopy(base)
    del data["mechanisms"]
    rep = _run_judge(data)
    check("缺 mechanisms 判红", rep.has_red(), True)

    # ⑤ 机制 id 重复 ⇒ 红
    data = copy.deepcopy(base)
    data["mechanisms"].append(copy.deepcopy(data["mechanisms"][0]))
    rep = _run_judge(data)
    check("重复 id 判红", any("id 重复" in f.message for f in rep.reds), True)

    # ⑥ 删掉评审承诺的机制 ⇒ 红（承诺面不许静默缩）
    data = copy.deepcopy(base)
    data["mechanisms"] = [m for m in data["mechanisms"] if m["id"] != "timeout"]
    rep = _run_judge(data)
    check("删机制判红", "timeout" in _red_subjects(rep), True)

    # ⑦ keywords 空 ⇒ 红（关键词网张不开）
    data = copy.deepcopy(base)
    data["mechanisms"][0]["keywords"] = []
    rep = _run_judge(data)
    check("空 keywords 判红", any("keywords" in f.message for f in rep.reds), True)

    # ⑧ exempt 没 reason ⇒ 红（豁免不是免死金牌）
    data = copy.deepcopy(base)
    for m in data["mechanisms"]:
        if m["id"] == "retry":
            m["claims"] = []
            m["exempt"] = [{"pkg": "demo/retryx"}]
    rep = _run_judge(data)
    check("exempt 缺 reason 判红", any("exempt 缺 reason" in f.message for f in rep.reds), True)

    # ⑨ 撞名对缺 qualifier ⇒ 红；补上 ⇒ 绿
    data = copy.deepcopy(base)
    del data["modules"][1]["qualifier"]
    rep = _run_judge(data)
    check("撞名缺 qualifier 判红", any("qualifier" in f.message for f in rep.reds), True)
    data["modules"][1]["qualifier"] = "Agent 应用运行时装配层"
    rep = _run_judge(data)
    check("补 qualifier 转绿", rep.has_red(), False)

    # ⑩ 真实 modules.json 若在场 ⇒ 必须自洽（现网数据本身就是判据）
    real = HERE.parent / "modules.json"
    if real.is_file():
        rep = _run_judge(json.loads(real.read_text(encoding="utf-8")))
        check("现网 modules.json 零红", rep.reds, [])

    # ⑪ 真跑一次 CLI 正常调用：不能有 Traceback / AttributeError
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "matrix.json"
        fixture.write_text(json.dumps(_fixture(), ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             "--matrix", str(fixture)],
            capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("CLI 正常调用退出码", proc.returncode, 0)
        check("CLI 输出无 Traceback", "Traceback" in out, False)
        check("CLI 输出无 AttributeError", "AttributeError" in out, False)

    if fails:
        print("❌ verify_mechanism_ownership 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_mechanism_ownership 自测通过"
          "（无归属判红 / 陈旧判红 / 承诺面判红 / 撞名判红 / CLI 无崩溃）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="八机制归属表 + 撞名 qualifier 守卫")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX),
                    help="modules.json 路径（默认本仓 modules.json）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    path = Path(args.matrix)
    if not path.is_file():
        print(f"❌ 找不到 {path}")
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"❌ modules.json 解析失败：{exc}")
        return 2

    report = Report(title="八机制归属")
    judge(data, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
