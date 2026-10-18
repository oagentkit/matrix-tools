#!/usr/bin/env python3
"""从 GitHub tag 生成机器可读的版本矩阵 versions.json。

结构对齐 oekit 的 versions.json 惯例：每模块给 latest / packages / matrixDeps /
tier / breakingIndex。结构变更后重跑即可刷新，无需手工维护。

用法:
    gen_versions.py                      # 读 modules.json，写 versions.json（不联网）
    gen_versions.py --org oagentkit      # 联网读各仓 tag，标注 latest
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matrix_paths import (  # noqa: E402  import 路径构造规则与 rewrite_imports.py 同源
    OLD_ROOT,
    new_import_path,
    short_legacy,
)

DEFAULT_MATRIX = HERE.parent / "modules.json"
DEFAULT_OUT = HERE.parent / "versions.json"


def latest_tag(org: str, repo: str) -> str | None:
    r = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/tags", "--jq", "[.[].name] | .[0] // empty"],
        capture_output=True, text=True,
    )
    t = r.stdout.strip()
    return t or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--org", help="给了就联网读各仓最新 tag")
    args = ap.parse_args()

    data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = args.org or data.get("defaultOrg")

    out = {
        "$comment": "oagentkit 版本矩阵（机器可读）。由 matrix-tools/scripts/gen_versions.py 生成，勿手改。",
        "$pathRule": f"import 路径 = github.com/{org or '{{org}}'}/<仓名>/<包名末段>；"
                     f"旧路径 {OLD_ROOT}/<包> 的中间层目录会被丢弃。"
                     f"每个包的 fromLegacy 给出该包在旧单仓里的路径。"
                     f"权威输出：rewrite_imports.py --print-rules",
        "org": org,
        "orgUrl": f"https://github.com/{org}" if org else None,
        "sourceMatrix": data.get("sourceModule"),
        "tierCount": len({m["tier"] for m in data["modules"]}),
        "moduleCount": len(data["modules"]),
        "packageCount": sum(len(m["packages"]) for m in data["modules"]),
        "modules": [],
    }

    for m in data["modules"]:
        repo = m["repo"]
        out["modules"].append({
            "repo": repo,
            "url": f"https://github.com/{org}/{repo}" if org else None,
            "tier": m["tier"],
            "latest": latest_tag(org, repo) if (args.org and org) else None,
            "packages": [
                {
                    "pkg": p,
                    "importPath": new_import_path(org, repo, p) if org else None,
                    # 旧单仓里的包路径，供工具做机器可读的旧→新替换，不必解析脚本源码
                    "fromLegacy": short_legacy(p),
                }
                for p in m["packages"]
            ],
            "matrixDeps": [
                {"repo": d, "url": f"https://github.com/{org}/{d}" if org else None}
                for d in sorted(m.get("dependsOn", []))
            ],
            "breakingIndex": [],
        })

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"# 生成 {args.out}：{out['moduleCount']} 模块 / {out['packageCount']} 包 / tier0-{out['tierCount']-1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
