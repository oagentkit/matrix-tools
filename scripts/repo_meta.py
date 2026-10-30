#!/usr/bin/env python3
"""远端仓库元信息的统一取数层（gh api）+ 几个纯函数的判定。

## 为什么要收在一处

「取某仓的最新 tag」「取某文件的内容」「列出仓里的路径」这三件事在
matrix-tools 的脚本里各写了一份（verify_dep_floors / verify_facade_release /
gen_versions…），于是**同一类 bug 要修三次**，而且已经修漏过一次：

    `gh api repos/{org}/{repo}/tags --jq "[.[].name] | .[0]"` 取「第一个 tag」
    —— 只有一个 tag 时它就是最新；但 guardx 拆出嵌套模块后，仓里同时存在
    `v0.6.0` / `audit/v0.1.0` / `bashguard/v0.1.0`，"第一个"是谁取决于 API
    的返回顺序。latest 取错 → 矩阵里写着「最新版本 v0.1.0」，下游照它拉代码。

所以这里的 `latest_tag` **只认 `^v\\d+\\.\\d+\\.\\d+`**（主模块的版本线），
嵌套模块的 tag（`audit/v0.1.0`）由 `nested_tag` 单独取。

## 取数纪律

- **必须重试**：一次扫描要发几十次 gh api，实测偶发瞬时失败。取不到就当
  「判不了」，不能当成「没有」——后者会让报告凭空少几行、让守卫静默失效。
- **一律走 gh**（已认证），不走裸 HTTP：省掉 token 管理，且能复用 gh 的重试。
"""

from __future__ import annotations

import argparse
import os

import base64
import json
import re
import subprocess
import time
from pathlib import Path

# 主模块版本线的 tag：v1.2.3（不含嵌套模块的 audit/v1.2.3 形态）
MAIN_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# CHANGELOG 的版本段标题行：`## [v1.2.3] — 2026-01-01` / `## v1.2.3`。
# 只认标题行**以版本号开头**——仓里的 `## 下游要做什么` 这类二级标题是
# 版本段内部的小节，不是版本段（否则「某段里提到了 v0.15.0」会被当成版本段）。
CHANGELOG_VERSION_HEADING = re.compile(r"^##\s*\[?(?P<v>v\d+\.\d+\.\d+)\]?")

# 嵌套模块的 tag：<子目录>/v1.2.3
NESTED_TAG_RE = re.compile(r"^(?P<prefix>[^/]+/[\w.\-/]*?)v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")


# 单次 gh api 调用的超时（秒）。**必须有**：没有超时的话，一次网络挂起就会让
# 整个门禁进程无限期挂着——实测 2026-10-20 有一次 `gh api` 挂死后本地跑了 7 小时
# 才被发现（CI 里则是被 job 的 timeout-minutes 杀掉，表现为「这个 job 莫名其妙地
# 超时」，报错完全指不到真正原因）。
GH_API_TIMEOUT = float(os.environ.get("OAGENTKIT_GH_API_TIMEOUT", "60"))

# GraphQL 的 `first` **硬上限是 100**：超过就报 EXCESSIVE_PAGINATION。
# 实测：`issues(first:200)` → "Requesting 200 records on the `issues` connection
# exceeds the `first` limit of 100 records."（issue matrix-tools#104）
# ⇒ 每页都不得超过这个数，要更多就翻页。
GRAPHQL_PAGE_MAX = 100
# 翻页的总页数上限：仓里有海量已关闭 PR 时不至于无限翻（`repository.issues`
# 把 PR 也一起返回，而 limit 只按 issue 计）。超了就判不了，不当成「就这些」。
GRAPHQL_MAX_PAGES = 20
# REST 侧的翻页上限：同一条纪律（`repository.issues` 把 PR 一起返回，
# 单页 100 条 ⇒ 20 页 = 2000 条，超了就判不了）。
# 实测 2026-10-31：agentkit 有 63 个 tag，默认只回第一页 30 个 ⇒ 必须翻页。
REST_MAX_PAGES = 20


