#!/usr/bin/env python3
"""api-lifecycle 定级门禁：kernel `apilifecheck` 判定逻辑的矩阵侧实现。

## 守的是哪条缺口（matrix-tools#256，评审 2.3-问题五）

「改了算不算 breaking」此前只靠 CHANGELOG 事后记：新增包可以不登记级别、
包注释可以不写定级、清单可以悄悄漂——**承诺面没有机器判定**。
kernel 的 `cmd/apilifecheck` 已把这套判定跑熟（50 包），本脚本按**同一判据**
推广到 oagentkit 各仓：判据同源 kernel `docs/api-compat.md`（见
`docs/adr/ADR-002`），实现单源在 matrix-tools——各仓/CI 只调用，不复制判据。

## 判定（与 kernel apilifecheck 对齐；**包级**不是符号级——判据同源）

1. **清单自洽**：`version: 1`；`pkg` 非空不重复；`level ∈ {Experimental,
   Stable, Deprecated}`；`since` 是 `vX.Y.Z`；`reason` 非空；`Deprecated`
   必须带 `replacement`（`import/path/pkg.导出符号` 且符号真实存在），
   非 Deprecated 不许带。
2. **覆盖双向**：源码里的生产包目录（含子包；排除 `cmd/`、`testdata/`、
   `_test.go`-only）↔ 清单条目——**漏登记（新增包无定级）与幽灵登记同红**。
   新增导出符号不单独判级：它继承所在包的级别（api-compat.md 的刻意设计）。
3. **包注释标记**：`// 生命周期: <级别>` 必须出现在**恰好一个**文件的包注释
   （File.Doc）里，且级别与清单一致；缺标记 / 多处标记 / 级别不符同红。

## 接入是 opt-in：没放清单 ≠ 红，放了清单就跑不掉

仓里没有 `api-lifecycle.yaml` ⇒ 报「未接入」信息、退出 0（推广分批见
ADR-002：先接口面仓）。**一旦放入 yaml，全套判定立即生效**——
这正是「新增包必须显式定级，不允许逍遥于承诺之外」的机器化。

## 边界（诚实声明）

- 包注释识别是**词法近似**：`package` 行往上回溯**连续 `//` 行** = File.Doc
  （Go 里中间夹空行即不再附着，本实现同此规则；`/* */` 块注释要求紧贴
  `package`）。不是 go/ast——极端形态可能与 kernel 实现有出入，
  selftest 带正反例钉住主要形态。
- 替换符号存在性用正则找导出声明（近似 go/ast `symbolExists`）；
  **解析不了判红，不判绿**（判不了 ≠ 没问题）。

用法:
    verify_api_lifecycle.py --root .                # 矩阵级：扫克隆根下各仓
    verify_api_lifecycle.py --repo path/to/repo     # 单仓（各仓 CI 接线形态）
    verify_api_lifecycle.py --selftest              # 离线反证自测

退出码: 0 = 干净（黄不计）；1 = 有红；2 = 环境/输入不可用。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402

# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# ⚠️ 这组参数必须**无副作用**、且能让脚本**真正走完入口**（不能只让 argparse 早退）。
SMOKE_ARGS = ["--repo", "@repo/matrix-tools/scripts/fixtures/api_lifecycle_repo"]

LEVELS = ("Experimental", "Stable", "Deprecated")
SINCE_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# 包注释里的标记行（kernel 同款：中文「生命周期」+ 半角/全角冒号）
MARKER_RE = re.compile(r"生命周期[:：]\s*(Experimental|Stable|Deprecated)\b")
# replacement 形态：github.com/<org>/<repo>/<pkg>.<导出符号>（kernel splitReplacement 同款）
REPL_RE = re.compile(r"^github\.com/[\w.-]+/[\w.-]+/[\w./]+\.[A-Z]\w*$")
# --root 模式下跳过的非模块仓（与 clone 步骤同一批排除 + 门面）
SKIP_DIRS = {"matrix-tools", ".github", "oagentkit.github.io", "agentkit"}


# ---------------------------------------------------------------- 事实装配


def _has_prod_go(d: Path) -> bool:
    return any(p.is_file() and p.suffix == ".go" and not p.name.endswith("_test.go")
               for p in d.iterdir())


def prod_packages(repo: Path) -> dict[str, Path]:
    """源码里的生产包：{pkg 相对路径（根 = 仓名）: 目录}。

    口径对齐 kernel `pkgs.go`：含生产 `.go` 的目录都算（含子包），
    排除 `cmd/`、`testdata/`、`vendor/`、点目录与 `_test.go`-only。

    **剪掉嵌套独立检出**：子目录里带 `.git`（文件或目录：submodule /
    worktree / go-ci 把 matrix-tools 判据检出并排放在 workspace）的目录
    整棵不计——那是别人的包面。实测（agent#179 首跑）：go-ci 的
    `matrix-tools/` 检出里有 scripts/fixtures 与 symdump，不剪会被当成
    agent 的未登记生产包判红。
    """
    import os
    out: dict[str, Path] = {}
    repo = repo.resolve()
    for dirpath, dirnames, filenames in os.walk(repo):
        d = Path(dirpath)
        rel = d.relative_to(repo)
        if rel != Path(".") and any(part in ("cmd", "testdata", "vendor")
                                    or part.startswith(".")
                                    for part in rel.parts):
            dirnames[:] = []
            continue
        # 剪枝：嵌套独立检出（.git 文件或目录都算）整棵不进
        dirnames[:] = [n for n in dirnames
                       if not (d / n / ".git").exists() and not n.startswith(".")]
        if not any(f.endswith(".go") and not f.endswith("_test.go")
                   for f in filenames):
            continue
        pkg = repo.name if rel == Path(".") else str(rel).replace("\\", "/")
        out.setdefault(pkg, d)
    return out


def file_doc_levels(path: Path) -> list[str]:
    """该文件包注释（File.Doc 的词法近似）里命中的级别列表。

    规则：`package` 行往上**连续** `//` 行 = 包注释（中间夹空行即断，
    与 Go 附着规则一致）；紧贴的 `/* … */` 块同样算。多文件各可携带，
    全包恰好一处标记（kernel「一个包多处标记」判红同款）。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    idx = next((i for i, ln in enumerate(lines)
                if re.match(r"^package\s+\w", ln)), None)
    if idx is None:
        return []
    block: list[str] = []
    j = idx - 1
    while j >= 0 and lines[j].startswith("//"):
        block.append(lines[j])
        j -= 1
    if not block and j >= 0 and lines[j].strip().endswith("*/"):
        k = j
        while k >= 0 and "/*" not in lines[k]:
            k -= 1
        if k >= 0:
            block = lines[k:j + 1]
    hits = []
    for ln in block:
        m = MARKER_RE.search(ln.lstrip("/").strip())
        if m:
            hits.append(m.group(1))
    return hits


