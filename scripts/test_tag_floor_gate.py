#!/usr/bin/env python3
"""打 tag 前的「依赖地板时效」门禁 —— 自测（接线 + 行为 + 反证样本）。

## 为什么要有这道门禁（2026-10-27 实测）

上一轮发 textx v0.7.0 / queue v0.6.4 之前，我跑了
`verify_tag_floor.py --org oagentkit --latest-only` ⇒ ✅ 绿（基线内欠账 0、
基线外新增 0）⇒ 于是放心打了 tag。

**但那两个新 tag 当时根本还不存在** ⇒「各仓最新 tag」判的是**上一批**
（v0.6.2 / v0.6.3）⇒ 那个绿毫无意义。CI 的 `Release floor` job 随后判红：

- textx@v0.7.0 钉 `oekit/pkg` v0.20.0，而发版时刻最新已是 v0.28.0；
- queue@v0.6.4 钉 `oekit/concurrency` v0.19.0，而发版时刻最新已是 v0.22.0。

tag 受 protect-tags ruleset 保护**不能重打** ⇒ 只能补发 textx v0.7.1 /
queue v0.6.5（只抬地板、不改行为）才清零。

⇒ 病根不是「门禁没跑」，是**门禁在打 tag 之前看不见那个还没诞生的 tag**。
修法是把这道检查放进**打 tag 这个动作里**（`release.py cmd_tag`），
让它自己会拒绝，而不是指望人记得先跑对口径的门禁。

## 这个自测要钉住什么

1. **行为**：地板落后 ⇒ 必须报出问题；地板 == 最新 ⇒ 必须放过；
   **判不了（取不到 tag）⇒ 也必须报出问题**（硬规则 3：判不了 ≠ 没问题）；
   没有第一方依赖 ⇒ 无事可判。
2. **接线**：`release.py` 的 `cmd_tag` 里必须真的调用了它。
3. **反证样本**：把接线抽掉（改一份副本）⇒ 接线判定必须变红。
   没有反证样本的自测会在「接线被悄悄删掉」时照样绿 ——
   而这正是这类「义务缺失」型修复的典型失效形态（它不会让任何门禁变红）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import release  # noqa: E402

RELEASE_PY = HERE / "release.py"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


# ---------------------------------------------------------------- 假仓


def make_fake_repo(root: Path, repo: str, go_mod: str) -> None:
    """造一个只含 go.mod 的 git 仓（够 floor_gate_problems 取数用）。"""
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(go_mod, encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(d), "add", "go.mod"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)


def fake_tags(tags: list[tuple[str, str]]):
    """→ 一个可注入的 tags_of（owner, repo, min_version) -> [(tag, date)]。"""
    def _t(owner: str, repo: str, min_version: str | None = None):
        return tags
    return _t


# ---------------------------------------------------------------- 行为

def test_behavior() -> None:
    print("行为：")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # ① 地板落后 ⇒ 必须报出，且能指出该抬到哪个版本
        make_fake_repo(root, "lag", "module x\n\ngo 1.24.0\n\n"
                                   "require github.com/oekit/pkg v0.20.0\n")
        probs = release.floor_gate_problems(
            root, "lag", "HEAD",
            tags_of=fake_tags([("v0.20.0", "2026-10-01T00:00:00+00:00"),
                               ("v0.28.0", "2026-10-25T00:00:00+00:00")]))
        real = [p for p in probs if not p.get("note")]
        check("地板落后 ⇒ 报出问题", len(real) == 1, f"got {probs}")
        check("报出正确的依赖与目标版本",
              bool(real) and real[0]["dep"] == "github.com/oekit/pkg"
              and real[0]["should_be"] == "v0.28.0",
              f"got {real}")

        # ② 地板 == 最新 ⇒ 放过
        make_fake_repo(root, "ok", "module y\n\ngo 1.24.0\n\n"
                                  "require github.com/oekit/pkg v0.28.0\n")
        probs = release.floor_gate_problems(
            root, "ok", "HEAD",
            tags_of=fake_tags([("v0.28.0", "2026-10-25T00:00:00+00:00")]))
        check("地板 == 最新 ⇒ 放过", probs == [], f"got {probs}")

        # ③ 判不了（取不到该依赖的 tag）⇒ 也必须报问题（不许当成绿）
        probs = release.floor_gate_problems(
            root, "ok", "HEAD", tags_of=fake_tags([]))
        check("判不了 ⇒ 报出问题（判不了 ≠ 没问题）", len(probs) == 1, f"got {probs}")
        check("判不了时带 note",
              bool(probs) and bool(probs[0].get("note")), f"got {probs}")

        # ④ 没有第一方依赖 ⇒ 无事可判
        make_fake_repo(root, "nodep", "module z\n\ngo 1.24.0\n\n"
                                     "require github.com/lib/pq v1.12.3\n")
        probs = release.floor_gate_problems(
            root, "nodep", "HEAD", tags_of=fake_tags([]))
        check("无第一方依赖 ⇒ 放过", probs == [], f"got {probs}")


# ---------------------------------------------------------------- 接线

def cmd_tag_calls_floor_gate(src: str) -> bool:
    """AST 判定：`cmd_tag` 的函数体里有没有真的调用地板门禁。

    为什么用 AST 而不是 grep：grep 只能证明「文本里出现过这个词」，
    而这个词完全可能出现在注释或 docstring 里 —— 那就是**假接线**。

    为什么要求**两个**调用都在：取数（`floor_gate_problems`）与
    报告（`report_floor_gate`）缺一不可 ——
    只取数不报告 = 判完了没人看（静默空转）；
    只报告不取数 = 报告的是空结果（永远绿）。
    """
    tree = ast.parse(src)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_tag":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    name = f.id if isinstance(f, ast.Name) else (
                        f.attr if isinstance(f, ast.Attribute) else "")
                    if name in ("floor_gate_problems", "report_floor_gate"):
                        found.add(name)
    return found == {"floor_gate_problems", "report_floor_gate"}


def has_skip_switch(src: str) -> bool:
    """豁免开关必须存在、且**默认不生效**（默认空串 ⇒ 不跳过）。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "add_argument":
                for kw in node.keywords:
                    if kw.arg == "default":
                        v = kw.value
                        if isinstance(v, ast.Constant) and v.value == "":
                            # 再确认 flag 名确实是 --skip-floor-check
                            if node.args and isinstance(node.args[0], ast.Constant) \
                                    and node.args[0].value == "--skip-floor-check":
                                return True
    return False


