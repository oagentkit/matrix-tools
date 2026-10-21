#!/usr/bin/env python3
"""守卫报告层：把各守卫的判定结论渲染成**红 / 黄 / 绿**三档。

## 为什么要有三档（而不是「红 / 绿」两档）

矩阵里同时存在两种失败模式，而且它们此消彼长：

- **查得太松**：`latest` 字段没人查（矩阵源写着门面 v1.8.20，真实是 v1.8.21），
  没有任何门禁会红。
- **查得太紧**：CHANGELOG 覆盖门禁在 CI 上拿新 tag 比旧工作树，
  持续报出**根本不存在的**漂移。

两档模型只能二选一：要么把「可疑」也算红（于是噪声淹没真问题），
要么把「可疑」算绿（于是问题被静默）。本模块给出第三档 **黄**：

| 档 | 含义 | 默认行为 |
|---|---|---|
| 红 | **确认违规**（事实清楚、判据明确） | 退出码非 0，门禁失败 |
| 黄 | **可疑 / 判不了 / 待确认**（取不到、不确定、语义模糊） | **不失败**，但必须出现在报告与产物里 |
| 绿 | 确认通过 | — |

黄档不是「放过」，是「**记账**」：它让「查不到」这件事本身变成可观测的量，
而不是和「没问题」混在一起消失。

需要时用 `--fail-on-yellow` 把黄档升级成红（用于「全矩阵体检」这类
非 PR 触发的巡检场景）。

## 高内聚低耦合

本模块**只负责渲染**：把 `Finding` 列表变成人读的文本 + 机器读的 JSON。
它**不做任何判定**——「这条算不算违规」是各守卫自己的事。
所以：

- 各守卫可以共用本模块而不会共享判断逻辑；
- 本模块变了对所有守卫无影响（只影响输出长什么样）。

## 通用性

不绑定具体矩阵：主体名、等级、措辞都由调用方给。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict

RED = "red"
YELLOW = "yellow"
GREEN = "green"
INFO = "info"

_LEVEL_ORDER = {RED: 0, YELLOW: 1, INFO: 2, GREEN: 3}
_LEVEL_ICON = {RED: "❌", YELLOW: "⚠️ ", INFO: "ℹ️ ", GREEN: "✅"}


@dataclass
class Finding:
    """一条判定结论。

    subject：主体（仓名 / 包名 / 脚本名）
    message：结论（**必须具体到可行动**——「哪里不对」而不是「有问题」）
    hint：修法（可选，但强烈建议给）
    """

    level: str
    subject: str
    message: str
    hint: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    """一次守卫判定的全部结论。"""

    title: str = "守卫"
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def red(self, subject: str, message: str, hint: str | None = None) -> None:
        self.findings.append(Finding(RED, subject, message, hint))

    def yellow(self, subject: str, message: str, hint: str | None = None) -> None:
        self.findings.append(Finding(YELLOW, subject, message, hint))

    def info(self, subject: str, message: str, hint: str | None = None) -> None:
        self.findings.append(Finding(INFO, subject, message, hint))

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- 统计 ------------------------------------------------------------

    @property
    def reds(self) -> list[Finding]:
        return [f for f in self.findings if f.level == RED]

    @property
    def yellows(self) -> list[Finding]:
        return [f for f in self.findings if f.level == YELLOW]

    def counts(self) -> dict:
        out = {RED: 0, YELLOW: 0, INFO: 0}
        for f in self.findings:
            out[f.level] = out.get(f.level, 0) + 1
        return out

    def has_red(self) -> bool:
        return bool(self.reds)

    def has_yellow(self) -> bool:
        return bool(self.yellows)

    # -- 渲染 ------------------------------------------------------------

    def render_text(self) -> str:
        lines: list[str] = []
        counts = self.counts()
        ordered = sorted(self.findings, key=lambda f: (_LEVEL_ORDER.get(f.level, 9), f.subject))
        for f in ordered:
            lines.append(f"{_LEVEL_ICON.get(f.level, '·')} {f.subject}: {f.message}")
            if f.hint:
                lines.append(f"     ↳ 修法：{f.hint}")
        for n in self.notes:
            lines.append(f"ℹ️  {n}")
        summary = (f"{self.title}：红 {counts.get(RED, 0)} / "
                   f"黄 {counts.get(YELLOW, 0)} / 信息 {counts.get(INFO, 0)}")
        if not self.findings:
            lines.append(f"✅ {summary}（无问题）")
        else:
            lines.append(summary)
        return "\n".join(lines)

    def render_json(self) -> str:
        return json.dumps({
            "title": self.title,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
            "notes": self.notes,
        }, ensure_ascii=False, indent=2)

    def exit_code(self, fail_on_yellow: bool = False) -> int:
        """红 ⇒ 1；仅黄 ⇒ 0（除非要求黄也失败）。"""
        if self.has_red():
            return 1
        if fail_on_yellow and self.has_yellow():
            return 1
        return 0

    def emit(self, fail_on_yellow: bool = False, as_json: bool = False) -> int:
        print(self.render_json() if as_json else self.render_text())
        return self.exit_code(fail_on_yellow)


def emit(report: Report, fail_on_yellow: bool = False, as_json: bool = False) -> int:
    """统一的出口。所有守卫都用它，保证输出格式一致。"""
    return report.emit(fail_on_yellow=fail_on_yellow, as_json=as_json)


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    r = Report(title="测试守卫")
    check("空报告退出码", r.exit_code(), 0)
    check("空报告计数", r.counts(), {RED: 0, YELLOW: 0, INFO: 0})
    check("空报告渲染含「无问题」", "无问题" in r.render_text(), True)

    r.yellow("a", "可疑", "看看")
    check("仅黄不失败", r.exit_code(), 0)
    check("黄可升级为失败", r.exit_code(fail_on_yellow=True), 1)
    check("黄计入计数", r.counts()[YELLOW], 1)

    r.red("b", "确认违规")
    check("有红则失败", r.exit_code(), 1)

    text = r.render_text()
    check("红在黄之前渲染", text.index("❌") < text.index("⚠️"), True)
    check("渲染含修法", "修法：看看" in text, True)
    check("渲染含汇总", "红 1 / 黄 1" in text, True)

    payload = json.loads(r.render_json())
    check("JSON 条数", len(payload["findings"]), 2)
    check("JSON 计数", payload["counts"][RED], 1)

    if fails:
        print("❌ judge_report 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ judge_report 自测通过（三档分级 / 渲染 / JSON）")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="守卫报告层（红/黄/绿）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()
    raise SystemExit(selftest())
