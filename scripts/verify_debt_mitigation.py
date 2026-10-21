#!/usr/bin/env python3
"""欠账基线的缓解状态门禁：基线里的每一条，危害是否已消必须**自己重算**。

## 守的是什么（issue matrix-tools#72）

`scripts/changelog_tag_debt.json` 记的是「**已发布 tag 的 CHANGELOG 快照里缺该
tag 自己的版本段**」这条欠账。机制上：**条目永远无法退休**——

    org 有 protect-tags ruleset ⇒ tag 不可改不可删
    ⇒ 那个 tag 处的 CHANGELOG.md 内容不可变
    ⇒ 那条快照永远缺自己那一段
    ⇒ 基线里的这一条永远判 stale

所以它实际是**永久豁免清单**，不是可清空的债务台账。而基线内 stale 只警告不挡
⇒ 谁往基线里加一条新欠账，它就变成「已知」而只留一行警告 ⇒ **新增欠账被基线
吸收，无人可见**。

## 关键区分：条目不可退休 ≠ 危害仍在

欠账条目**不可退休**（机制决定），但**危害可以消除**：

- **危害** = 下游拿到的**最新 CHANGELOG** 里缺那个版本的段。人和工具读发布说明
  读的是最新那份，不是 tag 快照里那份残缺的。
- **消除办法** = 后续版本重生成 CHANGELOG 时把该版本段补进最新 CHANGELOG。
  补发补丁 tag 也一样能消除危害（它会带着补好的 CHANGELOG 一起发出去）——
  但它**不退休**原条目（快照不可变，那条快照仍然缺自己那段）。

实测（2026-10-22，本门禁重算）：27 条基线条目 → **已缓解 27 / 未缓解 0**。

## 判据（结构信号，与措辞无关）

**该仓 main 的 CHANGELOG.md 里是否含该版本段**。

## 为什么不能读基线文件里写的状态

如果 oracle 直接采信基线文件里的「已缓解」标记，那个标记自己就成了**新的可写豁免**：
谁都能写一行 `"mitigated"` 让门禁变绿——**共享判定 = 共同盲区**。

所以本门禁的规矩是：

- **判定只用重算出来的结果**；
- 基线文件里写的状态**只用于核对**：
  - 文件说「已缓解」而重算是「未缓解」 ⇒ **红**（这是企图豁免，不是文档陈旧）；
  - 文件说「未缓解」而重算是「已缓解」 ⇒ **黄**（台账陈旧；判定不受影响）；
  - 文件里没有这一条 ⇒ **黄**（台账不完整）。

## 三档

| 档 | 条件 | 行为 |
|---|---|---|
| 红 | 基线内 + **未缓解**（危害仍在） | 退出码非 0，提示补发补丁 tag 消除危害 |
| 红 | 文件声称「已缓解」但重算为「未缓解」 | 企图豁免，判红 |
| 黄 | 取数失败（**判不了**）/ 台账陈旧 / 台账缺项 | 不失败，但必须出现在报告里 |
| 信息 | 基线内 + **已缓解**（危害已消） | 只记账 |

基线**外**的 stale 仍由 `verify_changelog_at_tag.py` 硬失败（现状不变）。

## 用法

    # 全矩阵重算（联网）
    verify_debt_mitigation.py --org oagentkit

    # 只算一个仓
    verify_debt_mitigation.py --org oagentkit --repo llm

    # 离线自测（含反证样本）
    verify_debt_mitigation.py --selftest

退出码: 0 = 无未缓解条目（黄灯也不失败）；1 = 有未缓解条目或企图豁免。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import repo_meta  # noqa: E402
from judge_report import INFO, RED, YELLOW, Finding, Report, emit  # noqa: E402

DEFAULT_DEBT = HERE / "changelog_tag_debt.json"

# 基线条目的两种状态。
MITIGATED = "mitigated"       # 危害已消：最新 CHANGELOG 已含该版本段
UNMITIGATED = "unmitigated"   # 危害仍在：最新 CHANGELOG 仍缺该版本段


# ---------------------------------------------------------------- 配置读取


def load_baseline(path: Path) -> tuple[list[str], dict[str, str], bool, str | None]:
    """读欠账基线 → (条目列表, 文件声称的状态表, 台账是否存在, 错误信息)。

    条目形如 `repo@version`。状态表来自基线文件的 `mitigation.entries`
    ——它**只用于核对**，不参与本门禁的判定（见文件头「为什么不能读基线文件里
    写的状态」）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], {}, False, f"欠账基线 {path} 不存在"
    except (ValueError, OSError) as exc:
        return [], {}, False, f"欠账基线 {path} 读不了：{exc}"
    if not isinstance(data, dict):
        return [], {}, False, f"欠账基线 {path} 顶层不是对象"

    tags = data.get("tags")
    if not isinstance(tags, list):
        return [], {}, False, f"欠账基线 {path} 里没有 tags 列表"

    entries: list[str] = []
    for t in tags:
        if not isinstance(t, str) or "@" not in t:
            return [], {}, False, f"欠账基线 {path} 的条目不是 repo@version 形态：{t!r}"
        entries.append(t)

    claimed: dict[str, str] = {}
    has_ledger = False
    mitigation = data.get("mitigation")
    if isinstance(mitigation, dict):
        raw = mitigation.get("entries")
        if isinstance(raw, dict):
            has_ledger = True
            for k, v in raw.items():
                if v in (MITIGATED, UNMITIGATED):
                    claimed[str(k)] = str(v)
    return entries, claimed, has_ledger, None


