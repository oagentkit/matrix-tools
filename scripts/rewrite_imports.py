#!/usr/bin/env python3
"""agentkit import 路径改写器。

把**历史** import 路径机械改写成**当前**路径。历史有两类：

  ① 旧单仓路径 `git.enjoye.top/enjoydream/agentkit/<pkg>` → `github.com/{org}/{repo}/{pkg}`
     （单仓 → 矩阵那次大迁移留下的，由 modules.json 的 packages 列表生成）；
  ② 矩阵**内部**的历史路径（某仓发版时自己改了包路径，如 llm v0.9.0 把
     `github.com/oagentkit/llm/llm` 收口成 `github.com/oagentkit/llm`）
     —— 由 scripts/historical_paths.json 登记。

两类路径都**曾经发布过**，下游可能正引用着 ⇒ 迁移必须能机械执行、可机械校验。

对标 oekit 的 matrix-tools/scripts/ekit2oekit.py（ekit→oekit 迁移），同样提供 --print-rules。

用法:
    rewrite_imports.py --org <org> --print-rules      # 看映射表，不动文件
    rewrite_imports.py --org <org> --check            # 只报告将要改什么，有改动则 exit 1
    rewrite_imports.py --org <org> --apply            # 真改写 .go 与 go.mod
    rewrite_imports.py --org <org> --manual           # 列出需人工处理的非 Go 文件
    rewrite_imports.py --selftest                     # 自测（带反证样本）
    rewrite_imports.py --single github.com/o/agentkit --apply   # 单仓整体改名（不拆）

设计约束:
  * 只改 import 块内的引号字符串，不碰注释、不碰其他字符串字面量 ——
    测试里断言模块路径、或文档里出现的旧路径必须原样保留。
  * 每个包必须命中映射表；未登记的包一律报错退出，不静默跳过。
    静默跳过是这类脚本最危险的失败模式：改了一半，看起来成功了。
  * ⚠️ **扫描范围必须覆盖映射表里的全部旧路径**，不能只认旧单仓根：
    只认旧单仓根时，「只 import 矩阵内部历史路径」的文件会被整个跳过 ⇒
    改写器什么都不做却退出 0（静默空转）。前缀集合见 _old_prefixes()。
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix_paths import (  # noqa: E402  路径构造规则由 matrix_paths 唯一持有
    DEFAULT_ORG,
    OLD_ROOT,
    build_mapping,
)

# 路径怎么拼（仓名 + 包名末段）由 matrix_paths.py 定义，本脚本不再自己推导。
# 这条规则曾经在本文件和迁移期临时脚本里各写一遍，任一方漏掉就是整仓构建断裂。
IMPORT_LINE_RE = re.compile(r'^(\s*(?:[\w.]+\s+)?"[^"]+")\s*(//.*)?$')
BLOCK_START_RE = re.compile(r"^\s*import\s*\(\s*(//.*)?$")
SINGLE_IMPORT_RE = re.compile(r'^(\s*import\s+(?:[\w.]+\s+)?"[^"]+")\s*(//.*)?$')
MODULE_LINE_RE = re.compile(r"^module\s+" + re.escape(OLD_ROOT) + r"\s*$", re.M)
QUOTED_RE = re.compile(r'"([^"]+)"')

# 非 Go 文件里出现旧路径时，这些是纯文档，改写脚本不碰，只报告。
TEXT_EXT = {".md", ".yaml", ".yml", ".toml", ".json", ".sh", ".py", ".txt", ".mk"}
SKIP_DIRS = {".git", ".worktrees", ".v2c", ".mimosa", "node_modules"}


def default_matrix() -> Path:
    """定位 modules.json。

    脚本从 agentkit 单仓搬到 matrix-tools 后，--modules 曾硬编码成旧布局的
    `docs/split-matrix/modules.json`，导致在本仓直接 FileNotFoundError。
    两个布局都认，避免再被搬一次就断。
    """
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "modules.json",
                 here.parent.parent / "docs/split-matrix/modules.json"):
        if cand.is_file():
            return cand
    return here.parent / "modules.json"


def load_matrix(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 modules.json，返回 (旧包路径 -> 新导入路径, 所属仓列表)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping = build_mapping(data["modules"], "{org}")
    repos = [mod["repo"] for mod in data["modules"]]
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
    """精确匹配最长前缀；未登记的 agentkit 包在此暴露为错误（调用方负责计数）。

    ⚠️ 键侧也要做 `{org}` 替换：历史路径表（historical_paths.json）里的键带 `{org}` 占位符，
    因为同一张表要服务多个 org（姊妹矩阵 oekit 也用本工具）。
    """
    if old == OLD_ROOT:
        return f"github.com/{org}/agentkit"
    best = None
    best_key = None
    for src in mapping:
        key = src.replace("{org}", org)
        if old == key or old.startswith(key + "/"):
            if best is None or len(key) > len(best_key):
                best, best_key = src, key
    if best is None:
        return old
    return mapping[best].replace("{org}", org) + old[len(best_key):]


def walk(root: Path, suffixes: set[str] | None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if suffixes is None or p.suffix in suffixes:
                yield p


def _old_prefixes(mapping: dict[str, str], org: str) -> list[str]:
    """扫描时要认的「旧路径前缀」集合 = 旧单仓根 + 映射表里全部键。

    键侧的 `{org}` 换成真实 org —— 因为文件里写的是具体 org 的路径。
    ⚠️ 只放 OLD_ROOT 是不够的：那会让「只 import 矩阵内部历史路径」的文件被整个跳过
    ⇒ 改写器静默空转（既不改写也不报错）。这个集合是那类文件的可见性保证。
    """
    return [OLD_ROOT] + [k.replace("{org}", org) for k in mapping]


def _selftest() -> int:
    """自测（带反证样本）。

    「跑通即绿」的自测是摆设——它证明不了本工具能抓住它要防的东西。
    所以这里每条断言都配一个**反证**：把被依赖的规则/前缀集合抽掉之后，
    断言必须**变红**；否则这条断言本身没有牙齿。

    覆盖三件事：
      ① 矩阵内部的历史路径能被改写（llm/llm → llm）；
      ② 旧单仓路径仍然照改（不回归）；
      ③ 未登记的路径仍然**报错退出**（fail-loud 不许退化成静默跳过），
         且扫描前缀集合覆盖历史路径（否则相关文件被整个跳过 ⇒ 静默空转）。
    """
    fails: list[str] = []
    org = "oagentkit"
    mapping, _ = load_matrix(default_matrix())
    hist_key = "github.com/{org}/llm/llm"
    hist_path = f"github.com/{org}/llm/llm"

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✅' if ok else '❌'} {name}")
        if not ok:
            if detail:
                print(f"      {detail}")
            fails.append(name)

    print("rewrite_imports 自测（每条断言配反证）\n")

    # ① 矩阵内部的历史路径：llm/llm → llm
    src = 'package p\n\nimport (\n\t"github.com/oagentkit/llm/llm"\n)\n'
    out, hits = rewrite_source(src, mapping, org)
    check("历史路径 github.com/oagentkit/llm/llm 被改写成 github.com/oagentkit/llm",
          '"github.com/oagentkit/llm"' in out and hits == [hist_path],
          f"实际输出：{out!r} hits={hits}")

    # ① 反证：抽掉历史规则后，上面那条断言必须失效（证明它有牙齿）
    naked = {k: v for k, v in mapping.items() if k != hist_key}
    out_naked, hits_naked = rewrite_source(src, naked, org)
    check("【反证】抽掉历史规则 ⇒ 改写失效（断言有牙齿）",
          hist_path in out_naked and hits_naked == [],
          f"实际输出：{out_naked!r} hits={hits_naked}")

    # ② 旧单仓路径仍然照改（不回归）
    legacy_src = 'package p\n\nimport (\n\t"git.enjoye.top/enjoydream/agentkit/llm"\n)\n'
    legacy_out, legacy_hits = rewrite_source(legacy_src, mapping, org)
    check("旧单仓路径仍被改写（不回归）",
          '"github.com/oagentkit/llm"' in legacy_out and legacy_hits != [],
          f"实际输出：{legacy_out!r}")

    # ③ 未登记的路径：必须原样返回 ⇒ 由 main() 计入 unknown ⇒ exit 1（fail-loud）
    unknown_path = "github.com/oagentkit/llm/nosuchpkg"
    got = _map(unknown_path, mapping, org)
    check("未登记的路径原样返回（由调用方报错退出，不静默跳过）",
          got == unknown_path, f"实际返回：{got}")

    # ③ 反证：未登记路径**不能**被历史规则误伤（llm/llm 前缀不得吃掉 llm/llmtest）
    sibling = "github.com/oagentkit/llm/llmtest"
    got_sibling = _map(sibling, mapping, org)
    check("【反证】历史规则不得误伤同前缀的兄弟路径（llm/llmtest）",
          got_sibling == sibling, f"实际返回：{got_sibling}")

    # ③ 扫描前缀集合必须覆盖历史路径（否则只含历史路径的文件被整个跳过 ⇒ 静默空转）
    prefixes = _old_prefixes(mapping, org)
    check("扫描前缀集合覆盖历史路径（只含历史路径的文件不会被跳过）",
          any(hist_path in p for p in prefixes),
          f"前缀集合：{prefixes[:5]}…")

    # ③ 反证：旧的「只看 OLD_ROOT」写法确实覆盖不到历史路径（这就是静默空转的根因）
    check("【反证】旧的「只看 OLD_ROOT」写法覆盖不到历史路径（静默空转的根因）",
          not any(hist_path in p for p in [OLD_ROOT]),
          "反例不成立：说明前缀集合的必要性无从证明")

    if fails:
        print(f"\n❌ {len(fails)} 项不符：{fails}")
        return 1
    print("\n✅ 全部断言通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default=DEFAULT_ORG,
                    help=f"目标 GitHub 组织名（默认 {DEFAULT_ORG}）")
    ap.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    ap.add_argument("--modules", default=str(default_matrix()),
                    help="modules.json 路径（默认自动定位 matrix-tools 根，其次 agentkit 旧布局）")
    ap.add_argument("--single", help="单仓整体改名目标路径，如 github.com/o/agentkit")
    ap.add_argument("--print-rules", action="store_true", help="打印映射表后退出")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件；有改动 exit 1")
    ap.add_argument("--apply", action="store_true", help="执行改写")
    ap.add_argument("--manual", action="store_true", help="列出需人工处理的非 Go 文件")
    ap.add_argument("--selftest", action="store_true", help="跑自测（带反证样本）后退出")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

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
        # ⚠️ 只有**旧单仓**的键能这么切；历史路径表里的键（矩阵内部路径）不是旧单仓路径，
        # 早期写法 `k[len(OLD_ROOT) + 1:]` 会把它们切成垃圾 ⇒ 这里按前缀判定。
        mapping = {
            k: (f"{args.single}/{k[len(OLD_ROOT) + 1:]}"
                if k == OLD_ROOT or k.startswith(OLD_ROOT + "/") else v)
            for k, v in mapping.items()
        }

    if args.print_rules:
        # 映射表由 matrix_paths.py 生成（本脚本与 llms.txt / versions.json 共用同一来源），
        # 这样「文档里的规则」与「脚本实际执行的规则」不可能各说各话。
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "matrix_paths.py"),
             "--matrix", str(mpath), "--org", args.org],
        ).returncode

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

    # 只扫「含有任一已知旧路径前缀」的文件。
    # ⚠️ 早先写的是 `if OLD_ROOT not in text: continue` —— **只**认旧单仓根。
    # 于是「只 import 矩阵内部历史路径」（如 github.com/oagentkit/llm/llm）的文件
    # 被**整个跳过**：既不改写、也不报错 ⇒ 改写器「什么都没做」却退出 0
    # ⇒ 下游拿到一个静默空转的迁移器，直到编译报 module not found 才发现。
    prefixes = _old_prefixes(mapping, args.org)

    for p in walk(root, {".go"}):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not any(pref in text for pref in prefixes):
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
        print(f"\n!! 下列路径未登记在矩阵中，已跳过（必须补进 modules.json 的 packages，"
              f"或 historical_paths.json 的历史路径表）：", file=sys.stderr)
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