def gh_api_status(path: str, retries: int = 3) -> tuple[str | None, str]:
    """调 `gh api <path>`；→ (正文, 状态)，状态 ∈ {ok, not_found, error}。

    为什么要区分 not_found 与 error：**「这个文件在该 ref 下不存在」和「这次调用
    失败了」是两件事**。前者是事实（当时仓里确实没有这个文件），后者是「判不了」。
    把两者都折叠成 None，守卫就会把网络失败当成「当时没有」而静默放过——
    这正是「一直绿、但从没生效过」的典型形态。
    """
    for attempt in range(retries):
        try:
            p = subprocess.run(
                ["gh", "api", path], capture_output=True, text=True,
                timeout=GH_API_TIMEOUT)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None, "error"
        except OSError:           # gh 不在 PATH 上
            return None, "error"
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout, "ok"
        if "404" in (p.stderr or "") or "Not Found" in (p.stderr or ""):
            return None, "not_found"
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None, "error"


def gh_api(path: str, retries: int = 3) -> str | None:
    """调 `gh api <path>` 取 JSON 文本；取不到返回 None（**判不了 ≠ 没有**）。"""
    text, _status = gh_api_status(path, retries=retries)
    return text


def gh_api_paginated(path: str, retries: int = 3,
                     max_pages: int = REST_MAX_PAGES) -> str | None:
    """调 GitHub REST list 端点取**全量** JSON 数组；取不到返回 None。

    ## 为什么不能直接用 gh_api

    GitHub 的 REST list 端点**默认只返回第一页**（30 条；`per_page` 最多 100）。
    实测 2026-10-31：`gh api repos/oagentkit/agentkit/tags --jq length` 返回 **30**，
    而该仓真实有 **63** 个 tag ⇒ 任何「数 tag」的判据都会**假报缺口**。

    ## 为什么自己翻页而不用 `gh api --paginate`

    `gh api --paginate` 会一直翻到空页为止，**没有上限**——而 `repository.issues`
    这类端点把 PR 也一起返回，仓里 PR 一多就是几十页。
    本函数自己翻页并带**页数上限**（REST_MAX_PAGES），超限 ⇒ 返回 None（判不了），
    **不当成「就这些」**——与 GraphQL 侧「游标不前进 ⇒ 判不了」同一纪律。

    中途某页取不到 ⇒ **判不了**（返回 None），不拿已取到的部分当全量。

    返回值是**合并后的单个 JSON 数组**（`json.dumps` 出来）⇒ 调用方照常
    `json.loads` 即可，不用改解析。
    """
    sep = "&" if "?" in path else "?"
    base = f"{path}{sep}per_page=100"
    out: list = []
    for page in range(1, max_pages + 1):
        raw = gh_api(f"{base}&page={page}", retries=retries)
        if not raw:
            # 取不到这一页 ⇒ 判不了（不拿已取到的部分冒充全量）
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        out.extend(data)
        if len(data) < 100:
            # 不满一页 ⇒ 已是最后一页
            return json.dumps(out, ensure_ascii=False)
    # 翻到上限还没翻完 ⇒ 判不了（不当成「就这些」）
    return None


def fetch_file_status(org: str, repo: str, path: str, ref: str = "main") -> tuple[str | None, str]:
    """取仓里某文件的原文；→ (正文, 状态)，状态 ∈ {ok, not_found, error}。

    not_found = 该 ref 下确实没有这个文件；error = 判不了（取不到、解析失败）。
    """
    raw, status = gh_api_status(f"repos/{org}/{repo}/contents/{path}?ref={ref}")
    if raw is None:
        return None, status
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, "error"
    if "content" not in payload:
        # 超大文件走 download_url、或返回了别的结构 → 判不了
        return None, "error"
    try:
        return base64.b64decode(payload["content"]).decode("utf-8"), "ok"
    except (ValueError, KeyError):
        return None, "error"


def fetch_file(org: str, repo: str, path: str, ref: str = "main") -> str | None:
    """取仓里某文件的原文（base64 解码后）。取不到返回 None。"""
    text, _status = fetch_file_status(org, repo, path, ref=ref)
    return text


def fetch_tree_paths(org: str, repo: str, ref: str = "main") -> list[str] | None:
    """列出仓里的全部文件路径（递归）。取不到返回 None。"""
    raw = gh_api(f"repos/{org}/{repo}/git/trees/{ref}?recursive=1")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if payload.get("truncated"):
        # 树被截断 = 目录不完整 → 判不了，不能当「就这些」
        return None
    return [e.get("path", "") for e in payload.get("tree", []) if e.get("type") == "blob"]