# ---------------------------------------------------------------- 判定层


def judge_entry(repo: str, version: str, sections: set[str] | None,
                claimed: str | None, has_ledger: bool = True) -> tuple[str, list[Finding]]:
    """判定单条基线条目 → (重算出的状态, 结论列表)。

    ⚠️ `sections` 是**事实**（该仓最新 CHANGELOG 的版本段集合）；
    状态由它**重算**得出。`claimed`（基线文件声称的状态）只用于核对，
    永远不参与状态判定。
    """
    key = f"{repo}@{version}"
    findings: list[Finding] = []

    if sections is None:
        # 判不了 ≠ 没问题：取不到最新 CHANGELOG ⇒ 判不了 ⇒ 黄灯
        findings.append(Finding(
            YELLOW, key,
            "取不到该仓的 CHANGELOG.md——判不了，不等于「已缓解」",
            "确认 gh 认证可用（GH_TOKEN）与网络可达；"
            "这一条必须有人看，不能当成绿灯"))
        return "unknown", findings

    recomputed = MITIGATED if version in sections else UNMITIGATED

    # ---- 核对基线文件声称的状态（只核对，不采信）----
    if claimed is None:
        if has_ledger:
            findings.append(Finding(
                YELLOW, key,
                "基线台账里没有这一条的缓解状态——台账不完整",
                "跑一次重算把状态写回 changelog_tag_debt.json 的 mitigation.entries；"
                "台账只是给人读的账本，判定始终以重算结果为准"))
    elif claimed == MITIGATED and recomputed == UNMITIGATED:
        findings.append(Finding(
            RED, key,
            "基线台账声称「已缓解」，但重算结果是「未缓解」——"
            "这是一次**企图豁免**（该仓最新 CHANGELOG 仍缺该版本段）",
            "台账字段不参与判定，改它不会让门禁变绿——"
            "危害仍在就得补发补丁 tag 消除它"))
    elif claimed == UNMITIGATED and recomputed == MITIGATED:
        findings.append(Finding(
            YELLOW, key,
            "基线台账声称「未缓解」，但重算结果是「已缓解」——台账陈旧",
            "跑一次重算把状态写回 changelog_tag_debt.json 的 mitigation.entries"))

    # ---- 危害本身 ----
    if recomputed == UNMITIGATED:
        findings.append(Finding(
            RED, key,
            "基线内欠账且**危害仍在**：该仓最新 CHANGELOG 里仍缺该版本段"
            "（下游读到的发布说明缺这个版本自己的内容）",
            "补发一个补丁 tag 可**消除危害**（新 tag 会带着补好的 CHANGELOG 发出去）；"
            "但它**不退休本条目**——tag 快照受 protect-tags 保护、不可变，"
            "那条快照永远缺自己那一段"))
    else:
        findings.append(Finding(
            INFO, key,
            "基线内欠账，但**危害已消**：该仓最新 CHANGELOG 已含该版本段",
            "只记账。本条目**不会退休**——tag 快照不可变，那条快照永远缺自己那一段；"
            "补发补丁 tag 消除的是危害，不是条目"))

    return recomputed, findings


def judge(entries: list[str], claimed: dict[str, str],
          sections_by_repo, has_ledger: bool = True) -> tuple[dict[str, str], list[Finding]]:
    """判定一批基线条目 → (各条重算出的状态, 结论列表)。

    `sections_by_repo(repo)` → set[str] | None（None = 判不了）。
    """
    states: dict[str, str] = {}
    findings: list[Finding] = []
    for key in entries:
        repo, _, version = key.partition("@")
        state, fs = judge_entry(repo, version, sections_by_repo(repo),
                                claimed.get(key), has_ledger=has_ledger)
        states[key] = state
        findings.extend(fs)
    return states, findings


# ---------------------------------------------------------------- 自测