def test_wiring() -> None:
    print("接线：")
    src = RELEASE_PY.read_text(encoding="utf-8")
    check("cmd_tag 里真的调用了地板门禁", cmd_tag_calls_floor_gate(src))
    check("存在显式豁免开关且默认不生效", has_skip_switch(src))

    # 反证样本：抽掉接线 ⇒ 判定必须变红
    print("反证样本：")
    for label, needle, repl in [
        ("抽掉取数调用", 'floor_gate_problems(root, args.repo, "HEAD")',
         '__REMOVED__(root, args.repo, "HEAD")'),
        ("抽掉报告调用", "report_floor_gate(rep, args.repo,", "report_floor_gate_DISABLED(rep, args.repo,"),
    ]:
        broken = src.replace(needle, repl)
        check(f"{label}后源码确实变了（反证样本有效）", broken != src)
        check(f"{label}后接线判定变红", not cmd_tag_calls_floor_gate(broken))

    broken2 = src.replace('"--skip-floor-check", default=""',
                          '"--skip-floor-check", default="yes"')
    check("豁免开关默认非空时判定变红", broken2 != src and not has_skip_switch(broken2))


def main() -> int:
    print("== 打 tag 前的依赖地板时效门禁 · 自测 ==")
    test_behavior()
    test_wiring()
    if FAILS:
        print(f"\n❌ {len(FAILS)} 项不通过：{'、'.join(FAILS)}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
