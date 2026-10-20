#!/usr/bin/env python3
"""从 git 历史生成 CHANGELOG.md。

## 为什么要有这个

CONTRIBUTING 的「矩阵卫生」要求各仓 `CHANGELOG.md` 在场且不过期，但 13 个模块仓
**一个都没有**（AGENTS.md 倒是都有，所以那条要求只塌了一半）。

手写 13 份 changelog 不现实：每个仓十几个版本，人工整理既慢又会漏。
但仓库的 **commit message 已经很规范**——全矩阵统一用 conventional commits
前缀（`feat(x):` / `fix(x):` / `chore(deps):` / `refactor!(x):` …），
而且每个版本都是 annotated tag，tag 就是天然的版本边界。

所以：本脚本按 tag 切段，每段取该段内的 commit，按 conventional commit 分类渲染，
格式对齐 `oekit/pkg` 的既有写法（带日期的 `## [vX.Y.Z] — YYYY-MM-DD` 段落 +
`### feat(scope)：标题` 小标题 + 要点列表）。

## 边界与已知限制

- **只覆盖 tag 之后的 commit**：没有 tag 的提交不会被收录。这是刻意的——
  changelog 记的是「发出去的东西」。
- **同 commit 多个 tag**：某些仓连着两个 tag 指向同一 commit（改版本号重打），
  这时后一个 tag 的区间为空，渲染成「无内容变更」，不编造条目。
- **不解析 issue 链接**：commit 里的 `(#13)` 原样带出，不去猜 issue URL。
- **首次生成会覆盖已有文件**：用 `--out` 指定路径；`--check` 只比对不写。

用法:
    gen_changelogs.py --repo agent --root /path/to/oagentkit
    gen_changelogs.py --repo agent --root ... --next-version v0.6.1   # 预写待发版段
    gen_changelogs.py --repo agent --root ... --out /tmp/CHANGELOG.md
    gen_changelogs.py --repo agent --root ... --check   # 报告是否过期，不写

退出码: 0 = 成功 / --check 通过；1 = --check 发现过期或出错。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# conventional commit：type(scope)!: subject
# ⚠️ 叹号位置有两种写法，都要认：
#   - 标准：feat(api)!: ...      （叹号在 scope 之后）
#   - 本矩阵实际在用：refactor!(agent): ...  （叹号在 scope 之前）
# 只认标准写法时，仓库里大量 refactor!/feat! 提交会退化成裸 bullet——
# 看着像「没分类」，实质是解析失败。
CC_RE = re.compile(
    r"^(?P<type>[a-z]+)(?P<bang1>!)?(?:\((?P<scope>[^)]*)\))?(?P<bang2>!)?:\s*(?P<subject>.+)$"
)

# 本项目的派单流程要求 PR 标题带 `[round-N] ` 前缀（round 账本靠 PR 标题记账），
# squash merge 之后这个前缀会留在 commit subject 的**最前面**。
#
# ⚠️ 不剥它会导致**整段 commit body 被静默丢弃**（实测 2026-10-20）：
#   1. subject 带前缀 → CC_RE 不匹配 → render_entry 归入 "other" 且 body 置空
#      （第三个返回值就是 body，直接写死成 []）
#   2. "other" 桶的渲染分支只取标题行，body 不落盘
#   3. 后果：发布说明里的 API 说明、代码块、判据锚点全部消失，
#      而**没有任何门禁能发现**——门禁比的正是丢 body 之后的结果。
#
# 所以必须先剥前缀再解析 conventional commit。
# 只认**行首**的 `[round-<数字>] `；subject 内部出现的 `[round-2]` 不是前缀。
ROUND_PREFIX_RE = re.compile(r"^\[round-(\d+)\]\s+")

# markdown 列表符号：`- ` / `* ` / `+ ` / `1. `
MD_LIST_PREFIX_RE = re.compile(r"^(?:[*+-]|\d+[.)])\s+")


def strip_round_prefix(subject: str) -> str:
    """剥掉 subject **行首**的 `[round-N] ` 前缀。

    只剥一次、只剥行首：subject 内部的 `[round-2]`（如 `feat(x): [round-2] foo`）
    不是前缀，剥了会改写作者本意。
    """
    return ROUND_PREFIX_RE.sub("", subject, count=1)


# 哪些类型值得单独起小标题（其余压成一行要点）
HEADING_TYPES = {"feat", "fix", "perf", "refactor", "security"}

# 类型 → 中文小标题
TYPE_LABEL = {
    "feat": "新增",
    "fix": "修复",
    "perf": "性能",
    "refactor": "重构",
    "docs": "文档",
    "test": "测试",
    "build": "构建",
    "chore": "杂务",
    "ci": "CI",
    "style": "格式",
    "security": "安全",
    "revert": "回滚",
}

VERSION_ORDER_HINT = "v"


def run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def semver_key(tag: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", tag)
    if not m:
        return (0, 0, 0, 0, "")
    maj, mi, pa, pre = m.groups()
    return (int(maj), int(mi), int(pa), 0 if pre else 1, pre or "")


def tags_in_order(repo_dir: Path) -> list[str]:
    out = run(["git", "tag", "--list"], repo_dir)
    tags = [t.strip() for t in out.splitlines() if re.match(r"^v\d+\.\d+\.\d+", t.strip())]
    return sorted(tags, key=semver_key, reverse=True)


def tag_date(repo_dir: Path, tag: str) -> str:
    out = run(["git", "log", "-1", "--format=%cs", tag], repo_dir).strip()
    return out or "未知日期"


def head_date(repo_dir: Path) -> str:
    out = run(["git", "log", "-1", "--format=%cs", "HEAD"], repo_dir).strip()
    return out or "未知日期"


def commits_between(repo_dir: Path, prev: str | None, cur: str) -> list[dict]:
    rng = f"{prev}..{cur}" if prev else cur
    out = run(["git", "log", "--format=%H%x1f%s%x1f%b%x1e", rng], repo_dir)
    items = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        items.append({"sha": sha, "subject": subject, "body": body})
    return items


def is_changelog_only(repo_dir: Path, sha: str) -> bool:
    """该提交是否**只**改了 CHANGELOG.md。

    为什么要判这个：CHANGELOG.md 由本脚本生成，于是「补 CHANGELOG」这个提交
    本身会落进版本段里——而它对读者零信息量（发布说明里写「发布说明已更新」）。

    更要命的是它让「预写 → 提交 → 打 tag」流程**自相矛盾**（实测）：
      - 预写时 HEAD 还没有那个提交 → 段里没有它
      - 提交后再重生成 → 那个提交已在区间里 → 段里多出它
    两者不一致 → `.github` 的「各仓 CHANGELOG 未过期」门禁（按 tag 切段重生成
    后与提交版逐字节比对）永远红，且**越修越红**。

    所以版本段内排除「只改 CHANGELOG.md」的提交。判据用**实际改了哪些文件**
    （不看 subject 文案）——文案会变，文件不会。
    """
    files = run(["git", "show", "--pretty=", "--name-only", sha], repo_dir)
    names = [ln.strip() for ln in files.splitlines() if ln.strip()]
    return names == ["CHANGELOG.md"]


def is_merge_commit(repo_dir: Path, sha: str) -> bool:
    """该提交是否是 merge commit（两个以上父提交）。"""
    parents = run(["git", "log", "-1", "--format=%P", sha], repo_dir).split()
    return len(parents) > 1


def filter_changelog_only(repo_dir: Path, commits: list[dict]) -> list[dict]:
    """从版本段里剔除两类零信息量的提交：

    1. **只改 CHANGELOG.md** 的提交（见 is_changelog_only 注释）；
    2. **merge commit**——「Merge pull request #N from …」对读者零信息量，
       它引入的 PR 的各个 commit 本来就单列在段里了。

    剔除 merge commit 还有第二个作用：让「预写 → 提交 → 打 tag」流程与
    「走 PR 合并后再打 tag」两种打法**产出同一份 CHANGELOG**——
    走 PR 会多一个 merge commit 落进段里（实测差一行），
    而 tag 快照必须是「预写时看到的那份」，两者必须逐字节一致。
    """
    out = []
    for c in commits:
        if is_changelog_only(repo_dir, c["sha"]):
            continue
        if is_merge_commit(repo_dir, c["sha"]):
            continue
        out.append(c)
    return out


def dedent_body(body: str) -> list[str]:
    """去掉 git log --format=%b 给续行加的 4 空格缩进。

    不去的话，commit body 里的 markdown 表格会被缩成代码块——
    `| 熔断 | ... |` 变成 `    | 熔断 | ... |`，渲染出来完全不是表格。
    """
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return []
    # 只对「看起来是 git 续行缩进」的公共前缀生效：4 空格
    while lines and all(ln.startswith("    ") or not ln.strip() for ln in lines):
        lines = [ln[4:] if ln.startswith("    ") else ln for ln in lines]
    return lines


PR_SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")


def strip_pr_suffix(subject: str) -> str:
    """去掉 subject 末尾的 GitHub PR 号后缀「(#N)」。

    为什么必须去：GitHub 的 **squash merge** 会把 PR 内的 commit 合成一个，
    并把 subject 改写成「<PR 标题> (#N)」。于是：

      - 预写待发版段时（`--next-version`）看到的是 PR **head** 的 subject，
        没有「(#N)」；
      - 事后重生成时（按 tag 切段）看到的是 squash 后 commit 的 subject，
        带「(#N)」。

    两者不一致 ⇒ tag 快照里的 CHANGELOG 与 main 上的**逐字节不同**
    ⇒ `.github` 的「各仓 CHANGELOG 未过期」门禁永远红，且**每次发版必红**
    （实测 2026-10-20：agentkit v1.8.16 段标题与重生成结果差一个「(#25)」）。

    在**渲染层**统一去掉后缀 ⇒ 预写与事后重生成必然一致，
    与「用哪种合并策略」解耦。

    顺带的好处：PR 号对读发布说明的人零信息量（得去 GitHub 上查才知道是什么），
    去掉后标题更干净。
    """
    return PR_SUFFIX_RE.sub("", subject).strip()


def drop_redundant_body_lines(body: list[str], subject: str) -> list[str]:
    """丢掉 body 开头**连续多行**与 subject 重复的行。

    为什么需要：squash merge 时 commit body = PR 描述正文，而 PR 描述的**开头**
    通常是标题的 markdown 版（实测 agent：
    ```
    * chore(deps): 依赖地板升到「历史最新 + 0.1.0」
    chore(deps): 依赖地板升到「历史最新 + 0.1.0」
    ```
    两行都与 subject 重复——第一行带 markdown 列表符，第二行不带）。

    渲染时若原样保留，重生成结果会比提交版多出重复行 ⇒ 逐字节比对失败。

    判据是**等值**，不是词表：每行去掉 markdown 列表符号与 `(#N)` 后缀后，
    必须与「剥掉 round 前缀与 PR 号后缀的 subject」**逐字相等**才丢。
    不相等就保留——那可能是正文里的真实首行。

    ⚠️ 这条规则不会吞掉正文：实测 mcpkit v0.8.2 段的正文首行是
    `补一组**通用 MCP 客户端能力**……`，与 subject 不等，122 行完整保留。
    """
    out = list(body)
    while out:
        bare = MD_LIST_PREFIX_RE.sub("", out[0].strip(), count=1).strip()
        # 首行也可能带 PR 号后缀（squash 后 subject 有、首行跟着抄了也会有），
        # 比较前必须同样剥掉，否则永远判为「不重复」。
        bare = strip_pr_suffix(bare)
        if bare and bare == subject.strip():
            out.pop(0)
            continue
        break
    return out


def render_entry(c: dict) -> tuple[str, str, list[str]]:
    """→ (type, heading, body_lines)

    body **原样保留**，不逐行加 "- " 前缀——commit body 本身已是 markdown
    （列表、表格、代码块），再套一层 "- " 会把表格和代码块拆坏。

    ⚠️ subject 行首的 `[round-N] ` 前缀必须先剥：带前缀时 CC_RE 不匹配，
    commit 会降级成 "other" 且 **body 整段被丢弃**（见 ROUND_PREFIX_RE 注释）。
    """
    subject = strip_round_prefix(c["subject"])
    m = CC_RE.match(subject)
    if not m:
        return "other", strip_pr_suffix(subject), []
    ctype = m.group("type")
    scope = m.group("scope") or ""
    bang = "!" if (m.group("bang1") or m.group("bang2")) else ""
    subj = strip_pr_suffix(m.group("subject").strip())
    label = TYPE_LABEL.get(ctype, ctype)
    head = f"{label}({scope}){bang}：{subj}" if scope else f"{label}：{subj}"
    body = drop_redundant_body_lines(dedent_body(c["body"]),
                                      strip_pr_suffix(subject))
    return ctype, head, body


def render(repo_dir: Path, repo: str, next_version: str | None = None) -> str:
    # 区间必须**升序**计算：tag 区间的定义是「上一个更旧的 tag .. 这个 tag」。
    # 若从最新 tag 倒着算，prev=None 会退化成「该 tag 的全部可达历史」，
    # 于是最新版本段里塞进整个仓的初始提交，而次新版本段反而是空的。
    asc = sorted(tags_in_order(repo_dir), key=semver_key)
    segments: list[tuple[str, str | None, list[dict]]] = []
    prev = None
    for tag in asc:
        # 区间划分用**未过滤**的提交列表（tag 边界是发版事实，不能被过滤改写），
        # 只在**渲染段内容**时剔除「只改 CHANGELOG.md」的提交。
        segments.append((tag, prev, commits_between(repo_dir, prev, tag)))
        prev = tag
    # 输出按版本倒序（CHANGELOG 惯例：最新在上）
    segments.reverse()

    # 待发版段：把「上一个 tag 之后的提交」预写成下一个版本的版本段。
    # 没有它，「先提交 CHANGELOG 再打 tag」的打法下 tag 快照里永远没有自己那段
    # （生成器只认已存在的 tag → 鸡生蛋）。带上它，发版顺序就能改成
    #   gen_changelogs.py --next-version vX.Y.Z → 提交 → 打 tag vX.Y.Z
    # tag 快照里就有自己那段了（issue matrix-tools#17 的根治点）。
    if next_version:
        newest = asc[-1] if asc else None
        if newest is None or semver_key(next_version) > semver_key(newest):
            segments.insert(0, (next_version, newest,
                                commits_between(repo_dir, newest, "HEAD")))

    lines = [f"# {repo} CHANGELOG", "",
             "> 本文件由 `matrix-tools/scripts/gen_changelogs.py` 从 git 历史生成，"
             "按 tag 切段、取每段内的 conventional commit 渲染。", ""]
    for tag, prev, commits in segments:
        date = (head_date(repo_dir) if tag == next_version
                else tag_date(repo_dir, tag))
        lines.append(f"## [{tag}] — {date}")
        lines.append("")
        # 剔除「只改 CHANGELOG.md」的提交——它们对读者零信息量，
        # 且会让「预写 → 提交 → 打 tag」流程自相矛盾（见 is_changelog_only 注释）。
        commits = filter_changelog_only(repo_dir, commits)
        if not commits:
            lines.append(f"- 与 {prev} 相比无代码差异（版本号/打包变更）")
            lines.append("")
            continue
        groups: dict[str, list[dict]] = {}
        for c in commits:
            ctype, _, _ = render_entry(c)
            groups.setdefault(ctype, []).append(c)
        for ctype in sorted(groups, key=lambda t: (t not in HEADING_TYPES, t)):
            if ctype in HEADING_TYPES:
                for c in groups[ctype]:
                    _, head, body = render_entry(c)
                    lines.append(f"### {head}")
                    lines.append("")
                    if body:
                        lines.extend(body)
                    else:
                        lines.append(f"- {c['sha'][:7]}")
                    lines.append("")
            else:
                # ⚠️「压成一行要点」≠「丢弃正文」。
                # 实测 2026-10-20（agent 仓 v0.6.0 段）：chore(deps) 的 body 里是
                # 版本线重置说明与 go.sum 注意事项——**实质内容**，却因为 chore
                # 不在 HEADING_TYPES 而被整段丢弃。
                #
                # 而且**逐字节门禁根本看不出来**：产物也是丢 body 之后生成的，
                # 比对必然一致 ⇒ 门禁全绿而内容在丢。只有直接扫原始 git 历史的
                # 独立 oracle 才抓得到（见 oagentkit_changelog_coverage_oracle.py）。
                for c in groups[ctype]:
                    _, head, body = render_entry(c)
                    lines.append(f"- {head}")
                    if body:
                        lines.append("")
                        lines.extend(body)
                        lines.append("")
                lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# 提交版 CHANGELOG 里的版本段标题
VERSION_HEAD_RE = re.compile(r"^## \[(v\d+\.\d+\.\d+(?:-[^\]]+)?)\]", re.M)


def pending_versions_in(content: str, repo_dir: Path) -> list[str]:
    """提交版里「版本号比最新 tag 还新」的段 = 已预写但还没打 tag 的待发版段。

    为什么必须单独识别：发版流程是「预写 → 提交 → 打 tag」，而打 tag 发生在
    **发版时**。所以从预写提交到打 tag 之间，提交版**必然**比「按 tag 切段
    重生成」多一个待发版段。只按 tag 比对 ⇒ 这段窗口里门禁必然红，
    而红的不是真问题（实测 2026-10-20：agent / mcpkit 等 7 仓连续红，
    其中 agent 的 `## [v0.10.1] — 2026-10-20` 就是这种预写段，
    v0.10.1 的 tag 还没打）。
    """
    newest = tags_in_order(repo_dir)
    newest_key = semver_key(newest[0]) if newest else (0, 0, 0, 0, "")
    return [v for v in VERSION_HEAD_RE.findall(content)
            if semver_key(v) > newest_key]


def diagnose(cur: str, want: str) -> str:
    """给「已过期」报错补上指得到真正原因的定位信息。

    教训：CI 报错文案要指得到真正原因。「已过期」这四个字会把排查带偏——
    实测中真正的差异是「渲染器把 commit 降级成 other 丢了 body」，
    而「过期」完全指不到它。
    """
    import difflib

    notes: list[str] = []
    cur_v = VERSION_HEAD_RE.findall(cur)
    want_v = VERSION_HEAD_RE.findall(want)
    only_cur = sorted(set(cur_v) - set(want_v))
    only_want = sorted(set(want_v) - set(cur_v))
    if only_cur:
        notes.append("提交版多出的版本段: " + ", ".join(only_cur))
    if only_want:
        notes.append("重生成多出的版本段: " + ", ".join(only_want))
    diff = list(difflib.unified_diff(
        cur.splitlines(), want.splitlines(),
        fromfile="提交版", tofile="重生成", lineterm="", n=1))
    if diff:
        notes.append("差异（前 20 行）：")
        notes.extend("  " + d for d in diff[:20])
    return "\n".join(notes)


def check_against_history(repo_dir: Path, repo: str, cur: str) -> tuple[bool, str]:
    """比对提交版 CHANGELOG 与「按 git 历史重生成」的结果 → (是否一致, 说明)。

    两种比对：
      1. 按 tag 切段重生成；
      2. 若提交版含「已预写但还没打 tag」的待发版段，再带上该段比对一次。

    第 2 种是必需的，不是宽松化：打 tag 发生在发版时，预写到发版之间
    提交版必然多一个段。若只按 tag 比对，这段窗口里门禁必然红，
    且**红的原因与本 PR 无关**（实测它把 oagentkit/.github#15 卡了两天）。
    """
    want = render(repo_dir, repo)
    if cur == want:
        return True, ""
    pending = pending_versions_in(cur, repo_dir)
    if len(pending) == 1:
        want = render(repo_dir, repo, pending[0])
        if cur == want:
            return True, f"（含未打 tag 的待发版段 {pending[0]}）"
    return False, diagnose(cur, want)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True)
    return r.stdout


def selftest() -> int:
    """自测：证明「预写 → 提交 → 打 tag」流程产出**自洽**的 CHANGELOG。

    为什么必须自测：这个脚本的失败形态是「**看起来**生成了正确的文件，
    但与事后重生成的结果不一致」——不跑完整流程就发现不了，
    而后果是 `.github` 的「各仓 CHANGELOG 未过期」门禁永远红（且越修越红）。

    判据是**端到端**：在临时仓里跑完整流程，然后用同一个 render() 重新生成，
    要求与提交版**逐字节一致**。两个场景都要过：
      场景1：预写 → 提交 → 直接在那个提交上打 tag
      场景2：预写 → 走 PR（产生 merge commit）→ 在合并后打 tag
    """
    import shutil
    import tempfile

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: got={got!r} want={want!r}")

    tmp = Path(tempfile.mkdtemp(prefix="gen-changelogs-selftest-"))
    try:
        repo = tmp / "demo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")

        # v0.1.0：一条真实功能提交
        (repo / "a.go").write_text("package a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat(x): 第一个功能")
        _git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")

        for scenario, use_pr in (("场景1 直推打 tag", False), ("场景2 走 PR 打 tag", True)):
            base = _git(repo, "rev-parse", "main").strip()
            # v0.2.0 的真实改动
            (repo / "b.go").write_text("package b\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "feat(y): 第二个功能")

            # 预写待发版段
            pre = render(repo, "demo", "v0.2.0")
            (repo / "CHANGELOG.md").write_text(pre)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "docs(changelog): 预写 v0.2.0 段")

            if use_pr:
                _git(repo, "checkout", "-q", "-b", "chore/pre")
                _git(repo, "checkout", "-q", "main")
                _git(repo, "merge", "-q", "--no-ff", "chore/pre",
                     "-m", "Merge pull request #1 from demo/chore/pre")
            # 在当前 tip 上打 tag
            _git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")

            committed = (repo / "CHANGELOG.md").read_text()
            regen = render(repo, "demo")  # 事后重生成（tag 已存在）
            check(f"{scenario}: 事后重生成与提交版逐字节一致", regen, committed)

            # tag 快照里必须含自己那段
            snap = _git(repo, "show", "v0.2.0:CHANGELOG.md")
            check(f"{scenario}: tag 快照含自己的版本段",
                  "## [v0.2.0]" in snap, True)

            # 预写段里不能出现「补 CHANGELOG」这个零信息量条目
            check(f"{scenario}: 段内剔除「补 CHANGELOG」条目",
                  "补 CHANGELOG.md（从 git 历史生成）" in pre, False)

            # ⚠️ 反证：squash merge 的 subject 后缀「(#N)」必须被去掉。
            # 实测 2026-10-20：预写时看到 PR head 的 subject（无后缀），
            # 事后重生成看到 squash 后的 subject（带「(#25)」）——
            # 两者不一致 ⇒ tag 快照与 main 逐字节不同 ⇒ 门禁每次发版必红。
            #
            # 样本必须**真的带**「(#N)」，否则断言是假通过（实测踩过：
            # 原本样本的 subject 不带后缀，移除 strip 后自测仍绿）。
            (repo / "d.go").write_text("package d\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "feat(w): 带 PR 号的提交 (#99)")
            pre_pr = render(repo, "demo", "v0.9.0")
            check(f"{scenario}: 去掉 subject 的 PR 号后缀",
                  "(#99)" in pre_pr, False)
            check(f"{scenario}: 去掉后缀后标题仍完整",
                  "带 PR 号的提交" in pre_pr, True)
            _git(repo, "reset", "-q", "--hard", "HEAD~1")
            (repo / "d.go").unlink(missing_ok=True)

            # 场景2 额外验证：merge commit 也不能进段
            if use_pr:
                check("场景2: 段内剔除 merge commit",
                      "Merge pull request #1" in pre, False)

            # ⚠️ 反证：merge 规则必须**被单独检验**，不能被「只改 CHANGELOG.md」
            # 规则顺带覆盖。上一条断言在 merge commit 的 diff 只含 CHANGELOG.md
            # 时是**假通过**——它已被第一条规则剔掉，merge 规则压根没生效。
            # 这里构造一个「merge commit 且改了非 CHANGELOG 文件」的样本：
            # 分支上同时提交了代码与预写的 CHANGELOG，合并后 merge commit
            # 相对第一父**改了不止 CHANGELOG.md** → 只有 merge 规则能剔掉它。
            if use_pr:
                (repo / "c.go").write_text("package c\n")
                _git(repo, "checkout", "-q", "chore/pre")
                _git(repo, "add", "-A")
                _git(repo, "commit", "-q", "-m", "feat(z): 第三个功能")
                _git(repo, "checkout", "-q", "main")
                _git(repo, "merge", "-q", "--no-ff", "chore/pre",
                     "-m", "Merge pull request #2 from demo/chore/pre")
                # 判定 merge commit「改了非 CHANGELOG 文件」：用 -m（逐父对比）
                # 取并集——`git show --name-only` 对 merge commit **返回空**
                # （Git 默认不展示 merge 的 diff），直接用会误判。
                merged_files = [
                    ln.strip() for ln in
                    _git(repo, "diff-tree", "--no-commit-id", "--name-only",
                         "-r", "-m", "HEAD").splitlines()
                    if ln.strip()
                ]
                check("反证样本: merge commit 确实改了非 CHANGELOG 文件",
                      any(f != "CHANGELOG.md" for f in merged_files), True)
                pre2 = render(repo, "demo", "v0.3.0")
                check("反证: 段内剔除「改了非 CHANGELOG 的 merge commit」",
                      "Merge pull request #2" in pre2, False)
                # 复位
                _git(repo, "reset", "-q", "--hard", "HEAD~1")
                _git(repo, "branch", "-D", "chore/pre")

            # 复位到 v0.1.0，重跑下一个场景
            _git(repo, "tag", "-d", "v0.2.0")
            _git(repo, "reset", "-q", "--hard", base)
            (repo / "CHANGELOG.md").unlink(missing_ok=True)

        # ────────────────────────────────────────────────────────────
        # 新增用例：round 前缀 / 重复首行 / 待发版段
        #
        # 这三类的共同点是「渲染结果与提交版不一致」，而旧自测**全绿**——
        # 它们是 2026-10-20 真实踩出来的（7 个仓连续红）。
        # ────────────────────────────────────────────────────────────
        import os

        # 固定提交日期：待发版段用的是 HEAD 的提交日期，跨零点会让
        # 「预写时的日期」与「事后重生成的日期」不同 → 逐字节比对假失败。
        saved_env = {k: os.environ.get(k) for k in
                     ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")}
        os.environ["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00+00:00"
        os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00+00:00"
        try:
            repo2 = tmp / "demo2"
            repo2.mkdir()
            _git(repo2, "init", "-q", "-b", "main")
            _git(repo2, "config", "user.email", "t@example.com")
            _git(repo2, "config", "user.name", "T")
            (repo2 / "a.go").write_text("package a\n")
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q", "-m", "feat(x): 第一个功能")
            _git(repo2, "tag", "-a", "v0.1.0", "-m", "v0.1.0")

            # 用例1：subject 行首带 `[round-N] ` 前缀。
            # 反证：旧行为下 CC_RE 不匹配 → 归 other → body 整段丢失。
            (repo2 / "b.go").write_text("package b\n")
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q",
                 "-m", "[round-3] feat(mcp): 带 round 前缀的提交 (#77)",
                 "-m", "MARKER_BODY_LINE 这是 body 里的关键行")
            r1 = render(repo2, "demo2", "v0.2.0")
            check("round 前缀: 走 conventional commit 路径（起小标题）",
                  "### 新增(mcp)：带 round 前缀的提交" in r1, True)
            check("round 前缀: body 关键行必须保留（反证：旧行为丢 body）",
                  "MARKER_BODY_LINE 这是 body 里的关键行" in r1, True)
            check("round 前缀: 不再退化成 other 的 bullet 形式",
                  "- [round-3] feat(mcp):" in r1, False)

            # 用例2：subject **内部**的 `[round-N]` 不是前缀，不能剥。
            (repo2 / "c.go").write_text("package c\n")
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q", "-m", "feat(x): [round-9] 内嵌不算前缀")
            r2 = render(repo2, "demo2", "v0.3.0")
            check("内嵌 [round-N] 不被剥离",
                  "[round-9] 内嵌不算前缀" in r2, True)

            # 用例3：body 首行与 subject 重复 → 渲染时跳过；
            #         正文首行不是重复时必须保留。
            (repo2 / "d.go").write_text("package d\n")
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q",
                 "-m", "feat(y): 重复首行测试 (#88)",
                 "-m", "* feat(y): 重复首行测试 (#88)\n\n真正的正文第一行")
            r3 = render(repo2, "demo2", "v0.4.0")
            check("重复首行: 渲染结果不含 markdown 重复行",
                  "* feat(y): 重复首行测试" in r3, False)
            check("重复首行: 真正的正文必须保留",
                  "真正的正文第一行" in r3, True)

            # 用例5：非 HEADING_TYPES 的类型（chore/docs）其 body **必须**保留。
            # 反证：旧行为下 other 桶渲染分支只取 head，body 被丢弃。
            # 实测受害样本：agent 仓 v0.6.0 段的
            # `chore(deps): 依赖地板升到「历史最新 + 0.1.0」`，其 body 是
            # 版本线重置说明与 go.sum 注意事项——实质内容，却整段消失。
            (repo2 / "e.go").write_text("package e\n")
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q",
                 "-m", "chore(deps): 地板升到某版本 (#66)",
                 "-m", "* chore(deps): 地板升到某版本 (#66)\n\nCHORE_BODY_MARKER 这行正文必须留下")
            r5 = render(repo2, "demo2", "v0.6.0")
            check("other 桶（chore）: body 必须保留（反证：旧行为丢 body）",
                  "CHORE_BODY_MARKER 这行正文必须留下" in r5, True)
            check("other 桶（chore）: 与 subject 重复的行被去掉",
                  "chore(deps): 地板升到某版本 (#66)" in r5, False)

            # 用例4：待发版段（预写 → 提交 → **还没打 tag**）必须判为一致。
            pre4 = render(repo2, "demo2", "v0.5.0")
            (repo2 / "CHANGELOG.md").write_text(pre4)
            _git(repo2, "add", "-A")
            _git(repo2, "commit", "-q", "-m", "docs(changelog): 预写 v0.5.0 段")
            committed4 = (repo2 / "CHANGELOG.md").read_text()
            ok4, note4 = check_against_history(repo2, "demo2", committed4)
            check("待发版段: 预写但未打 tag 时判为一致", ok4, True)
            check("待发版段: 说明里点明是待发版段", "v0.5.0" in note4, True)
            # 反证：内容被改坏（删掉一个标题）必须判为不一致。
            broken4 = committed4.replace("### 新增(mcp)：带 round 前缀的提交", "")
            ok4b, _ = check_against_history(repo2, "demo2", broken4)
            check("待发版段: 内容缺失时判为不一致（反证）", ok4b, False)
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("❌ gen_changelogs 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ gen_changelogs 自测通过（预写→提交→打 tag 端到端自洽，"
          "两种发版打法产出一致）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 git 历史生成 CHANGELOG.md")
    ap.add_argument("--repo", help="仓名（相对 --root）")
    ap.add_argument("--root", default="/Users/zhangyi/oagentkit")
    ap.add_argument("--out", help="输出路径；默认写 <root>/<repo>/CHANGELOG.md")
    ap.add_argument("--check", action="store_true", help="只比对不写；不一致则 exit 1")
    ap.add_argument("--selftest", action="store_true",
                    help="自测：验证「预写→提交→打 tag」流程端到端自洽")
    ap.add_argument("--next-version", metavar="vX.Y.Z",
                    help="把「上一个 tag 之后的提交」预写成该版本的待发版段（打 tag 前用）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.repo:
        ap.error("--repo 是必需的（--selftest 模式除外）")

    repo_dir = Path(args.root) / args.repo
    if not (repo_dir / ".git").exists():
        print(f"❌ 不是 git 仓: {repo_dir}")
        return 1

    # ⚠️ 没有 tag 就要**报错**，不能静默生成空文件。
    # 实测：`git clone --depth 1` 出来的浅克隆有 0 个 tag，生成器会输出一个
    # 「0 个版本段」的空 CHANGELOG——用它去 --check 就会拿空内容跟真文件比，
    # 报出「已过期」的假红；用它去生成就会把真 CHANGELOG 覆盖成空文件。
    # 退化输入必须显式失败，不能静默产出看似成功的空结果。
    if not tags_in_order(repo_dir):
        print(f"❌ {args.repo}: 找不到任何版本 tag。"
              "若是浅克隆（--depth 1）请改用完整克隆；若是全新仓则应先发版。")
        return 1

    out = Path(args.out) if args.out else repo_dir / "CHANGELOG.md"

    if args.check:
        if not out.exists():
            print(f"❌ {args.repo} 缺 CHANGELOG.md")
            return 1
        cur = out.read_text(encoding="utf-8")
        ok, note = check_against_history(repo_dir, args.repo, cur)
        if ok:
            print(f"✅ {args.repo} 的 CHANGELOG.md 是最新的{note}")
            return 0
        print(f"❌ {args.repo} 的 CHANGELOG.md 与 git 历史不一致（已过期）")
        if note:
            print(note)
        return 1

    content = render(repo_dir, args.repo, args.next_version)
    out.write_text(content, encoding="utf-8")
    n = content.count("\n## [")
    print(f"✅ {args.repo}: 生成 {n} 个版本段 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
