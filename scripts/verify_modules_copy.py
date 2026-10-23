#!/usr/bin/env python3
"""站点 `data/modules.json` 与矩阵源 `modules.json` 的副本一致性门禁。

## 为什么需要它（2026-10-23 实测的文档承诺缺口）

`modules.json` 是矩阵的**单一数据源**；站点仓 `oagentkit.github.io/data/modules.json`
是它的**副本**。这件事在三处都有明文：

- 站点仓 `AGENTS.md`：「本仓 `data/modules.json` 是该文件的副本，schema 相同、
  内容必须**逐字段一致**」「**禁止在本仓单方面发明模块信息。** 站点与源一旦漂移，
  就会出现两套互相矛盾的对外清单」
- `.github/CONTRIBUTING.md` 发版第 4 步：「同步站点 `data/modules.json`
  （站点的矩阵数据源，与 modules.json 同源）」
- `.github/CONTRIBUTING.md` 另附实测：「2026-10-21：站点 `data/modules.json`
  曾缺 `llm/port`（站点副本落后于矩阵源）——就是这一步缺失的直接后果」

**但这三处承诺没有任何机器校验守着**，而矩阵对同一份数据的另外两个副本
**都有门禁**：

| 副本 | 文档承诺 | 机器校验 |
|---|---|---|
| `docs/public/llms.txt` | 与 modules.json 一致 | `verify_docs.py` ✅ |
| `docs/public/versions.json` | matrix-tools 的逐字节副本 | `verify_versions_drift.py` ✅ |
| `data/modules.json` | 与 modules.json 逐字段一致 | **无** ❌ |

三处排查下来，这一条的缺口比「少一个门禁」还多一层：

1. **没有门禁** ⇒ 副本漂了没人报警。`verify_docs.py` 守的是 llms.txt，
   `verify_versions_drift.py` 守的是 versions.json，两者都**不碰** data/modules.json；
   站点仓自己的 CI（`pages.yml`）只跑 `gen_module_pages.py --check`，
   校验的是「生成物 vs 站点自己的副本」——**副本本身烂掉了，它照样绿**。
2. **发布工具也不复制它** ⇒ `release.py sync-matrix` 只拷 `docs/public/versions.json`
   （源码里就这一处 `site_copy`），`site/data/modules.json` 从头到尾没人碰。
   于是 CONTRIBUTING 第 4 步里那句「同步站点 data/modules.json」是**纯手工动作**，
   漏了没有任何反馈——2026-10-21 那次就是这么漏的。

⇒ 这正是本仓反复消灭的形态：**文档承诺由脚本强制校验 ≠ 实际有脚本在强制校验**
（`scripts/invariants.json` 的 `why` 里列了三个同族案例）。

## 判据（独立判定，不复用任何生成器）

1. 两侧文件都必须存在且是合法 JSON ⇒ 否则判红（副本缺失/损坏本身就是违规）；
2. 顶层字段集合必须相同（不多不少）；
3. `modules` 按 `repo` 对齐后**逐字段深比**（含嵌套 dict / list，list 按顺序比）；
   少一个仓、多一个仓、少一个包、改一个 tier、改一条描述，都必须报出来；
4. 其余顶层字段（`tiers` / `infraRepos` / `retiringPackages` / `droppedPackages` …）
   同样深比。

**只共享渲染层**（`judge_report.py`），不 import 任何生成器或判定脚本
（本仓硬规则：事实可以共享，判定不能共享）。

## 修法（报告里给出的方向）

先改 `matrix-tools/modules.json`（源是权威），再把源拷到站点 `data/modules.json`
——**源先、副本后**（与 versions.json 副本的纪律一致）。
本门禁只判红，不代劳复制。

## 怎么用

    # CI 口径：矩阵源 vs 站点副本
    python3 scripts/verify_modules_copy.py --matrix modules.json \\
        --compare site/data/modules.json

    # 离线自测（反证样本）
    python3 scripts/verify_modules_copy.py --selftest

退出码: 0 = 逐字段一致；1 = 有漂移 / 副本不可读。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from judge_report import Report  # noqa: E402  —— 只共享渲染层


# ---------------------------------------------------------------- 事实层


def load_json(path: Path):
    """读一个 JSON 文件。失败返回 (None, 原因)——事实层不抛异常。"""
    if not path.is_file():
        return None, f"文件不存在：{path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001 —— 任何解析失败都要被看见
        return None, f"JSON 解析失败：{exc}"


def modules_by_repo(doc: dict) -> dict:
    """modules 列表 → {repo: 模块字典}。非列表/非字典的畸形输入交给判定层。"""
    out = {}
    for m in doc.get("modules") or []:
        if isinstance(m, dict) and "repo" in m:
            out[m["repo"]] = m
    return out


# ---------------------------------------------------------------- 判定层


def _short(v, limit: int = 90) -> str:
    if isinstance(v, str):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def deep_diff(src, cp, path: str, out: list, limit: int = 60) -> None:
    """深比两个值，把差异写进 out（(路径, 源值, 副本值)）。

    list 按**顺序**比：副本的 packages 顺序变了，站点的模块页顺序就会变——
    那也是漂移，不是无害的格式差异。
    """
    if len(out) >= limit:
        return
    if isinstance(src, dict) and isinstance(cp, dict):
        for k in src:
            if k not in cp:
                out.append((f"{path}.{k}", src[k], "<缺>"))
            else:
                deep_diff(src[k], cp[k], f"{path}.{k}", out, limit)
        for k in cp:
            if k not in src:
                out.append((f"{path}.{k}", "<无>", cp[k]))
            if len(out) >= limit:
                return
        return
    if isinstance(src, list) and isinstance(cp, list):
        # 标量列表（packages / topics 这类）：按**集合**比，报「缺哪个 / 多哪个」——
        # 位置比会把一个删除报成一片位移，读的人得自己数（实测：删掉 llm/port
        # 会报成 5 处位移，而不是 1 处「缺 llm/port」）。
        scalar = all(not isinstance(x, (dict, list)) for x in src + cp)
        if scalar:
            missing = [x for x in src if x not in cp]
            extra = [x for x in cp if x not in src]
            for x in missing:
                out.append((f"{path}（副本缺）", x, "<缺>"))
            for x in extra:
                out.append((f"{path}（副本多）", "<无>", x))
            if not missing and not extra and src != cp:
                out.append((f"{path}（顺序不同）", src, cp))
            return
        for i in range(min(len(src), len(cp))):
            deep_diff(src[i], cp[i], f"{path}[{i}]", out, limit)
        for i in range(len(cp), len(src)):
            out.append((f"{path}[{i}]", src[i], "<缺>"))
        for i in range(len(src), len(cp)):
            out.append((f"{path}[{i}]", "<无>", cp[i]))
        return
    if src != cp:
        out.append((path, src, cp))


def judge(src_path: Path, cp_path: Path) -> Report:
    """判定本体：矩阵源 vs 站点副本。抽成独立函数是为了能离线自测。"""
    rep = Report(title="站点 data/modules.json 副本一致性")

    src, err = load_json(src_path)
    if src is None:
        rep.red("矩阵源", f"{src_path} 读不到：{err}",
                "矩阵源 modules.json 缺失或损坏——先修源")
        return rep

    cp, err = load_json(cp_path)
    if cp is None:
        rep.red("站点副本", f"{cp_path} 读不到：{err}",
                "站点仓必须带一份 data/modules.json 副本（它与矩阵源同源）")
        return rep

    if not isinstance(src, dict) or not isinstance(cp, dict):
        rep.red("副本", "两侧顶层都不是 JSON 对象，判不了",
                "modules.json 的顶层必须是对象")
        return rep

    diffs: list = []

    # ① 顶层字段集合
    sk, ck = set(src), set(cp)
    for k in sorted(sk - ck):
        diffs.append((k, src[k], "<缺>"))
    for k in sorted(ck - sk):
        diffs.append((k, "<无>", cp[k]))

    # ② modules 按 repo 对齐后逐字段比
    sm, cm = modules_by_repo(src), modules_by_repo(cp)
    for repo in sorted(set(sm) | set(cm)):
        if repo not in cm:
            diffs.append((f"modules[{repo}]", "<整仓缺失>", "<缺>"))
            continue
        if repo not in sm:
            diffs.append((f"modules[{repo}]", "<无>", "<站点多出的仓>"))
            continue
        deep_diff(sm[repo], cm[repo], f"modules[{repo}]", diffs)

    # ③ 其余顶层字段深比（modules 已单独比过）
    for k in sorted(sk & ck):
        if k == "modules":
            continue
        deep_diff(src[k], cp[k], k, diffs)

    if diffs:
        rep.red("站点副本", f"与矩阵源有 {len(diffs)} 处逐字段差异",
                "先改 matrix-tools/modules.json（源是权威），再把源拷到站点 "
                "data/modules.json——源先、副本后")
        for path, a, b in diffs[:40]:
            rep.note(f"  · {path}：源={_short(a)} 副本={_short(b)}")
        if len(diffs) > 40:
            rep.note(f"  · …（另有 {len(diffs) - 40} 处）")
        return rep

    same_bytes = src_path.read_bytes() == cp_path.read_bytes()
    rep.info("站点副本",
             f"与矩阵源逐字段一致（{len(sm)} 个仓 / {len(sk)} 个顶层字段）"
             + ("，且逐字节相同" if same_bytes else "；字节不同但字段一致（仅格式差异）"))
    return rep


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    """反证自测：拿**故意造坏**的副本跑一遍，要求它报出来。

    「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西
    （matrix-tools/AGENTS.md 硬规则 2）。

    样本覆盖：
      ① 完全一致 → 不报
      ② 副本少一个包（2026-10-21 真实形态：站点副本缺 llm/port）→ 必报
      ③ 副本改一个 tier → 必报
      ④ 副本多一个仓 → 必报
      ⑤ 副本少一个顶层字段 → 必报
      ⑥ 副本文件不存在 → 必报
      ⑦ 副本 JSON 损坏 → 必报
      ⑧ 副本的嵌套字段（pkgDesc）被改 → 必报
      ⑨ packages 顺序不同 → 必报（顺序变了，站点模块页顺序也会变）
    """
    import copy
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    base = {
        "schemaVersion": 1,
        "tiers": {"0": "基础层"},
        "modules": [
            {"repo": "llm", "tier": 2, "packages": ["llm", "decision"],
             "pkgDesc": {"llm": "大模型门面"}},
            {"repo": "textx", "tier": 0, "packages": ["textutil"], "pkgDesc": {}},
        ],
    }

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "modules.json"
        cp = td / "data" / "modules.json"
        cp.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

        def write_copy(doc) -> None:
            cp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        # ① 一致 → 不报
        write_copy(copy.deepcopy(base))
        check("① 一致不报", judge(src, cp).has_red(), False)

        # ② 副本少一个包（真实事故形态）→ 必报
        d = copy.deepcopy(base)
        d["modules"][0]["packages"].remove("decision")
        write_copy(d)
        r = judge(src, cp)
        check("② 少包必报", r.has_red(), True)
        check("② 报出包名", "decision" in r.render_text(), True)

        # ③ 改 tier → 必报
        d = copy.deepcopy(base)
        d["modules"][1]["tier"] = 3
        write_copy(d)
        check("③ 改 tier 必报", judge(src, cp).has_red(), True)

        # ④ 多一个仓 → 必报
        d = copy.deepcopy(base)
        d["modules"].append({"repo": "ghost", "tier": 2, "packages": []})
        write_copy(d)
        check("④ 多仓必报", judge(src, cp).has_red(), True)

        # ⑤ 少一个顶层字段 → 必报
        d = copy.deepcopy(base)
        del d["tiers"]
        write_copy(d)
        check("⑤ 少顶层字段必报", judge(src, cp).has_red(), True)

        # ⑥ 副本文件不存在 → 必报
        cp.unlink()
        check("⑥ 副本缺失必报", judge(src, cp).has_red(), True)

        # ⑦ 副本 JSON 损坏 → 必报
        cp.write_text("{不是合法 JSON", encoding="utf-8")
        check("⑦ 副本损坏必报", judge(src, cp).has_red(), True)

        # ⑧ 嵌套字段被改 → 必报
        d = copy.deepcopy(base)
        d["modules"][0]["pkgDesc"]["llm"] = "被人改过的描述"
        write_copy(d)
        check("⑧ 嵌套字段必报", judge(src, cp).has_red(), True)

        # ⑨ packages 顺序不同 → 必报
        d = copy.deepcopy(base)
        d["modules"][0]["packages"] = ["decision", "llm"]
        write_copy(d)
        check("⑨ 顺序不同必报", judge(src, cp).has_red(), True)

    if fails:
        print("❌ verify_modules_copy 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_modules_copy 自测通过"
          "（一致不报；少包/改 tier/多仓/少字段/缺副本/坏 JSON/嵌套字段/乱序 必报）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="站点 data/modules.json 与矩阵源 modules.json 的逐字段一致性门禁")
    ap.add_argument("--matrix", help="矩阵源 modules.json 路径")
    ap.add_argument("--compare", help="站点副本 data/modules.json 路径")
    ap.add_argument("--selftest", action="store_true",
                    help="离线反证自测（带故意造坏的样本）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.matrix or not args.compare:
        print("❌ 缺少 --matrix 或 --compare（或用 --selftest）")
        return 1

    rep = judge(Path(args.matrix).resolve(), Path(args.compare).resolve())
    return rep.emit()


if __name__ == "__main__":
    raise SystemExit(main())
