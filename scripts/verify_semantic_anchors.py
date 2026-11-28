#!/usr/bin/env python3
"""七词语义表契约锚校验——`docs/exec-semantics.md` §十 锚表 vs 真实代码。

## 守的是哪条缺口（oagentkit/agent#175，形态照 kernel error-semantics 惯例）

error-semantics.md 的机器面是「表 ↔ 代码双向判红」（apilifecheck）；本表
（执行语义七词，消费方视角对照）的可机判面是**契约测试锚**：锚表每行
= 仓 + 路径 + 测试函数，是「这一词有可执行判据」的索引。测试被删/改名
而锚表没改 ⇒ 表退化成装饰（指向不存在的判据）⇒ 红。**先改表再动测试**。

## 判定（词法近似，同 verify_api_lifecycle 的诚实声明纪律）

1. **表可解析**：§「契约测试锚」小节后必须解析出 ≥1 行锚；行形态
   `| 词 | 仓 | 路径 | 符号 |`——单元格错位/空行即判「表坏了」（红）。
2. **锚真实**：`仓/路径` 文件里必须出现 `func <符号>`（词法近似，不是
   go/ast——测试函数形态简单，近似足够；边界由 selftest 正反例钉）。
3. **读不到 = 判不了 = 红**（#79）：本地检出缺仓时回退
   `raw.githubusercontent.com`；两边都读不到必须报出来，不许跳过当通过。

## 数据面

- 本地优先：`<root>/<repo>/<path>`（矩阵克隆根 / 本机检出）；
- 回退 raw：`https://raw.githubusercontent.com/<org>/<repo>/main/<path>`，
  org 默认 `oagentkit`，`goagent-kernel` / `goagent-runtime` 在 `yi-nology`
  （与 matrix_paths「单仓可迁出默认 org」同款映射，只此两仓）。

用法:
    python3 scripts/verify_semantic_anchors.py --doc docs/exec-semantics.md --root .
    python3 scripts/verify_semantic_anchors.py --selftest
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 单仓迁出默认 org 的映射（matrix_paths 同款；当前只此两仓，新增时改这里）
ORG_OVERRIDES = {
    "goagent-kernel": "yi-nology",
    "goagent-runtime": "yi-nology",
}
DEFAULT_ORG = "oagentkit"
RAW_BASE = "https://raw.githubusercontent.com"

# 锚表行：| 词 | 仓 | 路径 | 符号 |（单元格内不允许裸 |；路径限 .go）
_ANCHOR_RE = re.compile(
    r"^\|\s*([A-Za-z]+)\s*\|\s*([A-Za-z0-9_-]+)\s*\|\s*([A-Za-z0-9_./-]+\.go)\s*"
    r"\|\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*$")
_SECTION_RE = re.compile(r"^##\s*十、契约测试锚", re.M)


class AnchorError(RuntimeError):
    """锚读不到（不是「锚不存在」）——#79：读不到 = 不知道 = 判不了。"""


def parse_anchors(doc_text: str) -> list[tuple[str, str, str, str]]:
    """解析 §十 锚表 → [(词, 仓, 路径, 符号)]；表缺失/零行 ⇒ AnchorError。"""
    m = _SECTION_RE.search(doc_text)
    if not m:
        raise AnchorError("文档缺「## 十、契约测试锚」小节（表结构被改名/删除）")
    section = doc_text[m.end():]
    # 小节止于下一个 ## 标题
    nxt = re.search(r"^##\s", section, re.M)
    if nxt:
        section = section[:nxt.start()]
    anchors = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        if set(line.replace("|", "").replace(" ", "")) <= set("-:") and line.strip("| "):
            continue  # 分隔行
        am = _ANCHOR_RE.match(line)
        if am:
            anchors.append(am.groups())
        elif "Test" in line or ".go" in line:
            # 看起来想当锚但形态不对（单元格错位/多列）⇒ 表坏了，判红
            raise AnchorError(f"锚表行形态不可解析（判不了 ≠ 通过）：{line[:120]}")
    if not anchors:
        raise AnchorError("锚表解析出 0 行——表空或形态全错")
    return anchors


def check_anchor(anchor: tuple[str, str, str, str], read) -> str | None:
    """单锚判定：read(repo, path) → 文件文本（读不到抛 AnchorError）。"""
    word, repo, path, sym = anchor
    try:
        text = read(repo, path)
    except AnchorError as e:
        return f"{word}: {repo}/{path} 读不到（判不了 ≠ 通过）——{e}"
    if f"func {sym}(" not in text:
        return (f"{word}: {repo}/{path} 里没有 `func {sym}(`——锚指向的测试"
                f"已删除/改名（先改 exec-semantics.md 锚表，再动测试）")
    return None


def make_reader(root: str | None):
    """本地优先、raw 回退的读取器；两边都读不到 ⇒ AnchorError（fail-closed）。

    raw 拉取带**有界重试**（3 次、退避 1s/2s）：本机到 raw.githubusercontent
    实测有瞬时超时（2026-11-28 首跑 21 条里 2 条 timeout），一次抖动不该变
    红灯——但重试耗尽仍判红（有界失败，不是无限等）。
    """
    import time

    root_p = Path(root) if root else None
    cache: dict[tuple[str, str], str] = {}  # 同 (仓,路径) 只读一次（多锚同文件不重复拉）

    def read(repo: str, path: str) -> str:
        key = (repo, path)
        if key in cache:
            return cache[key]
        if root_p is not None:
            local = root_p / repo / path
            if local.exists():
                cache[key] = local.read_text(encoding="utf-8", errors="replace")
                return cache[key]
        org = ORG_OVERRIDES.get(repo, DEFAULT_ORG)
        url = f"{RAW_BASE}/{org}/{repo}/main/{path}"
        last = None
        for attempt in range(3):
            if attempt:
                time.sleep(attempt)  # 1s / 2s 退避
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 固定域名
                    cache[key] = resp.read().decode("utf-8", errors="replace")
                    return cache[key]
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = e
        raise AnchorError(f"本地无检出且 raw 拉取失败（3 次重试耗尽，{url}）：{last}") from last

    return read


