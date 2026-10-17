#!/usr/bin/env python3
"""按 modules.json 建仓：13 个模块仓 + 基建仓，带 description 与 topics。

幂等 —— 已存在的仓只补/改元数据，不重建。默认 dry-run，加 --apply 才真建。

用法:
    create_repos.py                 # dry-run，打印将做什么
    create_repos.py --apply         # 真建
    create_repos.py --apply --private
    create_repos.py --apply --modules-only    # 只建 13 个模块仓，不带基建仓

建仓在 GitHub 上是公开且难以完全撤回的动作，所以默认 dry-run。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_MODULES = "docs/split-matrix/modules.json"


def gh(*args: str, check: bool = True) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.stderr.write(f"gh {' '.join(args)} 失败:\n{r.stderr}\n")
    return r.returncode, (r.stdout + r.stderr).strip()


def repo_exists(org: str, repo: str) -> bool:
    rc, _ = gh("api", f"repos/{org}/{repo}", check=False)
    return rc == 0


def plan_rows(data: dict, modules_only: bool) -> list[tuple[str, str, list[str], int]]:
    """返回 (repo, ghDescription, topics, tier)；infra 仓 tier 用 -1 表示「基建」。"""
    rows: list[tuple[str, str, list[str], int]] = []
    for m in data["modules"]:
        rows.append((m["repo"], m["ghDescription"], m["topics"], m["tier"]))
    if not modules_only:
        for r in data.get("infraRepos", []):
            rows.append((r["repo"], r["ghDescription"], r["topics"], -1))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--modules", default=DEFAULT_MODULES)
    ap.add_argument("--org")
    ap.add_argument("--apply", action="store_true", help="真建仓（默认 dry-run）")
    ap.add_argument("--private", action="store_true", help="私有仓（默认公开）")
    ap.add_argument("--modules-only", action="store_true", help="只建 13 个模块仓")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    mpath = Path(args.modules)
    if not mpath.is_absolute():
        mpath = root / mpath
    if not mpath.exists():
        print(f"错误：找不到 {mpath}", file=sys.stderr)
        return 2

    data = json.loads(mpath.read_text(encoding="utf-8"))
    org = args.org or data["defaultOrg"]
    vis = "private" if args.private else "public"
    mode = "apply" if args.apply else "dry-run"

    rc, who = gh("api", "user", "--jq", ".login", check=False)
    if rc != 0:
        print("错误：gh 未认证，先跑 gh auth login", file=sys.stderr)
        return 2
    rc, _ = gh("api", f"orgs/{org}/memberships/{who.strip()}", check=False)
    if rc != 0:
        print(f"错误：账号 {who.strip()} 不是 org {org} 的成员，无法建仓", file=sys.stderr)
        return 2

    rows = plan_rows(data, args.modules_only)
    print(f"# 模式={mode}  org={org}  可见性={vis}  账号={who.strip()}")
    print(f"# 共 {len(rows)} 个仓（模块 {len(data['modules'])}"
          f"{' + 基建 ' + str(len(data.get('infraRepos', []))) if not args.modules_only else ''}）\n")

    created = updated = skipped = failed = 0
    for repo, desc, topics, tier in rows:
        tag = f"tier{tier}" if tier >= 0 else "基建"
        exists = repo_exists(org, repo)

        if not exists:
            action = "创建"
            cmd = ["repo", "create", f"{org}/{repo}", f"--{vis}",
                   "--description", desc, "--disable-issues=false"]
        else:
            action = "更新元数据"
            cmd = ["repo", "edit", f"{org}/{repo}", "--description", desc]

        print(f"  [{action:<6}] {repo:<18} {tag:<6} {desc[:46]}")
        for t in topics:
            print(f"             topic: {t}")

        if not args.apply:
            continue

        rc, out = gh(*cmd)
        if rc != 0:
            print(f"             !! 失败: {out.splitlines()[-1] if out else '未知'}", file=sys.stderr)
            failed += 1
            continue
        created += 0 if exists else 1
        updated += 1 if exists else 0

        if topics:
            # gh repo edit --add-topic 收【逗号分隔】的 topic 列表。
            # 传空格分隔会被当成单个含空格的 topic，被 API 拒掉。
            rc, out = gh("repo", "edit", f"{org}/{repo}", "--add-topic", ",".join(topics), check=False)
            if rc != 0:
                print(f"             !! topic 设置失败: {out.splitlines()[-1] if out else '未知'}",
                      file=sys.stderr)
            else:
                print(f"             topics 已设置: {','.join(topics)}")

    if not args.apply:
        print(f"\n# dry-run 未执行任何变更。加 --apply 真正建仓。")
        return 0

    print(f"\n# 完成：新建 {created}，更新 {updated}，失败 {failed}")
    if failed:
        return 1
    print(f"# 下一步：按 tier 0→3 顺序迁包（见 docs/split-matrix/README.md §4）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
