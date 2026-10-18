#!/usr/bin/env python3
"""versions.json 与 modules.json 的漂移检测（忽略 latest）。

modules.json 是单一数据源，versions.json 是它的生成物。两者漂移时消费方读到的
包清单 / import 路径 / 分层全是错的，而且**不会编译失败**——只是读到错的元数据。

比对时忽略 latest：它按设计要联网读 tag（gen_versions.py --org），
离线 CI 里必然是 null，拿它比会天天假红。

用法:
    verify_versions_drift.py
    verify_versions_drift.py --matrix modules.json --versions versions.json

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


def normalized(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    for m in d.get("modules", []):
        m.pop("latest", None)   # 按设计联网读，离线必为 null
    return json.dumps(d, ensure_ascii=False, sort_keys=True, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--versions", default=str(HERE.parent / "versions.json"))
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

    if a == b:
        print(f"✅ {versions.name} 与 {matrix.name} 一致（latest 字段除外）")
        return 0

    print(f"❌ {versions.name} 与 {matrix.name} 漂移：\n")
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile=f"{versions.name} (committed)", tofile="regenerated", lineterm=""))
    for line in diff[:80]:
        print(line)
    if len(diff) > 80:
        print(f"...（另有 {len(diff) - 80} 行）")
    print("\n修法：python3 scripts/gen_versions.py --org oagentkit")
    print("     然后把 matrix-tools/versions.json 与站点 docs/public/versions.json 一起提交。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
