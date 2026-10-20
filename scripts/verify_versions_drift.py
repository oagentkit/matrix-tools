#!/usr/bin/env python3
"""versions.json 与 modules.json 的漂移检测（忽略 latest / breakingIndex / depFloor）。

modules.json 是单一数据源，versions.json 是它的生成物。两者漂移时消费方读到的
包清单 / import 路径 / 分层全是错的，而且**不会编译失败**——只是读到错的元数据。

比对时忽略三个「**环境相关**字段」：它们按设计要读矩阵外的仓，离线 CI 里必然取不到值：

- `latest`：联网读 tag（gen_versions.py --org）
- `breakingIndex`：读各仓 CHANGELOG
- `depFloor`：读各仓 go.mod

这三个字段各自有**联网对账门禁**守着（verify_breaking_index.py / verify_dep_floor.py），
离线这条只管「versions.json 是不是 modules.json 的确定性产物」。

⚠️ 「忽略」与「有门禁守」必须成对：只忽略不补门禁，字段就成了没人守的声明。

用法:
    verify_versions_drift.py
    verify_versions_drift.py --matrix modules.json --versions versions.json
    verify_versions_drift.py --compare ../site/docs/public/versions.json

--compare 额外比对另一份 versions.json（站点 docs/public/versions.json 是本文件的副本）。
    这一步**逐字节全等**（含 latest/breakingIndex/depFloor）：站点是副本，任何差异都是漂移。

退出码: 0 = 一致；1 = 漂移。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))


def normalized(path: Path, drop_latest: bool = True, drop_breaking: bool = True,
               drop_dep_floor: bool = True) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    if drop_latest:
        for m in d.get("modules", []):
            m.pop("latest", None)   # 按设计联网读，离线必为 null
            for n in m.get("nestedModules", []):
                n.pop("latest", None)   # 嵌套模块的 tag 同理（audit/v0.1.0）
        for f in d.get("facades", []):
            f.pop("latest", None)
    if drop_breaking:
        # breakingIndex 由**各仓 CHANGELOG** 抽取，离线重生成必然是空的
        # （那要读 14 个仓的文件）。它有自己的对账门禁
        # （verify_breaking_index.py），这里不比——否则这条检查天天假红。
        for m in d.get("modules", []):
            m.pop("breakingIndex", None)
        for f in d.get("facades", []):
            f.pop("breakingIndex", None)
    if drop_dep_floor:
        # depFloor 由**各仓 go.mod** 推出，离线重生成必然是空的（那要读
        # 14 个仓的 go.mod，而离线 CI 只 checkout 了本仓）。它有自己的对账门禁
        # （verify_dep_floor.py，联网读远端 go.mod 与矩阵登记逐仓比对），
        # 这里不比——否则这条检查天天假红。
        #
        # ⚠️ 不能只 drop 不补门禁：那会让 depFloor 变成「没人守的字段」，
        #    手改了也没人发现。drop 与对账门禁必须**成对**出现。
        for m in d.get("modules", []):
            m.pop("depFloor", None)
        for f in d.get("facades", []):
            f.pop("depFloor", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True, indent=2)


def report(title: str, a: str, b: str, fix: str) -> int:
    print(f"❌ {title}\n")
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="committed", tofile="expected", lineterm=""))
    for line in diff[:80]:
        print(line)
    if len(diff) > 80:
        print(f"...（另有 {len(diff) - 80} 行）")
    print(f"\n修法：{fix}")
    return 1


# --------------------------------------------------------------------------
# 漂移方向：哪一边才是陈旧的那一边
# --------------------------------------------------------------------------

def _remote_latest_truth(org: str, versions_doc: dict) -> dict[str, str]:
    """远端各仓的真实最新 tag → {仓名: latest}。

    取不到就**不返回该仓**（调用方据此把它排除在判定之外）——
    「判不了」不能当成「与真相一致」。
    """
    import repo_meta  # 事实层：只取数，不判定

    truth: dict[str, str] = {}
    for repo in latest_map(versions_doc):
        try:
            tag = repo_meta.latest_tag(org, repo)
        except Exception:
            tag = None
        if tag:
            truth[repo] = tag
    return truth


def latest_map(doc: dict) -> dict[str, str]:
    """versions.json 文档 → {仓名: latest}（modules + facades 两段都收）。"""
    out: dict[str, str] = {}
    for m in doc.get("modules", []):
        if m.get("repo"):
            out[m["repo"]] = m.get("latest", "")
    for f in doc.get("facades", []):
        if f.get("repo"):
            out[f["repo"]] = f.get("latest", "")
    return out


def _semver(v: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def stale_side(source: dict, copy: dict, truth: dict[str, str] | None = None) -> str:
    """判定「哪一边陈旧」→ "source" | "copy" | "both" | "unknown"。

    ## 为什么必须判方向

    `versions.json` 是**源**（由 gen_versions.py 生成），站点那份是**逐字节副本**。
    正常情况下源更新 → 拷副本。但实测出现过反过来的情形：
    源的 facade `latest` 是 v1.8.20，而副本已经是 v1.8.21（真实 tag 也是 v1.8.21）——
    **源陈旧、副本正确**。

    此时若按「以源为准、cp 过去」的提示照做，就会把站点从**正确的** v1.8.21
    降级成**错误的** v1.8.20 —— 修法本身制造新漂移。

    ## 判据

    - 有**远端真相**（联网取到的最新 tag）时：谁与真相不符，谁就是陈旧的。
    - 没有真相时（离线）：比较源与副本的 latest——副本比源**新**，
      说明源没跟上（副本被更新过而源没刷新）⇒ 源陈旧。
    """
    s_latest = latest_map(source)
    c_latest = latest_map(copy)

    if truth:
        s_bad = {r for r, v in s_latest.items() if truth.get(r) and v != truth[r]}
        c_bad = {r for r, v in c_latest.items() if truth.get(r) and v != truth[r]}
        if s_bad and c_bad:
            return "both"
        if s_bad:
            return "source"
        if c_bad:
            return "copy"
        return "unknown"

    newer_in_copy = [
        r for r, v in c_latest.items()
        if v and s_latest.get(r) and _semver(v) > _semver(s_latest[r])
    ]
    newer_in_source = [
        r for r, v in s_latest.items()
        if v and c_latest.get(r) and _semver(v) > _semver(c_latest[r])
    ]
    if newer_in_copy and not newer_in_source:
        return "source"
    if newer_in_source and not newer_in_copy:
        return "copy"
    return "unknown"


def fix_hint(direction: str) -> str:
    """按漂移方向给出**正确**的修法。"""
    if direction == "source":
        return ("源陈旧、副本是新的——修法：\n"
                "  1) python3 scripts/gen_versions.py --org <org>   # 先把源刷新到远端真相\n"
                "  2) cp matrix-tools/versions.json <站点>/docs/public/versions.json\n"
                "  3) 两个仓一起提交\n"
                "  ⚠️ 不要直接 cp——那会把站点从正确值降级成陈旧值。")
    if direction == "copy":
        return ("副本陈旧、源是新的——修法：\n"
                "  cp matrix-tools/versions.json <站点>/docs/public/versions.json 后一起提交")
    if direction == "both":
        return ("源与副本**都**与远端真相不符——修法：\n"
                "  1) python3 scripts/gen_versions.py --org <org>   # 刷新源\n"
                "  2) cp matrix-tools/versions.json <站点>/docs/public/versions.json\n"
                "  3) 两个仓一起提交")
    return ("两侧无法判定方向（离线且 latest 无高下）——修法：\n"
            "  1) python3 scripts/gen_versions.py --org <org>   # 刷新源\n"
            "  2) cp matrix-tools/versions.json <站点>/docs/public/versions.json\n"
            "  3) 两个仓一起提交\n"
            "  （加 --org 可让本门禁联网取远端真相，判定方向更准）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--versions", default=str(HERE.parent / "versions.json"))
    ap.add_argument("--compare", help="另一份 versions.json（站点副本），要求逐字节一致")
    ap.add_argument("--org", help="组织名；给了就联网取远端真相，漂移方向判定更准")
    ap.add_argument("--selftest", action="store_true", help="离线自测（漂移方向判定）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    matrix, versions = Path(args.matrix), Path(args.versions)
    for p in (matrix, versions):
        if not p.is_file():
            print(f"❌ 找不到文件: {p}")
            return 1

    with tempfile.TemporaryDirectory() as td:
        regen = Path(td) / "versions.regen.json"
        r = subprocess.run(
            [sys.executable, str(HERE / "gen_versions.py"),
             "--matrix", str(matrix), "--out", str(regen)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("❌ gen_versions.py 失败：")
            print(r.stdout + r.stderr)
            return 1
        a, b = normalized(versions), normalized(regen)

    rc = 0
    if a == b:
        print(f"✅ {versions.name} 与 {matrix.name} 一致（latest 字段除外）")
    else:
        rc = report(f"{versions.name} 与 {matrix.name} 漂移", a, b,
                    "python3 scripts/gen_versions.py --org oagentkit\n"
                    "     然后把 matrix-tools/versions.json 与站点 docs/public/versions.json 一起提交。")

    # 站点副本必须与 matrix-tools 的完全一致（含 latest）——它是逐字节副本，
    # 任何差异都是「改了这边忘了那边」。llms.txt 有 verify_docs.py 守着，
    # versions.json 此前没有任何检查，这是同一个漂移缺口。
    #
    # ⚠️ 修法**必须按漂移方向给**：源是权威、副本是派生物，但源自己也会陈旧
    #    （实测：源的 facade latest 是 v1.8.20，副本已是 v1.8.21，真实 tag 也是 v1.8.21）。
    #    此时「cp 源过去」会把站点从正确值降级成陈旧值 —— 修法本身制造新漂移。
    if args.compare:
        other = Path(args.compare)
        if not other.is_file():
            print(f"❌ 找不到待比对文件: {other}")
            return 1
        x, y = (normalized(versions, drop_latest=False, drop_breaking=False, drop_dep_floor=False),
                normalized(other, drop_latest=False, drop_breaking=False, drop_dep_floor=False))
        if x == y:
            print(f"✅ 站点副本一致: {other}")
        else:
            truth = _remote_latest_truth(args.org, versions) if args.org else None
            direction = stale_side(json.loads(versions.read_text(encoding="utf-8")),
                                    json.loads(other.read_text(encoding="utf-8")),
                                    truth)
            rc = report(f"站点副本 {other} 与 {versions.name} 不一致"
                        f"（陈旧的一边：{direction}）", x, y, fix_hint(direction))

    return rc


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

def selftest() -> int:
    """反证自测：漂移方向的判定必须与真实情形一致。

    样本覆盖四种形态：
      ① 源陈旧、副本新（**本轮实测的真实情形**）——判定必须是 "source"
      ② 副本陈旧、源新 —— 判定必须是 "copy"
      ③ 两侧都陈旧 —— "both"
      ④ 两侧一致 —— 不报漂移

    特别断言：情形 ① 的修法**不能**是「cp 源到副本」——
    那会把站点从正确值降级成陈旧值。
    """
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    def doc(pairs: dict[str, str]) -> dict:
        return {"modules": [{"repo": r, "latest": v} for r, v in pairs.items()],
                "facades": []}

    src_old = doc({"facade": "v1.8.20", "textx": "v0.6.1"})
    src_new = doc({"facade": "v1.8.21", "textx": "v0.6.1"})
    copy_new = doc({"facade": "v1.8.21", "textx": "v0.6.1"})

    # ① 源陈旧、副本新（离线判定）
    check("源陈旧副本新 → 判 source",
          stale_side(src_old, copy_new), "source")
    # 有远端真相时同样判 source
    check("有真相时 → 判 source",
          stale_side(src_old, copy_new, {"facade": "v1.8.21"}), "source")
    # ② 副本陈旧、源新
    check("源新副本陈旧 → 判 copy",
          stale_side(src_new, src_old), "copy")
    check("有真相时 → 判 copy",
          stale_side(src_new, src_old, {"facade": "v1.8.21"}), "copy")
    # ③ 两侧都陈旧
    both_old = doc({"facade": "v1.8.19"})
    check("两侧都陈旧 → 判 both",
          stale_side(both_old, both_old, {"facade": "v1.8.21"}), "both")
    # ④ 一致
    check("两侧一致 → unknown",
          stale_side(src_new, copy_new, {"facade": "v1.8.21"}), "unknown")

    # 关键断言：情形 ① 的修法不得是「cp 源到副本」
    hint_source = fix_hint("source")
    hint_copy = fix_hint("copy")
    check("源陈旧的修法先刷新源", "gen_versions.py" in hint_source, True)
    check("源陈旧的修法警告不要直接 cp", "不要直接 cp" in hint_source, True)
    check("副本陈旧的修法是 cp", "cp matrix-tools/versions.json" in hint_copy, True)

    # semver 比较必须是数字比较（字符串比会把 v0.9.10 判成小于 v0.9.9）
    check("v0.9.10 > v0.9.9", _semver("v0.9.10") > _semver("v0.9.9"), True)
    check("v1.8.21 > v1.8.9", _semver("v1.8.21") > _semver("v1.8.9"), True)

    if fails:
        print("❌ verify_versions_drift 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_versions_drift 自测通过（漂移方向判定 + 修法方向）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