def gh_graphql(query: str, variables: dict | None = None,
               retries: int = 3) -> dict | None:
    """调 `gh api graphql` → 解析后的 JSON；取不到返回 None（**判不了 ≠ 没有**）。

    与 `gh_api` 分开：GraphQL 要 POST 一个查询体，`gh api <path>` 那套
    「路径 + query string」的形态表达不了。
    """
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        # None 必须发成 **JSON null**（gh 的 -F 会把字面量 "null" 解析成 null）。
        # 直接 f-string 会得到字符串 "None" ⇒ GraphQL 把它当游标/参数用 ⇒
        # 报 "`None` does not appear to be a valid cursor."（issue matrix-tools#104）
        cmd += ["-F", f"{key}=null" if value is None else f"{key}={value}"]

    for attempt in range(retries):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_API_TIMEOUT)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except OSError:            # gh 不在 PATH 上
            return None
        if p.returncode == 0 and p.stdout.strip():
            try:
                payload = json.loads(p.stdout)
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            if payload.get("errors"):
                return None           # GraphQL 报错 ⇒ 判不了，不是「没有」
            return payload
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


# 已关闭 issue 的**关闭来源**取数（GraphQL）。
#
# 为什么必须单独取：`gh issue list --json closedByPullRequestsReferences`
# 只覆盖「**被 PR 关闭**」这一条路。而 GitHub 还会读 **commit message** 里的
# 关闭关键词——squash 合并会把 commit message 原样带进 main ⇒
# commit message 里的 `Closes #N` 照样触发自动关单，那条路在 REST 侧完全不可见。
#
# issue 时间线上的 `closedEvent.closer` 有三种取值，正好对应三种关闭方式：
#   - `PullRequest`：被某个 PR 关闭（**可以是别的仓的 PR**——GitHub 支持
#     `Closes owner/repo#N` 跨仓关单；实测 mcpkit#17 就是被 toolgate#24 关的）；
#   - `Commit`：被某个提交关闭（commit message 里的关闭关键词）；
#   - `null`：人工关闭（`gh issue close` 或 UI 关单）。
CLOSER_QUERY = """
query($owner:String!,$name:String!,$n:Int!,$after:String){
  repository(owner:$owner,name:$name){
    issues(first:$n, states:CLOSED, after:$after,
           orderBy:{field:CREATED_AT, direction:DESC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        __typename
        number
        ... on Issue{
          timelineItems(first:50, itemTypes:[CLOSED_EVENT]){
            pageInfo{hasNextPage}
            nodes{ ... on ClosedEvent{ createdAt closer{
              __typename
              ... on PullRequest{number state merged repository{nameWithOwner}}
              ... on Commit{oid committedDate repository{nameWithOwner}}
            }}}
          }
        }
      }
    }
  }
}
"""


def parse_closer_events(events: list[dict]) -> dict:
    """时间线上的关闭事件列表 → closer 描述（**纯函数**，离线可测）。

    取**最后一次**关闭事件：issue 可能被关过又重开过，这时最后一次的
    closer 才是当前这次关闭的来源。
    """
    if not events:
        return {"type": "unknown"}         # 判不到关闭事件 ⇒ 判不了

    closer = events[-1].get("closer") or {}
    kind = closer.get("__typename")
    if kind == "PullRequest":
        return {
            "type": "pull_request",
            "pr": closer.get("number"),
            "pr_repo": ((closer.get("repository") or {}).get("nameWithOwner")),
            "merged": closer.get("merged"),
            "oid": None,
        }
    if kind == "Commit":
        return {
            "type": "commit",
            "pr": None,
            "pr_repo": ((closer.get("repository") or {}).get("nameWithOwner")),
            "merged": None,
            "oid": closer.get("oid"),
        }
    # closer 为 null ⇒ 人工关单
    return {
        "type": "manual",
        "pr": None,
        "pr_repo": None,
        "merged": None,
        "oid": None,
    }