def pkg_markers(pkg_dir: Path) -> list[tuple[str, str]]:
    """包目录里全部命中的 (文件名, 级别)。"""
    out = []
    for f in sorted(pkg_dir.glob("*.go")):
        if f.name.endswith("_test.go"):
            continue
        for lvl in file_doc_levels(f):
            out.append((f.name, lvl))
    return out


def load_manifest(path: Path) -> tuple[dict | None, str | None]:
    """读 api-lifecycle.yaml → (清单 dict, 错误)。pyyaml 缺失 ⇒ 明确报环境问题。"""
    try:
        import yaml
    except ImportError:
        return None, "缺 pyyaml（python3 -m pip install pyyaml）——判不了不是没问题"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001  yaml 解析错误必须原样报出
        return None, f"yaml 解析失败：{exc}"
    if not isinstance(data, dict):
        return None, "清单顶层必须是映射（version/packages）"
    return data, None


def symbol_exists(pkg_dir: Path, symbol: str) -> bool:
    """包里是否有该名的顶层导出声明（go/ast symbolExists 的正则近似）。"""
    pat = re.compile(rf"^(func|type|const|var)\s+{re.escape(symbol)}\b",
                     re.M)
    for f in pkg_dir.glob("*.go"):
        if f.name.endswith("_test.go"):
            continue
        try:
            if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- 判定


