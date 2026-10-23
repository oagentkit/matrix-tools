#!/usr/bin/env python3
"""各仓「依赖零性红线」声明 vs 该仓 `go.mod` 的实际 require 段。

## 守的不变量

**仓级文档里声明的依赖零性红线，必须与该仓 `go.mod` 的实际 require 段相容。**

`AGENTS.md` / `README.md` 是仓级的第一入口（人读 + agent 读）。文档写一条
「本仓零依赖」，而 `go.mod` 里明明有一条 require —— 读文档的人/agent 会按文档
（而不是按仓库事实）做决定。

## 为什么这条缺口此前没人守

实测 2026-10-24：`guardx/AGENTS.md` 写着

- 第 13 行：**本仓是 1 个 Go module、1 条版本线、零外部依赖。**
- 第 42 行：**本仓不得有外部依赖**（`require` 段必须为空）。
- 第 44 行：**新增外部依赖前先确认 `oekit/pkg` 里没有。**

而 `guardx/go.mod` 实测是 `require github.com/oekit/pkg v0.22.1` ⇒ **第 42 行是假的**；
第 44 行又把 `oekit/pkg` 当成一个「外部依赖」⇒ **同一份文件里两条指令互斥**，
无论怎么解读「外部依赖」都自相矛盾。

**这不是「忘了改」，是「一直在改、文档一直没跟」**（`git log -- go.mod`）：
2026-10-19 22:48 `3c0d845`（audit/bashguard 拆出嵌套 module）之后 require 段**为空**，
文档那一刻**是真的**；23:59 `2b628b1` 首次引入 `require github.com/oekit/pkg v0.16.0`
⇒ 文档从这一刻起变假；之后 4 次**有意的**地板抬升（v0.18.0 → v0.20.0 → v0.22.0 →
v0.22.1，每次一个独立 PR）都没回改它。

而矩阵自己的权威口径站在文档这边：`modules.json` 的 tier 0 定义是
「零**重量级第三方**地板（纯 stdlib / **第一方基座**）」，`verify_dep_floor.py`
的基座白名单也含 `github.com/oekit/pkg` ⇒ 第一方基座依赖是**允许**的。
真正该守的是「**零第三方依赖**」，写成了「零外部依赖 / require 段必须为空」。

三个本该抓住它的机制逐个失效：

1. `verify_repo_docs.py` **在 CI 里跑**，但它是**报告器**（`continue-on-error`，
   恒不失败）——它比的是整文件 diff，而 13 个仓的 README/AGENTS.md 都有大量
   手写段落 ⇒ 噪音太大 ⇒ 恒红 ⇒ 与「红了就加豁免」同命。
2. `gen_docs.py` 的合并模型要求目标文件带生成区标记，而这 13 个仓的
   README/AGENTS.md **一个都没有** ⇒ 生成器一律跳过 ⇒ 没有任何自动路径能更新它们。
3. `verify_repo_tiers.py`（2026-10-24 新增）只判**层位数字与层名**，不判依赖红线。

## 判据：只认「钉死了语义」的声明

「外部依赖」在矩阵里有两个读法：「一条 require 都没有」与「没有第三方依赖」
——`oekit/*`（姊妹矩阵的第一方基座）算哪一种？文档不钉死 ⇒ **机器核对不了**
（硬规则 3：判不了 ≠ 没问题）。

| 文档里的声明 | 判据 | 判定 |
|---|---|---|
| 钉死「`require` 段必须为空」/「`go list -m all` 只有自己一行」 | require 段（direct + indirect）必须为空 | 否则**红** |
| 钉死「零第三方依赖」/「不得有第三方依赖」 | require 段里不得有第三方 | 否则**红** |
| 只写「零外部依赖」/「无外部依赖」（**没钉死语义**） | 核对不了 | **黄** |
| `go.mod` 读不到 | 取数失败 | **黄** |
| 文档里没有任何零性声明 | 没有可判的声明 | 绿（不是「不合格」） |

**第三方**的定义：不属于 `github.com/oagentkit/*`（矩阵内模块）、
也不属于 `github.com/oekit/*`（第一方基座）的依赖。
「第三方」是矩阵自己的既有词汇（`obs/README.md` 的「外部依赖地板」一节、
`modules.json` 的 tier 0 定义、`verify_dep_floor.py` 的基座白名单都这么用）。

### 两个 require 集合口径一致

「`require` 段必须为空」与「零第三方依赖」都看**整个 require 段**
（direct + indirect），不只看直接依赖。理由就是文档自己给的理由：
「import 某个包会把**整个模块的 require** 拉进消费方的模块图（Go 按 module
结算依赖面，不按 package）」——间接依赖同样进消费方的模块图。

## 判据边界（与 `verify_repo_tiers.py` 同款）

**只判本仓自述，不判跨仓提及**：一句话里若先出现了**别的**矩阵仓名，
那是在谈论别的仓（例：guardx/README.md 的「`textx`/`runtime` 是零第三方依赖底座」
是在说 textx 与 runtime），不对本仓判。
同理，「不属于零第三方依赖底座」这类**否定句**也不判。

⚠️ 本守卫**不做语义否定分析**：一句话里若既有本仓自述又有跨仓提及，
按「先出现者」处理。仓名匹配用 ASCII 边界（`rag` / `llm` / `agent` 这类短名
在中文里会撞词，必须按词边界匹配）。

## 用法

    python3 scripts/verify_repo_dep_claims.py --root .            # 校验（CI 口径）
    python3 scripts/verify_repo_dep_claims.py --selftest          # 离线自测（带反证样本）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge_report import Report, emit  # noqa: E402
from matrix_facts import parse_go_mod  # noqa: E402

DEFAULT_MATRIX = "matrix-tools/modules.json"

# 仓级第一入口的两个文件（README 与 AGENTS 都在其列——实测这类声明
# 大多写在 README 的「外部依赖地板」一节，AGENTS.md 的「分层与依赖」一节）
DOC_FILES = ("AGENTS.md", "README.md")

# 第一方：矩阵内模块
MATRIX_PREFIX = "github.com/oagentkit/"
# 第一方基座：姊妹矩阵 oekit（纯 stdlib 邻接的基座仓）
BASE_PREFIX = "github.com/oekit/"

# ── 类型 A：钉死了「require 段为空」 ──
#
# 「`require` 段必须为空」——原文里 require 可能带反引号、段前可能有空格；
# 「`go list -m all` 只有自己一行」——同一句话的另一种写法。
EMPTY_CLAIM_RE = re.compile(
    r"require`?\s*段\s*(?:必须)?为空"
    r"|`?go list -m all`?\s*只有自己一行"
)

# ── 类型 B：钉死了「不得有第三方依赖」 ──
#
# ⚠️ 只认这几种**完整**说法。「新增第三方依赖前先确认……」那种是**要求**
# 而不是**状态声明**，不是本守卫要核对的声明。
THIRD_PARTY_CLAIM_RE = re.compile(
    r"零第三方依赖|无第三方依赖|不得有第三方依赖|不得引入第三方依赖"
)

# ── 类型 C：没钉死语义的「零外部依赖」 ──
#
# 「外部依赖」可以指「一条 require 都没有」，也可以指「没有第三方依赖」——
# `oekit/*`（第一方基座）算哪一种，文档没说 ⇒ 核对不了 ⇒ 判黄。
AMBIGUOUS_CLAIM_RE = re.compile(
    r"零外部依赖|无外部依赖|没有外部依赖|外部依赖必须为空"
    r"|不得有外部依赖|不得引入外部依赖"
)

# 否定句前缀（「不属于零第三方依赖底座」这类不是在声明本仓的状态）
NEGATION_RE = re.compile(r"不属于|不是|并非|不再是")


@dataclass
class Claim:
    """文档里认出的一处依赖零性声明。"""

    path: str          # 相对仓根的路径，如 "AGENTS.md"
    line: int          # 行号（1 起）
    kind: str          # "empty" | "third_party" | "ambiguous"
    text: str          # 命中的那一句（截断）

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


def _other_repo_names(repos: list[str], repo: str) -> list[str]:
    """别的矩阵仓名（用于认「跨仓提及」）。

    短名（`rag` / `llm` / `agent` / `tools`）在中文与英文里都容易撞词
    （`storage` / `fragment` / `agentkit` / `llms.txt`），必须按 ASCII 词边界匹配。
    """
    out = []
    for other in repos:
        if other == repo:
            continue
        out.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(other)}(?![A-Za-z0-9])"))
    return out


def parse_claims(repo: str, docs: dict[str, str], repos: list[str]) -> list[Claim]:
    """从仓级文档里认出本仓自述的依赖零性声明。

    `docs` = {相对路径: 文本}。跨仓提及与否定句**不判**（见「判据边界」）。
    """
    others = _other_repo_names(repos, repo)
    claims: list[Claim] = []
    for path, text in docs.items():
        for idx, line in enumerate(text.splitlines(), start=1):
            # 命中位置 → 决定这一行是「本仓自述」还是「跨仓提及」
            hits: list[tuple[int, str]] = []
            for m in EMPTY_CLAIM_RE.finditer(line):
                hits.append((m.start(), "empty"))
            for m in THIRD_PARTY_CLAIM_RE.finditer(line):
                hits.append((m.start(), "third_party"))
            for m in AMBIGUOUS_CLAIM_RE.finditer(line):
                hits.append((m.start(), "ambiguous"))
            if not hits:
                continue
            hits.sort()
            # 同一行既有钉死的说法又有松的说法 ⇒ **钉死的赢**
            # （「本仓不得有外部依赖（require 段必须为空）」⇒ 按「require 段为空」判）
            pinned = [h for h in hits if h[1] != "ambiguous"]
            kind = pinned[0][1] if pinned else "ambiguous"
            start = (pinned[0] if pinned else hits[0])[0]

            # 否定句 ⇒ 不判
            window = line[max(0, start - 8):start]
            if NEGATION_RE.search(window):
                continue
            # 跨仓提及 ⇒ 不判（别的仓名出现在这句声明**之前**）
            if any(o.search(line[:start]) for o in others):
                continue

            claims.append(Claim(path=path, line=idx, kind=kind,
                                text=line.strip()[:120]))
    return claims


def third_party(requires: set[str]) -> list[str]:
    """require 段里的第三方依赖（不属于矩阵模块、不属于第一方基座）。"""
    out = []
    for item in requires:
        path = item.split(" ", 1)[0]
        if path.startswith(MATRIX_PREFIX) or path.startswith(BASE_PREFIX):
            continue
        out.append(item)
    return sorted(out)


def scan_repo(root: Path, repo: str, repos: list[str]) -> RepoScan:
    """扫一个仓：认声明 + 取 go.mod 事实。"""
    scan = RepoScan(repo=repo)
    docs: dict[str, str] = {}
    for name in DOC_FILES:
        p = root / repo / name
        if not p.is_file():
            continue
        try:
            docs[name] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    scan.claims = parse_claims(repo, docs, repos)

    gomod = root / repo / "go.mod"
    if not gomod.is_file():
        scan.go_mod_missing = True
    else:
        try:
            scan.requires = set(parse_go_mod(gomod.read_text(encoding="utf-8"))["requires"])
        except OSError:
            scan.go_mod_unreadable = True
    return scan


@dataclass
class RepoScan:
    """一个仓扫出来的声明 + go.mod 事实。"""

    repo: str
    claims: list[Claim] = field(default_factory=list)
    requires: set[str] = field(default_factory=set)
    go_mod_missing: bool = False
    go_mod_unreadable: bool = False


def judge(root: Path, matrix_path: Path) -> Report:
    """判定各仓的依赖零性声明与 go.mod 是否相容。"""
    rep = Report(title="各仓依赖零性红线声明")
    if not matrix_path.is_file():
        rep.red("modules.json", f"找不到矩阵数据源：{matrix_path}",
                "路径相对矩阵根目录；确认 CI 布局里有 matrix-tools/modules.json")
        return rep
    try:
        data = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rep.red("modules.json", f"读不了矩阵数据源 {matrix_path}：{exc}")
        return rep

    modules = data.get("modules") or []
    if not modules:
        rep.red("modules.json", "数据源里没有 modules")
        return rep

    repos = [str(m.get("repo", "")).strip() for m in modules if m.get("repo")]
    clean = 0
    claimed = 0
    yellowed = 0

    for repo in repos:
        scan = scan_repo(root, repo, repos)
        if not scan.claims:
            # 没有任何零性声明 ⇒ 没有可判的声明（**不是**「不合格」）
            continue
        claimed += 1

        if scan.go_mod_missing:
            rep.yellow(repo, "该仓有依赖零性声明，但读不到 go.mod —— 核对不了",
                       "确认 CI 布局里有该仓的克隆（含 go.mod）")
            yellowed += 1
            continue
        if scan.go_mod_unreadable:
            rep.yellow(repo, "该仓有依赖零性声明，但 go.mod 读不出来 —— 核对不了",
                       "确认 go.mod 可读")
            yellowed += 1
            continue

        requires = sorted(scan.requires)
        tp = third_party(scan.requires)
        repo_yellow = False
        for c in scan.claims:
            if c.kind == "empty":
                if requires:
                    rep.red(repo,
                            f"{c.where} 声明「require 段为空」，"
                            f"但 go.mod 里有 {len(requires)} 条 require："
                            + "、".join(requires),
                            "矩阵的 tier 0 定义允许第一方基座（github.com/oekit/*）；"
                            "改成「零第三方依赖」并列出允许的基座依赖"
                            "（样板：obs/README.md 的「外部依赖地板」一节）")
            elif c.kind == "third_party":
                if tp:
                    rep.red(repo,
                            f"{c.where} 声明「零第三方依赖」，"
                            f"但 go.mod 里有 {len(tp)} 条第三方直接/间接依赖："
                            + "、".join(tp),
                            "第三方依赖要么移出本仓（另起仓），要么改掉这句声明")
            else:  # ambiguous
                repo_yellow = True
                rep.yellow(repo,
                           f"{c.where} 写「零外部依赖」——语义没钉死，核对不了："
                           "它指「一条 require 都没有」还是「没有第三方依赖」？"
                           "（第一方基座 github.com/oekit/* 算哪一种？）",
                           "改成「零第三方依赖」并列出允许的依赖"
                           "（样板：obs/README.md 的「外部依赖地板」一节）")
        if repo_yellow:
            yellowed += 1
        else:
            clean += 1

    rep.note(f"逐仓比对 {len(repos)} 个模块仓的依赖零性声明 vs go.mod 实际 require 段；"
             f"有声明的仓 {claimed} 个（核对通过 {clean} 个 / 判不了 {yellowed} 个）")
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


_GOMOD_BASE_ONLY = """module github.com/oagentkit/alpha

