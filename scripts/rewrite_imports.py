#!/usr/bin/env python3
"""agentkit import 路径改写器。

把单仓模块路径 git.enjoye.top/enjoydream/agentkit/<pkg> 按 modules.json 的模块矩阵
改写成 github.com/{org}/{repo}/{pkg}，包名一律保留，只加模块前缀 —— 因此改写是纯前缀映射，
可机械执行、可机械校验。

对标 oekit 的 matrix-tools/scripts/ekit2oekit.py（ekit→oekit 迁移），同样提供 --print-rules。

用法:
    rewrite_imports.py --org <org> --print-rules      # 看映射表，不动文件
    rewrite_imports.py --org <org> --check            # 只报告将要改什么，有改动则 exit 1
    rewrite_imports.py --org <org> --apply            # 真改写 .go 与 go.mod
    rewrite_imports.py --org <org> --manual           # 列出需人工处理的非 Go 文件
    rewrite_imports.py --single github.com/o/agentkit --apply   # 单仓整体改名（不拆）

设计约束:
  * 只改 import 块内的引号字符串，不碰注释、不碰其他字符串字面量 ——
    测试里断言模块路径、或文档里出现的旧路径必须原样保留。
  * 每个包必须命中映射表；未登记的包一律报错退出，不静默跳过。
    静默跳过是这类脚本最危险���失败模式：改了一半，看起来成功了。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

OLD_ROOT = "git.enjoye.top/enjoydream/agentkit"
DEFAULT_ORG = "oagentkit"  # Open Agent Kit —— agent/LLM 编排工具链矩阵
IMPORT_LINE_RE = re.compile(r'^(\s*(?:[\w.]+\s+)?"[^"]+")\s*(//.*)?$')
BLOCK_START_RE = re.compile(r"^\s*import\s*\(\s*(//.*)?$")
SINGLE_IMPORT_RE = re.compile(r'^(\s*import\s+(?:[\w.]+\s+)?"[^"]+")\s*(//.*)?$')
MODULE_LINE_RE = re.compile(r"^module\s+" + re.escape(OLD_ROOT) + r"\s*$", re.M)
QUOTED_RE = re.compile(r'"([^"]+)"')

# 非 Go 文件里出现旧路径时，这些是纯文档，改写脚本不碰，只报告。
TEXT_EXT = {".md", ".yaml", ".yml", ".toml", ".json", ".sh", ".py", ".txt", ".mk"}
SKIP_DIRS = {".git", ".worktrees", ".v2c", ".mimosa", "node_modules"}


def load_matrix(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 modules.json，返回 (旧包路径 -> 新导入路径, 所属仓列表)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    repos: list[str] = []
    for mod in data["modules"]:
        repo = mod["repo"]
        repos.append(repo)
        for pkg in mod["packages"]:
            # 新路径取包的末段：knowledge/rag -> rag/rag, worker/pglease -> queue/pglease
            leaf = pkg.rsplit("/", 1)[-1]
            mapping[f"{OLD_ROOT}/{pkg}"] = f"github.com/{{org}}/{repo}/{leaf}"
    return mapping, repos


def rewrite_source(text: str, mapping: dict[str, str], org: str) -> tuple[str, list[str]]:
    """逐行状态机：只在 import 块/单行 import 内改写引号里的路径。返回 (新文本, 命中列表)。"""
    lines = text.split("\n")
    out: list[str] = []
    hits: list[str] = []
    in_block = False

    for line in lines:
        if in_block:
            if line.strip() == ")":
                in_block = False
                out.append(line)
                continue
            stripped = line.lstrip()
            if stripped.startswith("//") or not stripped:
                out.append(line)
                continue
            m = IMPORT_LINE_RE.match(line)
            if not m:
                # import 块内出现非 import 行（几乎不可能），保守放过并标记
                out.append(line)
                continue
            qm = QUOTED_RE.search(m.group(1))
            if qm:
                old = qm.group(1)
                new = _map(old, mapping, org)
                if new != old:
                    line = line.replace(f'"{old}"', f'"{new}"', 1)
                    hits.append(old)
            out.append(line)
            continue

        if BLOCK_START_RE.match(line):
            in_block = True
            out.append(line)
            continue

        m = SINGLE_IMPORT_RE.match(line)
        if m:
            qm = QUOTED_RE.search(m.group(1))
            if qm:
                old = qm.group(1)
                new = _map(old, mapping, org)
                if new != old:
                    line = line.replace(f'"{old}"', f'"{new}"', 1)
                    hits.append(old)
            out.append(line)
            continue

        out.append(line)

    return "\n".join(out), hits


def _map(old: str, mapping: dict[str, str], org: str) -> str:
    """精确匹配最长前缀；未登记的 agentkit 包在此暴露为错误（调用方负责计数）。"""
    if old == OLD_ROOT:
        return f"github.com/{org}/agentkit"
    best = None
    for src in mapping:
        if old == src or old.startswith(src + "/"):
            if best is None or len(src) > len(best):
                best = src
    if best is None:
        return old
    return mapping[best].replace("{org}", org) + old[len(best):]


def walk(root: Path, suffixes: set[str] | None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if suffixes is None or p.suffix in suffixes:
                yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default=DEFAULT_ORG,
                    help=f"目标 GitHub 组织名（默认 {DEFAULT_ORG}）")
    ap.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    ap.add_argument("--modules", default="docs/split-matrix/modules.json")
    ap.add_argument("--single", help="单仓整体改名目标路径，如 github.com/o/agentkit")
    ap.add_argument("--print-rules", action="store_true", help="打印映射表后退出")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件；有改动 exit 1")
    ap.add_argument("--apply", action="store_true", help="执行改写")
    ap.add_argument("--manual", action="store_true", help="列出需人工处理的非 Go 文件")
    args = ap.parse_args()

    if not args.org:
        print(f"错误：--org 不能为空（默认 {DEFAULT_ORG}）", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    mpath = Path(args.modules)
    if not mpath.is_absolute():
        mpath = root / mpath
    if not mpath.exists():
        print(f"错误：找不到矩阵文件 {mpath}", file=sys.stderr)
        return 2

    mapping, repos = load_matrix(mpath)

    if args.single:
        # 单仓模式：所有包统一搬到新路径下的同名子目录
        mapping = {k: f"{args.single}/{k[len(OLD_ROOT) + 1:]}" for k in mapping}

    if args.print_rules:
        print(f"# {len(mapping)} 条规则  org={args.org}  single={args.single or '-'}\n")
        width = max(len(k) for k in mapping)
        for src in sorted(mapping):
            dst = mapping[src].replace("{org}", args.org)
            print(f"{src:<{width}}  ->  {dst}")
        print(f"\n# {len(repos)} 个仓: {', '.join(sorted(repos))}")
        return 0

    if args.manual:
        print(f"# 非 Go 文件中出现 {OLD_ROOT} 的位置（脚本不碰，需人工确认）\n")
        found = 0
        for p in walk(root, TEXT_EXT):
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            n = text.count(OLD_ROOT)
            if n:
                found += n
                print(f"{n:>4}x  {p.relative_to(root)}")
        print(f"\n# 合计 {found} 处")
        return 0

    if not (args.check or args.apply):
        ap.print_help()
        return 2

    changed_files: list[tuple[Path, list[str]]] = []
    unknown: set[str] = set()
    total_hits = 0

    for p in walk(root, {".go"}):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if OLD_ROOT not in text:
            continue
        new_text, hits = rewrite_source(text, mapping, args.org)
        for h in hits:
            if _map(h, mapping, args.org) == h:
                unknown.add(h)
        if new_text != text:
            changed_files.append((p, hits))
            total_hits += len(hits)

    # go.mod 模块行（单仓模式）
    gomod = root / "go.mod"
    gomod_change = None
    if args.single and gomod.exists():
        t = gomod.read_text(encoding="utf-8")
        nt = MODULE_LINE_RE.sub(f"module {args.single}", t, count=1)
        if nt != t:
            gomod_change = nt

    mode = "apply" if args.apply else "check"
    print(f"# 模式={mode}  待改文件={len(changed_files)}  import 命中={total_hits}")

    if unknown:
        print(f"\n!! 下列路径未登记在矩阵中，已跳过（必须补进 modules.json）：", file=sys.stderr)
        for u in sorted(unknown):
            print(f"   {u}", file=sys.stderr)
        return 1

    for p, hits in changed_files:
        print(f"  {p.relative_to(root)}  ({len(hits)} 处)")
        if args.apply:
            p.write_text(_apply(p, mapping, args.org), encoding="utf-8")

    if gomod_change is not None:
        print(f"  go.mod  module -> {args.single}")
        if args.apply:
            gomod.write_text(gomod_change, encoding="utf-8")

    if args.apply:
        # 改写 import 路径会改变组内字母序（git.enjoye.top/... 原本排在 github.com/... 之前，
        # 改成 github.com/<org>/... 后要排到 github.com/cloudwego/... 之后），
        # 不重排就会让 gofmt 变脏、直接挂 CI 门禁。gofmt 是权威排序器，不要自己实现排序。
        fixed = _gofmt(root, [p for p, _ in changed_files])
        if fixed:
            print(f"\n# gofmt 已重排 {fixed} 个文件的 import（路径改写改变了字母序）")
        print("\n# 完成。下一步：go mod tidy && go build ./... && gofmt -l .")
        return 0

    if changed_files or gomod_change:
        print("\n# 有待改写内容（--check 不写文件）。确认无误后加 --apply。")
        return 1
    print("\n# 无待改写内容。")
    return 0


def _apply(p: Path, mapping: dict[str, str], org: str) -> str:
    text = p.read_text(encoding="utf-8")
    new_text, _ = rewrite_source(text, mapping, org)
    return new_text


def _gofmt(root: Path, files: list[Path]) -> int:
    """对改写过的文件跑 gofmt -w，重排因路径变化而错位的 import。返回实际被重排的文件数。"""
    if not files:
        return 0
    exe = shutil.which("gofmt") or "gofmt"
    before = []
    for p in files:
        try:
            before.append(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            before.append(None)
    try:
        subprocess.run([exe, "-w", *[str(p) for p in files]],
                       cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"!! gofmt 执行失败（{e}）。请手动跑 gofmt -w，否则 CI 门禁会因 import 排序挂掉。",
              file=sys.stderr)
        return 0
    return sum(1 for p, old in zip(files, before)
               if old is not None and p.read_text(encoding="utf-8") != old)


if __name__ == "__main__":
    sys.exit(main())
