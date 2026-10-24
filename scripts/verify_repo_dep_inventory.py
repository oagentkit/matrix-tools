#!/usr/bin/env python3
"""各仓「本仓依赖的矩阵内仓」清单 vs 该仓 `go.mod` 的**直接**矩阵内依赖。

## 守的不变量

**仓级第一入口（`README.md` / `AGENTS.md`）里声明的「本仓依赖哪些矩阵内仓」，
必须与该仓 `go.mod` 的直接矩阵内依赖一致。**

`README.md` / `AGENTS.md` 是仓级的第一入口（人读 + agent 读）。agent 读文档决定
「要不要引某个矩阵内仓」时，它依据的是**文档**，不是 `go.mod`。文档与 `go.mod`
不一致 ⇒ 读文档的人拿到的是假事实。

## 为什么这条缺口此前没人守

实测 2026-10-24（issue matrix-tools#127）：13 个模块仓的清单行全部对不上——

| 仓 | 文件 | 声明 | `go.mod` 直接矩阵内依赖 | 漂 |
|---|---|---|---|---|
| `obs` | `README.md` | `textx`、`guardx` | `guardx` | **多列 `textx`** |
| `toolgate` | `README.md` | `mcpkit` | `guardx`、`mcpkit` | **漏列 `guardx`** |
| `toolgate` | `AGENTS.md` | `mcpkit` | `guardx`、`mcpkit` | **漏列 `guardx`** |

两个仓的漂法相反（一多一少），但危害同向：**文档说的依赖集合 ≠ 编译器的依赖集合**。

根因不是「忘了改」，是**迁出/迁入时没有一条自动路径**：

- `obs` 的 `textx` 随 `019c029`（`refactor!: obsx 迁出本仓`）离开 `go.mod`，
  而同一份 README 第 22 行**已经记录了**那次迁移（eino 耦合的观测适配层迁往 `llm`）——
  文档记了「eino 走了」，却漏改了上一行的清单。
- `toolgate` 的 `guardx` 由 `5c49229`（`refactor!: invoke 迁出本仓`）带进 `go.mod`，
  而它自己的 AGENTS.md 把「invoke 链」写成了核心不变量 —— **依赖清单里没有 guardx**。

三个本该抓住它的机制逐个失效：

1. `verify_import_direction.py`（登记表的 `dependency-declaration` 不变量）判的是
   **`modules.json` 的 `dependsOn` ↔ `go.mod`** —— 实跑红 0 / 黄 0，
   `modules.json` 13/13 与 `go.mod` 一致 ⇒ **漂的是文档层，不是数据源**。
2. `verify_repo_dep_claims.py` 判的是依赖**零性**红线（「零第三方依赖」/「require 段为空」），
   实跑红 0 / 黄 0 —— 它不判「依赖了哪些仓」的清单。
3. `verify_repo_tiers.py` 判的是层位数字与层名 —— 也不判依赖清单。

⇒ 三条守卫都停在「**文档 ↔ `go.mod`**」这一层之前。本守卫补的就是这一层。

## 判据

**事实源 = `go.mod` 的 require 段里的「直接」依赖**（`// indirect` 的不算）。

`// indirect` 必须排除：实测 `toolgate/go.mod` 里
`github.com/oagentkit/runtime v0.7.2 // indirect` 是被 `mcpkit` 拉进来的，
把它算成「toolgate 依赖 runtime」会得出「漏声明」的假阳性。
`matrix_facts.parse_go_mod` 已经把 direct / indirect 分开（同一族事实，两处取数）。

清单口径与 `modules.json` 的 `dependsOn` 同口径（也是直接依赖）——
实测 13/13 一致，所以两个数据源不冲突。

| 情形 | 判定 |
|---|---|
| 清单集合 == `go.mod` 直接矩阵内依赖 | 绿 |
| 清单多列 / 漏列（任一方向） | **红** |
| 清单里有个认不出的仓名（拼错之类） | **黄** |
| 声明行存在但认不出仓名（没加反引号等） | **黄** |
| `go.mod` 读不到 | **黄**（判不了 ≠ 没问题） |
| **有**矩阵内直接依赖，但某个第一入口**没有**声明 | **黄**（覆盖缺口） |
| 无矩阵内直接依赖、也无声明 | 绿（不是「不合格」） |
| 显式写「无」且确实无矩阵内依赖 | 绿 |

### 为什么要两个文件都判

`AGENTS.md` 那一侧的漂移与 `README.md` 那一侧**同构**：
本守卫若只读一个文件，另一个就会安静地失去覆盖 —— 那正是矩阵反复出现的
「同一仓的两个第一入口互相矛盾」（比单个入口写错更糟：读者读到哪个就信哪个）。

「有依赖却没声明」判**黄**而不是红：它不是「说错了」，是**没说**——
但没说的后果同样是失去覆盖（判据管不到它），所以按「认不出 = 需要人看一眼」处理。

## 判据边界

- **只判清单行，不判散文**：本守卫只认「本仓依赖的矩阵内仓」这一行。
  散文里的跨仓提及（例如「`runtime`/`textx` 是零第三方依赖底座」）不是本仓自述，不判。
- **tier 0 的否定句不是声明**：「任何矩阵内模块都不得被本仓依赖」是**禁止**，
  不是「本仓依赖了哪些仓」的清单 ⇒ 不判。
- **包路径不是仓名**：`` `guardx/logredact` `` 是**包**路径，不是仓 ⇒ 不算进清单。
- 全文路径写法（`` `github.com/oagentkit/guardx` ``）与裸名等价。

## 用法

    python3 scripts/verify_repo_dep_inventory.py --root .            # 校验（CI 口径）
    python3 scripts/verify_repo_dep_inventory.py --selftest          # 离线自测（带反证样本）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402
from matrix_facts import load_matrix, parse_go_mod  # noqa: E402

DEFAULT_MATRIX = "matrix-tools/modules.json"

# 仓级第一入口的两个文件（与 verify_repo_dep_claims 同款覆盖面）
DOC_FILES = ("AGENTS.md", "README.md")

# 清单行的锚点：「本仓依赖的矩阵内仓：…」
CLAIM_RE = re.compile(r"本仓依赖的矩阵内仓\s*[：:]")

# 反引号片段
BACKTICK_RE = re.compile(r"`([^`]+)`")

# 裸仓名（小写字母开头，无 `/`）
PLAIN_NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")

# 矩阵内模块的全文路径
FULL_PATH_RE = re.compile(r"^github\.com/oagentkit/([a-z][a-z0-9]*)$")

# 显式空声明：「本仓依赖的矩阵内仓：无」
EMPTY_CLAIM_RE = re.compile(r"^[（(]?\s*无\s*[)）]?$")

# 否定句（「不属于…」「不是…」）
NEGATION_RE = re.compile(r"不属于|不是|并非|不再是")

MATRIX_PREFIX = "github.com/oagentkit/"


@dataclass
class Claim:
    """文档里认出的一处「本仓依赖的矩阵内仓」清单。"""

    path: str          # 相对仓根的路径，如 "AGENTS.md"
    line: int          # 行号（1 起）
    names: frozenset   # 清单里的仓名集合（认不出时为空）
    unknown: frozenset # 认不出是不是仓名的片段
    text: str          # 命中的那一行（截断）
    explicit_empty: bool = False   # 显式写「无」（= 声明本仓没有矩阵内依赖）

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


def _classify_token(token: str, known: set[str]) -> tuple[str | None, bool]:
    """反引号片段 → (仓名 or None, 是否「认不出」)。

    返回 (name, unknown)：name 非空 ⇒ 是个仓名；unknown 为真 ⇒
    是个裸标识符但**不在矩阵仓清单里**（拼错、或写了个非矩阵仓）。
    """
    m = FULL_PATH_RE.match(token)
    if m:
        name = m.group(1)
        return (name, False) if name in known else (None, True)
    if PLAIN_NAME_RE.match(token):
        if token in known:
            return token, False
        # 裸标识符但不是矩阵仓：可能是拼错 ⇒ 判「认不出」而不是静默忽略
        return None, True
    # 含 `/` 的路径（包路径 / 全文路径不匹配上面的形态）⇒ 不是仓名
    return None, False


def parse_claims(docs: dict[str, str], known: set[str]) -> list[Claim]:
    """从仓级文档里认出「本仓依赖的矩阵内仓」的清单行。"""
    claims: list[Claim] = []
    for path, text in docs.items():
        for idx, line in enumerate(text.splitlines(), start=1):
            m = CLAIM_RE.search(line)
            if not m:
                continue
            # 否定句 ⇒ 不判（「本仓不是依赖了 X」）
            if NEGATION_RE.search(line[max(0, m.start() - 8):m.start()]):
                continue

            tail = line[m.end():].strip()
            # 显式空声明：「本仓依赖的矩阵内仓：无。」
            if EMPTY_CLAIM_RE.match(tail.rstrip("。.").strip()):
                claims.append(Claim(path=path, line=idx, names=frozenset(),
                                    unknown=frozenset(), text=line.strip()[:120],
                                    explicit_empty=True))
                continue

            names: set[str] = set()
            unknown: set[str] = set()
            for tok in BACKTICK_RE.findall(tail):
                name, is_unknown = _classify_token(tok.strip(), known)
                if name:
                    names.add(name)
                elif is_unknown:
                    unknown.add(tok.strip())
            claims.append(Claim(path=path, line=idx, names=frozenset(names),
                                unknown=frozenset(unknown),
                                text=line.strip()[:120]))
    return claims


def direct_matrix_deps(gomod_text: str, known: set[str]) -> set[str]:
    """`go.mod` 原文 → 直接依赖的矩阵内仓名集合。"""
    facts = parse_go_mod(gomod_text)
    out: set[str] = set()
    for item in facts["requires_direct"]:
        path = item.split()[0]
        if not path.startswith(MATRIX_PREFIX):
            continue
        name = path[len(MATRIX_PREFIX):]
        if name in known:
            out.add(name)
    return out


def judge(root: Path, matrix_path: Path) -> Report:
    """逐仓比对「本仓依赖的矩阵内仓」清单 vs `go.mod` 的直接矩阵内依赖。"""
    rep = Report()
    rep.title = "各仓依赖清单声明"

    matrix = load_matrix(matrix_path)
    repos = matrix.repos
    known = set(repos)

    claimed = 0
    clean = 0
    yellowed = 0

    for repo in repos:
        repo_dir = root / repo
        gomod = repo_dir / "go.mod"

        if not gomod.exists():
            rep.yellow(repo, "读不到 go.mod，取数失败 ⇒ 核对不了",
                       "确认该仓在 CI 布局里已 clone（matrix-local-guards 会克隆全矩阵 13 仓）")
            yellowed += 1
            continue

        try:
            truth = direct_matrix_deps(gomod.read_text(encoding="utf-8"), known)
        except OSError as exc:
            rep.yellow(repo, f"读 go.mod 失败：{exc}", "确认文件可读")
            yellowed += 1
            continue

        docs: dict[str, str] = {}
        for name in DOC_FILES:
            p = repo_dir / name
            if p.exists():
                docs[name] = p.read_text(encoding="utf-8")

        claims = parse_claims(docs, known)
        by_file = {c.path: c for c in claims}
        repo_yellow = False

        # ① 每个存在的第一入口都要有清单行（有依赖时）
        for name in DOC_FILES:
            if name not in docs:
                continue
            if name in by_file:
                continue
            if truth:
                rep.yellow(repo, f"{name} 没有「本仓依赖的矩阵内仓」声明，"
                                 f"但 go.mod 直接依赖 {len(truth)} 个矩阵内仓"
                                 + ("：" + "、".join(sorted(truth)) if truth else ""),
                           "在该文件里补一行「本仓依赖的矩阵内仓：`…`」"
                           "（口径 = go.mod 的直接矩阵内依赖）")
                repo_yellow = True

        # ② 逐条判清单
        for c in claims:
            if c.unknown:
                rep.yellow(repo, f"{c.where} 的清单里有个认不出的名字："
                                 + "、".join(sorted(c.unknown)),
                           "改成矩阵内仓名（裸名或 github.com/oagentkit/<仓>）")
                repo_yellow = True
                continue
            if not c.names and not c.unknown:
                if c.explicit_empty:
                    # 显式声明「本仓没有矩阵内依赖」⇒ 与事实比对
                    if truth:
                        rep.red(repo, f"{c.where} 声明「本仓依赖的矩阵内仓：无」，"
                                      f"但 go.mod 直接依赖 " + "、".join(sorted(truth)),
                                "清单口径 = go.mod 的直接矩阵内依赖；改这一行，别改判据")
                    continue
                # 声明行存在但认不出仓名（没加反引号 / 写成散文）
                rep.yellow(repo, f"{c.where} 有清单声明，但认不出仓名：{c.text[:60]}",
                           "把仓名逐个用反引号包起来（例：`- 本仓依赖的矩阵内仓：`guardx`。`）")
                repo_yellow = True
                continue
            if c.names != truth:
                extra = sorted(c.names - truth)
                missing = sorted(truth - c.names)
                detail = []
                if extra:
                    detail.append("多列 " + "、".join(extra))
                if missing:
                    detail.append("漏列 " + "、".join(missing))
                rep.red(repo, f"{c.where} 的依赖清单与 go.mod 对不上（"
                              + "；".join(detail) + "）",
                        "清单口径 = go.mod 的**直接**矩阵内依赖（// indirect 不列）"
                        "；改这一行，别改判据")
                continue
            claimed += 1

        if repo_yellow:
            yellowed += 1
        else:
            clean += 1

    rep.note(f"逐仓比对 {len(repos)} 个模块仓的「本仓依赖的矩阵内仓」清单 vs go.mod 直接矩阵内依赖；"
             f"有清单声明的仓 {claimed} 个（核对通过 {clean} 个 / 判不了 {yellowed} 个）")
    return rep


# ---------------------------------------------------------------------------
# 自测（硬规则 2：必须带**反证样本**——故意造坏的被检对象必须被报出来）
# ---------------------------------------------------------------------------


def _mk_matrix(root: Path, repos: list[str]) -> Path:
    (root / "matrix-tools").mkdir(parents=True, exist_ok=True)
    path = root / "matrix-tools" / "modules.json"
    path.write_text(json.dumps({
        "tiers": {"0": "基础层：零重量级第三方地板（纯 stdlib / 第一方基座）"},
        "modules": [{"repo": r, "tier": 0} for r in repos],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _mk_repo(root: Path, repo: str, *, agents: str | None = None,
             readme: str | None = None, gomod: str | None = None) -> None:
    """写一个仓的测试夹具。

    **没传的文件会被删掉**——否则上一个用例留下的文件会漏进下一个用例
    （自测自己造出的假阳性/假阴性都是自测的 bug）。
    """
    d = root / repo
    d.mkdir(parents=True, exist_ok=True)
    for name, body in (("AGENTS.md", agents), ("README.md", readme),
                        ("go.mod", gomod)):
        p = d / name
        if body is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(body, encoding="utf-8")


_GOMOD_NONE = """module github.com/oagentkit/alpha

