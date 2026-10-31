#!/usr/bin/env python3
"""自测脚本的**发现层**：哪些脚本要跑、怎么跑。

## 为什么要有这一层（2026-10-31）

CI 里「跑哪些自测」这份清单**靠人记** ⇒ 必然漂移。实测形态：
`bump_dep_floors.py --selftest` 写了两周，**根本没进 CI**（写了没人跑＝摆设）；
`test_sync_matrix.py` 也没进 ⇒ 本地绿、CI 红。

原先的发现判据是「源码里含 `selftest` 这个子串」⇒ 两个方向的错都犯：

- **假阳性**：任何**提到** selftest 的文件都被算进去（哪怕它根本没有 selftest 入口）
  ⇒ 跑 `--selftest` 时脚本不认这个参数 ⇒ 报「unrecognized arguments」⇒ 误判红。
- **假阴性**：用了别的参数名（`--self-test`）或手工判 `sys.argv` 的脚本被漏掉。

⇒ 判据必须**结构化**：问「这个脚本**真的注册了** `--selftest` 入口吗」，
而不是「它的文本里出现过 selftest 吗」。

## 判据（AST，不看字符串）

一个脚本支持 `--selftest`，当且仅当它满足任一条：

1. `argparse` 注册了 `--selftest`（`add_argument("--selftest", ...)`）；
2. 模块级定义了 `def selftest(...)` 或 `def _selftest(...)`。

`scripts/test_*.py` 走**普通运行**（不传参）—— 那是「接线自测」那一拨。

## 边界

本模块**不含任何判定**：它只回答「跑哪些、怎么跑」。
「跑完算不算过」是 `selftest_all.py` 的事；「这些自测够不够」是
`verify_inventory.py` 的事（它拿本模块的集合去核对登记表的接线）。

## 用法

    from selftest_discovery import discover
    plain, with_selftest = discover(Path("scripts"))
"""
from __future__ import annotations

import ast
from pathlib import Path

SELFTEST_FLAG = "--selftest"


def _string_constants(tree: ast.AST) -> set[str]:
    """模块级（或类级）`NAME = "字面量"` 里取值为 --selftest 的常量名。

    ⚠️ 必须支持这种写法：`ap.add_argument(SELFTEST_FLAG, ...)`——
    判据若只认字面量，**本模块自己**就会被判成「没有 selftest 入口」
    （实测踩到：`SELFTEST_FLAG = "--selftest"` ⇒ 常量引用 ⇒ 漏判）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and node.value.value == SELFTEST_FLAG:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _has_selftest_entry(src: str) -> bool:
    """这个源码**真的**提供了 `--selftest` 入口吗（AST 判据）。

    解析失败 ⇒ 判 False（拿不准就不算它有自测入口）
    ⇒ 宁可漏跑（会被「集合 ⊇ 登记判据」那条判据抓住），也不误跑（会误判红）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    const_names = _string_constants(tree)
    for node in ast.walk(tree):
        # ① 模块级 def selftest(...) / def _selftest(...)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("selftest", "_selftest"):
                return True
        # ② argparse 注册了 --selftest（字面量或引用常量）
        elif isinstance(node, ast.Call):
            func = node.func
            attr = getattr(func, "attr", None)
            if attr == "add_argument" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == SELFTEST_FLAG:
                    return True
                if isinstance(first, ast.Name) and first.id in const_names:
                    return True
    return False


def discover(scripts_dir: Path) -> tuple[list[Path], list[Path]]:
    """→ (普通运行的自测脚本, 用 --selftest 运行的守卫脚本)。

    ⚠️ **不排除任何文件**（含本模块自己）：排除自身是**聚合器**的责任
    （`selftest_all.py` 排除自己，否则会递归）。
    本模块若把自己排除掉 ⇒ 它的自测永远没人跑 ⇒ 「写了没人跑＝摆设」。
    """
    plain: list[Path] = []
    with_selftest: list[Path] = []
    for path in sorted(Path(scripts_dir).glob("*.py")):
        if path.name.startswith("test_"):
            plain.append(path)
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _has_selftest_entry(src):
            with_selftest.append(path)
    return plain, with_selftest


