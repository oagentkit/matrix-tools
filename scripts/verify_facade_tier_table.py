#!/usr/bin/env python3
"""门面仓 `README.md` 的**全矩阵分层表** vs 矩阵单一数据源 `modules.json`。

## 守的不变量

**门面 README 里那张「13 个仓，按 tier 严格自底向上」的表，必须与 `modules.json` 逐 tier 逐仓一致。**

## 为什么需要它（实测 2026-10-30）

矩阵里有**三张分层表**，各自一度漂移，且**只有一张有门禁**：

| 载体 | 形态 | 门禁 |
|---|---|---|
| `.github/profile/README.md` | org profile 的模块矩阵表 | ✅ `verify_profile_tiers.py` |
| 各模块仓 `AGENTS.md` / `README.md` | 「本仓是 tier N」自述 | ✅ `verify_repo_tiers.py` |
| **`agentkit/README.md`** | **全矩阵分层表** | ❌ **无** |

2026-10-30 实测门面 README 那张表写的是**重定位之前的旧分层**：

| tier | 门面 README 写的 | modules.json 实际 |
|---|---|---|
| 0 | `textx` `runtime` `guardx` **`queue`** | `guardx` `runtime` `textx`（**3 个**） |
| 1 | `promptx` **`obs` `skillkit` `mcpkit` `rag`** | `promptx`（**1 个**） |
| 2 | `llm` `toolgate` `tools` | `llm` `mcpkit` `obs` `queue` `rag` `skillkit` `toolgate` `tools`（**8 个**） |

⇒ 同一个仓的两份第一入口对**同一张表**给出两套答案。
更糟的是**方向相反**：profile 那张表在 2026-10-21 已被修成正确值并接上守卫，
门面的这张表仍然是旧的——读者读门面 README 会按 4 层/4 仓的旧世界理解依赖方向。

### 为什么三个机制都没抓住它

1. `verify_repo_tiers.py` 判的是「**本仓自述**层位」（`本仓是 tier N`）——
   门面**没有**这种形态（它自述的是全矩阵分层表），它的形态没被这套判据覆盖；
2. `verify_profile_tiers.py` 只守 `.github/profile/README.md` 一个文件，
   参数写死 `--profile`；
3. `gen_docs.py` 能生成各仓文档骨架，但门面**不在它的生成范围**里（它只管 13 个模块仓），
   且门面的 README 是**手写丰富的那个**——不能整体覆盖。

⇒ 结果：这张表**没有任何自动路径能更新**，也没有任何门禁能发现它漂了。
直到 2026-10-30 有人把它和 modules.json 逐行比，才发现它已经错了整整一层重定位。

## 判据（独立判定，不复用任何生成器）

1. 门面 README 必须存在、且**必须含生成区标记**
   （`<!-- oagentkit:generated:begin -->` / `<!-- oagentkit:generated:end -->`）
   —— 标记是「这块内容归生成器管」的契约；没有标记 ⇒ 判红
   （否则这张表会退回纯手写，回到「漂了没人知道」的老路）；
2. 标记之间的表格行必须与数据源**逐 tier 逐仓**一致：
   - tier 数字集合必须与数据源的 tier 集合相同（多一层少一层都要报）；
   - 每个 tier 下的仓名集合必须**完全相同**（顺序不敏感）；
   - 仓名必须是数据源里的真实仓（写了不存在的仓 ⇒ 报）；
3. 表头行 `| tier | 模块 |` 与分隔行必须存在。

**只共享渲染层**（`judge_report.py`），不 import 任何生成器或判定脚本
（本仓硬规则：事实可以共享，判定不能共享）。

## 修法

改 `matrix-tools/modules.json`（分层是数据源说了算），
然后重跑本脚本 `--write` 重写门面 README 的生成区。

## 怎么用

    # CI 口径：矩阵源 vs 门面 README
    python3 scripts/verify_facade_tier_table.py --root <矩阵根>

    # 只校验（默认）
    python3 scripts/verify_facade_tier_table.py --facade <门面仓路径>

    # 按数据源重写生成区
    python3 scripts/verify_facade_tier_table.py --facade <门面仓路径> --write

    # 离线自测
    python3 scripts/verify_facade_tier_table.py --selftest

退出码: 0 = 一致；1 = 漂移 / 缺生成区 / 读不到；2 = 用法或环境错误。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

BEGIN = "<!-- oagentkit:generated:begin -->"
END = "<!-- oagentkit:generated:end -->"

# 表格行：| 0 | `guardx` `runtime` `textx` |
_ROW_RE = re.compile(r"^\|\s*(?P<tier>\d+)\s*\|\s*(?P<cells>.*?)\s*\|$")
# 单元格里的仓名：反引号包裹，或裸文本
_CELL_RE = re.compile(r"`([A-Za-z0-9_.-]+)`")


# ---------------------------------------------------------------- 取数（事实层）

def load_tiers(path: Path) -> dict[int, list[str]]:
    """modules.json → {tier: [仓名…]}（同 tier 内按仓名字母序）。

    **取数只做「事实」**：把数据源读成结构，不做任何判定。
    """
    if not path.is_file():
        raise FileNotFoundError(f"找不到矩阵数据源：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    by_tier: dict[int, list[str]] = {}
    for m in data.get("modules") or []:
        by_tier.setdefault(int(m["tier"]), []).append(str(m["repo"]))
    return {t: sorted(v) for t, v in by_tier.items()}


def render_table(by_tier: dict[int, list[str]]) -> str:
    """由数据源渲染门面 README 的分层表（反引号包裹仓名，tier 升序）。"""
    lines = ["| tier | 模块 |", "|---|---|"]
    for tier in sorted(by_tier):
        cells = " ".join(f"`{repo}`" for repo in by_tier[tier])
        lines.append(f"| {tier} | {cells} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- 判定（判定层）

def parse_table(block: str) -> dict[int, set[str]]:
    """从表格文本解析出 {tier: {仓名…}}。

    解析不出 tier 行的**静默丢弃**会让「表整段被删」变成「空表 ⇒ 看起来一致」，
    所以这里把「一个 tier 行都没解析出来」显式判成不一致（由 judge 判红）。
    """
    out: dict[int, set[str]] = {}
    for line in block.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        tier = int(m.group("tier"))
        cells = _CELL_RE.findall(m.group("cells"))
        if not cells:
            continue
        out.setdefault(tier, set()).update(cells)
    return out


def judge(table_text: str, by_tier: dict[int, list[str]]) -> list[str]:
    """比对分层表与数据源，返回问题列表；空列表 = 一致。"""
    problems: list[str] = []

    if BEGIN not in table_text or END not in table_text:
        problems.append(
            f"门面 README 里找不到生成区标记（需要 {BEGIN} / {END}）——"
            "没有标记这块内容就退回纯手写，漂了没有任何门禁会发现"
        )
        return problems

    i = table_text.index(BEGIN)
    j = table_text.index(END)
    if j < i:
        problems.append("生成区标记顺序反了（end 在 begin 之前）")
        return problems

    block = table_text[i + len(BEGIN):j]
    current = parse_table(block)

    if not current:
        problems.append("生成区里一个 tier 行都解析不出来（表格被删了或格式变了）")
        return problems

    want = {t: set(v) for t, v in by_tier.items()}

    # tier 集合
    only_cur = sorted(set(current) - set(want))
    only_want = sorted(set(want) - set(current))
    if only_cur:
        problems.append(f"表里多出数据源没有的 tier：{only_cur}")
    if only_want:
        problems.append(f"表里缺少数据源的 tier：{only_want}")

    # 逐 tier 逐仓
    for tier in sorted(set(current) & set(want)):
        got, exp = current[tier], want[tier]
        if got != exp:
            missing = sorted(exp - got)
            extra = sorted(got - exp)
            detail = []
            if missing:
                detail.append(f"少 {missing}")
            if extra:
                detail.append(f"多 {extra}")
            problems.append(f"tier {tier} 的模块与数据源不一致：{'；'.join(detail)}")

    return problems


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    """反证自测：拿**故意造坏**的样本跑，要求判据报出来。

    样本覆盖（AGENTS.md 硬规则 2：自测必须带反证样本，「跑通即绿」是摆设）：
      ① 与数据源一致            → 必须判绿
      ② 少一个仓                → 必须判红
      ③ 多一个仓                → 必须判红
      ④ 整层写错（层位整体漂移）→ 必须判红
      ⑤ 少一层 / 多一层         → 必须判红
      ⑥ 缺生成区标记            → 必须判红（否则退回手写就没人守了）
      ⑦ 生成区里表格被删空      → 必须判红（不得当成「一致」）
      ⑧ tier 数字相同但仓名不同 → 必须判红
    """
    failures: list[str] = []

    def check_ok(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    by_tier = {0: ["guardx", "runtime", "textx"], 1: ["promptx"],
               2: ["llm", "mcpkit", "obs", "queue", "rag", "skillkit", "toolgate", "tools"],
               3: ["agent"]}
    good = BEGIN + "\n" + render_table(by_tier) + "\n" + END

    # ① 一致 → 无问题
    check_ok("一致时无问题", judge(good, by_tier), [])

    # ② 少一个仓
    bad = BEGIN + "\n" + render_table({**by_tier, 0: ["guardx", "runtime"]}) + "\n" + END
    check_ok("少仓被判出", len(judge(bad, by_tier)), 1)

    # ③ 多一个仓
    bad = BEGIN + "\n" + render_table({**by_tier, 0: ["guardx", "runtime", "textx", "ghost"]}) + "\n" + END
    check_ok("多仓被判出", len(judge(bad, by_tier)), 1)

    # ④ 整层写错（线上真实形态：queue 还在 tier0）
    wrong = {0: ["guardx", "runtime", "textx", "queue"], 1: ["promptx", "obs"],
             2: ["llm", "toolgate", "tools"], 3: ["agent"]}
    bad = BEGIN + "\n" + render_table(wrong) + "\n" + END
    problems = judge(bad, by_tier)
    check_ok("整层漂移被判出", len(problems) >= 2, True)

    # ⑤ 少一层
    bad = BEGIN + "\n" + render_table({k: v for k, v in by_tier.items() if k != 3}) + "\n" + END
    check_ok("少一层被判出", len(judge(bad, by_tier)), 1)

    # ⑥ 缺生成区标记 ⇒ 判红（不能默认当成一致）
    check_ok("缺标记被判红", len(judge("| tier | 模块 |\n|---|---|\n", by_tier)), 1)

    # ⑦ 生成区里表格被删空 ⇒ 判红
    check_ok("表格被删空被判红", len(judge(BEGIN + "\n\n" + END, by_tier)), 1)

    # ⑧ tier 数字相同但仓名不同（最隐蔽的一种：数量对、集合错）
    same_count = {0: ["guardx", "runtime", "queue"], 1: ["promptx"],
                  2: ["llm", "mcpkit", "obs", "queue", "rag", "skillkit", "toolgate", "tools"],
                  3: ["agent"]}
    bad = BEGIN + "\n" + render_table(same_count) + "\n" + END
    check_ok("数量对但集合错也被判出", len(judge(bad, by_tier)), 1)

    if failures:
        print("❌ verify_facade_tier_table 自测失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ verify_facade_tier_table 自测通过"
          "（少仓 / 多仓 / 整层漂移 / 少层 / 缺标记 / 表格删空 / 数量对集合错）")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="门面 README 分层表 vs 矩阵数据源的一致性门禁")
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"),
                    help="矩阵单一数据源 modules.json 路径")
    ap.add_argument("--facade", help="门面仓本地路径（默认 <root>/agentkit）")
    ap.add_argument("--root", help="矩阵根目录（门面仓在其下，目录名 agentkit）")
    ap.add_argument("--write", action="store_true",
                    help="按数据源重写门面 README 的生成区（默认只校验）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.facade and not args.root:
        print("❌ 需要 --facade <门面仓路径> 或 --root <矩阵根>")
        return 2

    matrix_path = Path(args.matrix)
    facade_path = Path(args.facade) if args.facade else Path(args.root) / "agentkit"

    try:
        by_tier = load_tiers(matrix_path)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 2
    except json.JSONDecodeError as e:
        print(f"❌ {matrix_path} 不是合法 JSON：{e}")
        return 2

    if not facade_path.is_dir():
        print(f"❌ 找不到门面仓目录：{facade_path}")
        return 2
    readme = facade_path / "README.md"
    if not readme.is_file():
        print(f"❌ 找不到门面 README：{readme}")
        return 2
    text = readme.read_text(encoding="utf-8")

    if args.write:
        expected = render_table(by_tier)
        if BEGIN not in text or END not in text:
            print(f"❌ {readme} 里没有生成区标记，无法自动重写——"
                  f"请手工加上 {BEGIN} / {END} 后再跑 --write")
            return 1
        i = text.index(BEGIN)
        j = text.index(END)
        new_text = text[:i + len(BEGIN)] + "\n" + expected + "\n" + text[j:]
        if new_text != text:
            readme.write_text(new_text, encoding="utf-8")
            print(f"✅ 已按 {matrix_path.name} 重写 {readme.name} 的生成区")
        else:
            print(f"✅ {readme.name} 的生成区已是最新")
        text = new_text

    problems = judge(text, by_tier)
    if problems:
        print("❌ 门面 README 的分层表与矩阵数据源漂移：\n")
        for p in problems:
            print(f"   - {p}")
        print("\n修法：")
        print("  1) 改 matrix-tools/modules.json（分层是数据源说了算，不要手改门面 README）；")
        print("  2) 然后跑：python3 scripts/verify_facade_tier_table.py --root <矩阵根> --write")
        return 1

    print(f"✅ 门面 README 分层表与 {matrix_path.name} 一致"
          f"（{sum(len(v) for v in by_tier.values())} 个仓，逐 tier 逐仓比对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