go 1.24.0

require github.com/oekit/pkg v0.22.1
"""

_GOMOD_THIRD_PARTY = """module github.com/oagentkit/alpha

go 1.24.0

require github.com/oekit/pkg v0.22.1

require github.com/cloudwego/eino v0.3.0
"""

_GOMOD_EMPTY = """module github.com/oagentkit/alpha

go 1.24.0
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
        matrix = _mk_matrix(root, ["alpha", "beta"])

        # ① 一致样本：钉死的「零第三方依赖」+ 只依赖基座 ⇒ 无红无黄
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有第三方依赖**（`require` 段只允许第一方基座）。\n",
                 readme="# alpha\n\n## 外部依赖地板\n\n- **零第三方依赖。** 只依赖 `github.com/oekit/pkg`。\n",
                 gomod=_GOMOD_BASE_ONLY)
        _mk_repo(root, "beta", agents="# beta\n\n没有依赖零性声明。\n", gomod=_GOMOD_BASE_ONLY)
        rep = judge(root, matrix)
        check("一致样本不判红", reds(rep), set())
        check("一致样本不判黄", yellows(rep), set())

        # ② 反证 A：钉死的「require 段必须为空」但 go.mod 有 require ⇒ 判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有外部依赖**（`require` 段必须为空）。\n",
                 gomod=_GOMOD_BASE_ONLY)
        rep = judge(root, matrix)
        check("require 段为空但实际有 require → 判红", "alpha" in reds(rep), True)

        # ③ 反证 B：「零第三方依赖」但 go.mod 有第三方 ⇒ 判红
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有第三方依赖**。\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("零第三方依赖但实际有第三方 → 判红", "alpha" in reds(rep), True)

        # ④ 反证 C：「零外部依赖」（没钉死语义）⇒ 判黄（不是红，也不是绿）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 零外部依赖。\n",
                 gomod=_GOMOD_BASE_ONLY)
        rep = judge(root, matrix)
        check("零外部依赖 → 判黄", "alpha" in yellows(rep), True)
        check("零外部依赖 → 不判红", "alpha" in reds(rep), False)

        # ⑤ 反证 D：同一行既有钉死的说法又有松的说法 ⇒ **钉死的赢**（判红，不是黄）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有外部依赖**（`require` 段必须为空）。\n",
                 gomod=_GOMOD_BASE_ONLY)
        rep = judge(root, matrix)
        check("钉死说法优先 → 判红", "alpha" in reds(rep), True)
        check("钉死说法优先 → 不判黄", "alpha" in yellows(rep), False)

        # ⑥ 反证 E：间接第三方依赖**也**判红（require 段口径含 indirect）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有第三方依赖**。\n",
                 gomod="""module github.com/oagentkit/alpha

go 1.24.0

require github.com/oekit/pkg v0.22.1

require github.com/cloudwego/eino v0.3.0 // indirect
""")
        rep = judge(root, matrix)
        check("间接第三方也判红", "alpha" in reds(rep), True)

        # ⑦ 反证 F：`go list -m all` 只有自己一行 ⇒ 同样按「require 段为空」判
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n迁出后主模块 `go list -m all` 只有自己一行。\n",
                 gomod=_GOMOD_BASE_ONLY)
        rep = judge(root, matrix)
        check("go list -m all 只有自己一行 → 判红", "alpha" in reds(rep), True)
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n迁出后主模块 `go list -m all` 只有自己一行。\n",
                 gomod=_GOMOD_EMPTY)
        rep = judge(root, matrix)
        check("go list -m all 只有自己一行（真的为空）→ 不判红", "alpha" in reds(rep), False)

        # ⑧ 反证 G：go.mod 缺失 ⇒ 判黄（判不了 ≠ 没问题）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **本仓不得有第三方依赖**。\n",
                 gomod=None)
        (root / "alpha" / "go.mod").unlink(missing_ok=True)
        rep = judge(root, matrix)
        check("缺 go.mod → 判黄", "alpha" in yellows(rep), True)
        check("缺 go.mod → 不判红", "alpha" in reds(rep), False)

        # ⑨ 跨仓提及**不误报**：「beta 是零第三方依赖底座」不是在声明 alpha
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- beta 是零第三方依赖底座，alpha 与它同级。\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("跨仓提及不判红", "alpha" in reds(rep), False)
        check("跨仓提及不判黄", "alpha" in yellows(rep), False)

        # ⑩ 否定句**不误报**：「不属于零第三方依赖底座」不是在声明本仓
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- 迁出的包带第三方依赖，不属于零第三方依赖底座。\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("否定句不判红", "alpha" in reds(rep), False)
        check("否定句不判黄", "alpha" in yellows(rep), False)

        # ⑪ 口语「零依赖」不算声明（说的是**包**，不是本仓的 require 段）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n**新包默认进本仓**（零依赖）。\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("口语「零依赖」不判红", "alpha" in reds(rep), False)
        check("口语「零依赖」不判黄", "alpha" in yellows(rep), False)

        # ⑫ 「新增第三方依赖前先确认……」是**要求**不是**状态声明** ⇒ 不判
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n- **新增第三方依赖前先确认 `oekit/pkg` 里没有。**\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("「新增第三方依赖…」不判红", "alpha" in reds(rep), False)

        # ⑬ 没有零性声明 ⇒ 无判定（不是黄）
        _mk_repo(root, "alpha",
                 agents="# alpha\n\n没有任何依赖零性声明。\n",
                 gomod=_GOMOD_THIRD_PARTY)
        rep = judge(root, matrix)
        check("没有声明 → 不判红", "alpha" in reds(rep), False)
        check("没有声明 → 不判黄", "alpha" in yellows(rep), False)

    if fails:
        print("❌ verify_repo_dep_claims 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ verify_repo_dep_claims 自测通过"
          "（一致→绿 / require 段为空但非空→红 / 零第三方但有第三方→红 / "
          "零外部依赖→黄 / 钉死说法优先→红 / 间接第三方→红 / "
          "go list -m all 只有自己一行→红 / 缺 go.mod→黄 / "
          "跨仓提及不误报 / 否定句不误报 / 口语「零依赖」不误报 / "
          "「新增第三方依赖…」不判 / 无声明→绿）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="各仓依赖零性声明 vs go.mod 实际 require 段")
    ap.add_argument("--root", default=".", help="矩阵根目录（各仓的父目录）")
    ap.add_argument("--matrix", default=None,
                    help="modules.json 路径（默认 <root>/matrix-tools/modules.json）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--json-out", default=None, help="把 JSON 报告写到该文件")
    ap.add_argument("--fail-on-yellow", action="store_true", help="黄档也判失败")
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