def judge_manifest(data: dict, pkgs: dict[str, Path],
                   report: Report, subject: str) -> dict[str, str]:
    """清单自洽 + 覆盖双向 + 标记对账；返回 {pkg: level}（供后续引用）。"""
    if data.get("version") != 1:
        report.red(subject, f"version 必须是 1，实得 {data.get('version')!r}",
                   "清单格式单源在 kernel docs/api-compat.md「清单格式」节（ADR-002 同源）")
    entries = data.get("packages")
    if not isinstance(entries, list) or not entries:
        report.red(subject, "packages 为空——接入了清单就必须逐包定级")
        return {}

    declared: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            report.red(subject, f"packages 条目必须是映射：{e!r}")
            continue
        pkg = e.get("pkg")
        if not isinstance(pkg, str) or not pkg.strip():
            report.red(subject, f"条目缺 pkg：{e!r}")
            continue
        if pkg in declared:
            report.red(pkg, "pkg 重复登记——重复即歧义")
            continue
        declared[pkg] = e

        level = e.get("level")
        if level not in LEVELS:
            report.red(pkg, f"level 出词表：{level!r}（词表：{'/'.join(LEVELS)}）")
        since = e.get("since")
        if not isinstance(since, str) or not SINCE_RE.match(since):
            report.red(pkg, f"since 必须是 vX.Y.Z，实得 {since!r}")
        reason = e.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            report.red(pkg, "reason 为空——每条定级都要能说清依据（防免死金牌）")
        repl = e.get("replacement")
        if level == "Deprecated":
            if not isinstance(repl, str) or not REPL_RE.match(repl):
                report.red(pkg, f"Deprecated 必须带 replacement"
                                f"（github.com/<org>/<repo>/<pkg>.<符号>），实得 {repl!r}")
            elif pkg in pkgs:
                sym = repl.rsplit(".", 1)[-1]
                if not symbol_exists(pkgs[pkg], sym):
                    report.red(pkg, f"replacement 符号不存在：{repl}",
                               "解析不到导出声明判红——判不了不能当通过")
        elif repl is not None:
            report.red(pkg, "非 Deprecated 条目不许带 replacement")

    # 覆盖双向
    for pkg in sorted(set(pkgs) - set(declared)):
        report.red(f"{pkg}", "生产包未登记进 api-lifecycle.yaml——新增包必须显式定级，"
                             "不允许逍遥于承诺之外",
                   "在清单里加 pkg/level/since/reason，并在包注释写 `// 生命周期: <级别>`")
    for pkg in sorted(set(declared) - set(pkgs)):
        report.red(pkg, "幽灵登记：清单里有、源码里没有这个包（目录删了没清清单）")

    # 标记对账（只查双向都存在的包——其余已被上面的红覆盖）
    for pkg in sorted(set(declared) & set(pkgs)):
        hits = pkg_markers(pkgs[pkg])
        want = declared[pkg].get("level")
        if not hits:
            report.red(pkg, "包注释缺 `// 生命周期: <级别>` 标记",
                       "标记必须写在包注释里（紧贴 package 的 // 注释块），"
                       "且与清单 level 一致")
        elif len(hits) > 1:
            report.red(pkg, f"包注释多处标记：{hits}——一个包恰好一处")
        elif hits[0][1] != want:
            report.red(pkg, f"标记级别与清单不符：注释 {hits[0][1]} ≠ 清单 {want}")

    return {p: e.get("level") for p, e in declared.items()}


def judge_repo(repo: Path, report: Report) -> str:
    """单仓判定。返回状态 ∈ {checked, opted-in-empty, not-opted-in}。

    先 `resolve()`：调用方常写 `--repo .`（各仓 CI 形态），而 `Path(".").name`
    是**空串**——根包（如 llm）会按空名对账，源码有根包 ⇒ 未登记/幽灵登记双红。
    实测踩到：go-ci 接线用 `--repo .` 指向 llm 即中招。
    """
    repo = repo.resolve()
    manifest = repo / "api-lifecycle.yaml"
    if not manifest.is_file():
        report.info(repo.name, "未接入 api-lifecycle（无 api-lifecycle.yaml）——"
                               "推广分批见 ADR-002，接入即全套判定生效")
        return "not-opted-in"
    data, err = load_manifest(manifest)
    if err:
        report.red(repo.name, f"api-lifecycle.yaml 读不了：{err}")
        return "checked"
    judge_manifest(data, prod_packages(repo), report, repo.name)
    return "checked"