def list_closed_issue_closers(org: str, repo: str, limit: int = 100) -> dict[int, dict] | None:
    """取仓里**已关闭 issue 的关闭来源**（谁关的：PR / 提交 / 人工）。

    → {issue 号: closer 描述}；取不到返回 None（**判不了 ≠ 没有**）。

    closer 描述的形状（事实，不含任何判定）：

        {"type": "pull_request", "pr": 32, "pr_repo": "oagentkit/llm",
         "merged": True, "oid": None}
        {"type": "commit", "pr": None, "pr_repo": None,
         "merged": None, "oid": "aa60d8facf3e…"}
        {"type": "manual", "pr": None, "pr_repo": None,
         "merged": None, "oid": None}

    取「**最后一次**关闭事件」：issue 可能被关过又重开过，这时最后一次
    的 closer 才是当前这次关闭的来源。

    为什么在事实层（而不是判定层）取：这是「谁关的单」这一**事实**，
    「该不该有回执」才是判定。事实可以共享，判定不能共享（见 AGENTS.md
    三层架构）。

    ⚠️ **必须翻页**（issue matrix-tools#104）：GraphQL 对 `first` 有**硬上限 100**
    ——超过就报 `EXCESSIVE_PAGINATION`，而 `gh_graphql()` 把 GraphQL 错误吞成 None
    ⇒ 整仓「取不到」⇒ 回执门禁把**全矩阵**判成「判不了」且退出码仍是 0。
    所以这里每页 `first` 恒 ≤ `GRAPHQL_PAGE_MAX`，页间用 `after` 游标串起来，
    累计取到 `limit` 条为止。
    """
    out: dict[int, dict] = {}
    fetched = 0                 # 已取到的 **issue** 条数（PR 不计入 limit 口径）
    cursor: str | None = None
    exhausted = True            # 页数预算用完时为 True（⇒ 可能还有更多 ⇒ 判不了）

    for _page_no in range(GRAPHQL_MAX_PAGES):
        if fetched >= limit:
            exhausted = False
            break
        payload = gh_graphql(CLOSER_QUERY, {
            "owner": org, "name": repo, "n": GRAPHQL_PAGE_MAX, "after": cursor,
        })
        if not payload:
            return None
        issues = ((payload.get("data") or {}).get("repository") or {}).get("issues")
        if not isinstance(issues, dict):
            return None
        nodes = issues.get("nodes") or []
        for node in nodes:
            # `repository.issues` 在 GraphQL 里**同时返回 PR**（PR 也是 issue），
            # 而 IssueFilters 又没有 types 参数可过滤 ⇒ 按 __typename 剔掉。
            if node.get("__typename") != "Issue":
                continue
            number = node.get("number")
            if not isinstance(number, int):
                continue
            timeline = node.get("timelineItems") or {}
            events = timeline.get("nodes") or []
            if (timeline.get("pageInfo") or {}).get("hasNextPage"):
                out[number] = {"type": "unknown"}
                continue
            out[number] = parse_closer_events(events)
            fetched += 1
            if fetched >= limit:
                break

        page_info = issues.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            exhausted = False        # 翻完了 ⇒ 没有更多
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            return None               # 有下一页却拿不到游标 ⇒ 判不了，不能当成「就这些」
        if not nodes:
            return None               # 空页却在翻页 ⇒ 状态不自洽 ⇒ 判不了

    if exhausted:
        # 页数预算用完：可能还有更多 issue ⇒ 判不了，不能当成「就这些」
        return None
    return out


