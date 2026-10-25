#!/usr/bin/env python3
"""旧单仓包路径 → 新矩阵 import 路径的唯一构造规则。

**为什么要有这个文件**：import 路径怎么拼是整个拆分里最反直觉、后果最严重的一条规则。
规律只有一句——**新路径 = github.com/{org}/{仓名}/{包名末段}**，旧路径的中间层目录会被丢掉。
迁移实测 246 处 import 改写，如果任何一个仓（或任何一处脚本）没推导出这条规则，
结果不是「不好看」，而是**整仓构建断裂**。

历史上这条规则被复制在 `rewrite_imports.py` 与迁移期临时脚本里，两边各自推导。
谁少推一条、谁改一次，全靠人记。现在它是本文件里的一个函数，
`rewrite_imports.py` / `gen_versions.py` / `gen_llms_txt.py` 全部 import 它，
文档与机器输出都从同一个函数生成——**改规则只改这一处**。

⚠️ 唯一的例外：包名末段 **等于** 仓名时，包落在 **module 根**，路径不再重复末段。
Go 惯例要求包名不重复 import 路径末段（`llm/llm` 是 stutter），
而 `llm` / `rag` 这两个仓恰好仓名=包名，所以规则必须在这里收口，
否则产出 `github.com/oagentkit/llm/llm` 这种永远打嗝的公开路径。
这是**唯一**的例外，写在这里是为了让所有推导者只能从这里读到它。

反直觉的四点（真实数据，来自 modules.json）：

    agentkit/knowledge/rag  ->  oagentkit/rag           中间层 knowledge 丢掉，且末段=仓名 → 落 module 根
    agentkit/worker/pglease ->  oagentkit/queue/pglease  中间层 worker 丢掉，且换仓
    agentkit/llm            ->  oagentkit/llm           末段=仓名 → 落 module 根，不重复
    agentkit/llm/wrap       ->  oagentkit/llm/wrap      末段≠仓名 → 照常挂在仓下

用法:
    python3 scripts/matrix_paths.py --matrix modules.json --org oagentkit --print-rules
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# 旧单仓 module 根，与 rewrite_imports.py 共用同一常量（这里定义，那边 import）
OLD_ROOT = "git.enjoye.top/enjoydream/agentkit"
DEFAULT_ORG = "oagentkit"

# 矩阵**内部**的历史 import 路径表（曾经发布过、现已退役的路径 → 当前路径）。
# 与旧单仓路径不是一类东西：旧单仓路径由 modules.json 的 packages 列表天然覆盖，
# 矩阵内部的历史路径（某仓发版时自己改了包路径，如 llm v0.9.0）不覆盖 ⇒ 单独一张表。
HISTORICAL_PATHS_FILE = Path(__file__).resolve().parent / "historical_paths.json"


def load_historical_paths() -> list[dict]:
    """读历史 import 路径表；文件缺失 ⇒ 空表（不报错，保持数据源可选）。"""
    if not HISTORICAL_PATHS_FILE.is_file():
        return []
    data = json.loads(HISTORICAL_PATHS_FILE.read_text(encoding="utf-8"))
    return list(data.get("paths", []))


def build_historical_mapping(org: str) -> dict[str, str]:
    """{历史路径: 当前路径}。路径模板里的 `{org}` 按 org 替换。

    `org` 传字面量 `"{org}"` 时两侧都留占位符（rewrite_imports.py 在匹配时才替换，
    因为它要同时服务多个 org）；传真实 org 时直接展开（--print-rules 的输出）。
    """
    out: dict[str, str] = {}
    for entry in load_historical_paths():
        out[entry["from"].replace("{org}", org)] = entry["to"].replace("{org}", org)
    return out


def leaf(pkg: str) -> str:
    """包路径的末段。`knowledge/rag` -> `rag`，`llm` -> `llm`。"""
    return pkg.rsplit("/", 1)[-1]


def new_import_path(org: str, repo: str, pkg: str) -> str:
    """**核心规则**：github.com/{org}/{repo}/{pkg 末段}，末段==仓名时收口到 module 根。

    仓名一律用 modules.json 的 repo，包名一律取末段——中间层目录（knowledge/、worker/、llm/）
    在旧单仓里是为了组织目录，在新矩阵里由**仓**承担，因此必须丢弃。

    唯一的例外是 `leaf(pkg) == repo`：此时包直接住在 module 根，
    import 路径就是 module 路径本身，不再拼末段。
    Go 的惯例是包名不重复 import 路径末段，而 llm / rag 两个仓的包名恰好等于仓名，
    不收口就会产出 `oagentkit/llm/llm` 这种 stutter 路径作为长期公开 API。
    """
    l = leaf(pkg)
    if l == repo:
        return f"github.com/{org}/{repo}"
    return f"github.com/{org}/{repo}/{l}"


def legacy_import_path(pkg: str) -> str:
    """旧单仓里的完整 import 路径，用于 fromLegacy 字段与 --print-rules 的左列。"""
    return f"{OLD_ROOT}/{pkg}"


def short_legacy(pkg: str) -> str:
    """省略 module 根的旧包路径，文档里读起来更短：`agentkit/knowledge/rag`。"""
    return f"agentkit/{pkg}"


def build_mapping(modules: list[dict], org: str) -> dict[str, str]:
    """返回 {旧完整 import 路径: 新 import 路径}。

    两类历史路径都在这里：
    ① 旧单仓路径（`git.enjoye.top/enjoydream/agentkit/<pkg>`）—— 由 modules.json 的
       packages 列表逐条生成；
    ② 矩阵内部的历史路径（见 historical_paths.json）—— 某仓发版时自己改掉的包路径，
       曾经发布过 ⇒ 下游可能正引用着 ⇒ 必须同样能机械改写。
    """
    mapping: dict[str, str] = {}
    for mod in modules:
        repo = mod["repo"]
        for pkg in mod["packages"]:
            mapping[legacy_import_path(pkg)] = new_import_path(org, repo, pkg)
    mapping.update(build_historical_mapping(org))
    return mapping


def misleading(modules: list[dict], org: str) -> list[tuple[str, str]]:
    """挑出「直觉会写错」的条目，用于在 llms.txt 里展示真实对照。

    两类坑：
      1. 中间层被丢弃（knowledge/rag -> oagentkit/rag）—— 直觉会保留 knowledge
      2. 末段与仓名相同（llm -> oagentkit/llm）—— 直觉会以为要重复成 llm/llm，
         实际是收口到 module 根

    保留第 2 类不是为了讲历史，是因为它现在是规则的唯一例外，
    下一个人重新怀疑时，这里就是答案。
    """
    out: list[tuple[str, str]] = []
    for mod in modules:
        repo = mod["repo"]
        for pkg in mod["packages"]:
            if leaf(pkg) != pkg or repo == leaf(pkg):
                out.append((short_legacy(pkg), new_import_path(org, repo, pkg)))
    return out


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="打印 旧→新 import 路径映射表（权威输出）")
    ap.add_argument("--matrix", default=str(here.parent / "modules.json"))
    ap.add_argument("--org", default=DEFAULT_ORG)
    ap.add_argument("--print-rules", action="store_true", default=True,
                    help="打印映射表（本脚本默认行为，保留此参数只为与 rewrite_imports.py 对齐）")
    args = ap.parse_args()

    data = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    org = args.org or data.get("defaultOrg") or DEFAULT_ORG
    mapping = build_mapping(data["modules"], org)

    print(f"# {len(mapping)} 条规则  org={org}\n")
    width = max(len(k) for k in mapping)
    for src in sorted(mapping):
        print(f"{src:<{width}}  ->  {mapping[src]}")
    print(f"\n# {len({m['repo'] for m in data['modules']})} 个仓: "
          f"{', '.join(sorted({m['repo'] for m in data['modules']}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
