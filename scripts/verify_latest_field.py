#!/usr/bin/env python3
"""`latest` 字段对账：矩阵源写的「最新版本」必须等于远端真实最新 tag。

## 守的是什么

`versions.json` 是矩阵的**机器可读权威源**——AI Agent、文档站、依赖工具都读它。
它的 `latest` 字段写着每个仓的「当前最新版本」。

实测缺陷（2026-10-21）：facade `agentkit` 的 `latest` 是 **v1.8.20**，
而远端真实最新 tag 已是 **v1.8.21**。

**为什么一直没人发现**：`versions.json drift` 那条门禁**明确忽略** `latest`
（它要联网读 tag，离线 CI 取不到），而全 CI **没有任何** `gen_versions.py --org`
的调用 —— 也就是说 `latest` 是个**没人守的字段**。

「忽略 + 补门禁」必须成对：只忽略不补，字段就成了没人守的声明。

## 为什么独立于 gen_versions.py

本脚本**不 import 生成器**，自己取远端 tag（`repo_meta.latest_tag`）再比对。
若复用生成器的推导，就是「门禁与被检对象共享逻辑」——
生成器取错 tag 时门禁会跟着错（矩阵里已经踩过：`audit/v0.1.0` 这类
嵌套模块 tag 混进主模块 latest 的事故）。

## 取数纪律

`latest_tag` 只认 `^v\\d+\\.\\d+\\.\\d+`（主模块版本线），
嵌套模块的 tag（`audit/v0.1.0`）由 `nested_tag` 单独取。
取不到 ⇒ **黄灯**（判不了 ≠ 没问题）。

用法:
    verify_latest_field.py --org oagentkit
    verify_latest_field.py --versions versions.json --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import repo_meta  # noqa: E402  （事实层：只取 tag）
from judge_report import Report, emit  # noqa: E402


def latest_map(doc: dict) -> dict[str, str]:
    """versions.json 文档 → {仓名: latest}（modules + facades 两段都收）。"""
    out: dict[str, str] = {}
    for m in doc.get("modules", []):
        if m.get("repo"):
            out[m["repo"]] = m.get("latest", "")
    for f in doc.get("facades", []):
        if f.get("repo"):
            out[f["repo"]] = f.get("latest", "")
    return out


def judge(doc: dict, org: str, report: Report) -> None:
    declared = latest_map(doc)
    if not declared:
        report.red("versions.json", "文档里没有任何 modules/facades 条目")
        return
    for repo, declared_version in sorted(declared.items()):
        remote = repo_meta.latest_tag(org, repo)
        if remote is None:
            report.yellow(repo, f"取不到远端最新 tag（判不了），本地写的是 {declared_version}",
                          "网络失败或仓不存在——判不了不能当成「没问题」")
            continue
        if not declared_version:
            report.red(repo, f"latest 字段为空，远端最新是 {remote}",
                       f"跑 gen_versions.py --org {org} 刷新")
            continue
        if declared_version != remote:
            report.red(repo, f"latest 写的是 {declared_version}，远端最新是 {remote}",
                       f"跑 gen_versions.py --org {org} 刷新矩阵源，"
                       "再把刷新后的 versions.json 拷到站点副本")
        else:
            report.info(repo, f"latest = {remote}（与远端一致）")


def selftest() -> int:
    """反证自测：latest 与远端真相不符时必须报红；取不到时必须报**黄**而不是绿。"""
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    doc = {"modules": [{"repo": "a", "latest": "v1.0.0"}],
           "facades": [{"repo": "b", "latest": "v2.0.0"}]}

    # 用假的 repo_meta 顶替（判据必须是纯逻辑，联网部分由 --org 模式覆盖）
    orig = repo_meta.latest_tag
    try:
        repo_meta.latest_tag = lambda org, repo: {"a": "v1.0.0", "b": "v2.0.1"}.get(repo)
        rep = Report(title="latest 对账")
        judge(doc, "acme", rep)
        red_subjects = {f.subject for f in rep.reds}
        check("陈旧 latest 判红", "b" in red_subjects, True)
        check("正确 latest 不报红", "a" in red_subjects, False)

        # 取不到 ⇒ 黄，不是绿
        repo_meta.latest_tag = lambda org, repo: None
        rep2 = Report(title="latest 对账")
        judge(doc, "acme", rep2)
        check("取不到判黄", rep2.has_yellow(), True)
        check("取不到不判红", rep2.has_red(), False)
    finally:
        repo_meta.latest_tag = orig

    if fails:
        print("❌ verify_latest_field 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_latest_field 自测通过（陈旧判红 / 取不到判黄）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="versions.json 的 latest 字段对账")
    ap.add_argument("--versions", default=str(HERE.parent / "versions.json"))
    ap.add_argument("--org", default="oagentkit")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    path = Path(args.versions)
    if not path.is_file():
        print(f"❌ 找不到 {path}")
        return 2
    doc = json.loads(path.read_text(encoding="utf-8"))
    report = Report(title="latest 字段对账")
    judge(doc, args.org, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