def list_closed_issues(org: str, repo: str, limit: int = 100) -> list[dict] | None:
    """列出仓里**已关闭**的 issue（含「被哪些 PR 自动关闭」与评论正文）。

    → list[dict]；取不到返回 None（**判不了 ≠ 没有**）。

    为什么不用 `gh api`：REST 的 issue 列表**没有** `closedByPullRequestsReferences`
    字段，也不带评论正文——「哪个 PR 关掉了它」与「评论说了什么」这两个事实
    只在 GraphQL 里有（`gh issue list --json` 走的就是 GraphQL）。
    所以这里必须调 `gh issue list`，而不是 `gh api repos/.../issues`。

    返回的每条是 `gh issue list --json` 的原样 dict，键集固定为：
    number / title / closedAt / comments / closedByPullRequestsReferences / labels。
    """
    cmd = ["gh", "issue", "list", "--repo", f"{org}/{repo}", "--state", "closed",
           "--limit", str(limit),
           "--json", "number,title,closedAt,comments,closedByPullRequestsReferences,labels"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_API_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        payload = json.loads(p.stdout or "[]")
    except ValueError:
        return None
    return payload if isinstance(payload, list) else None


def changelog_version_sections(text: str) -> set[str]:
    """CHANGELOG 里的**版本段集合**（结构信号，与措辞无关）。

    判据只看版本段标题行：`## [v1.2.3] — 2026-01-01` / `## v1.2.3`。
    仓里的其他二级标题（`## 下游要做什么` / `## 为什么抬`）**不是**版本段——
    它们是某个版本段内部的正文小节，所以标题行必须以版本号开头才算。

    为什么放在**事实层**：「某份 CHANGELOG 里有哪些版本段」是事实，
    「缺了某段算不算违规」才是判定。事实可以共享，判定不能共享
    （见 AGENTS.md 三层架构）。
    """
    out: set[str] = set()
    for line in (text or "").splitlines():
        m = CHANGELOG_VERSION_HEADING.match(line)
        if m:
            out.add(m.group("v"))
    return out


def fetch_changelog_sections(org: str, repo: str,
                             ref: str = "main") -> tuple[set[str] | None, str]:
    """取某 ref 处 CHANGELOG 的版本段集合；→ (集合, 状态)。

    状态 ∈ {ok, not_found, error}：
    - not_found = 该 ref 下确实没有 CHANGELOG.md（事实如此）
    - error     = **判不了**（取不到、解析失败）——不能当成「没有版本段」
    """
    text, status = fetch_file_status(org, repo, "CHANGELOG.md", ref=ref)
    if status != "ok" or text is None:
        return None, status
    return changelog_version_sections(text), "ok"


def fetch_repo_snapshot(org: str, repo: str, dest: "Path", ref: str = "main") -> "Path | None":
    """把某仓的源码快照（tarball）下载并解到 dest 下，返回解出来的仓根目录。

    取不到返回 None（**判不了 ≠ 没有**）。

    为什么用 tarball 而不是逐文件取：contents API 的目录端点只返回文件元数据、
    不带内容，逐文件取 48 个包要 130+ 次调用（实测 2m31s，CI 里每次 PR 都等）。
    tarball 端点**一次调用给整个仓**（实测 1.5s/仓），14 个仓约 20s。

    解包只落**普通文件**：符号链接、硬链接、绝对路径、`..` 路径一律跳过——
    远端 tarball 是不可信输入，不能让它往 dest 外写文件。
    """
    import io
    import tarfile

    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{org}/{repo}/tarball/{ref}"],
            capture_output=True,
            timeout=GH_API_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    target = dest / repo
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tf:
            for m in tf.getmembers():
                if "/" not in m.name:
                    continue
                rel = m.name.split("/", 1)[1]
                if not rel or m.issym() or m.islnk():
                    continue
                parts = Path(rel).parts
                if rel.startswith("/") or ".." in parts:
                    continue
                out = target / rel
                if m.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(m)
                    if src is not None:
                        out.write_bytes(src.read())
    except (tarfile.TarError, OSError, ValueError):
        return None
    return target if target.is_dir() else None


def _all_tags(org: str, repo: str) -> list[str]:
    # 分页取全量：默认只回第一页（30 条）⇒ 会漏掉较旧的 tag，
    # 让「tag 数量/集合」类判据假报缺口（实测 agentkit 真实 63 个、默认只回 30）。
    raw = gh_api_paginated(f"repos/{org}/{repo}/tags")
    if not raw:
        return []
    try:
        return [t.get("name", "") for t in json.loads(raw)]
    except ValueError:
        return []


def latest_tag(org: str, repo: str) -> str | None:
    """主模块的最新版本 tag（只认 `vX.Y.Z`），取不到返回 None。

    ⚠️ 不能用「tags 数组的第一个」——嵌套模块的 tag（`audit/v0.1.0`）也在同一个
    仓里，"第一个"是谁取决于 API 返回顺序。
    """
    tags = [t for t in _all_tags(org, repo) if MAIN_TAG_RE.match(t)]
    if not tags:
        return None
    return max(tags, key=version_key)


def nested_tag(org: str, repo: str, prefix: str) -> str | None:
    """嵌套模块的最新 tag（prefix 形如 `audit/`），取不到返回 None。"""
    pat = re.compile(rf"^{re.escape(prefix)}v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")
    tags = [t for t in _all_tags(org, repo) if pat.match(t)]
    if not tags:
        return None
    return max(tags, key=lambda t: version_key(t[len(prefix):]))


def version_key(v: str) -> tuple:
    """semver 比较键：段按**数字**比（字符串比会把 v0.9.10 判成小于 v0.9.9）。"""
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v.strip())
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def module_path_from_gomod(text: str) -> str | None:
    """go.mod 文本 → module 路径。取不到返回 None。"""
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip()
        if line.startswith("module "):
            return line[len("module "):].strip().strip('"')
    return None