def plan_for(scripts_dir: Path) -> list[tuple[Path, list[str]]]:
    """→ [(脚本, 额外参数)]，可直接喂给 subprocess。"""
    plain, with_selftest = discover(scripts_dir)
    return plain + [(p, [SELFTEST_FLAG]) for p in with_selftest]


def _selftest() -> int:
    """自测：用**构造的 fixture** 验证发现判据有区分力。

    ⚠️ fixture 必须能区分「旧写法（子串匹配）」与「新写法（AST 判据）」——
    否则这条判据就是没有区分力的摆设。
    """
    import json
    import tempfile

    fails: list[str] = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        scripts = root / "scripts"
        scripts.mkdir()

        # ① test_ 前缀 ⇒ 普通运行那一拨
        (scripts / "test_a_wire.py").write_text("print('ok')\n", encoding="utf-8")
        # ② argparse 注册了 --selftest ⇒ 判据自测那一拨
        (scripts / "b_argparse.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--selftest', action='store_true')\n"
            "print('ok')\n", encoding="utf-8")
        # ③ 模块级 def _selftest ⇒ 也是那一拨
        (scripts / "c_func.py").write_text(
            "def _selftest():\n    return 0\n", encoding="utf-8")
        # ④ ⚠️ **反证样本**：正文里提到 selftest，但**没有**入口
        #    （旧写法「源码含 selftest 子串」会把它算进去 ⇒ 跑 --selftest 会报
        #      unrecognized arguments ⇒ 误判红）
        (scripts / "d_decoy.py").write_text(
            "# 这个文件只是在注释里提到 selftest，没有任何 selftest 入口。\n"
            "# 它不该被当成自测脚本。\n"
            "print('ok')\n", encoding="utf-8")
        # ⑤ ⚠️ **反证样本**：用了别的参数名 ⇒ 不是 --selftest 入口
        (scripts / "e_other_flag.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--self-test', action='store_true')\n"
            "print('ok')\n", encoding="utf-8")
        # ⑥ 语法错误 ⇒ 不得崩（跳过即可）
        (scripts / "f_broken.py").write_text("def (:\n", encoding="utf-8")

        plain, with_selftest = discover(scripts)
        plain_names = [p.name for p in plain]
        st_names = [p.name for p in with_selftest]

        check("普通运行那一拨", plain_names, ["test_a_wire.py"])
        check("判据自测那一拨", st_names, ["b_argparse.py", "c_func.py"])

        # 反证：提到 selftest 但没入口的，不得被发现
        check("反证：decoy 不得进自测集合", "d_decoy.py" in st_names, False)
        # 反证：别的参数名不算
        check("反证：别的 flag 不得进自测集合", "e_other_flag.py" in st_names, False)
        # 语法错误不得让发现崩掉（能跑到这里就说明没崩）
        check("语法错误的脚本不得进任何一拨",
              "f_broken.py" in plain_names + st_names, False)

        # ⑦ 真实仓库：发现集合必须覆盖所有登记判据（这条由 verify_inventory 判，
        #    这里只断言「判据不是空的」——空集合说明判据本身坏了）
        real_scripts = Path(__file__).resolve().parent
        r_plain, r_st = discover(real_scripts)
        check("真实仓库发现集合非空", len(r_plain) + len(r_st) > 0, True)
        check("真实仓库：接线自测那一拨非空", len(r_plain) > 0, True)
        check("真实仓库：判据自测那一拨非空", len(r_st) > 0, True)

    if fails:
        print("发现层自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("发现层自测：7 条断言全过")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="自测脚本发现层")
    ap.add_argument(SELFTEST_FLAG, action="store_true", help="跑本模块自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    scripts_dir = Path(__file__).resolve().parent
    plain, with_selftest = discover(scripts_dir)
    for p in plain:
        print("%s " % p.name)
    for p in with_selftest:
        print("%s %s" % (p.name, SELFTEST_FLAG))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