def judge(root: Path, report: Report, only: Path | None = None) -> None:
    if only is not None:
        if not only.is_dir():
            report.red(str(only), "仓目录不存在")
            return
        judge_repo(only, report)
        return
    repos = sorted(d for d in root.iterdir()
                   if d.is_dir() and d.name not in SKIP_DIRS
                   and (d / "go.mod").is_file())
    if not repos:
        report.red(str(root), "根目录下一个带 go.mod 的仓都没找到——取数面空了")
        return
    checked = 0
    for d in repos:
        if judge_repo(d, report) == "checked":
            checked += 1
    report.note(f"已接入 {checked} / 扫描 {len(repos)} 仓"
                f"（未接入报 info，清单一放即全套判定）")


# ---------------------------------------------------------------- 自测


_FIXTURE_MANIFEST = """\
# api-lifecycle 定级清单（判定逻辑单源见 matrix-tools docs/adr/ADR-002）
version: 1
packages:
  - pkg: demopkg
    level: Experimental
    since: v0.1.0
    reason: fixture——窄接口未过一个 minor 无 breaking 窗口
  - pkg: demorepo
    level: Experimental
    since: v0.1.0
    reason: fixture 根包——钉住 --repo . 的根包名解析（Path(".").name 是空串）
"""

_FIXTURE_GO = """\
// Package demopkg fixture 包。
//
// 生命周期: Experimental——fixture。
package demopkg
"""

_FIXTURE_ROOT_GO = """\
// Package demorepo fixture 根包（钉住 --repo . 形态的根包名解析）。
//
// 生命周期: Experimental——fixture。
package demorepo
"""


def _write_fixture(root: Path, manifest: str = _FIXTURE_MANIFEST,
                   go_src: str = _FIXTURE_GO) -> Path:
    repo = root / "demorepo"
    (repo / "demopkg").mkdir(parents=True, exist_ok=True)
    (repo / "api-lifecycle.yaml").write_text(manifest, encoding="utf-8")
    (repo / "demopkg" / "demo.go").write_text(go_src, encoding="utf-8")
    (repo / "root.go").write_text(_FIXTURE_ROOT_GO, encoding="utf-8")
    (repo / "go.mod").write_text("module example.com/demorepo\n", encoding="utf-8")
    return repo


def _run(repo: Path) -> Report:
    rep = Report(title="api-lifecycle")
    judge_repo(repo, rep)
    return rep