go 1.24.0
"""


def _mk_leaf(root: Path, name: str) -> None:
    """造一个「没有矩阵内依赖」的叶子仓。

    自测矩阵里除了被检对象还有别的仓；不把它们造出来，
    它们会因「读不到 go.mod」被判黄，自测就被自己的夹具噪声带偏。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(
        f"module github.com/oagentkit/{name}\n\ngo 1.24.0\n", encoding="utf-8")
    for doc in ("AGENTS.md", "README.md"):
        (d / doc).write_text(f"# {name}\n\n本仓没有矩阵内依赖。\n", encoding="utf-8")

_GOMOD_ONE = """module github.com/oagentkit/alpha

go 1.24.0

require github.com/oagentkit/beta v0.1.0
"""

_GOMOD_ONE_INDIRECT = """module github.com/oagentkit/alpha

go 1.24.0

require github.com/oagentkit/beta v0.1.0

require github.com/oagentkit/gamma v0.1.0 // indirect
"""


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    def reds(rep) -> set[str]:
        return {f.subject for f in rep.reds}

    def yellows(rep) -> set[str]:
        return {f.subject for f in rep.yellows}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        matrix = _mk_matrix(root, ["alpha", "beta", "gamma"])
        _mk_leaf(root, "beta")
        _mk_leaf(root, "gamma")

        # ① 一致样本：清单 == go.mod 直接依赖 ⇒ 绿
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("一致样本不判红", reds(rep), set())
        check("一致样本不判黄", yellows(rep), set())

        # ② 反证 A：清单多列一个依赖 ⇒ 判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`、`gamma`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`、`gamma`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("多列一个依赖 → 判红", "alpha" in reds(rep), True)

        # ③ 反证 B：清单漏列一个依赖 ⇒ 判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod="""module github.com/oagentkit/alpha

