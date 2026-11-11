#!/usr/bin/env python3
"""gen_skill.py（oagentkit）数据与渲染自测——**无副作用、不联网**。

覆盖：渲染确定性 / 场景表 mods 死键 / 提及子包死键（∈ modules.json packages）/
frontmatter name 一致 / 头号判据存在（agentkit 门面废弃必须写进 SKILL）。
用法：python3 scripts/gen_skill_selftest.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE))

import gen_skill  # noqa: E402

failures: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


mods = {m["repo"]: m for m in gen_skill.load_modules()["modules"]}

# 1) 渲染确定性
for name, fn in (("skill", gen_skill.render_skill), ("modules", gen_skill.render_modules),
                 ("pkgs", gen_skill.render_pkgs)):
    expect(fn() == fn(), f"render_{name}() 两次渲染不一致")

# 2) 场景表 mods 死键
bad = sorted({m for _, rows in gen_skill.SCENARIOS for r in rows for m in r.get("mods", [])
              if m not in mods})
expect(not bad, f"SCENARIOS 引用了矩阵不存在的模块：{bad}")
empty = [r["scene"] for _, rows in gen_skill.SCENARIOS for r in rows if not r.get("mods")]
expect(not empty, f"SCENARIOS 这些行没填 mods：{empty}")

# 3) 场景表提及的子包死键（use/tips 里的标识符若命中已知子包名集合的拼写变体才查——
#    精确做法：把反引号内「含 / 的路径」尾段与 packages 对账）
all_pkgs = {p for m in mods.values() for p in m["packages"]}
dead = set()
for _, rows in gen_skill.SCENARIOS:
    for r in rows:
        blob = r["use"] + " " + r.get("tips", "")
        for t in re.findall(r"`([a-z][a-z0-9]*(?:/[a-z0-9]+)+)`", blob):
            tail = t.split("/")[-1]
            # 形如 repo/pkg 或 pkg/sub 的引用：尾段必须在已知包集合里
            if "/" in t and tail not in all_pkgs and t.split("/")[0] in mods:
                dead.add(t)
expect(not dead, f"场景表引用了不存在的子包路径：{sorted(dead)}")

# 4) 头号判据与 frontmatter
skill_md = gen_skill.render_skill()
expect("agentkit 门面已废弃" in skill_md, "SKILL.md 缺头号判据（agentkit 门面废弃）")
head = skill_md.split("---", 2)
m = re.search(r"^name:\s*(\S+)\s*$", head[1], re.M)
expect(bool(m) and m.group(1) == "oagentkit", f"frontmatter name 不一致：{m and m.group(1)}")
desc = " ".join(re.search(r"description: >-\n((?:  .*\n)+)", skill_md).group(1).split())
expect(len(desc) <= 1024, f"description 超长 {len(desc)}")

# 5) 兄弟 release skill（若在）name 一致
rel = gen_skill.MT_ROOT / "skills" / "oagentkit-release" / "SKILL.md"
if rel.exists():
    fm = rel.read_text(encoding="utf-8").split("---", 2)
    m2 = re.search(r"^name:\s*(\S+)\s*$", fm[1], re.M)
    expect(bool(m2) and m2.group(1) == "oagentkit-release", "oagentkit-release name 不一致")

if failures:
    print(f"❌ gen_skill（oagentkit）自测失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("✓ gen_skill（oagentkit）自测通过（确定性/死键/头号判据/frontmatter）")
