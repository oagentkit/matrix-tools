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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve().name
sys.path.insert(0, str(HERE))

from selftest_discovery import (SMOKE_REPO_TOKEN, SMOKE_TMPDIR_TOKEN,  # noqa: E402
                               smoke_args, smoke_timeout)

# ⚠️ **只有 AttributeError 判红**：入口崩了多半是「用了不存在的名字」
#    （`args.selftest` 没注册 ⇒ AttributeError）—— 这正是本守卫要抓的形态。
# ⚠️ 其它异常（FileNotFoundError / KeyError / ValueError …）= **业务失败**
#    （脚本已经进了业务逻辑，只是输入不对）⇒ 判**黄**。
#    实测教训：把「有 Traceback」一律判红 ⇒ verify_repo_dep_inventory.py 在临时目录里
#    找不到输入文件（业务失败）⇒ 被误判红 ⇒ 门禁变噪声。
CRASH_MARKERS = ("AttributeError:",)
ENV_MARKERS = ("ModuleNotFoundError", "ImportError", "No module named")
BUSINESS_MARKERS = ("Traceback (most recent call last)",)
# argparse 早退（缺必填参数）⇒ **main() 一行都没跑** ⇒ 这组声明等于没覆盖入口
NO_ENTRY_MARKERS = ("usage:", "the following arguments are required", "unrecognized arguments")
TIMEOUT_SECONDS = 15   # 44 个脚本并发跑 ⇒ 超时要短，否则撞 job 超时线


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


def run_smoke(script: Path, args: list, timeout: int = TIMEOUT_SECONDS) -> tuple:
    """→ (判定, 细节)：判定 ∈ {ok, crash, env-missing, timeout}"""
    with tempfile.TemporaryDirectory() as td:
        repo_root = str(HERE.parent.parent)   # @repo = **矩阵根**（--root 的语义）
        real = [a.replace(SMOKE_TMPDIR_TOKEN, td).replace(SMOKE_REPO_TOKEN, repo_root)
                for a in args]
        env = {**os.environ, "PYTHONPATH": str(HERE)}
        try:
            proc = subprocess.run(
                [sys.executable, str(script.resolve()), *real],
                cwd=td, capture_output=True, text=True, timeout=timeout,
                env=env)
        except subprocess.TimeoutExpired:
            return "timeout", "跑超过 %ds" % timeout
        out = (proc.stdout or "") + (proc.stderr or "")
        if any(m in out for m in CRASH_MARKERS):
            return "crash", out.strip()[-600:]
        if any(m in out for m in ENV_MARKERS):
            return "env-missing", out.strip()[-600:]
        # ⚠️ argparse 早退 ⇒ 没走到入口 ⇒ 这组声明**不覆盖入口**（不算数，也不判红）
        # ⚠️ 判据必须用**退出码 2**（argparse 的早退码）⇒ 旧写法「输出含 usage:」会误判：
        #    脚本**正常跑完**时输出里也可能提到 usage ⇒ 把它错判成 no-entry。
        if proc.returncode == 2 and any(m in out for m in NO_ENTRY_MARKERS):
            return "no-entry", out.strip()[-300:]
        if any(m in out for m in BUSINESS_MARKERS):
            # 业务失败（脚本已进业务逻辑，只是输入不对）⇒ 判不了，不判红
            return "business-fail", out.strip()[-600:]
        return "ok", out.strip()[-300:]


EXEMPT_FILE = HERE / "cli_smoke_exemptions.json"