def selftest() -> int:
    """反证自测：每种违规形态都必须让自测转红（有区分力的断言）。"""
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # ① 合法基线 ⇒ 零红
        repo = _write_fixture(td)
        check("① 合法基线零红", _run(repo).reds, [])

        # ② 新增包未登记（验收红例形态）⇒ 红且点名
        import shutil
        (repo / "newpkg").mkdir()
        (repo / "newpkg" / "new.go").write_text("package newpkg\n", encoding="utf-8")
        rep = _run(repo)
        check("② 新增包未登记判红", any(f.subject == "newpkg" for f in rep.reds), True)
        shutil.rmtree(repo / "newpkg")

        # ③ 缺包注释标记 ⇒ 红
        (repo / "demopkg" / "demo.go").write_text("package demopkg\n", encoding="utf-8")
        rep = _run(repo)
        check("③ 缺标记判红", any("缺 `// 生命周期" in f.message for f in rep.reds), True)

        # ④ 标记与清单级别不符 ⇒ 红
        (repo / "demopkg" / "demo.go").write_text(
            "// Package demopkg。\n// 生命周期: Stable——不符。\npackage demopkg\n",
            encoding="utf-8")
        rep = _run(repo)
        check("④ 级别不符判红", any("不符" in f.message for f in rep.reds), True)

        # ⑤ 一个包两处标记 ⇒ 红
        (repo / "demopkg" / "demo.go").write_text(_FIXTURE_GO, encoding="utf-8")
        (repo / "demopkg" / "other.go").write_text(
            "// 生命周期: Experimental。\npackage demopkg\n", encoding="utf-8")
        rep = _run(repo)
        check("⑤ 多处标记判红", any("多处标记" in f.message for f in rep.reds), True)
        (repo / "demopkg" / "other.go").unlink()

        # ⑥ reason 空 / level 出词表 ⇒ 红
        bad = _FIXTURE_MANIFEST.replace(
            "reason: fixture——窄接口未过一个 minor 无 breaking 窗口", "reason: ''")
        (repo / "api-lifecycle.yaml").write_text(bad, encoding="utf-8")
        rep = _run(repo)
        check("⑥ 空 reason 判红", any("reason 为空" in f.message for f in rep.reds), True)

        # ⑦ 幽灵登记 ⇒ 红
        ghost = _FIXTURE_MANIFEST + (
            "  - pkg: gonepkg\n    level: Stable\n    since: v0.1.0\n"
            "    reason: 源码里没有它\n")
        (repo / "api-lifecycle.yaml").write_text(ghost, encoding="utf-8")
        rep = _run(repo)
        check("⑦ 幽灵登记判红", any("幽灵登记" in f.message for f in rep.reds), True)

        # ⑧ 未接入（无 yaml）⇒ info 不红（opt-in 分批，ADR-002）
        bare = td / "bare"
        bare.mkdir()
        (bare / "go.mod").write_text("module example.com/bare\n", encoding="utf-8")
        rep = _run(bare)
        check("⑧ 未接入不判红", rep.has_red(), False)
        check("⑧ 未接入报 info", any("未接入" in f.message for f in rep.findings), True)

        # ⑨ 真跑一次 CLI：正常仓退出 0、输出无 Traceback / AttributeError
        #    （先把 ⑥⑦ 弄脏的清单恢复成合法基线——否则测的是脏状态不是正常入口）
        (repo / "api-lifecycle.yaml").write_text(_FIXTURE_MANIFEST, encoding="utf-8")
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--repo", str(repo)],
            capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("⑨ CLI 正常调用退出码", proc.returncode, 0)
        check("⑨ CLI 输出无 Traceback", "Traceback" in out, False)
        check("⑨ CLI 输出无 AttributeError", "AttributeError" in out, False)

        # ⑩ 相对路径形态 `--repo .`（各仓 CI 接线形态）：根包名必须解析成目录名
        #    （Path(".").name 是空串 ⇒ 根包对账会双红，实测 llm 中招）
        proc10 = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--repo", "."],
            cwd=repo, capture_output=True, text=True, timeout=60)
        out10 = (proc10.stdout or "") + (proc10.stderr or "")
        check("⑩ --repo . 退出码", proc10.returncode, 0)
        check("⑩ --repo . 无 Traceback", "Traceback" in out10, False)

        # ⑪ 嵌套独立检出（.git）不计入本仓包面——go-ci 把 matrix-tools 判据
        #    检出并排放在 workspace 的形态（agent#179 首跑：fixture/symdump
        #    被当成未登记生产包判红）。先恢复干净清单再验。
        (repo / "api-lifecycle.yaml").write_text(_FIXTURE_MANIFEST, encoding="utf-8")
        nested = repo / "nested_checkout"
        nested.mkdir()
        (nested / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (nested / "sneaky").mkdir()
        (nested / "sneaky" / "s.go").write_text("package sneaky\n", encoding="utf-8")
        rep11 = _run(repo)
        check("⑪ 嵌套检出不计入包面", rep11.reds, [])
        shutil.rmtree(nested)

    if fails:
        print("❌ verify_api_lifecycle 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_api_lifecycle 自测通过"
          "（新增包无定级判红 / 标记三态判红 / 幽灵登记判红 / 未接入不红 / CLI 无崩溃）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="api-lifecycle 定级门禁（kernel apilifecheck 判据同源）")
    ap.add_argument("--root", help="矩阵克隆根（平铺各仓目录）——矩阵级扫描")
    ap.add_argument("--repo", help="单仓路径（各仓 CI 接线形态）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.root and not args.repo:
        print("❌ 需要 --root 或 --repo（或用 --selftest）")
        return 2

    report = Report(title="api-lifecycle 定级")
    if args.repo:
        judge(Path(args.repo), report, only=Path(args.repo))
    else:
        judge(Path(args.root), report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
