#!/usr/bin/env python3
"""通用守卫：**入口冒烟** —— 真跑一次脚本的「正常调用」，断言它没崩。

## 为什么需要它（2026-11-01 实测事故，issue matrix-tools#170）

`bump_dep_floors.py` 曾因 `if args.selftest:` 引用了**没注册**的 argparse 属性
⇒ **每一次正常调用**都抛 `AttributeError`
⇒ schedule 的「Dep floor watch」巡检**每小时红**，而 **CI 全绿**
（CI 只跑 `--selftest` 那条早期分支，**从不走 `main()`**）⇒ CLI 入口零覆盖。

静态守卫 `verify_cli_args.py` 覆盖了「引用了没注册的属性」这一整类；
但「注册了却用错」的其它崩溃形态，**只有真跑一次才看得见** ⇒ 本守卫补这一半。

## 为什么要求**显式声明**（`SMOKE_ARGS`）

自动跑「正常调用」可能产生**副作用**（写文件 / 开 issue / 发 PR / 推分支）
⇒ 不能对所有脚本无差别地跑 ⇒ 只有**作者显式声明**了无副作用参数，才纳入冒烟
⇒ **声明本身就是承诺**。

```python
SMOKE_ARGS = ["--root", "@tmpdir", "--api-report"]   # @tmpdir → 临时目录
```

⚠️ 声明必须**无副作用**且**尽量不联网**（联网多的脚本跑起来慢，还会拖 CI）。

## 判定（三档，判不了 ≠ 没问题）

| 现象 | 判定 |
|---|---|
| 输出含 `AttributeError` | **红** —— 入口引用了不存在的属性（本事故的形态） |
| 输出含 `Traceback` 且**不是**缺依赖 | **红** —— 入口崩了 |
| 输出含 `Traceback` + `ModuleNotFoundError` / `ImportError` | **黄** —— 环境缺依赖，**判不了** |
| 跑超时 | **黄** —— 判不了（可能只是慢或在建连） |
| 有 CLI 入口但**没声明** `SMOKE_ARGS` | **黄** —— 欠账可见（不判红，避免逼人乱声明） |

⚠️ 判定**不看退出码**：业务失败也是非 0 ⇒ 只看输出里有没有崩溃痕迹。

## 隔离

- 在**临时工作目录**里跑（`cwd=tmpdir`）⇒ 相对路径的副作用落不到仓库里；
- `PYTHONPATH` 指到 `scripts/` ⇒ 兄弟模块 import 正常；
- 每次独立临时目录 ⇒ 互不干扰。

## 用法

    python3 scripts/verify_cli_smoke.py          # 真跑
    python3 scripts/verify_cli_smoke.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve().name
sys.path.insert(0, str(HERE))

from selftest_discovery import SMOKE_TMPDIR_TOKEN, smoke_args  # noqa: E402

CRASH_MARKERS = ("Traceback (most recent call last)", "AttributeError:")
ENV_MARKERS = ("ModuleNotFoundError", "ImportError", "No module named")
TIMEOUT_SECONDS = 90


def has_cli_entry(src: str) -> bool:
    """这个脚本有 CLI 入口吗（`if __name__ == "__main__"`）？"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
                if test.left.id == "__name__":
                    return True
    return False


def run_smoke(script: Path, args: list) -> tuple:
    """→ (判定, 细节)：判定 ∈ {ok, crash, env-missing, timeout}"""
    with tempfile.TemporaryDirectory() as td:
        real = [a.replace(SMOKE_TMPDIR_TOKEN, td) for a in args]
        env = {**os.environ, "PYTHONPATH": str(HERE)}
        try:
            proc = subprocess.run(
                [sys.executable, str(script.resolve()), *real],
                cwd=td, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                env=env)
        except subprocess.TimeoutExpired:
            return "timeout", "跑超过 %ds" % TIMEOUT_SECONDS
        out = (proc.stdout or "") + (proc.stderr or "")
        if any(m in out for m in CRASH_MARKERS):
            if any(m in out for m in ENV_MARKERS):
                return "env-missing", out.strip()[-600:]
            return "crash", out.strip()[-600:]
        return "ok", out.strip()[-300:]


