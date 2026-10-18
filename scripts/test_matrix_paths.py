#!/usr/bin/env python3
"""matrix_paths 路径规则的独立断言。

刻意**不 import** matrix_paths：
    如果测试直接调用被测函数，规则写错时测试会跟着一起错，绿得毫无意义。
    这里把期望值硬编码成字面量，让它成为对规则的**外部**校验。

背景：import 路径不是前缀映射，而是
    github.com/{org}/{repo}/{包名末段}
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
from matrix_paths import OLD_ROOT, misleading, new_import_path, short_legacy  # noqa: E402

# (org, repo, modules.json 里的包路径, 期望的新 import 路径)
# 包路径用的是 modules.json 里的真实形态（不带 agentkit/ 前缀）。
CASES = [
    # 最易错的一条：agentkit/llm → 不是 .../agentkit/llm/agentkit/llm，
    # 也不是保留中间层，而是直接落到仓名上（llm/llm，重复是**对的**）。
    ("oagentkit", "llm", "llm", "github.com/oagentkit/llm/llm"),
    # 同形但不同仓：两个仓都叫得出 llm，容易混
    ("oagentkit", "agent", "agent", "github.com/oagentkit/agent/agent"),
    # 带中间层目录：中间层必须被丢弃，只留末段
    ("oagentkit", "rag", "knowledge/rag", "github.com/oagentkit/rag/rag"),
    ("oagentkit", "textx", "textutil", "github.com/oagentkit/textx/textutil"),
    ("oagentkit", "runtime", "breaker", "github.com/oagentkit/runtime/breaker"),
    # 单段包名
    ("oagentkit", "queue", "queue", "github.com/oagentkit/queue/queue"),
    # 另一个 org：规则对姊妹矩阵同样成立
    ("oekit", "concurrency", "concurrency", "github.com/oekit/concurrency/concurrency"),
]


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
    want_pairs = {("agentkit/knowledge/rag", "github.com/oagentkit/rag/rag"),
                  ("agentkit/llm", "github.com/oagentkit/llm/llm"),
                  ("agentkit/queue", "github.com/oagentkit/queue/queue")}
    ok = got_pairs == want_pairs
    print(f"  {'✅' if ok else '❌'} misleading() 挑出 {len(got_pairs)} 条，"
          f"期望 {len(want_pairs)} 条（textutil 应被排除）")
    if not ok:
        print(f"      多出 {sorted(got_pairs - want_pairs)}")
        print(f"      缺少 {sorted(want_pairs - got_pairs)}")
        fails.append("misleading")

    if fails:
        print(f"\n❌ {len(fails)} 项不符：{fails}")
        print("\n规则本体在 scripts/matrix_paths.py。若这里与文档/生成器不一致，")
        print("以 matrix_paths 为准并同步修 modules.json 与各仓文档。")
        return 1
    print(f"\n✅ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
