#!/usr/bin/env python3
"""versions.json 与 modules.json 的漂移检测（忽略 latest）。

modules.json 是单一数据源，versions.json 是它的生成物。两者漂移时消费方读到的
包清单 / import 路径 / 分层全是错的，而且**不会编译失败**——只是读到错的元数据。

比对时忽略 latest：它按设计要联网读 tag（gen_versions.py --org），
离线 CI 里必然是 null，拿它比会天天假红。

用法:
    verify_versions_drift.py
    verify_versions_drift.py --matrix modules.json --versions versions.json
    verify_versions_drift.py --compare ../site/docs/public/versions.json

--compare 额外比对另一份 versions.json（站点 docs/public/versions.json 是本文件的副本）。
    这一步**不比 latest 以外的东西也要全等**：站点是逐字节副本，任何差异都是漂移。

退出码: 0 = 一致；1 = 漂移。
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


def normalized(path: Path, drop_latest: bool = True, drop_breaking: bool = True) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    if drop_latest:
        for m in d.get("modules", []):
            m.pop("latest", None)   # 按设计联网读，离线必为 null
            for n in m.get("nestedModules", []):
                n.pop("latest", None)   # 嵌套模块的 tag 同理（audit/v0.1.0）
        for f in d.get("facades", []):
            f.pop("latest", None)
    if drop_breaking:
        # breakingIndex 由**各仓 CHANGELOG** 抽取，离线重生成必然是空的
        # （那要读 14 个仓的文件）。它有自己的对账门禁
        # （verify_breaking_index.py），这里不比——否则这条检查天天假红。
        for m in d.get("modules", []):
            m.pop("breakingIndex", None)
        for f in d.get("facades", []):
            f.pop("breakingIndex", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True, indent=2)


def report(title: str, a: str, b: str, fix: str) -> int:
    print(f"❌ {title}\n")
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="committed", tofile="expected", lineterm=""))
    for line in diff[:80]:
        print(line)
    if len(diff) > 80:
        print(f"...（另有 {len(diff) - 80} 行）")
    print(f"\n修法：{fix}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--versions", default=str(HERE.parent / "versions.json"))
    ap.add_argument("--compare", help="另一份 versions.json（站点副本），要求逐字节一致")
    args = ap.parse_args()

    matrix, versions = Path(args.matrix), Path(args.versions)
    for p in (matrix, versions):
        if not p.is_file():
            print(f"❌ 找不到文件: {p}")
            return 1

    with tempfile.TemporaryDirectory() as td:
        regen = Path(td) / "versions.regen.json"
        r = subprocess.run(
            [sys.executable, str(HERE / "gen_versions.py"),
             "--matrix", str(matrix), "--out", str(regen)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("❌ gen_versions.py 失败：")
            print(r.stdout + r.stderr)
            return 1
        a, b = normalized(versions), normalized(regen)

    rc = 0
    if a == b:
        print(f"✅ {versions.name} 与 {matrix.name} 一致（latest 字段除外）")
    else:
        rc = report(f"{versions.name} 与 {matrix.name} 漂移", a, b,
                    "python3 scripts/gen_versions.py --org oagentkit\n"
                    "     然后把 matrix-tools/versions.json 与站点 docs/public/versions.json 一起提交。")

    # 站点副本必须与 matrix-tools 的完全一致（含 latest）——它是逐字节副本，
    # 任何差异都是「改了这边忘了那边」。llms.txt 有 verify_docs.py 守着，
    # versions.json 此前没有任何检查，这是同一个漂移缺口。
    if args.compare:
        other = Path(args.compare)
        if not other.is_file():
            print(f"❌ 找不到待比对文件: {other}")
            return 1
        x, y = normalized(versions, drop_latest=False, drop_breaking=False), normalized(other, drop_latest=False, drop_breaking=False)
        if x == y:
            print(f"✅ 站点副本一致: {other}")
        else:
            rc = report(f"站点副本 {other} 与 {versions.name} 不一致", x, y,
                        "cp matrix-tools/versions.json <站点>/docs/public/versions.json 后一起提交")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
