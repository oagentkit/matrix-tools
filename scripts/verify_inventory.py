#!/usr/bin/env python3
"""不变量登记表的守门人：确保「承诺 ↔ 判据 ↔ 接线」三者齐全且不互相撒谎。

## 守的是什么

矩阵反复出现的失败形态是同一个：**文档承诺由脚本强制校验 ≠ 实际有脚本在强制校验**。

实测过的三个：profile README 说「tier 不变量由 verify_split.py 强制校验」——
那个脚本没被任何 CI 引用、数据路径也已失效；
门面 README 说「不接受任何新功能」——只有事后快照守卫，而快照把违规吸收了；
`versions.json` 的 `latest`——drift 门禁明确忽略它，而全 CI 无人在查。

所以本脚本查七件事：

1. **每条不变量的判据脚本都存在**（登记了却没写，或写完被删）。
2. **每条不变量的判据脚本都被登记的 CI 文件引用**（登记了却没接线 = 承诺是假的）。
3. **每个守卫脚本都被登记**（写了却没登记 = 死代码，或忘了接进 CI）。
4. **独立 oracle 不得悄悄 import 生成器**——耦合必须显式登记并写明理由。
5. **每条判据脚本都支持 `--selftest`，且该自测被 CI 真的调用过**（硬规则 2）。
6. **登记的触发条件真的能跑起来**（登记表的第三腿：什么条件下跑）。
7. **登记的 CI 位置真的跑这个判据**（接线不能只在别的 job 里，更不能只在 `--selftest` 里）。

## 第 6 条为什么必须有

登记表给每个 CI 登记项都写了 `triggers`，`why` 里也明写「判据在哪、谁在跑、
**什么条件下跑**」是三件事之一、「缺任何一件，verify_inventory.py 会判红」——
但本脚本此前**从不读这个字段**（`grep -c trigger scripts/verify_inventory.py` ⇒ 0），
而它在登记表里出现 31 次。

实测形态：抽掉 workflow 的 `schedule` 触发后，门禁仍然报「红 0 / 黄 0」——
**登记说会跑、实际不会跑，正是这张表要消灭的形态**。而 `schedule` 恰恰是
矩阵跨仓审计唯一的自愈入口（matrix-tools#76 修的正是「模块仓缺 schedule ⇒
跨仓欠账没人抓」）。

所以这里补上第三腿：
① 登记的 trigger 必须存在于 workflow 级 `on:`；
② job 级 `if:` 不得把已登记的 trigger 排除掉（workflow 声明了 `schedule`
   不等于该 job 真会在 `schedule` 上跑）；
③ `if:` 判不了 ⇒ **判黄**不判红（硬规则 3：判不了 ≠ 没问题，但也不是确认违规）。

## 第 7 条为什么必须有：「接上了」不等于「在跑」

第 2 条此前只做**整文件文本 grep**（`scripts/<name>` 出现在这个 workflow 的**任何位置**
就算「已接线」）。它有两处看不见的盲区：

- **不区分 job**：登记写的是 `file` + `job`，判定却只扫整个文件——
  判据在 A job 里跑，登记写的是 B job，照样报「已接线」；
- **不区分自测与判定**：同一个文件里 `--selftest` 的那一步会被当成判定接线——
  而自测**证明的是判据自己活着**，不是「这条不变量被守住了」。

实测形态（2026-10-24，issue matrix-tools#119）：`invariants.json` 的 `facade-release-floor`
登记成 `matrix-tools-ci.yml :: facade-symbols`，而那个 job 只跑 `verify_facade_symbols.py`，
`verify_facade_release.py` 在该文件里**只**出现在 `guards` job 的 `--selftest` 步骤；
真判定在**另一个仓**的 `.github/.../repo-ci.yml :: matrix-docs`。
反证实验：把那个真判定步骤**整个删掉**，`verify_inventory.py` 仍然报「红 0 / 黄 0」、退出码 0
——**门禁对自己的登记表失明**。

所以第 7 条要求：**登记的那个 job 里必须真的有一步调用该判据**（不含 `--selftest`）。
判定粒度是**步骤**而不是行（`--selftest` 写在续行上的写法不该被误判成真判定）。
YAML 读不出来 ⇒ **判黄**（硬规则 3：判不了 ≠ 没问题）。

## 第 4 条为什么用「登记制」而不是自动判定

「import 了生成器」不等于「判定逻辑被共享」——
`verify_pending_release.py` 用 `gen_changelogs.tags_in_order` 取事实，
但它的**判定**（预写段是否丢失）与生成器的**渲染**无关。

自动判定「这次 import 是否影响了判定」需要读代码语义，不可靠。
所以本脚本强制的是**披露**：耦合必须写进登记表并给出理由，由人 review。
没披露的耦合 ⇒ 红灯。

## 通用性

登记表是数据（`scripts/invariants.json`），本脚本不绑定具体矩阵；
CI 文件路径、脚本名、job 名都从登记表读。

用法:
    verify_inventory.py
    verify_inventory.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402

# 守卫类脚本的文件名前缀——只有这些必须被登记为不变量。
GUARD_PREFIXES = ("verify_", "check_", "scan_")

# **共享层**：只取事实 / 只渲染，**不含判定**。守卫 import 它们不构成
# 「与被检对象共享判断逻辑」，因此不需要逐条披露。
#
# 为什么单独分出来：矩阵踩过两次「共享判断逻辑 ⇒ 共同盲区」的坑
# （门禁复用生成器的解析函数、逐字节门禁比的是生成器丢 body 之后的结果）。
# 解法不是「一律不许 import」，而是**把共享面分成两层**：
#   - 取数层/渲染层：共享是安全的（事实没有判定，渲染没有判定）；
#   - 生成器/判据层：共享是危险的，必须逐条披露并写明理由。
SHARED_LAYER = {
    "matrix_facts",   # 事实层：go.mod / import 图 / tag / git 历史
    "judge_report",   # 渲染层：红黄绿三档的格式化
    "repo_meta",      # 取数层：gh api 取 tag / 文件 / 树
    "matrix_paths",   # import 路径规则本体（自带独立断言 test_matrix_paths.py）
}

# **生成器/判据层**：import 它们可能把「判定逻辑」也共享出去，
# 必须逐条披露在登记表的 sharedWith 里并写明理由。
GENERATOR_STEMS = {
    "gen_changelogs", "gen_versions", "gen_llms_txt", "gen_docs",
    "breaking_index", "rewrite_imports",
    # floorBumps 的抽取规则本体：判据是「相邻两 tag 的 go.mod 差异」，
    # 独立 oracle（verify_floor_bumps.py）必须自己判——它一 import 就共享了判定。
    "gen_floor_bumps",
}

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)\s+import|import\s+([A-Za-z_][\w]*))",
                         re.M)


def imported_modules(script: Path) -> set[str]:
    """脚本 import 的本地模块名（排除标准库）。

    判据只看**同目录的本地模块**——那是矩阵内部的耦合面。
    标准库与第三方包不在这个仓库里，不构成「与被检对象共享逻辑」。
    """
    if not script.is_file():
        return set()
    text = script.read_text(encoding="utf-8", errors="ignore")
    local = {p.stem for p in script.parent.glob("*.py")}
    out = set()
    for m in _IMPORT_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name and name in local:
            out.add(name)
    return out


def generator_to_judge_coupling(registry: dict, scripts_dir: Path, report) -> None:
    """生成器 → 守卫 的耦合必须逐条披露，未披露 ⇒ 红灯。

    ## 判据

    找出所有**生成器**（`gen_*.py` / `breaking_index.py` / `rewrite_imports.py`）
    import 了哪些**守卫**（登记在 `inv["judges"]` 里的脚本），
    再看这些耦合有没有写进对应不变量的 `sharedWith`。

    守卫名从**登记表**读，不是靠文件名前缀猜——登记表才是「哪些脚本是守卫」的权威。

    ## 为什么这个方向也要查（实测 2026-10-30）

    第 3 条查的是「守卫 → 生成器」，用的是 `inv["judges"]` 里的脚本。
    **生成器 → 守卫**没人查，而它更危险：

        gen_versions.py import verify_dep_floor，用它的 floor_of() 写
        versions.json 的 depFloor；而 verify_dep_floor 的对账又用 floor_of()
        核对 versions.json ⇒ 对账成了**恒真式**（两边永远相等），
        floor_of 的任何 bug 双盲。

    这正是「共享判断逻辑 ⇒ 共同盲区」的镜像形态。
    """
    invs = registry.get("invariants", []) if registry else []

    # 登记表里出现的全部守卫脚本 → 它们对应的不变量（用于查 sharedWith）
    # 键用**模块名**（去 .py 后缀）：imported_modules 返回的就是模块名，
    # 用带后缀的脚本名做键会一个都匹配不上（实测踩过：交集恒为空 ⇒ 判据从不触发）。
    judge_to_inv: dict[str, set[str]] = {}
    for inv in invs:
        for j in inv.get("judges", []):
            stem = j[:-3] if j.endswith(".py") else j
            judge_to_inv.setdefault(stem, set()).add(inv.get("id", "?"))

    generator_files = sorted(
        p for p in scripts_dir.glob("*.py")
        if p.stem in GENERATOR_STEMS or p.name.startswith("gen_")
    )

    for gen in generator_files:
        imported = imported_modules(gen)
        judges = sorted(m for m in imported if m in judge_to_inv)
        if not judges:
            continue
        for j in judges:
            # 这个守卫参与的所有不变量里，是否有任一条披露了这条耦合。
            # 披露的是**被 import 的那个模块名**（= 守卫），不是生成器自己——
            # 第 3 条的 sharedWith 记的也是「这个判据脚本耦合了哪个模块」。
            declared_any = False
            for inv_id in judge_to_inv[j]:
                inv = next((x for x in invs if x.get("id") == inv_id), None)
                if not inv:
                    continue
                declared = {s.get("module") for s in inv.get("sharedWith", [])}
                if j in declared:
                    declared_any = True
                    break
            if not declared_any:
                report.red(
                    "generator-coupling",
                    f"生成器 {gen.name} import 了守卫 {j}.py，"
                    f"但没有在任何不变量的 sharedWith 里披露",
                    "生成器复用守卫 ⇒ 对账可能变成恒真式"
                    "（生成器算的，门禁拿同一个函数算，永远相等）——"
                    "「共享判断逻辑 ⇒ 共同盲区」的镜像形态。"
                    "要么把耦合写进对应不变量的 sharedWith 并写明理由，"
                    "要么把取数下沉到事实层、两边各自实现判定")


def selftest_invoked(ci_text: str, script: str) -> bool:
    """CI 里是否有一步**以 --selftest 调用**了这个判据脚本。

    按**行**判定：同一个文件里既有普通调用又有 --selftest 调用时，
    只有后者算数（普通调用是门禁本身，不是自测）。
    """
    for line in ci_text.splitlines():
        if f"scripts/{script}" in line and "--selftest" in line:
            return True
    return False


def auto_discovery_wired(ci_text: str) -> bool:
    """CI 里是否有**自动发现式接线**（真的跑聚合器的分片）。

    形态：`python3 scripts/selftest_all.py --shard i/n`。

    ⚠️ **只认 `--shard`，不认 `--plan-shards`**：`--plan-shards` 只是「算片数」，
    它在 `selftest-plan` job 里 ⇒ 若认它，则「计划 job 在、真正跑分片的 job 没了」
    也会被判成「已接线」⇒ 接线是假的（实测变异：这正是没转红的原因）。
    ⇒ 判据必须落在**真正跑自测的那一步**上。
    ⇒ 判据将来若换形态 ⇒ 这里会判红（保守：宁可红，不放过假接线）。

    为什么要有这条：CI 里「跑哪些自测」这份清单**靠人记** ⇒ 必然漂移
    （实测：`bump_dep_floors.py --selftest` 写了两周根本没进 CI）。
    ⇒ 改成「脚本自己发现 + CI 分片跑」⇒ 新增自测脚本自动被纳入。
    ⇒ 但这样一来，「接线」就不能再靠 grep 文本判定 ⇒ 必须改成**集合包含**关系。
    """
    for line in ci_text.splitlines():
        if "selftest_all.py" in line and "--shard" in line:
            return True
    return False


def discovered_selftests(scripts_dir: Path) -> set:
    """自测**自动发现集合**（哪些脚本会被聚合器跑到）。

    取数层在 `selftest_discovery.py`（AST 判据，不靠子串匹配）。
    ⚠️ 发现层坏了 ⇒ 这个集合就是错的 ⇒ 下面第 8 条判据会判红（这正是它的用处）。
    """
    try:
        from selftest_discovery import discover
    except ImportError as exc:  # 判不了 ⇒ 空集合 ⇒ 第 8 条判红（判不了 ≠ 没问题）
        print("⚠️ 读不到自测发现层：%s" % exc, file=sys.stderr)
        return set()
    _plain, with_selftest = discover(scripts_dir)
    return {p.name for p in with_selftest}


# ---------------------------------------------------------------------------
# 第 7 条：登记的 CI 位置必须**真的**跑这个判据
# ---------------------------------------------------------------------------


def _job_steps(wf_path: Path, job_name: str, cache: dict):
    """取某个 workflow 里某个 job 的所有 `run:` 步骤文本。

    返回 (steps, err)：
      · steps 是 list ⇒ job 存在，元素是各步骤的 run 文本；
      · steps 是 None 且 err 是 None ⇒ **job 不存在**；
      · err 非 None ⇒ **判不了**（YAML 解析失败 / 环境缺 pyyaml）。

    cache 按 wf_path 缓存——与 check_triggers 用同一个缓存，语义一致
    （缓存是每次 judge() 调用内部新建的，改了文件重跑就是新内容）。
    """
    key = str(wf_path)
    if key not in cache:
        cache[key] = _load_yaml(Path(wf_path))
    data, err = cache[key]
    if err is not None:
        return None, err
    jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return None, "workflow 里没有 jobs 段"
    job = jobs.get(job_name)
    if job is None:
        return None, None
    steps = []
    for st in ((job.get("steps") or []) if isinstance(job, dict) else []):
        if isinstance(st, dict):
            r = st.get("run")
            if r:
                steps.append(str(r))
    return steps, None


def _step_kinds(steps: list[str], script: str) -> tuple[bool, bool]:
    """这个 job 里对 `script` 的调用分类：(真判定, 自测)。

    判定粒度是**步骤**而不是行——「提到脚本且同一步骤提到 --selftest」⇒ 自测步骤；
    「提到脚本且该步骤没有 --selftest」⇒ 真判定步骤。
    行级判定会把 `--selftest` 写在续行上的写法误判成真判定。
    """
    real = selftest = False
    for s in steps:
        if f"scripts/{script}" not in s:
            continue
        if "--selftest" in s:
            selftest = True
        else:
            real = True
    return real, selftest


def _real_locations(workflows: dict[str, str], script: str, cache: dict) -> list[str]:
    """这个判据在**所有**已登记的 workflow 里，真正跑判定的地方。

    用于修法文案：登记指错了位置时，直接告诉人「真判定在哪」。
    """
    out = []
    for wf_name, wf_path in workflows.items():
        key = str(wf_path)
        if key not in cache:
            cache[key] = _load_yaml(Path(wf_path))
        data, err = cache[key]
        if err is not None or not isinstance(data, dict):
            continue
        for jn, jb in ((data.get("jobs") or {}).items()):
            steps = []
            for st in ((jb.get("steps") or []) if isinstance(jb, dict) else []):
                if isinstance(st, dict) and st.get("run"):
                    steps.append(str(st["run"]))
            real, _ = _step_kinds(steps, script)
            if real:
                out.append(f"{wf_name} :: {jn}")
    return out


# ---------------------------------------------------------------------------
# 触发条件（什么条件下跑）——登记表的第三腿
# ---------------------------------------------------------------------------

# job 级 `if:` 里，只有这两种形式能静态判定；其余写法一律判「判不了」。
_CMP_EQ = re.compile(r"^\s*github\.event_name\s*==\s*'([^']+)'\s*$")
_CMP_NE = re.compile(r"^\s*github\.event_name\s*!=\s*'([^']+)'\s*$")


def _load_yaml(path: Path):
    """解析 YAML。返回 (data, err)；err 非 None 表示**判不了**（不是判绿）。

    pyyaml 的兜底沿用 `.github/workflows/repo-ci.yml` 里已有的写法
    （先试 import，失败再 pip install）——CI 里已显式装 pyyaml，
    这条只是本地/离线环境的保险。
    """
    try:
        import yaml
    except ImportError:
        try:
            import subprocess
            import sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyyaml"])
            import yaml
        except Exception as exc:  # noqa: BLE001
            return None, f"环境缺 pyyaml 且无法安装（{exc}）"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"YAML 解析失败（{exc}）"


def workflow_events(wf) -> set[str]:
    """workflow 级 `on:` 声明的事件集合。

    ⚠️ PyYAML 按 YAML 1.1 解析，裸键 `on:` 会被解析成布尔 True
    ⇒ 两个键都要试，否则读出来是空集（本判据会因此「全绿但什么都没查」）。
    """
    if not isinstance(wf, dict):
        return set()
    on = wf.get("on")
    if on is None:
        on = wf.get(True)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(k) for k in on.keys()}
    return set()


def _split_top(expr: str, op: str) -> list[str]:
    """按顶层分隔符切分（括号内的分隔符不算）。"""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and expr.startswith(op, i):
            out.append("".join(cur))
            cur = []
            i += len(op)
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _conjunct_value(conj: str, event: str):
    """单个合取项对给定事件的取值：True / False / None(判不了)。"""
    m = _CMP_EQ.match(conj)
    if m:
        return event == m.group(1)
    m = _CMP_NE.match(conj)
    if m:
        return event != m.group(1)
    return None


def condition_allows(expr, event: str):
    """job 级 `if:` 表达式在 `event` 事件下是否放行。

    返回 True / False / None(判不了)。
    只认 `github.event_name` 的 `==` / `!=` 形式（含 `||` 与 `&&` 组合）；
    其余写法（`always()`、`needs.*` 等）**不猜**——判不了就是判不了。
    """
    if expr is None:
        return True
    expr = str(expr).strip()
    if not expr:
        return True
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()

    vals: list = []
    for alt in _split_top(expr, "||"):
        value = True
        unknown = False
        for conj in _split_top(alt, "&&"):
            r = _conjunct_value(conj, event)
            if r is False:
                value = False
                break
            if r is None:
                unknown = True
        vals.append(False if value is False else (None if unknown else True))

    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def check_triggers(inv_id: str, ci: dict, workflows: dict[str, str],
                   cache: dict, report: Report) -> None:
    """校验登记的触发条件是不是真的能跑起来（登记表的第三腿）。

    ① 登记的 trigger 必须存在于 workflow 级 `on:`，否则**判红**；
    ② job 级 `if:` 不得把已登记的 trigger 排除掉，否则**判红**；
    ③ `if:` 判不了 / YAML 判不了 ⇒ **判黄**（硬规则 3：判不了 ≠ 没问题，
       但也不是确认违规——不能凭「猜它会跑」就放过去）。
    """
    triggers = [str(t) for t in (ci.get("triggers") or [])]
    wf_name = ci.get("file", "")
    job_name = ci.get("job", "")

    if not triggers:
        report.yellow(
            inv_id,
            f"登记的 CI 接线没写触发条件：{wf_name} / {job_name}",
            "登记表承诺的是三件事——判据在哪、谁在跑、什么条件下跑；"
            "缺触发条件 ⇒ 第三腿无从核对（判不了 ≠ 没问题）")
        return

    wf_path = workflows.get(wf_name)
    if wf_path is None:
        return  # 文件找不到已由上层判红，不重复报

    if wf_path not in cache:
        cache[wf_path] = _load_yaml(Path(wf_path))
    data, err = cache[wf_path]
    if err is not None:
        report.yellow(inv_id, f"判不了 {wf_name} 的触发条件：{err}",
                      "解析不了 workflow ⇒ 无法核对登记的触发条件（判不了 ≠ 没问题）")
        return

    jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
    job = jobs.get(job_name) if isinstance(jobs, dict) else None
    if job is None:
        return  # job 名不存在已由上层判黄

    actual = workflow_events(data)
    cond = job.get("if") if isinstance(job, dict) else None

    for trig in triggers:
        if trig not in actual:
            report.red(
                inv_id,
                f"登记的触发条件 {trig} 在 {wf_name} 里不存在——承诺是假的",
                f"要么在 {wf_name} 的 on: 里补上 {trig}，要么把登记里的 {trig} 去掉——"
                f"「登记说会跑、实际不会跑」正是这张表要消灭的形态")
            continue
        allows = condition_allows(cond, trig)
        if allows is False:
            report.red(
                inv_id,
                f"job {job_name} 的 if 条件排除了登记的触发条件 {trig}",
                f"job 级 if: {cond} —— {wf_name} 在 workflow 级声明了 {trig}，"
                f"但这个 job 在 {trig} 事件下不会运行；"
                f"要么改 if，要么把 {trig} 从登记里去掉")
        elif allows is None:
            report.yellow(
                inv_id,
                f"判不了 job {job_name} 的 if 条件是否放行 {trig}（if: {cond}）",
                "只认 github.event_name 的 == / != 形式；其余写法无法静态判定"
                "（判不了 ≠ 没问题）")


def judge(registry: dict, scripts_dir: Path, workflows: dict[str, str],
          report: Report) -> None:
    invariants = registry.get("invariants", [])
    retired = {r.get("script") for r in registry.get("retired", [])}
    tools = set(registry.get("notInvariants", {}).get("tools", []))

    if not invariants:
        report.red("invariants.json", "登记表是空的——等于没有登记任何不变量")
        return

    # 1 + 2 + 7：判据脚本存在；登记的 CI 位置**真的**跑它
    #
    # 第 7 条（2026-10-24，issue matrix-tools#119）：登记写的是 `file` + `job`，
    # 判定却必须落到**那个 job 的某一步真判定**上——
    # 「脚本在这个 workflow 里出现过」不等于「这条不变量被守住了」。
    # 详见模块 docstring「第 7 条为什么必须有」。
    wf_job_cache: dict = {}
    for inv in invariants:
        inv_id = inv.get("id", "?")
        for judge_script in inv.get("judges", []):
            if not (scripts_dir / judge_script).is_file():
                report.red(inv_id, f"判据脚本 {judge_script} 不存在",
                           "登记表与实际脚本对不上——要么补上脚本，要么撤掉这条登记")
                continue
            any_real = False    # 该判据在**登记的位置**里有没有真判定步骤
            inspected = False   # 有没有成功读到某个 job 的步骤（读不到 ⇒ 不能断言）
            for ci in inv.get("ci", []):
                wf_name = ci.get("file", "")
                job_name = ci.get("job", "")
                wf_path = workflows.get(wf_name)
                if wf_path is None:
                    report.red(inv_id, f"登记的 CI 文件 {wf_name} 找不到",
                               "路径相对矩阵根目录；确认该文件存在")
                    continue
                content = Path(wf_path).read_text(encoding="utf-8", errors="ignore")
                # 精确匹配「scripts/<name>」——用子串匹配会把
                # check_facade_release.py 误判成 release.py 被引用。
                if f"scripts/{judge_script}" not in content:
                    report.red(
                        inv_id,
                        f"判据脚本 {judge_script} 没有被 {wf_name} 引用——承诺是假的",
                        f"在 {wf_name} 里加一步：python3 scripts/{judge_script} …")
                    continue
                if not job_name:
                    report.yellow(
                        inv_id,
                        f"{wf_name} 这条登记没写 job 名——判不了 {judge_script} 在哪跑",
                        "把 job 名补进登记表的 ci 条目")
                    continue
                steps, err = _job_steps(Path(wf_path), job_name, wf_job_cache)
                if err is not None:
                    report.yellow(
                        inv_id,
                        f"判不了 {wf_name} :: {job_name} 是否真的跑 {judge_script}（{err}）",
                        "YAML 读不出来 ⇒ 不能断言它没跑（硬规则 3：判不了 ≠ 没问题）")
                    continue
                if steps is None:
                    # job 名不存在 ⇒ 沿用「job 名存在性」黄灯
                    report.yellow(inv_id, f"登记的 job 名 {job_name} 在 {wf_name} 里找不到",
                                  "可能是 job 已改名——登记表要跟着改")
                    continue
                inspected = True
                real, selftest = _step_kinds(steps, judge_script)
                if real:
                    any_real = True
                    report.info(inv_id,
                                f"{judge_script} 已在 {wf_name} :: {job_name} 真判定")
                    continue
                # 登记的位置不跑这个判据 ⇒ 确认的假承诺 ⇒ 判红
                where = _real_locations(workflows, judge_script, wf_job_cache)
                why = ("该 job 里只有 --selftest 调用（自测证明的是判据自己活着，"
                       "不是这条不变量被守住）" if selftest
                       else "该 job 里根本没有这个判据的调用")
                if where:
                    fix = ("把登记表指向真正跑判定的地方：" + "、".join(where)
                           + f"；或让 {job_name} 也跑一次真判定")
                else:
                    fix = (f"在 {wf_name} :: {job_name} 里加一步真判定："
                           f"python3 scripts/{judge_script} …")
                report.red(
                    inv_id,
                    f"登记的 CI 位置不跑该判据：{wf_name} :: {job_name} —— {why}",
                    fix)
            if inspected and not any_real:
                report.red(
                    inv_id,
                    f"{judge_script} 在登记的所有 CI 位置都没有真判定步骤"
                    "——这条不变量没有门禁守着",
                    "要么把登记指向真正跑判定的地方，要么给它加一步真判定")

        # 3：独立 oracle 不得悄悄 import 生成器
        declared = {s.get("module") for s in inv.get("sharedWith", [])}
        for judge_script in inv.get("judges", []):
            script = scripts_dir / judge_script
            if not script.is_file():
                continue
            coupled = imported_modules(script) & GENERATOR_STEMS
            undisclosed = coupled - declared
            if undisclosed:
                report.red(
                    inv_id,
                    f"{judge_script} import 了未登记的耦合模块：{sorted(undisclosed)}",
                    "耦合必须写进登记表的 sharedWith 并写明理由——"
                    "「门禁与被检对象共享判断逻辑」是矩阵踩过两次的坑，"
                    "披露是最低限度的防线")

    # 3b：**生成器不得 import 守卫**（耦合的另一个方向）
    #
    # 为什么方向也要管（实测 2026-10-30）：第 3 条只查「守卫 → 生成器」，
    # 查的是 `inv["judges"]` 里的脚本。**生成器 → 守卫**这个方向完全没人查，
    # 而它比前一个方向更危险：
    #
    #   gen_versions.py import verify_dep_floor，用它的 floor_of() 写 versions.json 的
    #   depFloor；而 verify_dep_floor 的对账又用 floor_of() 去核对 versions.json。
    #   ⇒ 对账成了**恒真式**：生成器算的，门禁拿同一个函数算，永远相等。
    #   floor_of 的任何 bug（口径错、解析漏、过滤过头）**双盲**——
    #   而 depFloor 是矩阵**对外发布**的消费依据。
    #
    # 形态上是「共享判断逻辑 ⇒ 共同盲区」的镜像：
    # 前一个方向是门禁复用生成器，这个方向是生成器复用门禁，
    # 两者都会让「判定」与「被判定对象」共用同一套实现。
    #
    # 处置同第 3 条：**披露制**。耦合必须写进 sharedWith 并写明理由，
    # 由人 review「这次 import 是否影响了判定」。未披露 ⇒ 红灯。
    generator_to_judge_coupling(registry, scripts_dir, report)


    # 4：硬规则 2——每条守卫都要能自证还活着（AGENTS.md 硬规则 2）
    #
    # 「每条守卫都要有 --selftest，而且自测必须带反证样本」这条规则此前**只是文档**：
    # 登记表门禁只查「脚本存在 / 被 CI 引用 / job 名存在 / 耦合披露 / 守卫必须登记」，
    # 没有一条判「判据脚本支持 --selftest」——实测 27 个登记判据里有 4 个根本没有
    # --selftest 开关（verify_ghost_packages / scan_stale_refs / verify_docs /
    # verify_repo_docs），它们处于「一直绿、从没生效过」的高危形态且无人知晓。
    #
    # 所以这里把它变成**机器可执行的不变量**：
    #   ① 判据脚本必须支持 --selftest；
    #   ② 该自测必须在本 org 的 CI 里真的被调用一次（否则是「写了但没接线」）。
    ci_text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore")
                        for p in workflows.values())
    # 自动发现式接线：CI 里跑聚合器的分片 ⇒ 「跑哪些自测」由脚本自己算
    auto_wired = auto_discovery_wired(ci_text)
    discovered = discovered_selftests(scripts_dir)
    for inv in invariants:
        inv_id = inv.get("id", "?")
        for judge_script in inv.get("judges", []):
            script = scripts_dir / judge_script
            if not script.is_file():
                continue
            src = script.read_text(encoding="utf-8", errors="ignore")
            if "--selftest" not in src:
                report.red(
                    inv_id,
                    f"{judge_script} 没有 --selftest——硬规则 2 要求每条守卫都能自证还活着",
                    f"给这个守卫加 --selftest，并带上**故意造坏**的样本"
                    f"（「跑通即绿」的自测是摆设——它证明不了门禁能抓住它要防的东西）")
                continue
            if not selftest_invoked(ci_text, judge_script):
                # 自动发现式接线：那份清单由脚本自己算 ⇒ 判据在集合里就等于「已接线」
                if auto_wired and judge_script in discovered:
                    report.info(
                        inv_id,
                        f"{judge_script} 的自测由自动发现式接线覆盖"
                        f"（CI 里跑 scripts/selftest_all.py 的分片）")
                    continue
                report.red(
                    inv_id,
                    f"{judge_script} 的 --selftest 没有接进任何 CI——写了但没接线",
                    f"在 CI 的 guards job 里加一步："
                    f"python3 scripts/{judge_script} --selftest"
                    f"；或改成自动发现式接线（CI 跑 scripts/selftest_all.py --shard）")

    # 8：登记的判据脚本必须在**自测自动发现集合**里
    #
    # 为什么必须有这条：一旦 CI 改成「自动发现式接线」，「接上了」就不再是文本问题，
    # 而是**集合包含**问题——判据不在发现集合里 ⇒ 聚合器永远不跑它 ⇒ 接线是假的。
    # 这条判据同时是发现层本身的保险：发现层坏了（漏判/退化）⇒ 集合变小 ⇒ 判红。
    #
    # ⚠️ 只在**CI 真的用自动发现式接线**时才判红：否则 CI 是显式逐条写的，
    #    集合外的脚本仍可能被显式调用（第 5 条的文本路径已经管住了那种情形）。
    if auto_wired:
        for inv in invariants:
            inv_id = inv.get("id", "?")
            for judge_script in inv.get("judges", []):
                if not (scripts_dir / judge_script).is_file():
                    continue
                if judge_script in discovered:
                    continue
                report.red(
                    inv_id,
                    f"{judge_script} 不在自测自动发现集合里——它永远不会被聚合器跑到",
                    f"给它注册 --selftest 入口（argparse 的 add_argument(\"--selftest\") "
                    f"或模块级 def selftest / def _selftest）；"
                    f"发现层的判据在 scripts/selftest_discovery.py（AST 判据，不靠子串匹配）")

    # 6：登记的触发条件必须真的能跑起来（登记表的第三腿）
    #
    # 登记表给每个 CI 登记项都写了 `triggers`，`why` 里明写「判据在哪、谁在跑、
    # 什么条件下跑」是三件事之一、「缺任何一件，verify_inventory.py 会判红」——
    # 但本脚本此前从不读这个字段。实测：抽掉 workflow 的 `schedule` 触发后，
    # 门禁仍报「红 0 / 黄 0」，而 `schedule` 是跨仓审计唯一的自愈入口
    # （matrix-tools#76）。承诺与触发条件之间没有核对＝登记表自己会撒谎。
    wf_cache: dict = {}
    for inv in invariants:
        inv_id = inv.get("id", "?")
        for ci in inv.get("ci", []):
            check_triggers(inv_id, ci, workflows, wf_cache, report)

    # 5：每个守卫脚本都必须被登记
    registered: set[str] = set()
    for inv in invariants:
        registered.update(inv.get("judges", []))
    for script in sorted(scripts_dir.glob("*.py")):
        name = script.name
        if not name.startswith(GUARD_PREFIXES):
            continue
        if name in registered or name in retired:
            continue
        if name in tools:
            continue
        report.red(
            name,
            "守卫脚本没有任何不变量登记它——它是死代码，或忘了接进 CI",
            "要么为它写一条登记（claim + judges + ci），要么登记进 retired 说明被谁取代，"
            "要么删掉它——「写了但没接线」的门禁会给人虚假的安全感")


def selftest() -> int:
    """反证自测：登记表的失配都必须被抓出来。

    覆盖九类（每一类都带**故意造坏**的样本）：
      ① 未登记的守卫脚本        → 判红
      ② 未披露的生成器耦合      → 判红
      ③ 判据脚本没有 --selftest → 判红（硬规则 2）
      ④ --selftest 没接进 CI   → 判红（硬规则 2）
      ⑤ 登记的 job 里只有 --selftest → 判红（第 7 条：自测不是判定）
      ⑥ 真判定在别的 job、登记指错 → 判红（第 7 条）
      ⑦ 全部合规                → 不报
      ⑧ 补齐后                  → 转绿
      ⑨ if 判不了               → 判黄不判红（硬规则 3）
    """
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        scripts = root / "scripts"
        scripts.mkdir()
        # 合规的判据脚本：有 --selftest 开关
        (scripts / "verify_ok.py").write_text(
            "import sys\nfrom pathlib import Path\nimport json\n"
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        (scripts / "verify_coupled.py").write_text(
            "import gen_changelogs\nfrom matrix_facts import git\n"
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        # 反证样本：没有 --selftest 开关的判据脚本
        (scripts / "verify_noselftest.py").write_text("print('x')\n", encoding="utf-8")
        # 反证样本：有 --selftest，但 CI 里没调用它
        (scripts / "verify_unwired.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        (scripts / "verify_orphan.py").write_text(
            "print('x')\nimport argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        # 反证样本：登记的 job 里**只有 --selftest**（自测不是判定）
        (scripts / "verify_selftestonly.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        # 反证样本：真判定在**别的 job**（c），登记指错了位置（b）
        (scripts / "verify_elsewhere.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        (scripts / "gen_changelogs.py").write_text("x = 1\n", encoding="utf-8")
        # ⚠️ **反证样本 ⑪**：`--selftest` 只出现在**注释**里 ⇒
        #    AST 判据（scripts/selftest_discovery.py）**不认**它有自测入口
        #    ⇒ 它不在自动发现集合里 ⇒ 当 CI 改用「自动发现式接线」时，
        #    聚合器**永远不会**跑到它 ⇒ 接线是假的 ⇒ 必须判红。
        #    （老判据只看源码里有没有 "--selftest" 字样 ⇒ 这种脚本会被误当成有入口。）
        (scripts / "verify_commentonly.py").write_text(
            "print('x')\nap = None  # --selftest\n", encoding="utf-8")
        # 反证样本 ⑩：**生成器 import 守卫**（耦合的另一个方向）
        # gen_genjudge.py import verify_ok.py（一个已登记的守卫），
        # 而登记表里没有任何不变量披露这条耦合 ⇒ 必须判红。
        # 这正是线上真实形态：gen_versions.py import verify_dep_floor.py，
        # 而 dep-floor 登记成 oracle=independent / sharedWith=[]。
        (scripts / "gen_genjudge.py").write_text(
            "import verify_ok\nx = 1\n", encoding="utf-8")

        # workflow：ci.yml 的 `on:` **故意没有 schedule**（反证「登记了却不存在」）；
        # ci2.yml 的 `on:` 有 schedule，但 only-push 用 if 排除它（反证「if 排除了
        # 已登记的 trigger」），opaque 用判不了的 if（反证「判不了 ⇒ 判黄不判红」）。
        wf = root / "ci.yml"
        wf2 = root / "ci2.yml"
        # ci3.yml：**自动发现式接线**的样本（CI 跑聚合器的分片，而不是逐条写死清单）。
        # 它是判据 8 的舞台：接了自动发现 ⇒ 集合外的脚本永远不会被跑到 ⇒ 必须判红。
        wf3 = root / "ci3.yml"
        wf3.write_text(
            "on:\n"
            "  push:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  auto:\n    steps:\n"
            "      - run: python3 scripts/verify_commentonly.py\n"
            "      - run: python3 scripts/verify_commentonly.py --selftest\n"
            "      - run: python3 scripts/selftest_all.py --shard 0/2\n",
            encoding="utf-8")

        def write_ci1(steps: str, b_real: bool = False) -> None:
            # job b 是「登记的位置」——先只给 --selftest（反证：自测不是判定）；
            # b_real=True 时补上真判定（补齐后应当转绿）。
            # job c 是 verify_elsewhere.py 的**真判定**所在 ⇒ 登记指 b 就是指错了。
            b_steps = ("      - run: python3 scripts/verify_selftestonly.py --selftest\n"
                       "      - run: python3 scripts/verify_elsewhere.py --selftest\n")
            if b_real:
                b_steps += ("      - run: python3 scripts/verify_selftestonly.py\n"
                            "      - run: python3 scripts/verify_elsewhere.py\n")
            wf.write_text(
                "on:\n"
                "  push:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  a:\n    steps:\n" + steps +
                "  b:\n    steps:\n" + b_steps +
                "  c:\n    steps:\n"
                "      - run: python3 scripts/verify_elsewhere.py\n",
                encoding="utf-8")

        def write_ci2(steps: str) -> None:
            # ci2.yml 里也必须真的引用判据脚本——否则会先被「判据脚本没被 CI 引用」
            # 那条判定抓住，红的就不是触发条件这一条，自证就没有区分力了。
            wf2.write_text(
                "on:\n"
                "  push:\n"
                "  schedule:\n"
                "jobs:\n"
                "  only-push:\n"
                "    if: github.event_name == 'push'\n"
                "    steps:\n"
                "      - run: python3 scripts/verify_ok.py\n"
                "      - run: echo only-push\n"
                "  opaque:\n"
                "    if: always()\n"
                "    steps:\n"
                "      - run: python3 scripts/verify_ok.py\n"
                "      - run: echo opaque\n" + steps,
                encoding="utf-8")

        base_steps = (
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_ok.py --selftest\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n")
        write_ci1(base_steps)
        write_ci2("")

        registry = {
            "invariants": [
                {"id": "ok", "judges": ["verify_ok.py"], "oracle": "independent",
                 "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a",
                         "triggers": ["push", "pull_request"]}]},
                {"id": "coupled", "judges": ["verify_coupled.py"], "oracle": "independent",
                 "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a",
                         "triggers": ["push", "pull_request"]}]},
                {"id": "noselftest", "judges": ["verify_noselftest.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a",
                         "triggers": ["push", "pull_request"]}]},
                {"id": "unwired", "judges": ["verify_unwired.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a",
                         "triggers": ["push", "pull_request"]}]},
                # 反证样本 ⑤：登记了 schedule，但 ci.yml 的 on: 里没有 schedule
                {"id": "missing-trigger", "judges": ["verify_ok.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "a",
                         "triggers": ["push", "schedule"]}]},
                # 反证样本 ⑥：schedule 在 on: 里，但 job 的 if 把它排除了
                {"id": "excluded-trigger", "judges": ["verify_ok.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci2.yml", "job": "only-push",
                         "triggers": ["push", "schedule"]}]},
                # 反证样本 ⑦：if 判不了 ⇒ 判黄，不判红
                {"id": "unknown-if", "judges": ["verify_ok.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci2.yml", "job": "opaque",
                         "triggers": ["schedule"]}]},
                # 反证样本 ⑧：登记的 job 里只有 --selftest ⇒ 判红
                {"id": "selftest-only", "judges": ["verify_selftestonly.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "b",
                         "triggers": ["push", "pull_request"]}]},
                # 反证样本 ⑨：真判定在 job c，登记指了 job b ⇒ 判红
                {"id": "misplaced", "judges": ["verify_elsewhere.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci.yml", "job": "b",
                         "triggers": ["push", "pull_request"]}]},
                # 反证样本 ⑪：`--selftest` 只写在**注释**里 ⇒ 不在自动发现集合
                # ⇒ 而 ci3.yml 用了自动发现式接线 ⇒ 它永远不会被跑到 ⇒ 判红
                {"id": "commentonly", "judges": ["verify_commentonly.py"],
                 "oracle": "independent", "sharedWith": [],
                 "ci": [{"file": "ci3.yml", "job": "auto",
                         "triggers": ["push", "pull_request"]}]},
            ],
            "retired": [],
            "notInvariants": {"tools": ["gen_changelogs.py"]},
        }
        reg_path = root / "invariants.json"
        reg_path.write_text(json.dumps(registry), encoding="utf-8")

        rep = Report(title="不变量登记表")
        judge(registry, scripts,
              {"ci.yml": str(wf), "ci2.yml": str(wf2), "ci3.yml": str(wf3)}, rep)
        subjects = {f.subject for f in rep.reds}
        yellow_subjects = {f.subject for f in rep.yellows}
        check("未登记的守卫被抓出", "verify_orphan.py" in subjects, True)
        check("未披露的耦合被抓出", "coupled" in subjects, True)
        check("已登记且合规的不报", "ok" in subjects, False)
        # 反证样本 ⑪：自动发现式接线下，集合外的脚本（--selftest 只在注释里）判红
        check("注释型 --selftest 在自动发现接线下被判红",
              "commentonly" in subjects, True)
        # ⚠️ 「自动发现式接线」本身也要有判据：只有**跑分片**的那一步才算数，
        #    光有「算片数」（--plan-shards）不算 ⇒ 否则「计划 job 在、跑分片的
        #    job 没了」会被判成已接线 ⇒ 假接线漏过（实测变异：这正是没转红的原因）。
        check("只有 --plan-shards 不算已接线",
              auto_discovery_wired(
                  "      - run: python3 scripts/selftest_all.py --plan-shards\n"), False)
        check("跑分片才算已接线",
              auto_discovery_wired(
                  "      - run: python3 scripts/selftest_all.py --shard 0/2\n"), True)
        # ③ 硬规则 2：没有 --selftest 的判据脚本必须判红
        check("没有 --selftest 的判据被抓出", "noselftest" in subjects, True)
        # ④ 硬规则 2：--selftest 没接进 CI 必须判红
        check("--selftest 未接线的判据被抓出", "unwired" in subjects, True)
        # ⑤ 登记的 trigger 在 on: 里不存在 ⇒ 判红
        check("登记了不存在的 trigger 被抓出", "missing-trigger" in subjects, True)
        # ⑥ job 的 if 排除了登记的 trigger ⇒ 判红
        check("if 排除登记 trigger 被抓出", "excluded-trigger" in subjects, True)
        # ⑦ if 判不了 ⇒ 判黄，**不判红**（硬规则 3）
        check("if 判不了的不判红", "unknown-if" in subjects, False)
        check("if 判不了的判黄", "unknown-if" in yellow_subjects, True)
        # ⑧ 第 7 条：登记的 job 里只有 --selftest ⇒ 判红（自测不是判定）
        check("登记 job 里只有 --selftest 被抓出", "selftest-only" in subjects, True)
        # ⑨ 第 7 条：真判定在别的 job、登记指错 ⇒ 判红
        check("登记的 CI 位置指错被抓出", "misplaced" in subjects, True)
        misplaced_hint = " ".join(f.hint or "" for f in rep.reds if f.subject == "misplaced")
        check("修法文案指出真判定在哪个 job", "ci.yml :: c" in misplaced_hint, True)

        # ⑩ 生成器 → 守卫 的耦合未披露 ⇒ 判红（耦合的另一个方向）
        check("生成器 import 守卫未披露被抓出", "generator-coupling" in subjects, True)

        # 反证：把 --selftest 的 CI 接线去掉 ⇒ 必须转红（证明这条判定有区分力）
        write_ci1(
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n")
        rep_x = Report(title="不变量登记表")
        judge(registry, scripts, {"ci.yml": str(wf), "ci2.yml": str(wf2)}, rep_x)
        check("抽掉 --selftest 接线 → 判红", rep_x.has_red(), True)

        # 披露之后应当转绿（并把两个反证样本补齐）
        registry["invariants"][1]["sharedWith"] = [
            {"module": "gen_changelogs", "why": "只取事实"},
            {"module": "matrix_facts", "why": "只取事实"},
        ]
        # 反证样本 ⑩ 的补齐：把「生成器 import 守卫」这条耦合**披露**掉
        # （披露后应当转绿——披露制就是「写了理由就放行，由人 review」）
        for inv in registry["invariants"]:
            if inv["id"] == "ok":
                inv["sharedWith"] = [
                    {"module": "verify_ok", "why": "反证样本：披露生成器→守卫的耦合"},
                ]
        # 把孤儿脚本登记进不变量**并且**接进 CI（两者缺一都会红）
        registry["invariants"][0]["judges"].append("verify_orphan.py")
        registry["invariants"][0]["ci"].append(
            {"file": "ci.yml", "job": "a", "triggers": ["push", "pull_request"]})
        # 反证样本补齐：给它们加上 --selftest 开关并接进 CI
        (scripts / "verify_noselftest.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n", encoding="utf-8")
        write_ci1(
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_ok.py --selftest\n"
            "      - run: python3 scripts/verify_coupled.py\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n"
            "      - run: python3 scripts/verify_orphan.py\n"
            "      - run: python3 scripts/verify_orphan.py --selftest\n"
            "      - run: python3 scripts/verify_noselftest.py\n"
            "      - run: python3 scripts/verify_noselftest.py --selftest\n"
            "      - run: python3 scripts/verify_unwired.py\n"
            "      - run: python3 scripts/verify_unwired.py --selftest\n",
            b_real=True)
        # 触发条件补齐：ci.yml 真的加上 schedule；ci2.yml 的 only-push 去掉 if
        # 接线补齐（第 7 条）：每条登记的位置都必须有**真判定**步骤，
        # 不能只有 --selftest —— 补齐后应当转绿。
        wf.write_text(
            "on:\n"
            "  push:\n"
            "  pull_request:\n"
            "  schedule:\n"
            "jobs:\n"
            "  a:\n    steps:\n"
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: python3 scripts/verify_ok.py --selftest\n"
            "      - run: python3 scripts/verify_coupled.py\n"
            "      - run: python3 scripts/verify_coupled.py --selftest\n"
            "      - run: python3 scripts/verify_orphan.py\n"
            "      - run: python3 scripts/verify_orphan.py --selftest\n"
            "      - run: python3 scripts/verify_noselftest.py\n"
            "      - run: python3 scripts/verify_noselftest.py --selftest\n"
            "      - run: python3 scripts/verify_unwired.py\n"
            "      - run: python3 scripts/verify_unwired.py --selftest\n"
            "  b:\n    steps:\n"
            "      - run: python3 scripts/verify_selftestonly.py --selftest\n"
            "      - run: python3 scripts/verify_elsewhere.py --selftest\n"
            "      - run: python3 scripts/verify_selftestonly.py\n"
            "      - run: python3 scripts/verify_elsewhere.py\n"
            "  c:\n    steps:\n"
            "      - run: python3 scripts/verify_elsewhere.py\n",
            encoding="utf-8")
        wf2.write_text(
            "on:\n"
            "  push:\n"
            "  schedule:\n"
            "jobs:\n"
            "  only-push:\n"
            "    steps:\n"
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: echo only-push\n"
            "  opaque:\n"
            "    if: always()\n"
            "    steps:\n"
            "      - run: python3 scripts/verify_ok.py\n"
            "      - run: echo opaque\n",
            encoding="utf-8")
        # 反证样本 ⑪ 补齐：给「注释型」脚本注册**真的** --selftest 入口
        # （argparse 的 add_argument）⇒ 它进得了自动发现集合 ⇒ 判据 8 应当转绿。
        (scripts / "verify_commentonly.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument(\"--selftest\", action=\"store_true\")\n",
            encoding="utf-8")
        rep2 = Report(title="不变量登记表")
        judge(registry, scripts,
              {"ci.yml": str(wf), "ci2.yml": str(wf2), "ci3.yml": str(wf3)}, rep2)
        check("披露后转绿", rep2.has_red(), False)
        # 判不了的仍然是黄灯——补齐不该把它变成绿灯（硬规则 3）
        check("补齐后判不了的仍是黄灯",
              "unknown-if" in {f.subject for f in rep2.yellows}, True)

    if fails:
        print("❌ verify_inventory 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_inventory 自测通过"
          "（未登记 / 未接线 / 未披露耦合 / 无 --selftest / --selftest 未接线 / "
          "登记的 CI 位置不跑该判据 / 登记了不存在的 trigger / "
          "if 排除登记的 trigger / if 判不了判黄 / "
          "注释型 --selftest 在自动发现接线下被判红 十类检出；"
          "并自检「自动发现式接线」判据本身）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="不变量登记表的守门人")
    ap.add_argument("--registry", default=str(HERE / "invariants.json"))
    ap.add_argument("--root", default=str(HERE.parent.parent),
                    help="矩阵根目录（用于解析登记的 CI 文件路径）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-yellow", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    reg_path = Path(args.registry)
    if not reg_path.is_file():
        print(f"❌ 找不到登记表 {reg_path}")
        return 2
    registry = json.loads(reg_path.read_text(encoding="utf-8"))

    root = Path(args.root).resolve()
    scripts_dir = reg_path.parent

    # 要核对的 CI 文件 = **登记表里登记过的那些**（从登记表推导，不另维护一份清单）。
    #
    # 为什么不能硬编码一份文件清单：清单与登记表是两份数据 ⇒ 它们会漂
    # ⇒ 新登记的 CI 文件在核对时被**静默跳过**（本轮实测踩到：登记表新增了
    # `.github/.github/workflows/go-ci.yml` 这条腿，而核对脚本的硬编码清单里没有它
    # ⇒ 门禁报「登记的 CI 文件找不到」，而本地复刻 CI 布局跑却是绿的）。
    # ⇒ 一份数据源：登记表说要看哪个文件，就看哪个文件；
    #    工作区里找不到它 ⇒ 判红（「登记说有、实际没有」本来就是假的承诺）。
    workflows: dict[str, str] = {}
    for inv in registry.get("invariants", []) or []:
        for ci in inv.get("ci", []) or []:
            name = str(ci.get("file", ""))
            if not name or name in workflows:
                continue
            p = root / name
            if p.is_file():
                workflows[name] = str(p)

    report = Report(title="不变量登记表")
    judge(registry, scripts_dir, workflows, report)
    return emit(report, fail_on_yellow=args.fail_on_yellow, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
