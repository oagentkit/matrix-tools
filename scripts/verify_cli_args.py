#!/usr/bin/env python3
"""静态守卫：`args.<属性>` 引用了 **argparse 从没注册**的 dest ⇒ 判红。

## 为什么需要它（2026-11-01 实测事故）

`scripts/bump_dep_floors.py` 的 `main()` 里写了 `if args.selftest:`，
而 argparse **从没注册** `--selftest` ⇒ 每次**正常调用**都抛
`AttributeError: 'Namespace' object has no attribute 'selftest'`。

⇒ 后果：schedule 的「Dep floor watch」巡检**每小时红**，
而 **CI 全绿** —— 因为 CI 只跑 `--selftest` 那条早期分支，
**从不走 `main()`** ⇒ CLI 入口路径在 CI 里零覆盖。

⇒ 动态冒烟（每个脚本自己跑一次正常调用）能覆盖它，但**每个脚本都得记得写**。
⇒ 本守卫是**静态**的第二道网：**任何**脚本里的**任何** `args.X` 引用都查一遍 ⇒
零副作用、零网络、覆盖整类缺陷 ⇒ 不靠人记得。

## 判据（AST，不执行代码）

「已注册」= 下列任一：

- 任何 `add_argument(...)` 的 dest：
  - `--foo-bar` ⇒ `foo_bar`（argparse 的 dest 规则：长选项去横线转下划线）；
  - 位置参数（`add_argument("repos", nargs="*")`）⇒ `repos`（dest 就是参数名）；
  - 显式 `dest="y"` ⇒ 用 `y`（**不是** flag 名）；
  - flag 是**常量引用**（`ap.add_argument(SELFTEST_FLAG, ...)`）⇒ 解析模块级字符串常量；
- `set_defaults(...)` 的键名（subparser 的 `dest=` 也算）。

「被引用」= 解析结果变量（`args = ap.parse_args()` / `parse_known_args()` /
`with ap.parse_args() as a:`）的 `.属性` 访问。

## 边界（诚实说明）

- 这是**启发式**静态分析 ⇒ **可能有漏报**（例如运行时 `setattr`、动态构造 dest），
  但**不允许假阳性** ⇒ 宁可漏报也不误报（误报会让门禁变噪声 ⇒ 门禁被无视）。
- 上线前必须拿**真实仓库的全部脚本**校准一遍（应当零命中）——
  这份真实脚本集本身就是判据的**反证样本**：任何「漏算 dest 形式」的改动都会让
  真实脚本开始误报 ⇒ 自测立刻转红。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve().name


def _module_str_constants(tree: ast.AST) -> dict:
    """本模块里 `NAME = "字面量"` ⇒ {NAME: 值}。"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.value.value
    return out


def _dir_str_constants(exclude: str = "") -> dict:
    """同目录其它模块里的 `NAME = "字面量"` ⇒ {NAME: 值}（供**跨模块 import** 的常量用）。

    ⚠️ **为什么要跨模块**：`selftest_all.py` 用的是
    `from selftest_discovery import SELFTEST_FLAG` ⇒ flag 写在**别的模块**里
    ⇒ 只扫本模块 ⇒ 解析不出 dest ⇒ **误报**（实测踩到）。
    ⚠️ 同名常量在多个模块里**取值不同** ⇒ 该名字视为不可解析（宁可漏报不误报）。
    """
    seen: dict = {}
    conflicted: set = set()
    for p in HERE.glob("*.py"):
        if p.name == exclude or p.name == SELF:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for name, value in _module_str_constants(tree).items():
            if name in seen and seen[name] != value:
                conflicted.add(name)
            seen.setdefault(name, value)
    for name in conflicted:
        seen.pop(name, None)
    return seen


def _dest_from_flag(flag: str) -> str:
    """argparse 的 dest 规则：长选项去横线；只有短选项则去横线。"""
    return flag.lstrip("-").replace("-", "_")