go 1.24.0

require (
	github.com/oagentkit/beta v0.1.0
	github.com/oagentkit/gamma v0.1.0
)
""")
        rep = judge(root, matrix)
        check("漏列一个依赖 → 判红", "alpha" in reds(rep), True)

        # ④ 反证 C：`// indirect` 的不算本仓依赖 ⇒ 不判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod=_GOMOD_ONE_INDIRECT)
        rep = judge(root, matrix)
        check("indirect 不算依赖 → 不判红", "alpha" in reds(rep), False)
        check("indirect 不算依赖 → 不判黄", "alpha" in yellows(rep), False)

        # ⑤ 反证 D：同仓两个第一入口不一致 ⇒ 只有错的那个判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`、`gamma`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("两个第一入口不一致 → 判红", "alpha" in reds(rep), True)
        msgs = [f.message for f in rep.reds]
        check("红的是 README 那条", any("README.md" in m for m in msgs), True)
        check("AGENTS.md 那条不红", any("AGENTS.md" in m for m in msgs), False)

        # ⑥ 反证 E：尾部括注里的**包路径**不是仓名 ⇒ 不算进清单
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`（用 `beta/pkgx` 补数据）。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`（用 `beta/pkgx` 补数据）。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("包路径不算仓名 → 不判红", "alpha" in reds(rep), False)
        check("包路径不算仓名 → 不判黄", "alpha" in yellows(rep), False)

        # ⑦ 反证 F：有依赖但两个第一入口都没声明 ⇒ 判黄
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n本仓没有任何依赖清单。\n",
                 readme="# alpha\n\n本仓没有任何依赖清单。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("有依赖没声明 → 判黄", "alpha" in yellows(rep), True)
        check("有依赖没声明 → 不判红", "alpha" in reds(rep), False)

        # ⑧ 反证 G：只有一个入口声明 ⇒ 另一个判黄（覆盖缺口）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n本仓没有写依赖清单。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("只有一个入口声明 → 判黄", "alpha" in yellows(rep), True)
        check("只有一个入口声明 → 不判红", "alpha" in reds(rep), False)

        # ⑨ 反证 H：go.mod 缺失 ⇒ 判黄（判不了 ≠ 没问题）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod=None)
        (root / "alpha" / "go.mod").unlink(missing_ok=True)
        rep = judge(root, matrix)
        check("缺 go.mod → 判黄", "alpha" in yellows(rep), True)
        check("缺 go.mod → 不判红", "alpha" in reds(rep), False)

        # ⑩ 反证 I：声明行认不出仓名（没加反引号）⇒ 判黄
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：beta。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：beta。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("认不出仓名 → 判黄", "alpha" in yellows(rep), True)
        check("认不出仓名 → 不判红", "alpha" in reds(rep), False)

        # ⑪ 反证 J：tier 0 的否定句不是声明 ⇒ 不判
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 依赖严格自底向上。本仓是 tier 0，"
                        "任何矩阵内模块都不得被本仓依赖。\n",
                 readme="# alpha\n\n- 依赖严格自底向上。本仓是 tier 0，"
                        "任何矩阵内模块都不得被本仓依赖。\n",
                 gomod=_GOMOD_NONE)
        rep = judge(root, matrix)
        check("否定句不判黄", "alpha" in yellows(rep), False)
        check("否定句不判红", "alpha" in reds(rep), False)

        # ⑫ 反证 K：显式空声明 + 确实无依赖 ⇒ 绿
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：无。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：无。\n",
                 gomod=_GOMOD_NONE)
        rep = judge(root, matrix)
        check("显式空声明 → 不判红", "alpha" in reds(rep), False)
        check("显式空声明 → 不判黄", "alpha" in yellows(rep), False)

        # ⑬ 反证 L：全文路径写法与裸名等价 ⇒ 绿
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`github.com/oagentkit/beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`github.com/oagentkit/beta`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("全文路径写法 → 不判红", "alpha" in reds(rep), False)
        check("全文路径写法 → 不判黄", "alpha" in yellows(rep), False)

        # ⑭ 反证 M：清单里有个认不出的名字（拼错）⇒ 判黄
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`、`btea`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`、`btea`。\n",
                 gomod=_GOMOD_ONE)
        rep = judge(root, matrix)
        check("拼错的仓名 → 判黄", "alpha" in yellows(rep), True)

        # ⑮ 反证 N：非矩阵依赖（oekit 基座）不参与判定 ⇒ 绿
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 readme="# alpha\n\n- 本仓依赖的矩阵内仓：`beta`。\n",
                 gomod="""module github.com/oagentkit/alpha