def run(doc_path: str, root: str | None, out=print) -> int:
    p = Path(doc_path)
    if not p.exists():
        out(f"❌ 文档不存在：{doc_path}")
        return 1
    try:
        anchors = parse_anchors(p.read_text(encoding="utf-8"))
    except AnchorError as e:
        out(f"❌ {e}")
        return 1
    read = make_reader(root)
    problems = []
    for a in anchors:
        err = check_anchor(a, read)
        if err:
            problems.append(err)
    if problems:
        out(f"发现 {len(problems)}/{len(anchors)} 条锚问题：")
        for x in problems:
            out(f"  ❌ {x}")
        return 1
    out(f"✓ {len(anchors)} 条契约锚全部在真实代码中（exec-semantics.md §十）")
    return 0


# ---------- selftest（selftest_all 经 AST 发现本函数，跑 --selftest） ----------

def selftest() -> int:
    """合成夹具（零网络）：解析 / 存在 / 缺失 / 读不到 / 坏表 五形态。"""
    fails: list[str] = []

    def case(name: str, cond: bool, detail: str = ""):
        print(f"  {'✓' if cond else '❌'} {name}" + (f"——{detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    good_doc = (
        "# t\n\n## 九、随便\n\n表\n\n"
        "## 十、契约测试锚（机器校验面）\n\n"
        "| 词 | 仓 | 路径 | 符号 |\n|---|---|---|---|\n"
        "| Run | agent | agentrun/agentrun_test.go | TestRunSimpleReply |\n"
        "| Turn | goagent-kernel | watchdog/turn_test.go | TestTurnBracketCleanPairing |\n"
        "\n## 十一、关联\n\n无关行\n")
    anchors = parse_anchors(good_doc)
    case("T1 解析两行锚", len(anchors) == 2 and anchors[0][3] == "TestRunSimpleReply",
         str(anchors))
    case("T1b 小节边界（§十一 不吞进来）", all(a[1] != "" for a in anchors))

    files = {"agentrun/agentrun_test.go": "func TestRunSimpleReply(t *testing.T) {}\n",
             "watchdog/turn_test.go": "func TestTurnBracketOpenLeak(t *testing.T) {}\n"}
    ok_read = lambda repo, path: files[path]  # noqa: E731
    err = check_anchor(anchors[0], ok_read)
    case("T2 存在锚通过", err is None, str(err))
    err = check_anchor(anchors[1], ok_read)
    case("T3 符号缺失判红（有文件无函数）", err is not None and "TestTurnBracketCleanPairing" in err,
         str(err))

    def missing_read(repo, path):
        raise AnchorError("本地无检出且 raw 拉取失败")
    err = check_anchor(anchors[0], missing_read)
    case("T4 读不到判红（判不了 ≠ 通过）", err is not None and "读不到" in err, str(err))

    bad_doc = good_doc.replace(
        "| Run | agent | agentrun/agentrun_test.go | TestRunSimpleReply |\n", "").replace(
        "| Turn | goagent-kernel | watchdog/turn_test.go | TestTurnBracketCleanPairing |\n", "")
    try:
        parse_anchors(bad_doc)
        case("T5 空锚表判红", False, "0 行未抛错")
    except AnchorError:
        case("T5 空锚表判红", True)

    bad_row_doc = good_doc.replace("| Turn | goagent-kernel | watchdog/turn_test.go | TestTurnBracketCleanPairing |",
                                   "| Turn | goagent-kernel | watchdog/turn_test.go |")  # 少一列
    try:
        parse_anchors(bad_row_doc)
        case("T6 坏行判红", False, "缺列未抛错")
    except AnchorError:
        case("T6 坏行判红", True)

    try:
        parse_anchors("# 没有小节\n")
        case("T7 缺小节判红", False, "未抛错")
    except AnchorError:
        case("T7 缺小节判红", True)

    # 真实文档冒烟（若检出在旁）：只验解析，不验锚（锚验交给真跑）
    real = Path(__file__).resolve().parent.parent / "docs" / "exec-semantics.md"
    if real.exists():
        try:
            n = len(parse_anchors(real.read_text(encoding="utf-8")))
            case(f"T8 真文档解析出 {n} 行锚", n >= 10)
        except AnchorError as e:
            case("T8 真文档解析", False, str(e))

    if fails:
        print(f"❌ {len(fails)} 个用例失败：{fails}")
        return 1
    print("✓ verify_semantic_anchors selftest 全过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="七词语义表契约锚校验（agent#175）")
    ap.add_argument("--doc", default="docs/exec-semantics.md")
    ap.add_argument("--root", default=None,
                    help="含各仓检出的根目录（本地优先；缺仓回退 raw）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return run(args.doc, args.root)


if __name__ == "__main__":
    sys.exit(main())