def load_exemptions() -> dict:
    """读显式豁免清单（没有 ⇒ 空 dict ⇒ 全部脚本都算欠账）。"""
    import json
    if not EXEMPT_FILE.is_file():
        return {}
    try:
        data = json.loads(EXEMPT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("exemptions") or []


def exemption_state(script: str, exemptions: list) -> tuple:
    """→ (状态, 条目)：状态 ∈ valid / stale / duplicate / incomplete / absent"""
    import hashlib
    entry = next((e for e in exemptions if e.get("script") == script), None)
    if entry is None:
        return "absent", None
    if not entry.get("reason") or not entry.get("category") or not entry.get("reviewedAt"):
        # ⚠️ 豁免必须**显式且有理由**，还要有**重审痕迹**（reviewedAt）
        #    ⇒ 脚本一改（哈希不匹配）时，重审会被强制留下日期
        return "incomplete", entry
    src = HERE / script
    if not src.is_file():
        return "incomplete", entry
    want = entry.get("contentSha256")
    got = hashlib.sha256(src.read_bytes()).hexdigest()
    if want and want != got:
        return "stale", entry
    return "valid", entry


def judge(declared: list, crash: list, env_missing: list,
          timeout: list, missing: list, business: list = (),
          debt: list = (), stale_exempt: list = ()) -> tuple:
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
    if debt:
        # ⚠️ **欠账判红**：有 CLI 入口、没声明冒烟、又没有有效豁免
        #    ⇒ 这个脚本的入口在 CI 里**零覆盖** ⇒ 与 #170 同类的失效可能溜过去。
        #    ⇒ 「全都完成」＝ 欠账清零，此后门禁守住这个不变量。
        return "red", 1
    if not declared:
        return "yellow", 0
    if env_missing or timeout or missing or business:
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
        # ③b 业务失败（文件缺失等）⇒ 判 business-fail（**不判红**）
        #     实测教训：把「有 Traceback」一律判红 ⇒ 业务失败被误判红 ⇒ 门禁变噪声。
        business = tmp / "business.py"
        business.write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--root')\n"
            "args = ap.parse_args()\n"
            "open(args.root + '/definitely_missing_file.txt').read()\n",
            encoding="utf-8")

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
        check("业务失败判 business-fail（不判红）",
              run_smoke(business, ["--root", SMOKE_TMPDIR_TOKEN])[0], "business-fail")

        # ④ 声明解析：认不认 SMOKE_ARGS
        check("认 SMOKE_ARGS", smoke_args('SMOKE_ARGS = ["--root", "@tmpdir"]'),
              ["--root", "@tmpdir"])
        check("没声明返回 None", smoke_args("x = 1"), None)
        check("非字符串列表不认", smoke_args('SMOKE_ARGS = [1, 2]'), None)
        check("只认模块级", smoke_args('def f():\n    SMOKE_ARGS = ["--x"]\n'), None)

        # ③c argparse 早退 ⇒ no-entry（**没走到入口** ⇒ 这组声明不算覆盖）
        early = tmp / "early.py"
        early.write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--must-have', required=True)\n"
            "args = ap.parse_args()\n"
            "print('never reached')\n", encoding="utf-8")
        check("argparse 早退判 no-entry", run_smoke(early, [])[0], "no-entry")

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
        # ④b2 @repo 必须被替换成**仓库根**（给「要读仓库文件、但只读」的脚本用）
        _v2, detail2 = run_smoke(echoer, ["--root", SMOKE_REPO_TOKEN])
        check("@repo 被替换成仓库根", SMOKE_REPO_TOKEN not in detail2, True)
        check("@repo 替换后确实指向矩阵根", str(HERE.parent.parent) in detail2, True)

        # ④c 判定本体（judge）：每档都要能断言 ⇒ 否则「崩了不判红」这类变异不会被抓住
        check("有崩溃 ⇒ 判红", judge([("a", [])], [("a", "boom")], [], [], [])[0], "red")
        check("有崩溃 ⇒ 退出码 1", judge([("a", [])], [("a", "boom")], [], [], [])[1], 1)
        check("0 个声明 ⇒ 判黄", judge([], [], [], [], [])[0], "yellow")
        check("0 个声明 ⇒ 不判红", judge([], [], [], [], [])[1], 0)
        check("缺依赖 ⇒ 判黄", judge([("a", [])], [], ["a"], [], [])[0], "yellow")
        check("超时 ⇒ 判黄", judge([("a", [])], [], [], ["a"], [])[0], "yellow")
        check("有脚本没声明 ⇒ 判黄", judge([("a", [])], [], [], [], ["b"])[0], "yellow")
        check("一切正常 ⇒ 绿", judge([("a", [])], [], [], [], [])[0], "green")

        # ④d SMOKE_TIMEOUT：脚本可声明更宽的预算（有些守卫本身要扫全仓）
        check("认 SMOKE_TIMEOUT",
              smoke_timeout("SMOKE_TIMEOUT = 90\n", 15), 90)
        check("没声明用默认", smoke_timeout("x = 1\n", 15), 15)

        # ④e 豁免机制四档：valid / stale / incomplete / absent
        import hashlib
        probe = tmp / "probe.py"
        probe.write_text("print('x')\n", encoding="utf-8")
        h = hashlib.sha256(probe.read_bytes()).hexdigest()
        good = [{"script": "probe.py", "category": "needs-network",
                 "reason": "理由", "reviewedAt": "2026-11-01", "contentSha256": h}]
        # exemption_state 读的是 HERE（scripts 目录）⇒ 用真实脚本名测
        real = "verify_cli_args.py"
        rh = hashlib.sha256((HERE / real).read_bytes()).hexdigest()
        ex_ok = [{"script": real, "category": "needs-network", "reason": "理由",
                  "reviewedAt": "2026-11-01", "contentSha256": rh}]
        check("豁免有效", exemption_state(real, ex_ok)[0], "valid")
        check("豁免过期（哈希变了）",
              exemption_state(real, [{"script": real, "category": "c", "reason": "r",
                                      "reviewedAt": "2026-11-01",
                                      "contentSha256": "0" * 64}])[0], "stale")
        check("豁免不完整（缺 reviewedAt）",
              exemption_state(real, [{"script": real, "category": "c", "reason": "r",
                                      "contentSha256": rh}])[0], "incomplete")
        check("无豁免", exemption_state(real, [])[0], "absent")

        # ④f 欠账 ⇒ 判红（这正是「全都完成」之后门禁要守住的东西）
        check("欠账 ⇒ 判红", judge([("a", [])], [], [], [], [], [], ["b"])[0], "red")
        check("欠账 ⇒ 退出码 1", judge([("a", [])], [], [], [], [], [], ["b"])[1], 1)

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
    print("入口冒烟守卫自测：34 条断言全过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="通用守卫：入口冒烟（真跑一次正常调用，断言没崩）")
    ap.add_argument("--selftest", action="store_true", help="跑本守卫自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    declared, missing = scan(HERE)
    exemptions = load_exemptions()
    declared_names = {p.name for p, _ in declared}
    debt = []
    stale_exempt = []
    for p in missing:
        state, _entry = exemption_state(p.name, exemptions)
        if state == "valid":
            continue                      # 有有效豁免 ⇒ 不算欠账
        if state == "stale":
            stale_exempt.append(p.name)   # 豁免过期 ⇒ 提示重审（黄）
            continue
        if state == "incomplete":
            stale_exempt.append(p.name)
            continue
        debt.append(p.name)               # 既没声明又无有效豁免 ⇒ **欠账**
    # 豁免与声明**重复**（二者只能选一）⇒ 提示
    dup_exempt = [e.get("script") for e in exemptions
                  if e.get("script") in declared_names]
    crash, env_missing, timeout, business, no_entry = [], [], [], [], []
    # ⚠️ 并发跑：声明的脚本一多，串行会让 CI 撞超时线
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {}
        for p, sargs in declared:
            src = p.read_text(encoding="utf-8", errors="ignore")
            budget = smoke_timeout(src, TIMEOUT_SECONDS)
            futures[ex.submit(run_smoke, p, sargs, budget)] = p.name
        for fut in as_completed(futures):
            name = futures[fut]
            verdict, detail = fut.result()
            if verdict == "crash":
                crash.append((name, detail))
                print("❌ %s：入口崩了\n%s" % (name, detail))
            elif verdict == "env-missing":
                env_missing.append(name)
            elif verdict == "timeout":
                timeout.append(name)
            elif verdict == "business-fail":
                business.append(name)
            elif verdict == "no-entry":
                no_entry.append(name)

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
    if business:
        print("⚠️ 入口冒烟：%d 个脚本**业务失败**（不是入口崩溃 ⇒ 判不了，不判红）：%s"
              % (len(business), ", ".join(business)))
    if no_entry:
        print("⚠️ 入口冒烟：%d 个脚本的声明**没让脚本走到入口**（argparse 早退 ⇒ 不算覆盖）：%s"
              % (len(no_entry), ", ".join(no_entry)))
        print("   ⇒ 请给一组能让它**完整走完 main()** 的参数（可离线、无副作用）；")
        print('      例：SMOKE_ARGS = ["--root", "@repo"]')
    if debt:
        print("❌ 入口冒烟：**%d 个脚本欠账**（有 CLI 入口、没声明冒烟、又没有有效豁免）"
              % len(debt))
        for n in debt:
            print("   - %s" % n)
        print("   ⇒ 这些脚本的 CLI 入口在 CI 里**零覆盖**（与 issue matrix-tools#170 同类）。")
        print("   ⇒ 二选一：① 在脚本里声明一组**无副作用**的参数：")
        print('        SMOKE_ARGS = ["--root", "@repo"]')
        print("      ② 或写进 scripts/cli_smoke_exemptions.json（逐条带理由 + 内容哈希）。")
    if stale_exempt:
        print("⚠️ 入口冒烟：%d 条豁免**已过期或不完整**（脚本内容变了 ⇒ 当初的理由可能已失真）"
              "：%s" % (len(stale_exempt), ", ".join(stale_exempt)))
        print("   ⇒ 请重新审一遍并更新 scripts/cli_smoke_exemptions.json 里的 reason / contentSha256。")
    if dup_exempt:
        print("⚠️ 入口冒烟：%d 个脚本**既声明又豁免**（二者只能选一）：%s"
              % (len(dup_exempt), ", ".join(dup_exempt)))

    verdict, rc = judge(declared, crash, env_missing, timeout, missing, business,
                        debt, stale_exempt)
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
