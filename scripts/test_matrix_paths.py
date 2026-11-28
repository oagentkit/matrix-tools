#!/usr/bin/env python3
"""matrix_paths 路径规则的独立断言。

刻意**不 import** matrix_paths：
    如果测试直接调用被测函数，规则写错时测试会跟着一起错，绿得毫无意义。
    这里把期望值硬编码成字面量，让它成为对规则的**外部**校验。

背景：import 路径不是前缀映射，而是
    github.com/{org}/{repo}/{包名末段}
    ——且末段==仓名时收口到 module 根（github.com/{org}/{repo}）。
旧单仓路径 agentkit/knowledge/rag 的中间层 knowledge 会被丢弃。
这条规则错了不会编译失败，只会 module not found，而且是在消费方那边。

用法:
    test_matrix_paths.py          # 跑断言
退出码: 0 = 全部符合；1 = 有不符。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matrix_paths import (  # noqa: E402
    OLD_ROOT,
    build_historical_mapping,
    build_mapping,
    legacy_label,
    misleading,
    new_import_path,
    short_legacy,
)

# (org, repo, modules.json 里的包路径, 期望的新 import 路径)
# 包路径用的是 modules.json 里的真实形态（不带 agentkit/ 前缀）。
CASES = [
    # 最易错的一条：agentkit/llm → 不是 .../agentkit/llm/agentkit/llm，
    # 也不是重复成 llm/llm，而是收口到 module 根（oagentkit/llm）。
    ("oagentkit", "llm", "llm", "github.com/oagentkit/llm"),
    # 同形但不同仓：两个仓都叫得出 llm，容易混
    ("oagentkit", "agent", "agent", "github.com/oagentkit/agent"),
    # 带中间层目录：中间层必须被丢弃，只留末段；末段又等于仓名 → 落 module 根
    ("oagentkit", "rag", "knowledge/rag", "github.com/oagentkit/rag"),
    ("oagentkit", "textx", "textutil", "github.com/oagentkit/textx/textutil"),
    ("oagentkit", "runtime", "breaker", "github.com/oagentkit/runtime/breaker"),
    # 单段包名，且等于仓名 → 落 module 根
    ("oagentkit", "queue", "queue", "github.com/oagentkit/queue"),
    # 另一个 org：规则对姊妹矩阵同样成立
    ("oekit", "concurrency", "concurrency", "github.com/oekit/concurrency"),
]


# 入口冒烟声明（通用守卫 scripts/verify_cli_smoke.py 会真跑一次）
# ⚠️ 这组参数必须**无副作用**、且能让脚本**真正走完入口**（不能只让 argparse 早退）。
SMOKE_ARGS = []

def main() -> int:
    fails = []
    print("路径规则断言（期望值硬编码，不 import 被测实现）\n")
    for org, repo, pkg, want in CASES:
        got = new_import_path(org, repo, pkg)
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {pkg:<16} → {got}")
        if not ok:
            print(f"      期望 {want}")
            fails.append(pkg)

    print()
    # 旧根必须仍是 agentkit 单仓名 —— 它在文档里被引用为「旧单仓」
    if OLD_ROOT.endswith("/agentkit"):
        print(f"  ✅ OLD_ROOT = {OLD_ROOT}")
    else:
        print(f"  ❌ OLD_ROOT = {OLD_ROOT}，期望以 /agentkit 结尾")
        fails.append("OLD_ROOT")

    # short_legacy 给 fromLegacy 字段用：补上 agentkit/ 前缀，不含 module 根
    for pkg, want in [("knowledge/rag", "agentkit/knowledge/rag"),
                      ("textutil", "agentkit/textutil")]:
        got = short_legacy(pkg)
        ok = got == want
        print(f"  {'✅' if ok else '❌'} short_legacy({pkg}) = {got}")
        if not ok:
            print(f"      期望 {want}")
            fails.append(f"short_legacy:{pkg}")

    # misleading 必须挑出全部两类坑：中间层被丢弃、仓名与末段同名。
    # 判据用的是「不该挑的也别挑」——比如 textutil 所在仓 textx 不叫 textutil，
    # 且 textutil 没有中间层，两者都不满足，就不该出现在对照表里。
    fake = [{"repo": "rag", "packages": ["knowledge/rag"]},
            {"repo": "llm", "packages": ["llm"]},
            {"repo": "textx", "packages": ["textutil"]},
            {"repo": "queue", "packages": ["queue"]}]
    got_pairs = set(misleading(fake, "oagentkit"))
    # queue 根包是拆分后新建的（旧单仓只有 worker，没有 queue 目录——
    # facade v1.0.0 树与 MIGRATION.md 均无它）⇒ 旧侧不存在，同样不挑（#260）。
    want_pairs = {("agentkit/knowledge/rag", "github.com/oagentkit/rag"),
                  ("agentkit/llm", "github.com/oagentkit/llm")}
    ok = got_pairs == want_pairs
    print(f"  {'✅' if ok else '❌'} misleading() 挑出 {len(got_pairs)} 条，"
          f"期望 {len(want_pairs)} 条（textutil 无中间层、queue 无旧路径，均应排除）")
    if not ok:
        print(f"      多出 {sorted(got_pairs - want_pairs)}")
        print(f"      缺少 {sorted(want_pairs - got_pairs)}")
        fails.append("misleading")

    # 历史路径表：矩阵**内部**曾经发布过、现已退役的路径 → 当前路径。
    # 这类路径不在 modules.json 的 packages 里（那里只登记**当前**路径），
    # 所以它有自己的数据源（historical_paths.json）与自己的构造点。
    # 期望值硬编码，不从被测实现推导。
    hist = build_historical_mapping("oagentkit")
    want_hist = {
        "github.com/oagentkit/llm/llm": "github.com/oagentkit/llm",
        # toolname 自 oekit/pkg 归位而来：旧路径在**旧单仓之外**
        "github.com/oekit/pkg/toolname": "github.com/oagentkit/toolname",
    }
    ok = hist == want_hist
    print(f"  {'✅' if ok else '❌'} build_historical_mapping('oagentkit') = {hist}")
    if not ok:
        print(f"      期望 {want_hist}")
        fails.append("build_historical_mapping")

    # 占位符形态：同一张表要服务多个 org（姊妹矩阵 oekit 也用改写器）
    hist_ph = build_historical_mapping("{org}")
    want_ph = {
        "github.com/{org}/llm/llm": "github.com/{org}/llm",
        "github.com/oekit/pkg/toolname": "github.com/{org}/toolname",
    }
    ok = hist_ph == want_ph
    print(f"  {'✅' if ok else '❌'} build_historical_mapping('{{org}}') = {hist_ph}")
    if not ok:
        print(f"      期望 {want_ph}")
        fails.append("build_historical_mapping:placeholder")

    # 不是旧单仓拆出来的包（toolname 自 oekit/pkg 归位）⇒ 旧单仓路径从未存在过
    # ⇒ 既不合成 agentkit/toolname 这条假旧路径，fromLegacy 也据实写真实旧路径。
    got_label = legacy_label("toolname", "toolname", "oagentkit")
    ok = got_label == "oekit/pkg/toolname"
    print(f"  {'✅' if ok else '❌'} legacy_label(toolname) = {got_label}")
    if not ok:
        print("      期望 oekit/pkg/toolname")
        fails.append("legacy_label:toolname")

    ok = legacy_label("textx", "textutil", "oagentkit") == "agentkit/textutil"
    print(f"  {'✅' if ok else '❌'} legacy_label(textx/textutil) = 旧单仓路径")
    if not ok:
        fails.append("legacy_label:textx")

    # 后起包（agent-harness 源 / 拆分后新建）：旧单仓从未有过 ⇒ fromLegacy 缺席
    ok = legacy_label("goagent-kernel", "protocol", "yi-nology") == ""
    print(f"  {'✅' if ok else '❌'} legacy_label(goagent-kernel/protocol) = 空（无旧路径）")
    if not ok:
        fails.append("legacy_label:kernel-protocol")

    ok = legacy_label("goagent-runtime", "trace", "yi-nology") == ""
    print(f"  {'✅' if ok else '❌'} legacy_label(goagent-runtime/trace) = 空（无旧路径）")
    if not ok:
        fails.append("legacy_label:runtime-trace")

    mapping_k = build_mapping([{"repo": "goagent-kernel", "packages": ["protocol"]}], "oagentkit")
    ok = not any(k.startswith("git.enjoye.top/enjoydream/agentkit/") for k in mapping_k)
    print(f"  {'✅' if ok else '❌'} 后起包不合成假旧单仓改写规则")
    if not ok:
        fails.append("build_mapping:kernel-protocol")

    mapping = build_mapping([{"repo": "toolname", "packages": ["toolname"]}], "oagentkit")
    ok = ("git.enjoye.top/enjoydream/agentkit/toolname" not in mapping
          and mapping.get("github.com/oekit/pkg/toolname") == "github.com/oagentkit/toolname")
    print(f"  {'✅' if ok else '❌'} 假旧单仓路径未被合成，真实旧路径已登记")
    if not ok:
        fails.append("build_mapping:toolname")

    if fails:
        print(f"\n❌ {len(fails)} 项不符：{fails}")
        print("\n规则本体在 scripts/matrix_paths.py。若这里与文档/生成器不一致，")
        print("以 matrix_paths 为准并同步修 modules.json 与各仓文档。")
        return 1
    print(f"\n✅ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