go 1.24.0

require (
	github.com/oagentkit/beta v0.1.0
	github.com/oekit/pkg v0.22.0
)
""")
        rep = judge(root, matrix)
        check("非矩阵依赖不参与 → 不判红", "alpha" in reds(rep), False)
        check("非矩阵依赖不参与 → 不判黄", "alpha" in yellows(rep), False)

    if fails:
        print("❌ verify_repo_dep_inventory 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_repo_dep_inventory 自测通过"
          "（一致→绿 / 多列→红 / 漏列→红 / indirect 不算→绿 / "
          "两个入口不一致→红 / 包路径不算仓名→绿 / 有依赖没声明→黄 / "
          "只有一个入口声明→黄 / 缺 go.mod→黄 / 认不出仓名→黄 / "
          "否定句不判 / 显式空声明→绿 / 全文路径写法→绿 / "
          "拼错仓名→黄 / 非矩阵依赖不参与→绿）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="各仓「本仓依赖的矩阵内仓」清单 vs go.mod 直接矩阵内依赖")
    ap.add_argument("--root", default=".", help="矩阵根目录（各仓的父目录）")
    ap.add_argument("--matrix", default=None,
                    help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--json-out", default=None, help="把 JSON 报告写到该文件")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄灯也判失败")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    matrix_path = Path(args.matrix) if args.matrix else root / DEFAULT_MATRIX
    rep = judge(root, matrix_path)
    code = emit(rep, fail_on_yellow=args.fail_on_yellow, as_json=args.json)
    if args.json_out:
        Path(args.json_out).write_text(rep.render_json(), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