def judge(declared: list, crash: list, env_missing: list,
          timeout: list, missing: list) -> tuple:
    """→ (结论, 退出码)：本守卫的**判定本体**（抽成纯函数才能被自测覆盖）。

    ⚠️ 为什么必须抽出来：判据写在 `main()` 里 ⇒ 自测只测底层函数 ⇒
    「崩了不判红」「0 声明不判黄」这类**变异不会让自测转红**
    ⇒ 断言没有区分力（实测踩到：两项变异都没转红）。

    | 情形 | 结论 |
    |---|---|
    | 有脚本的正常调用崩了 | **红**（rc=1） |
    | 一个脚本都没纳入冒烟 | **黄**（什么都没检查 ≠ 没问题） |
    | 有脚本判不了（缺依赖 / 超时 / 没声明） | **黄** |
    | 全部通过 | 绿 |
    """
    if crash:
        return "red", 1
    if not declared:
        return "yellow", 0
    if env_missing or timeout or missing:
        return "yellow", 0
    return "green", 0


def scan(scripts_dir: Path):
    """→ (声明了冒烟的脚本, 有 CLI 但没声明的脚本)"""
    declared, missing = [], []
    for p in sorted(scripts_dir.glob("*.py")):
        if p.name == SELF:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        args = smoke_args(src)
        if args is not None:
            declared.append((p, args))
        elif has_cli_entry(src):
            missing.append(p)
    return declared, missing