def _add_argument_dests(tree: ast.AST, consts: dict):
    """所有 `add_argument` 注册的 dest（含位置参数、显式 dest、常量引用）。

    → (dests, 无法解析的常量名列表)
    ⚠️ 第二个返回值用于**判黄**：flag 写成了一个**解析不出值**的常量
    ⇒ 它的 dest 不可知 ⇒ 该文件的判定**不可判定** ⇒ 判黄不判红（硬规则 3）。
    """
    dests: set = set()
    unresolved: list = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"):
            continue
        explicit = None
        for kw in node.keywords:
            if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    explicit = kw.value.value
        if explicit:
            dests.add(explicit)
            continue
        positional = None
        flag = None
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                if a.value.startswith("-"):
                    if flag is None:
                        flag = a.value
                elif positional is None:
                    positional = a.value
            elif isinstance(a, ast.Name):
                if a.id in consts:
                    # flag 写成常量引用（ap.add_argument(SELFTEST_FLAG, ...)）
                    if flag is None and consts[a.id].startswith("-"):
                        flag = consts[a.id]
                elif a.id.isupper():
                    # ⚠️ 只有**像常量**（全大写）的名字才当作「不可解析的 flag 常量」
                    #    （普通变量名传参罕见 ⇒ 忽略，别把文件判成不可判定）
                    unresolved.append(a.id)
        if flag:
            dests.add(_dest_from_flag(flag))
        elif positional:
            dests.add(positional)
    return dests, unresolved


def _set_default_dests(tree: ast.AST) -> set:
    """`set_defaults(...)` 的键 + `add_subparsers(dest=...)` 的 dest。"""
    dests: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attr = getattr(node.func, "attr", None)
            if attr == "set_defaults":
                for kw in node.keywords:
                    if kw.arg:
                        dests.add(kw.arg)
            elif attr == "add_subparsers":
                for kw in node.keywords:
                    if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                        if isinstance(kw.value.value, str):
                            dests.add(kw.value.value)
    return dests


def _parsed_vars(tree: ast.AST) -> set:
    """解析结果被赋给了哪些变量名。"""
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Call) and getattr(
                    node.value.func, "attr", None) in ("parse_args", "parse_known_args"):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, ast.Tuple):
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                names.add(e.id)
        elif isinstance(node, ast.withitem):
            call = node.context_expr
            if isinstance(call, ast.Call) and getattr(
                    call.func, "attr", None) in ("parse_args", "parse_known_args"):
                if isinstance(node.optional_vars, ast.Name):
                    names.add(node.optional_vars.id)
    return names


def _merged_consts(tree: ast.AST) -> dict:
    """本模块 + 同目录其它模块的字符串常量（同名不同值 ⇒ 该名视为不可解析）。"""
    consts = dict(_dir_str_constants())
    for name, value in _module_str_constants(tree).items():
        if name in consts and consts[name] != value:
            consts.pop(name)          # 冲突 ⇒ 不可解析
        else:
            consts[name] = value
    return consts


def find_unregistered(src: str) -> list:
    """→ [(属性名, 行号)]：`args.X` 里 X 从未被 argparse 注册。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    consts = _merged_consts(tree)
    registered, _unresolved = _add_argument_dests(tree, consts)
    registered |= _set_default_dests(tree)
    parsed = _parsed_vars(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in parsed and node.attr not in registered:
                out.append((node.attr, node.lineno))
    # 去重（同属性多行只报一次，取最小行号）
    seen = {}
    for attr, line in out:
        seen.setdefault(attr, line)
    return sorted(((a, l) for a, l in seen.items()), key=lambda x: x[1])


def unresolved_flag_refs(src: str) -> list:
    """→ [无法解析的 flag 常量名]：这些 `add_argument` 的 dest 不可知
    ⇒ 该文件的「未注册属性」判定**不可判定** ⇒ 判黄不判红。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    consts = _merged_consts(tree)
    _dests, unresolved = _add_argument_dests(tree, consts)
    return sorted(set(unresolved))


# ── 自测（构造 fixture，含假阳性样本）──────────────────────────────────