def package_dirs(paths: list[str]) -> set[str]:
    """仓里的**包目录**集合（目录名，相对仓根；仓根本身用 "." 表示）。

    判据：目录下有 .go 文件（排除 _test.go 不影响——只要有非测试 .go 就是包）。
    嵌套 module 的目录**也算包**（它对外提供包），只是版本线独立。
    """
    dirs: set[str] = set()
    for p in paths:
        if not p.endswith(".go"):
            continue
        parts = p.split("/")
        if len(parts) == 1:
            dirs.add(".")          # 仓根自己就是一个包（llm / rag 那种）
        else:
            dirs.add(parts[0])
    return dirs


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # tag 过滤：嵌套模块的 tag 不得混进主模块的 latest
    check("主 tag 形态", MAIN_TAG_RE.match("v0.6.0") is not None, True)
    check("嵌套 tag 不算主 tag", MAIN_TAG_RE.match("audit/v0.1.0") is None, True)
    check("嵌套 tag 不算主 tag 2", MAIN_TAG_RE.match("bashguard/v0.1.0") is None, True)
    check("预发布主 tag 算", MAIN_TAG_RE.match("v1.0.0-rc.1") is not None, True)

    # version_key：数字比较
    check("v0.9.10 > v0.9.9", version_key("v0.9.10") > version_key("v0.9.9"), True)
    check("v0.10.0 > v0.9.9", version_key("v0.10.0") > version_key("v0.9.9"), True)
    check("预发布 < 正式", version_key("v1.0.0-rc.1") < version_key("v1.0.0"), True)

    # module_path_from_gomod
    gomod = "module github.com/oagentkit/guardx/audit\n\ngo 1.26.0\n"
    check("module 路径", module_path_from_gomod(gomod), "github.com/oagentkit/guardx/audit")
    check("无 module 行", module_path_from_gomod("go 1.26.0\n"), None)

    # package_dirs
    paths = [
        "logredact/logredact.go", "logredact/hint.go", "fence/fence.go",
        "audit/audit.go", "audit/go.mod", "bashguard/bashguard.go",
        "README.md", "go.mod",
    ]
    check("包目录集合", package_dirs(paths), {"logredact", "fence", "audit", "bashguard"})
    check("仓根是包", package_dirs(["main.go"]), {"."})

    # changelog_version_sections：只认版本段标题行
    cl = ("# llm CHANGELOG\n"
          "\n"
          "## [v0.15.2] — 2026-10-21\n"
          "\n"
          "## 下游要做什么\n"
          "\n"
          "跑 go mod tidy。\n"
          "\n"
          "## [v0.15.1] — 2026-10-20\n")
    check("版本段集合", changelog_version_sections(cl), {"v0.15.2", "v0.15.1"})
    check("非版本段的二级标题不算版本段",
          "v0.15.2" in changelog_version_sections("## 下游要做什么 v0.15.2\n"), False)
    check("三级标题不算版本段",
          changelog_version_sections("### v0.15.2\n"), set())
    check("无方括号的标题行也算",
          changelog_version_sections("## v1.0.0 — 2026-01-01\n"), {"v1.0.0"})
    check("空文本", changelog_version_sections(""), set())
    check("None 也不炸", changelog_version_sections(None), set())

    # parse_closer_events：关闭来源的三种形态
    #   PullRequest（被 PR 关，含跨仓 PR）/ Commit（被 commit message 关）/ null（人工关）
    pr_event = [{"closer": {"__typename": "PullRequest", "number": 24,
                            "merged": True,
                            "repository": {"nameWithOwner": "oagentkit/toolgate"}}}]
    check("PR 关闭",
          parse_closer_events(pr_event),
          {"type": "pull_request", "pr": 24, "pr_repo": "oagentkit/toolgate",
           "merged": True, "oid": None})
    commit_event = [{"closer": {"__typename": "Commit",
                                "oid": "aa60d8facf3ea010117cc495c56ef43ea6db86a7",
                                "repository": {"nameWithOwner": "oagentkit/matrix-tools"}}}]
    check("commit message 关闭",
          parse_closer_events(commit_event),
          {"type": "commit", "pr": None, "pr_repo": "oagentkit/matrix-tools",
           "merged": None, "oid": "aa60d8facf3ea010117cc495c56ef43ea6db86a7"})
    check("人工关闭", parse_closer_events([{"closer": None}]),
          {"type": "manual", "pr": None, "pr_repo": None,
           "merged": None, "oid": None})
    check("没有关闭事件 ⇒ 判不了", parse_closer_events([])["type"], "unknown")
    check("多个关闭事件取最后一次",
          parse_closer_events([{"closer": {"__typename": "PullRequest", "number": 1,
                                           "merged": True,
                                           "repository": {"nameWithOwner": "o/r"}}},
                               {"closer": None}])["type"],
          "manual")

    # ---- 关闭来源取数：**必须翻页**（issue matrix-tools#104）
    #
    # GraphQL 的 `first` 硬上限 100 ⇒ 超过就报 EXCESSIVE_PAGINATION ⇒
    # gh_graphql 把 GraphQL 错误吞成 None ⇒ 整仓「取不到」⇒
    # 回执门禁把**全矩阵**判成「判不了」且退出码仍是 0。
    def _issue_node(number: int) -> dict:
        return {"__typename": "Issue", "number": number,
                "timelineItems": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"closer": {"__typename": "PullRequest", "number": 7,
                                          "state": "MERGED", "merged": True,
                                          "repository": {"nameWithOwner": "o/r"}}}]}}

    def _page(numbers, has_next: bool, cursor=None) -> dict:
        return {"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": [_issue_node(n) for n in numbers]}}}}

    saved_gql = gh_graphql
    try:
        calls: list[dict] = []

        def fake_gql(query, variables=None, retries=3):
            calls.append(variables or {})
            if (variables or {}).get("after") is None:
                return _page([1, 2], True, "CUR1")
            return _page([3], False, None)

        globals()["gh_graphql"] = fake_gql
        closers = list_closed_issue_closers("oagentkit", "x", limit=150)
        check("翻页后第 2 页的 issue 收进来了", sorted(closers or {}), [1, 2, 3])
        check("翻页用了两次取数", len(calls), 2)
        check("第 2 次带上了 after 游标", calls[1].get("after"), "CUR1")
        check("每页 first 都不超过 GraphQL 上限",
              all((v.get("n") or 0) <= GRAPHQL_PAGE_MAX for v in calls), True)
        # 有区分力：单页实现只可能拿到第 1 页那 2 条 ⇒ 断言必须与它不同
        check("断言与单页结果不同（有区分力）", sorted(closers or {}) != [1, 2], True)

        # 反证样本：假翻页（游标不前进，永远只回第 1 页）⇒
        # **不能**把只看到的第 1 页当成「就这些」⇒ 必须判不了（返回 None）。
        calls.clear()

        def fake_gql_no_cursor(query, variables=None, retries=3):
            calls.append(variables or {})
            return _page([1, 2], True, "CUR1")

        globals()["gh_graphql"] = fake_gql_no_cursor
        closers_bad = list_closed_issue_closers("oagentkit", "x", limit=150)
        check("反证：假翻页时判不了（不当成「就这些」）", closers_bad, None)
    finally:
        globals()["gh_graphql"] = saved_gql

    # ---- GraphQL 变量为 None 时必须发成 **JSON null**（issue matrix-tools#104）
    # 直接 f-string 会得到字符串 "None" ⇒ GraphQL 拿它当游标 ⇒
    # 「`None` does not appear to be a valid cursor.」⇒ 整仓「取不到」。
    import subprocess as _sp

    captured: list[list[str]] = []

    class _FakeProc:
        returncode = 0
        stdout = '{"data":{}}'
        stderr = ""

    saved_run = _sp.run

    def fake_run(cmd, *a, **kw):
        captured.append(list(cmd))
        return _FakeProc()

    _sp.run = fake_run
    try:
        gh_graphql("query{}", {"after": None, "n": 100})
    finally:
        _sp.run = saved_run
    check("None 变量发成 JSON null", "after=null" in captured[0], True)
    check("None 变量没被发成字符串 None", "after=None" in captured[0], False)
    check("非 None 变量原样发", "n=100" in captured[0], True)

    # ---- REST 翻页（gh_api_paginated）：默认只回第一页 ⇒ 必须自己翻
    #
    # 实测 2026-10-31：agentkit 有 63 个 tag，而 `gh api .../tags`（默认 per_page=30）
    # 只回 30 个 ⇒ 任何「数 tag」的判据都会**假报缺口**。
    saved_api = gh_api
    try:
        seen: list[str] = []

        def fake_api(path, retries=3):
            seen.append(path)
            page = 1
            for seg in path.split("&"):
                if seg.startswith("page="):
                    page = int(seg.split("=", 1)[1])
            if page == 1:
                # 第一页满 100 条 ⇒ 还要翻
                return json.dumps([{"name": f"v0.0.{i}"} for i in range(100)])
            return json.dumps([{"name": "v0.1.0"}])   # 第二页不满 100 ⇒ 收尾

        globals()["gh_api"] = fake_api
        seen.clear()
        raw = gh_api_paginated("repos/o/r/tags")
        data = json.loads(raw) if raw else None
        check("REST 翻页后拿到全量", len(data or []), 101)
        check("REST 翻页取了两次", len(seen), 2)
        check("第 2 页带了 page 参数", any("page=2" in p for p in seen), True)
        check("每页都带 per_page=100", all("per_page=100" in p for p in seen), True)

        # 反证样本：中途某页取不到 ⇒ **判不了**（不拿已取到的部分冒充全量）
        seen.clear()

        def fake_api_fail_mid(path, retries=3):
            seen.append(path)
            if "page=2" in path:
                return None
            # 第 1 页必须**满 100 条** ⇒ 才会触发翻页（不满即收尾）
            return json.dumps([{"name": f"v0.0.{i}"} for i in range(100)])

        globals()["gh_api"] = fake_api_fail_mid
        check("反证：中途取不到 ⇒ 判不了（不当成「就这些」）",
              gh_api_paginated("repos/o/r/tags"), None)

        # 反证样本：第 1 页就取不到 ⇒ 判不了
        globals()["gh_api"] = lambda path, retries=3: None
        check("反证：第 1 页取不到 ⇒ 判不了", gh_api_paginated("repos/o/r/tags"), None)

        # 反证样本：翻到页数上限仍未收尾 ⇒ 判不了（不当成「就这些」）
        globals()["gh_api"] = lambda path, retries=3: json.dumps(
            [{"name": f"v0.0.{i}"} for i in range(100)])
        check("反证：页数超上限 ⇒ 判不了",
              gh_api_paginated("repos/o/r/tags", max_pages=3), None)
    finally:
        globals()["gh_api"] = saved_api

    if fails:
        print("❌ repo_meta 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ repo_meta 自测通过"
          "（tag 过滤 / semver / module 路径 / 包目录 / CHANGELOG 版本段解析 / "
          "关闭来源三态 / 关闭来源取数翻页 / REST 翻页与页数上限）")
    return 0


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(description="远端仓库元信息取数层")
    # --selftest 是「默认就跑自测」的显式写法（与其他守卫脚本的参数习惯一致）
    _ap.add_argument("--selftest", action="store_true", help="离线自测（默认行为）")
    _ap.parse_args()
    raise SystemExit(selftest())