# ── 自测 ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import shutil

    fails: list[str] = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    tmp = Path(tempfile.mkdtemp())
    try:
        # ① 崩的脚本（复刻事故形态）：引用没注册的属性 ⇒ 必须判 crash
        crasher = tmp / "crasher.py"
        crasher.write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--root')\n"
            "args = ap.parse_args()\n"
            "if args.selftest:\n"
            "    pass\n", encoding="utf-8")
        # ② 正常脚本：走通（业务失败也行，不能崩）
        fine = tmp / "fine.py"
        fine.write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--root')\n"
            "args = ap.parse_args()\n"
            "print('ok', args.root)\n", encoding="utf-8")
        # ③ 缺依赖 ⇒ 判 env-missing（判不了，不判红）
        needer = tmp / "needer.py"
        needer.write_text(
            "import argparse\n"
            "import a_module_that_does_not_exist_xyz\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--root')\n"
            "args = ap.parse_args()\n", encoding="utf-8")

        check("崩的脚本判 crash", run_smoke(crasher, ["--root", SMOKE_TMPDIR_TOKEN])[0], "crash")
        check("正常脚本判 ok", run_smoke(fine, ["--root", SMOKE_TMPDIR_TOKEN])[0], "ok")
        check("缺依赖判 env-missing", run_smoke(needer, ["--root", SMOKE_TMPDIR_TOKEN])[0],
              "env-missing")

        # ④ 声明解析：认不认 SMOKE_ARGS
        check("认 SMOKE_ARGS", smoke_args('SMOKE_ARGS = ["--root", "@tmpdir"]'),
              ["--root", "@tmpdir"])
        check("没声明返回 None", smoke_args("x = 1"), None)
        check("非字符串列表不认", smoke_args('SMOKE_ARGS = [1, 2]'), None)
        check("只认模块级", smoke_args('def f():\n    SMOKE_ARGS = ["--x"]\n'), None)

        # ④b @tmpdir 必须真的被替换成临时目录（否则脚本会收到字面量）
        echoer = tmp / "echoer.py"
        echoer.write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--root')\n"
            "args = ap.parse_args()\n"
            "print(args.root)\n", encoding="utf-8")
        _v, detail = run_smoke(echoer, ["--root", SMOKE_TMPDIR_TOKEN])
        check("@tmpdir 被替换成临时目录", SMOKE_TMPDIR_TOKEN not in detail, True)
        check("@tmpdir 替换后不是仓库路径", str(HERE) not in detail, True)

        # ④c 判定本体（judge）：每档都要能断言 ⇒ 否则「崩了不判红」这类变异不会被抓住
        check("有崩溃 ⇒ 判红", judge([("a", [])], [("a", "boom")], [], [], [])[0], "red")
        check("有崩溃 ⇒ 退出码 1", judge([("a", [])], [("a", "boom")], [], [], [])[1], 1)
        check("0 个声明 ⇒ 判黄", judge([], [], [], [], [])[0], "yellow")
        check("0 个声明 ⇒ 不判红", judge([], [], [], [], [])[1], 0)
        check("缺依赖 ⇒ 判黄", judge([("a", [])], [], ["a"], [], [])[0], "yellow")
        check("超时 ⇒ 判黄", judge([("a", [])], [], [], ["a"], [])[0], "yellow")
        check("有脚本没声明 ⇒ 判黄", judge([("a", [])], [], [], [], ["b"])[0], "yellow")
        check("一切正常 ⇒ 绿", judge([("a", [])], [], [], [], [])[0], "green")

        # ⑤ CLI 入口识别
        check("认 CLI 入口", has_cli_entry("if __name__ == '__main__':\n    pass\n"), True)
        check("没有 CLI 入口", has_cli_entry("print(1)\n"), False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("入口冒烟守卫自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("入口冒烟守卫自测：19 条断言全过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="通用守卫：入口冒烟（真跑一次正常调用，断言没崩）")
    ap.add_argument("--selftest", action="store_true", help="跑本守卫自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    declared, missing = scan(HERE)
    crash, env_missing, timeout = [], [], []
    for p, sargs in declared:
        verdict, detail = run_smoke(p, sargs)
        if verdict == "crash":
            crash.append((p.name, detail))
            print("❌ %s：入口崩了\n%s" % (p.name, detail))
        elif verdict == "env-missing":
            env_missing.append(p.name)
        elif verdict == "timeout":
            timeout.append(p.name)

    if crash:
        print("❌ 入口冒烟：%d 个脚本的正常调用**崩了**" % len(crash))
        print("   形态：main() 里引用了不存在的属性 ⇒ 每一次正常调用都抛异常。")
        print("   实测：bump_dep_floors.py 曾因此让 schedule 巡检每小时红，而 CI 全绿。")
        return 1

    if env_missing:
        print("⚠️ 入口冒烟：%d 个脚本**判不了**（环境缺依赖）：%s"
              % (len(env_missing), ", ".join(env_missing)))
    if timeout:
        print("⚠️ 入口冒烟：%d 个脚本**判不了**（超时）：%s"
              % (len(timeout), ", ".join(timeout)))
    if missing:
        print("⚠️ 入口冒烟：%d 个有 CLI 入口的脚本**还没声明** SMOKE_ARGS（判不了，不判红）："
              % len(missing))
        for p in missing[:12]:
            print("     - %s" % p.name)
        if len(missing) > 12:
            print("     …（其余 %d 个略）" % (len(missing) - 12))
        print("   ⇒ 要纳入冒烟，请在脚本里声明一组**无副作用**的参数：")
        print('      SMOKE_ARGS = ["--root", "@tmpdir"]')

    verdict, rc = judge(declared, crash, env_missing, timeout, missing)
    if verdict == "yellow" and not declared:
        # ⚠️ 一个脚本都没纳入冒烟 ⇒ 本守卫**什么都没检查** ⇒ 判黄不判绿
        #    （纪律：判不了 ≠ 没问题；「没扫到任何东西」不等于「没问题」）
        print("⚠️ 入口冒烟：**没有任何脚本声明 SMOKE_ARGS** ⇒ 本守卫当前**什么都没检查** ⇒ 判黄。")
        print("   ⇒ 请在有 CLI 入口的脚本里声明一组**无副作用**的参数，例如：")
        print('      SMOKE_ARGS = ["--root", "@tmpdir"]')
        return 0
    print("✅ 入口冒烟：%d 个声明了冒烟的脚本，全部没崩" % len(declared))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
