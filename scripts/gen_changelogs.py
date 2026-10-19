#!/usr/bin/env python3
"""从 git 历史生成 CHANGELOG.md。

## 为什么要有这个

CONTRIBUTING 的「矩阵卫生」要求各仓 `CHANGELOG.md` 在场且不过期，但 13 个模块仓
**一个都没有**（AGENTS.md 倒是都有，所以那条要求只塌了一半）。

手写 13 份 changelog 不现实：每个仓十几个版本，人工整理既慢又会漏。
但仓库的 **commit message 已经很规范**——全矩阵统一用 conventional commits
前缀（`feat(x):` / `fix(x):` / `chore(deps):` / `refactor!(x):` …），
而且每个版本都是 annotated tag，tag 就是天然的版本边界。

所以：本脚本按 tag 切段，每段取该段内的 commit，按 conventional commit 分类渲染，
格式对齐 `oekit/pkg` 的既有写法（带日期的 `## [vX.Y.Z] — YYYY-MM-DD` 段落 +
`### feat(scope)：标题` 小标题 + 要点列表）。

## 边界与已知限制

- **只覆盖 tag 之后的 commit**：没有 tag 的提交不会被收录。这是刻意的——
  changelog 记的是「发出去的东西」。
- **同 commit 多个 tag**：某些仓连着两个 tag 指向同一 commit（改版本号重打），
  这时后一个 tag 的区间为空，渲染成「无内容变更」，不编造条目。
- **不解析 issue 链接**：commit 里的 `(#13)` 原样带出，不去猜 issue URL。
- **首次生成会覆盖已有文件**：用 `--out` 指定路径；`--check` 只比对不写。

用法:
    gen_changelogs.py --repo agent --root /path/to/oagentkit
    gen_changelogs.py --repo agent --root ... --next-version v0.6.1   # 预写待发版段
    gen_changelogs.py --repo agent --root ... --out /tmp/CHANGELOG.md
    gen_changelogs.py --repo agent --root ... --check   # 报告是否过期，不写

退出码: 0 = 成功 / --check 通过；1 = --check 发现过期或出错。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# conventional commit：type(scope)!: subject
# ⚠️ 叹号位置有两种写法，都要认：
#   - 标准：feat(api)!: ...      （叹号在 scope 之后）
#   - 本矩阵实际在用：refactor!(agent): ...  （叹号在 scope 之前）
# 只认标准写法时，仓库里大量 refactor!/feat! 提交会退化成裸 bullet——
# 看着像「没分类」，实质是解析失败。
CC_RE = re.compile(
    r"^(?P<type>[a-z]+)(?P<bang1>!)?(?:\((?P<scope>[^)]*)\))?(?P<bang2>!)?:\s*(?P<subject>.+)$"
)

# 哪些类型值得单独起小标题（其余压成一行要点）
HEADING_TYPES = {"feat", "fix", "perf", "refactor", "security"}

# 类型 → 中文小标题
TYPE_LABEL = {
    "feat": "新增",
    "fix": "修复",
    "perf": "性能",
    "refactor": "重构",
    "docs": "文档",
    "test": "测试",
    "build": "构建",
    "chore": "杂务",
    "ci": "CI",
    "style": "格式",
    "security": "安全",
    "revert": "回滚",
}

VERSION_ORDER_HINT = "v"


def run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def semver_key(tag: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", tag)
    if not m:
        return (0, 0, 0, 0, "")
    maj, mi, pa, pre = m.groups()
    return (int(maj), int(mi), int(pa), 0 if pre else 1, pre or "")


def tags_in_order(repo_dir: Path) -> list[str]:
    out = run(["git", "tag", "--list"], repo_dir)
    tags = [t.strip() for t in out.splitlines() if re.match(r"^v\d+\.\d+\.\d+", t.strip())]
    return sorted(tags, key=semver_key, reverse=True)


def tag_date(repo_dir: Path, tag: str) -> str:
    out = run(["git", "log", "-1", "--format=%cs", tag], repo_dir).strip()
    return out or "未知日期"


def head_date(repo_dir: Path) -> str:
    out = run(["git", "log", "-1", "--format=%cs", "HEAD"], repo_dir).strip()
    return out or "未知日期"


def commits_between(repo_dir: Path, prev: str | None, cur: str) -> list[dict]:
    rng = f"{prev}..{cur}" if prev else cur
    out = run(["git", "log", "--format=%H%x1f%s%x1f%b%x1e", rng], repo_dir)
    items = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        items.append({"sha": sha, "subject": subject, "body": body})
    return items


def dedent_body(body: str) -> list[str]:
    """去掉 git log --format=%b 给续行加的 4 空格缩进。

    不去的话，commit body 里的 markdown 表格会被缩成代码块——
    `| 熔断 | ... |` 变成 `    | 熔断 | ... |`，渲染出来完全不是表格。
    """
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return []
    # 只对「看起来是 git 续行缩进」的公共前缀生效：4 空格
    while lines and all(ln.startswith("    ") or not ln.strip() for ln in lines):
        lines = [ln[4:] if ln.startswith("    ") else ln for ln in lines]
    return lines


def render_entry(c: dict) -> tuple[str, str, list[str]]:
    """→ (type, heading, body_lines)

    body **原样保留**，不逐行加 "- " 前缀——commit body 本身已是 markdown
    （列表、表格、代码块），再套一层 "- " 会把表格和代码块拆坏。
    """
    m = CC_RE.match(c["subject"])
    if not m:
        return "other", c["subject"], []
    ctype = m.group("type")
    scope = m.group("scope") or ""
    bang = "!" if (m.group("bang1") or m.group("bang2")) else ""
    subject = m.group("subject").strip()
    label = TYPE_LABEL.get(ctype, ctype)
    head = f"{label}({scope}){bang}：{subject}" if scope else f"{label}：{subject}"
    body = dedent_body(c["body"])
    return ctype, head, body


def render(repo_dir: Path, repo: str, next_version: str | None = None) -> str:
    # 区间必须**升序**计算：tag 区间的定义是「上一个更旧的 tag .. 这个 tag」。
    # 若从最新 tag 倒着算，prev=None 会退化成「该 tag 的全部可达历史」，
    # 于是最新版本段里塞进整个仓的初始提交，而次新版本段反而是空的。
    asc = sorted(tags_in_order(repo_dir), key=semver_key)
    segments: list[tuple[str, str | None, list[dict]]] = []
    prev = None
    for tag in asc:
        segments.append((tag, prev, commits_between(repo_dir, prev, tag)))
        prev = tag
    # 输出按版本倒序（CHANGELOG 惯例：最新在上）
    segments.reverse()

    # 待发版段：把「上一个 tag 之后的提交」预写成下一个版本的版本段。
    # 没有它，「先提交 CHANGELOG 再打 tag」的打法下 tag 快照里永远没有自己那段
    # （生成器只认已存在的 tag → 鸡生蛋）。带上它，发版顺序就能改成
    #   gen_changelogs.py --next-version vX.Y.Z → 提交 → 打 tag vX.Y.Z
    # tag 快照里就有自己那段了（issue matrix-tools#17 的根治点）。
    if next_version:
        newest = asc[-1] if asc else None
        if newest is None or semver_key(next_version) > semver_key(newest):
            segments.insert(0, (next_version, newest,
                                commits_between(repo_dir, newest, "HEAD")))

    lines = [f"# {repo} CHANGELOG", "",
             "> 本文件由 `matrix-tools/scripts/gen_changelogs.py` 从 git 历史生成，"
             "按 tag 切段、取每段内的 conventional commit 渲染。", ""]
    for tag, prev, commits in segments:
        date = (head_date(repo_dir) if tag == next_version
                else tag_date(repo_dir, tag))
        lines.append(f"## [{tag}] — {date}")
        lines.append("")
        if not commits:
            lines.append(f"- 与 {prev} 相比无代码差异（版本号/打包变更）")
            lines.append("")
            continue
        groups: dict[str, list[dict]] = {}
        for c in commits:
            ctype, _, _ = render_entry(c)
            groups.setdefault(ctype, []).append(c)
        for ctype in sorted(groups, key=lambda t: (t not in HEADING_TYPES, t)):
            if ctype in HEADING_TYPES:
                for c in groups[ctype]:
                    _, head, body = render_entry(c)
                    lines.append(f"### {head}")
                    lines.append("")
                    if body:
                        lines.extend(body)
                    else:
                        lines.append(f"- {c['sha'][:7]}")
                    lines.append("")
            else:
                for c in groups[ctype]:
                    _, head, _ = render_entry(c)
                    lines.append(f"- {head}")
                lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="从 git 历史生成 CHANGELOG.md")
    ap.add_argument("--repo", required=True, help="仓名（相对 --root）")
    ap.add_argument("--root", default="/Users/zhangyi/oagentkit")
    ap.add_argument("--out", help="输出路径；默认写 <root>/<repo>/CHANGELOG.md")
    ap.add_argument("--check", action="store_true", help="只比对不写；不一致则 exit 1")
    ap.add_argument("--next-version", metavar="vX.Y.Z",
                    help="把「上一个 tag 之后的提交」预写成该版本的待发版段（打 tag 前用）")
    args = ap.parse_args()

    repo_dir = Path(args.root) / args.repo
    if not (repo_dir / ".git").exists():
        print(f"❌ 不是 git 仓: {repo_dir}")
        return 1

    # ⚠️ 没有 tag 就要**报错**，不能静默生成空文件。
    # 实测：`git clone --depth 1` 出来的浅克隆有 0 个 tag，生成器会输出一个
    # 「0 个版本段」的空 CHANGELOG——用它去 --check 就会拿空内容跟真文件比，
    # 报出「已过期」的假红；用它去生成就会把真 CHANGELOG 覆盖成空文件。
    # 退化输入必须显式失败，不能静默产出看似成功的空结果。
    if not tags_in_order(repo_dir):
        print(f"❌ {args.repo}: 找不到任何版本 tag。"
              "若是浅克隆（--depth 1）请改用完整克隆；若是全新仓则应先发版。")
        return 1

    content = render(repo_dir, args.repo, args.next_version)
    out = Path(args.out) if args.out else repo_dir / "CHANGELOG.md"

    if args.check:
        if not out.exists():
            print(f"❌ {args.repo} 缺 CHANGELOG.md")
            return 1
        cur = out.read_text(encoding="utf-8")
        if cur != content:
            print(f"❌ {args.repo} 的 CHANGELOG.md 与 git 历史不一致（已过期）")
            return 1
        print(f"✅ {args.repo} 的 CHANGELOG.md 是最新的")
        return 0

    out.write_text(content, encoding="utf-8")
    n = content.count("\n## [")
    print(f"✅ {args.repo}: 生成 {n} 个版本段 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
