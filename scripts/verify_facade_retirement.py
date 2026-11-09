#!/usr/bin/env python3
"""门面退役条件判定守卫：「该退役了吗」必须是一个脚本能回答的问题。

## 守的是哪条缺口（matrix-tools#196）

门面仓 agentkit 在 modules.json 与 README 里都承诺「会退役」，但退役条件
**没有可判定的形式**——modules.json 写「按 README 写死的期限退役」，
README 却从未写死任何期限（唯一写下的条件「旧单仓下线后归档」是**外生事件**，
本 org 无从判定）。⇒ 退役成了一个永远判不了、因而永远不会被执行的承诺：
冻结基线（v1.8.21）之后门面又发了 50+ 个版本，没有任何门禁会说「这个仓该退了」。

本守卫把退役条件机器化为 `modules.json` 的 `retireWhen` 字段，并按它判定：

- **retireWhen 缺失 / 不可解析 / 无期限** ⇒ 红（登记不可判定 = 缺口本身）；
- **`notAfter` 期限已过** ⇒ 红「退役期限已过，请归档或再次改期并更新理由」；
- **改期期间矩阵内出现对门面的新消费** ⇒ 红（改期理由是「外部下游迁移中」，
  矩阵内消费会让迁移永远走不完；矩阵内本就不许消费门面——那是
  `facade-not-consumed-internally` 的职责，这里再拦一次是防两守卫口径漂移）；
- 期限未到且矩阵内零消费 ⇒ 绿（门面继续保活合法），并输出引用计数事实。

## 与同族守卫的分工（判定不共享）

- `check_facade_imports.py`：对外任意 roots 的「不许再引」（存量下游保护）；
- `verify_import_direction.py`：矩阵层级与「矩阵内不得消费门面」；
- 本守卫：**只回答「该不该退役」**。import 扫描自实现（facts），不 import
  上述任何一家的判定函数。

## 用法

    verify_facade_retirement.py --root <矩阵根>
    verify_facade_retirement.py --selftest

退出码: 0 = 干净 / 仅黄；1 = 红（该退役 / 登记缺失 / 改期期间出现矩阵内消费）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import INFO, Report, emit  # noqa: E402

# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# --root 指矩阵根：本守卫对矩阵根是**只读**的（扫 *.go + 读 modules.json）
SMOKE_ARGS = ["--root", "@repo"]

FACADE_REPO = "agentkit"
# 矩阵内「消费门面」的 import 形态（门面 module path + 旧单仓 module path）
_IMPORT_RE = re.compile(
    r'"(?:github\.com/oagentkit/agentkit(?:/[\w./]+)?|git\.enjoye\.top/enjoydream/agentkit(?:/[\w./]+)?)"'
)


def scan_facade_imports(root: Path, repos: list[str]) -> dict[str, list[str]]:
    """矩阵各仓源码 → {仓: import 行}。事实层：只收集，不判定。

    只扫 *.go；跳过 .git / vendor / node_modules 等噪声目录。
    门面仓自身不在扫描清单里（它 re-export 上游，不构成「消费」）。
    """
    hits: dict[str, list[str]] = {}
    skip_dirs = {".git", "vendor", "node_modules", ".worktrees", "dist", "site"}
    for repo in repos:
        repo_dir = root / repo
        if not repo_dir.is_dir():
            continue
        for p in repo_dir.rglob("*.go"):
            if any(part in skip_dirs for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if _IMPORT_RE.search(line):
                    hits.setdefault(repo, []).append(
                        f"{p.relative_to(repo_dir)}: {line.strip()}")
    return hits


def judge(root: Path, matrix_path: Path) -> Report:
    """判定门面退役条件。返回报告（不打印、不决定退出码）。"""
    rep = Report(title="门面退役条件")
    try:
        data = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        rep.red(FACADE_REPO, f"读不到 modules.json：{e}")
        return rep

    entry = next((e for e in data.get("infraRepos", [])
                  if e.get("repo") == FACADE_REPO), None)
    if entry is None:
        rep.red(FACADE_REPO, "modules.json 里没有门面仓条目")
        return rep
    rw = entry.get("retireWhen")
    if not isinstance(rw, dict):
        rep.red(FACADE_REPO,
                "retireWhen 缺失或不可解析——退役条件退回「永远判不了」形态（matrix-tools#196）",
                f"在 modules.json 的 {FACADE_REPO} 条目里登记 retireWhen："
                '{"notAfter": "YYYY-MM-DD", "reason": "改期理由", "matrixInternalZeroImports": true}')
        return rep
    deadline_raw = rw.get("notAfter")
    if not deadline_raw:
        rep.red(FACADE_REPO,
                "retireWhen 没有 notAfter 期限——「待外生事件」不可判定，"
                "退役承诺永远判不了（matrix-tools#196 的原始缺口）",
                "写死一个日期（YYYY-MM-DD）；到期判红，归档或再次改期并更新 reason")
        return rep
    try:
        deadline = date.fromisoformat(str(deadline_raw))
    except ValueError:
        rep.red(FACADE_REPO, f"notAfter 不是可解析日期：{deadline_raw!r}",
                "格式 YYYY-MM-DD")
        return rep
    if not rw.get("reason"):
        rep.red(FACADE_REPO, "retireWhen 没有改期理由（reason）——"
                             "无理由的期限就是下一个「写死但没写」",
                "写清为什么是这个日期（外部依赖 / 下游迁移状态）")
        return rep

    repos = [m.get("repo") for m in data.get("modules", []) if m.get("repo")]
    hits = scan_facade_imports(root, repos)
    total = sum(len(v) for v in hits.values())
    rep.info(FACADE_REPO, f"矩阵内门面/旧单仓 import：{total} 处（{len(hits)} 个仓）")
    for repo, lines in sorted(hits.items()):
        rep.info(FACADE_REPO, f"  {repo}: {lines[0]}（共 {len(lines)} 处）")

    if rw.get("matrixInternalZeroImports") and hits:
        rep.red(FACADE_REPO,
                "改期期间矩阵内出现对门面的消费——矩阵内本就不许引门面"
                "（facade-not-consumed-internally），且会让「下游迁移」永远走不完",
                "先消除矩阵内引用，再谈退役时间表")
        return rep

    today = date.today()
    if today > deadline:
        rep.red(FACADE_REPO,
                f"退役期限已过：notAfter={deadline}，今天 {today}。"
                f"登记的理由：{rw.get('reason')}",
                "归档门面仓；确需继续保活 ⇒ 更新 notAfter 并更新 reason"
                "（改期必须显式、有日期、有理由——不许回到「待外生事件」）")
    else:
        rep.info(FACADE_REPO,
                 f"退役期限未到（notAfter={deadline}，剩 {(deadline - today).days} 天），"
                 "门面继续保活合法")
    return rep


def selftest() -> int:
    """反证自测：四类缺陷样本必须判红 + 合规样本不误红 + CLI 入口冒烟。

    ① retireWhen 缺失 ② 无 notAfter ③ 期限已过 ④ 改期期间矩阵内出现消费
    合规：期限未到 + 矩阵内零消费。
    """
    import io
    import tempfile
    from contextlib import redirect_stdout

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    def fixture(infra: dict, consumer_go: str | None) -> tuple[Path, Path]:
        """造一个最小矩阵：1 个模块仓 + 可选的「消费门面」的 .go 文件。"""
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        modules = [{"repo": "demo", "tier": 0}]
        (root / "matrix-tools").mkdir()
        (root / "matrix-tools" / "modules.json").write_text(json.dumps({
            "modules": modules,
            "infraRepos": [dict({"repo": "agentkit"}, **infra)],
        }, ensure_ascii=False), encoding="utf-8")
        (root / "demo").mkdir()
        (root / "demo" / "go.mod").write_text(
            "module github.com/acme/demo\n\ngo 1.26.0\n", encoding="utf-8")
        if consumer_go:
            (root / "demo" / "use.go").write_text(
                "package demo\n\nimport (\n"
                "\t\"github.com/oagentkit/agentkit/textutil\"\n)\n", encoding="utf-8")
        return root, td

    def run_judge(root: Path):
        rep = judge(root, root / "matrix-tools" / "modules.json")
        infos = [f"{f.subject}: {f.message}" for f in rep.findings if f.level == INFO]
        return [f"{f.subject}: {f.message}" for f in rep.reds], infos

    # ① 缺 retireWhen
    root, td = fixture({"role": "门面仓"}, None)
    reds, _ = run_judge(root)
    check("缺 retireWhen 判红", any("retireWhen 缺失" in r for r in reds), True)
    td.cleanup()

    # ② 无 notAfter（「待外生事件」形态）
    root, td = fixture({"retireWhen": {"reason": "待旧单仓下线"}}, None)
    reds, _ = run_judge(root)
    check("无 notAfter 判红", any("没有 notAfter" in r for r in reds), True)
    td.cleanup()

    # ③ 期限已过
    root, td = fixture({"retireWhen": {"notAfter": "2020-01-01",
                                       "reason": "历史改期", "matrixInternalZeroImports": True}},
                       None)
    reds, _ = run_judge(root)
    check("期限已过判红", any("退役期限已过" in r for r in reds), True)
    td.cleanup()

    # ④ 改期期间矩阵内出现消费
    root, td = fixture({"retireWhen": {"notAfter": "2099-01-01",
                                       "reason": "外部下游迁移中",
                                       "matrixInternalZeroImports": True}},
                       "package demo")
    reds, _ = run_judge(root)
    check("改期期间矩阵内消费判红",
          any("矩阵内出现对门面的消费" in r for r in reds), True)
    td.cleanup()

    # 合规：期限未到 + 零消费 ⇒ 不红，且引用计数事实出现在输出里
    root, td = fixture({"retireWhen": {"notAfter": "2099-01-01",
                                       "reason": "外部下游迁移中",
                                       "matrixInternalZeroImports": True}},
                       None)
    reds, infos = run_judge(root)
    check("合规不误红", reds, [])
    check("输出引用计数事实", any("import：0 处" in i for i in infos), True)

    # CLI 入口冒烟：subprocess 真跑一次正常调用，输出不许有 Traceback/AttributeError
    # （判定不能只看退出码——业务失败也是 1 ⇒ 看输出内容）
    buf = io.StringIO()
    import subprocess
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        "--root", str(root)],
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    check("CLI 入口无 Traceback", "Traceback" not in out and "AttributeError" not in out, True)
    check("CLI 入口有结论行", "退役条件" in out or "门面" in out, True)

    if fails:
        print("❌ verify_facade_retirement 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_facade_retirement 自测通过"
          "（缺登记/无期限/过期/改期内消费 四类判红 + 合规不误红 + CLI 冒烟）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="门面退役条件判定（retireWhen 机器可答）")
    ap.add_argument("--root", default=".", help="矩阵根目录（modules.json 的克隆父目录）")
    ap.add_argument("--matrix", default=None,
                    help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也判失败")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    matrix_path = Path(args.matrix) if args.matrix else \
        root / "matrix-tools" / "modules.json"
    if not matrix_path.is_file():
        print(f"❌ 找不到 modules.json：{matrix_path}")
        return 2
    rep = judge(root, matrix_path)
    return emit(rep, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