def _levels(findings: list[Finding]) -> list[str]:
    return [f.level for f in findings]


def _reds(findings: list[Finding]) -> list[str]:
    """被判红的条目（去重，保持顺序）——同一条目可能有多条不同理由的红。"""
    out: list[str] = []
    for f in findings:
        if f.level == RED and f.subject not in out:
            out.append(f.subject)
    return out


def _yellows(findings: list[Finding]) -> list[str]:
    out: list[str] = []
    for f in findings:
        if f.level == YELLOW and f.subject not in out:
            out.append(f.subject)
    return out


def selftest() -> int:
    """反证自测。

    除了「该红的红、该绿的不红」，还要证明**自测本身有区分力**：
    把被防的那一路掐掉后，结论必须变——否则自测只是「跑通即绿」的摆设。

    本门禁要防的失效形态是「基线文件自己变成可写的豁免」，
    所以反证必须证明：**台账字段无论怎么写，都改不动判定结果**。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    entries = ["llm@v0.14.0", "llm@v0.13.0", "obs@v0.9.1"]

    # 该仓最新 CHANGELOG 的版本段：含 v0.14.0，不含 v0.13.0；obs 取不到。
    sections = {
        "llm": {"v0.15.2", "v0.14.0"},
        "obs": None,
    }

    def fetch(repo: str):
        return sections.get(repo)

    # ---- 1. 正常形态：一条已缓解、一条未缓解、一条判不了 ----
    states, findings = judge(entries, {}, fetch, has_ledger=False)
    check("已缓解的那条", states["llm@v0.14.0"], MITIGATED)
    check("未缓解的那条", states["llm@v0.13.0"], UNMITIGATED)
    check("判不了的那条", states["obs@v0.9.1"], "unknown")
    check("未缓解判红", _reds(findings), ["llm@v0.13.0"])
    check("判不了判黄", _yellows(findings), ["obs@v0.9.1"])
    check("已缓解只记账（不红不黄）",
          [f.level for f in findings if f.subject == "llm@v0.14.0"], [INFO])

    # ---- 2. 台账声称「已缓解」但重算是「未缓解」⇒ 判红（企图豁免）----
    states, findings = judge(entries, {"llm@v0.13.0": MITIGATED}, fetch)
    check("企图豁免仍判未缓解", states["llm@v0.13.0"], UNMITIGATED)
    check("企图豁免判红", _reds(findings), ["llm@v0.13.0"])
    check("企图豁免有专属理由",
          any("企图豁免" in f.message for f in findings), True)

    # ---- 3. 台账声称「未缓解」但重算是「已缓解」⇒ 判黄（台账陈旧）----
    states, findings = judge(entries, {"llm@v0.14.0": UNMITIGATED}, fetch)
    check("陈旧台账不改判定", states["llm@v0.14.0"], MITIGATED)
    check("陈旧台账判黄", "llm@v0.14.0" in _yellows(findings), True)
    check("陈旧台账不判红", "llm@v0.14.0" in _reds(findings), False)

    # ---- 4. 台账缺项 ⇒ 判黄，不影响判定 ----
    _, findings = judge(entries, {"llm@v0.14.0": MITIGATED}, fetch)
    check("台账缺项判黄",
          sorted(_yellows(findings)), ["llm@v0.13.0", "obs@v0.9.1"])
    check("台账缺项不判红", _reds(findings), ["llm@v0.13.0"])

    # ---- 5. 反证A：同一个台账、同一批条目，把**事实**换掉 ⇒ 结论必须变。
    #     这证明判定来自「自己重算」，而不是来自台账字段。
    _, f_on = judge(["llm@v0.13.0"], {"llm@v0.13.0": MITIGATED},
                    lambda repo: {"v0.13.0"})          # 事实：版本段在
    _, f_off = judge(["llm@v0.13.0"], {"llm@v0.13.0": MITIGATED},
                     lambda repo: {"v0.15.2"})         # 事实：版本段不在
    check("反证A：事实变了结论必须变（无红 → 红）",
          (_reds(f_on), _reds(f_off)), ([], ["llm@v0.13.0"]))

    # ---- 6. 反证B：把台账字段全写成「已缓解」⇒ 危害那条仍必须报红 ----
    #     这证明台账字段无论怎么写都改不动判定。
    _, findings = judge(entries,
                        {"llm@v0.13.0": MITIGATED, "llm@v0.14.0": MITIGATED},
                        fetch)
    check("反证B：台账全写已缓解，危害那条仍报红",
          _reds(findings), ["llm@v0.13.0"])

    # ---- 7. 反证C：判不了的那条，台账写什么都没用 ⇒ 仍判黄 ----
    _, findings = judge(entries, {"obs@v0.9.1": MITIGATED}, fetch)
    check("反证C：判不了的那条不因台账而转绿",
          "obs@v0.9.1" in _yellows(findings), True)
    check("反证C：判不了的那条不判红",
          "obs@v0.9.1" in _reds(findings), False)

    # ---- 8. 台账文件解析：形态不对必须报错而不是静默当空 ----
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "debt.json"
        p.write_text(json.dumps({"tags": ["没有版本号分隔符"]}), encoding="utf-8")
        _, _, _, err = load_baseline(p)
        check("条目形态不对要报错", bool(err), True)
        p.write_text(json.dumps({"tags": ["llm@v0.1.0"],
                                 "mitigation": {"entries": {"llm@v0.1.0": "mitigated"}}}),
                     encoding="utf-8")
        ents, claimed, has_ledger, err = load_baseline(p)
        check("正常文件读得出条目", ents, ["llm@v0.1.0"])
        check("正常文件读得出台账", claimed, {"llm@v0.1.0": MITIGATED})
        check("正常文件认得出台账存在", has_ledger, True)
        check("正常文件不报错", err, None)
        p.write_text(json.dumps({"tags": ["llm@v0.1.0"]}), encoding="utf-8")
        _, _, has_ledger, err = load_baseline(p)
        check("老格式（无台账）读得出条目", err, None)
        check("老格式认得出没有台账", has_ledger, False)
        p.write_text("{ 坏 json", encoding="utf-8")
        _, _, _, err = load_baseline(p)
        check("坏 JSON 要报错", bool(err), True)

    if fails:
        print("❌ verify_debt_mitigation 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_debt_mitigation 自测通过"
          "（已缓解只记账 / 未缓解判红 / 判不了判黄 / 企图豁免判红 / "
          "台账陈旧判黄 / 台账缺项判黄 / 反证 A、B、C）")
    return 0


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="欠账基线的缓解状态门禁")
    ap.add_argument("--org", help="组织名（联网重算）")
    ap.add_argument("--repo", action="append", default=[],
                    help="只算这些仓的条目（可重复；默认全基线）")
    ap.add_argument("--debt", default=str(DEFAULT_DEBT), help="欠账基线文件路径")
    ap.add_argument("--selftest", action="store_true", help="离线自测（含反证）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--json-out", default=None, help="把 JSON 报告写到该文件")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也失败")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    entries, claimed, has_ledger, err = load_baseline(Path(args.debt))
    report = Report(title="欠账基线缓解状态")
    if err:
        report.red("baseline", err, "确认 --debt 指向的是 scripts/changelog_tag_debt.json")
        return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if not entries:
        report.red("baseline", "欠账基线是空的——拒绝判定",
                   "空基线意味着「一条欠账都没有」，那是假的")
        return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if not has_ledger:
        report.yellow("baseline",
                      "基线文件里没有缓解状态台账（mitigation.entries）",
                      "跑一次重算把状态写回 changelog_tag_debt.json；"
                      "台账只是给人读的账本——判定始终以重算结果为准，"
                      "所以缺台账不影响本次判定，只影响可读性")

    if args.repo:
        scope = set(args.repo)
        entries = [e for e in entries if e.partition("@")[0] in scope]
        if not entries:
            report.yellow("scope", f"指定仓 {sorted(scope)} 在基线里没有条目",
                          "基线不含这些仓 ⇒ 这些仓没有这类欠账")
            return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)

    if not args.org:
        ap.error("需要 --org（联网重算）")

    # 一个仓只取一次最新 CHANGELOG（事实层取数，判定在 judge_entry 里）。
    repos = sorted({e.partition("@")[0] for e in entries})
    cache: dict[str, set[str] | None] = {}

    def sections_of(repo: str) -> set[str] | None:
        if repo not in cache:
            sections, status = repo_meta.fetch_changelog_sections(args.org, repo)
            cache[repo] = sections if status == "ok" else None
        return cache[repo]

    states, findings = judge(entries, claimed, sections_of, has_ledger=has_ledger)
    report.findings.extend(findings)

    mitigated = sum(1 for v in states.values() if v == MITIGATED)
    unmitigated = sum(1 for v in states.values() if v == UNMITIGATED)
    unknown = sum(1 for v in states.values() if v == "unknown")
    report.note(f"基线条目 {len(states)} 条：已缓解 {mitigated} / "
                f"未缓解 {unmitigated} / 判不了 {unknown}")
    report.note("判定只用重算结果；基线台账里的状态只用于核对（声称「已缓解」"
                "而重算为「未缓解」⇒ 判红，那是企图豁免）。")

    code = emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if args.json_out:
        Path(args.json_out).write_text(report.render_json(), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