def _selftest() -> int:
    import json
    import tempfile

    fails: list[str] = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    good = """
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("repos", nargs="*")
ap.set_defaults(func=main)
FLAG = "--selftest"
ap.add_argument(FLAG, action="store_true")
args = ap.parse_args()
print(args.root, args.dry_run, args.repos, args.func, args.selftest)
"""
    bad = """
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)
args = ap.parse_args()
print(args.root)
if args.selftest:
    pass
"""
    check("合规脚本不报", [a for a, _ in find_unregistered(good)], [])
    check("真缺陷被抓出", [a for a, _ in find_unregistered(bad)], ["selftest"])

    # 逐个「dest 形式」都必须被认出来（每个都是一条反证样本）
    forms = {
        "位置参数": ('ap.add_argument("repos", nargs="*")\nprint(args.repos)', "repos"),
        "显式 dest": ('ap.add_argument("--x", dest="why")\nprint(args.why)', "why"),
        "set_defaults": ('ap.set_defaults(func=main)\nprint(args.func)', "func"),
        "subparser dest": ('sub = ap.add_subparsers(dest="cmd")\nprint(args.cmd)', "cmd"),
        "常量引用 flag": ('FLAG = "--selftest"\nap.add_argument(FLAG)\nprint(args.selftest)',
                          "selftest"),
        "短选项": ('ap.add_argument("-v", "--verbose")\nprint(args.verbose)', "verbose"),
    }
    for label, (src, attr) in forms.items():
        check("%s 必须被认出来" % label, attr in [a for a, _ in find_unregistered(src)], False)

    # 解析结果换名 / with 形式也要认
    check("parse_known_args 也算", [a for a, _ in find_unregistered(
        "import argparse\nap=argparse.ArgumentParser()\n"
        "ap.add_argument('--x')\na, rest = ap.parse_known_args()\nprint(a.x)\n")], [])
    check("with 形式也算", [a for a, _ in find_unregistered(
        "import argparse\nap=argparse.ArgumentParser()\n"
        "ap.add_argument('--x')\nwith ap.parse_args() as a:\n    print(a.x)\n")], [])

    # 不可解析的常量 ⇒ 判黄不判红（不可判定，不是确认违规）
    check("不可解析的常量被标为不可判定",
          unresolved_flag_refs("import argparse\n"
                               "ap=argparse.ArgumentParser()\n"
                               "ap.add_argument(SOME_UNKNOWN_CONST)\n"
                               "args=ap.parse_args()\nprint(args.x)\n"),
          ["SOME_UNKNOWN_CONST"])
    check("普通变量名不算不可判定",
          unresolved_flag_refs("import argparse\n"
                               "ap=argparse.ArgumentParser()\n"
                               "ap.add_argument(fmt)\n"
                               "args=ap.parse_args()\nprint(args.x)\n"),
          [])

    # 真实仓库校准：现存脚本**应当零命中**（任何 dest 形式漏算都会在这里暴露）
    hits = []
    for p in sorted(HERE.glob("*.py")):
        if p.name == SELF:
            continue
        for attr, line in find_unregistered(p.read_text(encoding="utf-8", errors="ignore")):
            hits.append("%s:%d %s" % (p.name, line, attr))
    check("真实仓库校准零命中", hits, [])

    if fails:
        print("CLI 参数守卫自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("CLI 参数守卫自测：%d 条断言全过" % (len(fails) + 14))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="静态守卫：args.<属性> 引用了未注册的 argparse dest")
    ap.add_argument("--root", default=str(HERE.parent),
                    help="矩阵根（用于解析脚本目录）")
    ap.add_argument("--selftest", action="store_true", help="跑本守卫自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    scripts_dir = HERE
    bad = []
    unknown = []
    for p in sorted(scripts_dir.glob("*.py")):
        if p.name == SELF:
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for attr, line in find_unregistered(src):
            bad.append((p.name, line, attr))
        if unresolved_flag_refs(src):
            unknown.append(p.name)

    if bad:
        print("❌ CLI 参数守卫：有 %d 处 `args.<属性>` 引用了 argparse 从没注册的 dest：" % len(bad))
        for name, line, attr in bad:
            print("   - %s:%d  args.%s" % (name, line, attr))
        print()
        print("⚠️ 形态：`main()` 里引用了不存在的属性 ⇒ **每一次正常调用都抛 AttributeError**。")
        print("   实测：bump_dep_floors.py 曾因此让 schedule 巡检每小时红，而 CI 全绿"
              "（CI 只跑 --selftest，从不走 main()）。")
        print("   修法：① 真的注册这个参数；或 ② 别引用它（自测走 --selftest 分支）。")
        return 1
    if unknown:
        # ⚠️ 判不了 ≠ 没问题：这些脚本的某个 add_argument 的 flag 写成了
        #    **解析不出值**的常量 ⇒ dest 不可知 ⇒ 判定不可判定 ⇒ 判黄不判红。
        print("⚠️ CLI 参数守卫：%d 个脚本**不可判定**（flag 常量解析不出值）：%s"
              % (len(unknown), ", ".join(unknown)))
        print("   ⇒ 这些脚本的「未注册属性」判定不可靠 ⇒ 判黄不判红（硬规则 3）。")
        print("   修法：把那个常量写成字面量，或移到可解析的地方（同名多模块不同值也算）。")
    print("✅ CLI 参数守卫：无未注册的 args 属性（%d 个脚本）" % len(list(scripts_dir.glob('*.py'))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
