#!/usr/bin/env python3
"""sync-matrix 的自测：接线、降级路径、站点副本同步。

为什么要有这个自测（issue matrix-tools#133）：
`release.py sync-matrix` 的两个义务曾经**没有任何机器约束**——

  ① 不传本地根 ⇒ 生成器算不出 floorBumps，只能「搬运既有值」
     ⇒ 门面每次抬地板，矩阵源都少一条「下游必须跑 go mod tidy」的登记；
  ② 不拷 `site/data/modules.json` ⇒ 站点副本同步是**纯手工动作**，漏了没人报。

这两条都不会让任何门禁变红（门禁要么自己重算事实、要么压根不查这个字段），
所以「谁把它改回去」不会被发现 ⇒ 必须有一个自测把接线钉住。

自测覆盖：
  T1 floorBumps 的来源选择：给了本地根 ⇒ **现算**；没给 ⇒ **搬运**（退化路径仍在）
  T2 站点副本同步：内容不同 ⇒ 拷贝；已一致 ⇒ 不动；缺文件 ⇒ 报可疑
  T3 接线（AST 判定，不靠人读）：cmd_sync_matrix 调生成器时必须带 --floor-root，
     且必须调用站点副本同步
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_versions  # noqa: E402  生成器（取数层）
import release  # noqa: E402  发版工具
from judge_report import Report  # noqa: E402  报告层


def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(d), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path, name: str = "demo") -> None:
    """造一个有两个 tag、且第二个 tag 抬了依赖地板的本地 git 仓。"""
    d = root / name
    d.mkdir(parents=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "go.mod").write_text(
        "module example.com/demo\n\nrequire github.com/oekit/pkg v0.1.0\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "v0.1.0")
    _git(d, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    (d / "go.mod").write_text(
        "module example.com/demo\n\nrequire github.com/oekit/pkg v0.2.0\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "v0.2.0")
    _git(d, "tag", "-a", "v0.2.0", "-m", "v0.2.0")


class _Args:
    """floor_bumps_map 只需要这几个字段；显式写出来免得签名变了悄悄失效。

    2026-10-31 补 org：floor_bumps_map 拿 org 去做「本地 tag 完整性」的远端对账
    （远端 tag 集合是基准），少这个字段就会 AttributeError —— CI 的 Scripts job
    就是这样红过一次（本地自测清单漏了 scripts/test_*.py，没提前发现）。
    """

    def __init__(self, root=None, floor_root=None, preserve_from=None,
                 no_preserve=False, org=None):
        self.root = root
        self.floor_root = floor_root
        self.preserve_from = preserve_from
        self.no_preserve = no_preserve
        self.org = org


def t1_source_selection() -> list[str]:
    """T1：floorBumps 的来源必须是「现算」，且退化路径（搬运）仍在。"""
    fails: list[str] = []
    data = {"modules": [{"repo": "demo"}], "infraRepos": []}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_repo(root)

        bumps, note = gen_versions.floor_bumps_map(
            _Args(floor_root=str(root), no_preserve=True), data)
        if "现算" not in note:
            fails.append(f"T1① 给了本地根却没现算：{note}")
        got = bumps.get("demo") or []
        if len(got) != 1 or got[0].get("version") != "v0.2.0":
            fails.append(f"T1① 现算结果不对：{got}")
        if not got or not got[0].get("bumps"):
            fails.append("T1① 没认出 v0.1.0→v0.2.0 抬了地板")

        # T1③ 没给 org ⇒ **无法核对**本地 tag 完整性 ⇒ 来源说明必须写出来。
        # 「判不了 ≠ 没问题」：不写出来，操作者会以为核对过了，
        # 而现算结果其实可能漏算（tag 残缺 ⇒ 相邻对错 ⇒ 条目变少且不报错）。
        if "无法核对本地 tag 完整性" not in note:
            fails.append(f"T1③ 没给 org 时来源说明没声明「无法核对完整性」：{note}")

        bumps2, note2 = gen_versions.floor_bumps_map(
            _Args(no_preserve=True), data)
        if "搬运" not in note2:
            fails.append(f"T1② 没给本地根时应退化到搬运：{note2}")
        if bumps2:
            fails.append(f"T1② 搬运且不保留时不该有内容：{bumps2}")

        # 搬运路径仍会把既有值搬过去（不许被顺手删掉）
        with tempfile.TemporaryDirectory() as td2:
            keep = Path(td2) / "versions.json"
            keep.write_text('{"modules": [{"repo": "demo", "floorBumps": [{"version": "v0.1.9"}]}]}',
                            encoding="utf-8")
            bumps3, note3 = gen_versions.floor_bumps_map(
                _Args(preserve_from=str(keep)), data)
            if "搬运" not in note3 or not bumps3.get("demo"):
                fails.append(f"T1③ 搬运路径没搬既有值：{note3} {bumps3}")
    return fails


def t2_site_copy() -> list[str]:
    """T2：站点 data/modules.json 的同步：不同 ⇒ 拷；一致 ⇒ 不动；缺 ⇒ 可疑。"""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mt = root / "matrix-tools"
        site = root / "site"
        (mt / "scripts").mkdir(parents=True)
        (site / "data").mkdir(parents=True)
        (mt / "modules.json").write_text('{"a": 1}', encoding="utf-8")
        dst = site / "data" / "modules.json"
        dst.write_text('{"a": 0}', encoding="utf-8")

        rep = Report(title="T2")
        r = release.sync_site_modules(mt, site, rep)
        if r != "copied" or dst.read_text(encoding="utf-8") != '{"a": 1}':
            fails.append(f"T2① 内容不同却没拷：{r} {dst.read_text(encoding='utf-8')!r}")

        r = release.sync_site_modules(mt, site, rep)
        if r != "same":
            fails.append(f"T2② 已一致却动了：{r}")

        dst.unlink()
        rep2 = Report(title="T2")
        r = release.sync_site_modules(mt, site, rep2)
        if r != "missing":
            fails.append(f"T2③ 缺副本却报成功：{r}")
        if not rep2.yellows:
            fails.append("T2③ 缺副本没有报可疑（黄灯）")
    return fails


def _calls_in_func(func_name: str) -> list[ast.Call]:
    tree = ast.parse((HERE / "release.py").read_text(encoding="utf-8"))
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            out = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            break
    return out


def t3_wiring() -> list[str]:
    """T3：接线必须钉住——生成器调用要带 --floor-root，且要调站点副本同步。"""
    fails: list[str] = []
    calls = _calls_in_func("cmd_sync_matrix")

    gen_call = None
    for c in calls:
        for arg in c.args:
            if not isinstance(arg, ast.List):
                continue
            # 脚本路径是 `str(mt / "scripts" / "gen_versions.py")` 这样的表达式，
            # 不是纯字符串常量 ⇒ 递归取元素里的全部字符串常量。
            consts: list[str] = []
            for e in arg.elts:
                for sub in ast.walk(e):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        consts.append(sub.value)
            if any(n.endswith("gen_versions.py") for n in consts):
                gen_call = consts
    if gen_call is None:
        fails.append("T3① cmd_sync_matrix 里找不到调 gen_versions.py 的地方")
    elif "--floor-root" not in gen_call:
        fails.append(f"T3① 生成器调用没带 --floor-root：{gen_call}")

    if not any(isinstance(c.func, ast.Name) and c.func.id == "sync_site_modules"
               for c in calls):
        fails.append("T3② cmd_sync_matrix 没调 sync_site_modules（站点副本同步会漏）")
    return fails


def main() -> int:
    fails: list[str] = []
    for name, fn in (("T1", t1_source_selection), ("T2", t2_site_copy), ("T3", t3_wiring)):
        got = fn()
        if got:
            fails.extend(got)
            print(f"❌ {name} 失败：")
            for f in got:
                print(f"   - {f}")
        else:
            print(f"✅ {name} 通过")
    if fails:
        return 1
    print("✅ sync-matrix 自测通过"
          "（floorBumps 现算/搬运两条路都对；站点副本同步三种情形都对；接线钉住）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
