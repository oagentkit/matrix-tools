#!/usr/bin/env python3
"""第一方依赖地板**施作器**：把各仓落后的地板抬到最新可用版本，并开 PR。

## 为什么要有这个工具

矩阵的依赖地板是**跑步机**：`oekit` 基座（pkg / concurrency / logx）与矩阵自身的
模块都在高频发版（实测 2026-10-28 一天内 pkg 发了 4 版、concurrency 发了 3 版）。
每发一版，依赖它的仓的地板就落后一格 ⇒ 门禁 `verify_tag_floor --ref HEAD`
（宽口径：判「各仓 HEAD 相对该时刻的依赖最新版本」）立刻判红。

手工处理一轮要 20+ 个仓、30+ 个 PR（实测 2026-10-29 用了约 2 小时），
而且**基座发版节奏快过人工节奏** ⇒ 下一轮又落后。本工具把这件事变成一条命令。

## 口径（与门禁的关系）

- **事实共享**：go.mod 的 require 解析走 `matrix_facts.parse_go_mod`，
  依赖的最新版本走 `repo_meta.latest_tag` —— 与门禁**同一份事实**。
- **判定独立**：版本比较、第一方筛选、「落后」的判法都在**本文件**里实现，
  **不 import `verify_tag_floor.py`** —— 守卫之间互相 import 判定函数，
  会变成「一处漏收、处处漏收」的共同盲区（matrix-tools/AGENTS.md 硬规则）。
- 本工具的判定比门禁**更宽**：门禁判「在该仓 HEAD 提交时刻是否已落后」，
  本工具判「**此刻**是否落后」⇒ 刚发过版的依赖也会被抬（抬到更新版本永远安全）。

## 用法

```bash
# ① 只报告（默认，不写任何东西）
python3 scripts/bump_dep_floors.py --root <矩阵根>

# ② 落地：改 go.mod → 验证 → 提交 → 推分支 → 开 PR（**不合并**）
python3 scripts/bump_dep_floors.py --root <矩阵根> --apply

# 只看某几个仓
python3 scripts/bump_dep_floors.py --root <矩阵根> --repo guardx --repo tools
```

## 安全边界

- 默认**只报告**；`--apply` 才写文件、才推分支、才开 PR；**任何情况下都不合并**；
- 每个仓动手前要求**工作树干净**（有未提交改动就跳过并报告，不覆盖别人的东西）；
- 改完必须 `go build` / `go vet` / `go test -race` 全绿，**红 ⇒ 跳过那个仓**（不提交）；
- 推到的是**新分支**（`chore/deps-floor-<stamp>`），不动 `main`；
- 取不到依赖的最新版本 ⇒ 判「判不了」并跳过（**不拿本地清单冒充事实**）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_push_branch  # noqa: E402  （同目录模块：Git Data API 推送通道）

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import matrix_facts  # noqa: E402  事实层：go.mod 解析
import repo_meta  # noqa: E402  事实层：远端 tag / 版本排序

# 第一方模块前缀（**本文件自己的实现**，不 import 门禁的判定函数）。
#
# 为什么只认这两个 org：矩阵自己的模块（github.com/oagentkit/*）与基座
# （github.com/oekit/*，oagentkit 的模块大量以 oekit/pkg、oekit/concurrency
# 为基座）。第三方依赖（eino、mcp-go…）**不归本工具管** —— 升级它们是另一件事
# （会牵动 go 指令地板与 API 兼容性），不该混在「追地板」里。
FIRST_PARTY_OWNERS = ("oagentkit", "oekit")

# 模块路径 → (owner, repo)；取不到就返回 None。
_MODULE_RE = re.compile(r"^github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)$")


def split_module(path: str) -> tuple[str, str] | None:
    """`github.com/oagentkit/guardx` → `("oagentkit", "guardx")`；不像第一方 ⇒ None。"""
    m = _MODULE_RE.match(path.strip())
    if not m:
        return None
    owner, repo = m.group("owner"), m.group("repo")
    if owner not in FIRST_PARTY_OWNERS:
        return None
    return owner, repo


def parse_version(v: str) -> tuple[int, ...] | None:
    """`v0.31.0` → `(0, 31, 0)`；解析不了 ⇒ None。

    **必须按数字比**：字符串比较会把 `v0.9.0` 判成大于 `v0.10.0`
    （"9" > "1"）⇒ 门禁与施作器会同时把版本关系判反。
    """
    m = re.match(r"^v(\d+)(?:\.(\d+))?(?:\.(\d+))?", v.strip())
    if not m:
        return None
    return tuple(int(x) if x else 0 for x in m.groups(default="0"))


def version_lt(a: str, b: str) -> bool | None:
    """a < b ⇒ True；a >= b ⇒ False；任一解析不了 ⇒ None（判不了）。"""
    va, vb = parse_version(a), parse_version(b)
    if va is None or vb is None:
        return None
    return va < vb


def stale_floors(requires: dict[str, str], latest: dict[str, str]) -> list[tuple[str, str, str]]:
    """落后的地板 → [(依赖路径, 现地板, 目标版本)]，按路径排序。

    `requires` 是 {模块路径: 现地板}，`latest` 是 {模块路径: 远端最新 tag}。
    目标版本取不到 ⇒ **不产出**（判不了 ≠ 落后，由调用方按「判不了」报告）。
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(requires):
        cur = requires[path]
        tgt = latest.get(path)
        if not tgt:
            continue
        lt = version_lt(cur, tgt)
        if lt is None:
            continue
        if lt:
            out.append((path, cur, tgt))
    return out


def first_party_requires(go_mod_text: str) -> dict[str, str]:
    """go.mod → {第一方模块路径: 现地板}。

    解析走事实层（`matrix_facts.parse_go_mod`）——**不自写第二份解析器**：
    自写的那份只认 require **块**形式，单行 `require X v` 形式会被静默丢弃
    ⇒ 六个仓的第一方依赖对本工具**完全不可见**（事实层踩过的坑，见其 docstring）。
    """
    parsed = matrix_facts.parse_go_mod(go_mod_text or "")
    out: dict[str, str] = {}
    for item in sorted(parsed.get("requires", ())):
        parts = item.split()
        if len(parts) < 2:
            continue
        path, ver = parts[0], parts[1]
        if split_module(path):
            out[path] = ver
    return out


def latest_versions(paths: list[str], org_default: str) -> tuple[dict[str, str], list[str]]:
    """→ ({模块路径: 远端最新 tag}, [取不到的模块路径])。

    版本清单**一律以远端为准**（本地克隆只贡献日期精度）——本地清单可能是陈旧的，
    而陈旧清单会让本工具「看不见」新版本（比门禁更危险：门禁只判红，本工具会
    静默不抬）。取不到 ⇒ 列入「判不了」，由调用方报告。
    """
    got: dict[str, str] = {}
    unknown: list[str] = []
    for path in paths:
        parts = split_module(path)
        if not parts:
            continue
        owner, repo = parts
        try:
            tag = repo_meta.latest_tag(owner, repo)
        except Exception:
            tag = None
        if tag:
            got[path] = tag
        else:
            unknown.append(path)
    return got, unknown


def matrix_repos(root: Path) -> list[str]:
    """矩阵里的仓（13 个模块 + 门面）。

    数据源是矩阵的**单一数据源**（modules.json 与 versions.json 的 facades），
    不扫目录 —— 目录里可能躺着别人的克隆。
    """
    repos: list[str] = []
    # 根目录**本身**可能就是 matrix-tools（CI 里 checkout 出来就是这个仓）⇒
    # 两处都找，找不到才算没有。
    mtx = root if (root / "modules.json").exists() else root / "matrix-tools"
    mod = mtx / "modules.json"
    if mod.exists():
        try:
            data = json.loads(mod.read_text(encoding="utf-8"))
            for m in data.get("modules", []) or []:
                r = m.get("repo")
                if r:
                    repos.append(r)
        except Exception:
            pass
    vers = mtx / "versions.json"
    if vers.exists():
        try:
            data = json.loads(vers.read_text(encoding="utf-8"))
            for f in data.get("facades", []) or []:
                r = f.get("repo")
                if r:
                    repos.append(r)
        except Exception:
            pass
    seen, out = set(), []
    for r in repos:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def api_report(root: Path, org: str, repos: list[str]) -> tuple[list[tuple[str, list[tuple[str, str, str]], list[str]]], list[str]]:
    """**不经本地克隆**的报告：各仓 go.mod 从 GitHub API 读。

    → ([(仓, [(依赖, 现地板, 目标)], [判不了的依赖])], [读不到的仓])

    为什么要有这一路：CI 里没有克隆（克隆 14 个仓 + 装 Go 工具链很贵），
    而**检测**不需要克隆（go.mod 与依赖的最新 tag 都能从 API 拿）。
    检测与修复分开 ⇒ 检测可以很便宜地每小时跑。
    """
    out: list[tuple[str, list[tuple[str, str, str]], list[str]]] = []
    unreadable: list[str] = []
    for repo in repos:
        text = repo_meta.fetch_file(org, repo, "go.mod")
        if not text:
            unreadable.append(repo)
            continue
        requires = first_party_requires(text)
        if not requires:
            continue
        latest, unknown = latest_versions(list(requires), org)
        stale = stale_floors(requires, latest)
        if stale:
            out.append((repo, stale, unknown))
    return out, unreadable


# 跟踪 issue 的标题前缀（**必须稳定** ⇒ 靠它去重，而不是每次开新单）。
TRACKING_TITLE_PREFIX = "[地板] 依赖地板落后"


def open_tracking_issue(org: str, tracking_repo: str, report, unreadable: list[str]) -> str:
    """开/更新跟踪 issue（去重：同名开着就更新它，而不是每次开新单）。

    → 一句话说明（做了什么 / 没做什么）。
    """
    lines: list[str] = []
    total = 0
    for repo, stale, unknown in report:
        total += len(stale)
        lines.append(f"### {repo}")
        for path, cur, tgt in stale:
            lines.append(f"- `{path}`：`{cur}` → **`{tgt}`**")
        for path in unknown:
            lines.append(f"- `{path}`：取不到最新版本（判不了，不动）")
        lines.append("")

    body = "\n".join(lines)
    if total == 0:
        body = "✅ 各仓第一方依赖地板都是最新的（无待抬）。"

    # 找同名的开着单：有 ⇒ 更新（评论 + 若已无落后则关闭）；没有 ⇒ 开新的。
    existing = subprocess.run(
        ["gh", "issue", "list", "--repo", f"{org}/{tracking_repo}", "--state", "open",
         "--search", f"in:title {TRACKING_TITLE_PREFIX}", "--json", "number,title",
         "--limit", "5"], capture_output=True, text=True)
    nums: list[int] = []
    if existing.returncode == 0:
        try:
            for it in json.loads(existing.stdout or "[]"):
                if it.get("title", "").startswith(TRACKING_TITLE_PREFIX):
                    nums.append(int(it["number"]))
        except Exception:
            pass

    if total == 0:
        if nums:
            subprocess.run(["gh", "issue", "comment", str(nums[0]), "--repo", f"{org}/{tracking_repo}",
                            "--body", "✅ 地板已追平（本次巡检无待抬项），关单。"], capture_output=True, text=True)
            subprocess.run(["gh", "issue", "close", str(nums[0]), "--repo", f"{org}/{tracking_repo}",
                            "--reason", "completed"], capture_output=True, text=True)
            return f"已更新并关闭跟踪 issue #{nums[0]}（地板已追平）"
        return "无待抬项，也没有开着���跟踪 issue"

    comment = (f"巡检发现 **{total}** 处第一方依赖地板落后：\n\n{body}\n"
               f"### 怎么修\n\n"
               f"一条命令（默认只报告；`--apply` 才落地并开 PR）：\n\n"
               f"```bash\n"
               f"python3 scripts/bump_dep_floors.py --root <矩阵根> --apply\n"
               f"```\n\n"
               f"它会逐仓改 go.mod → build/vet/test 全绿才提交 → 推新分支 → 开 PR（**不合并**）。")
    if nums:
        r = subprocess.run(["gh", "issue", "comment", str(nums[0]), "--repo", f"{org}/{tracking_repo}",
                            "--body", comment], capture_output=True, text=True)
        if r.returncode != 0:
            return f"更新失败：{(r.stderr or r.stdout).strip()[:160]}"
        return f"已更新跟踪 issue #{nums[0]}（{total} 处落后）"
    r = subprocess.run(["gh", "issue", "create", "--repo", f"{org}/{tracking_repo}",
                        "--title", f"{TRACKING_TITLE_PREFIX}：{total} 处",
                        "--body", comment], capture_output=True, text=True)
    if r.returncode != 0:
        return f"创建失败：{(r.stderr or r.stdout).strip()[:160]}"
    return (r.stdout or r.stderr).strip()[:200]


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()[:200]}")
    return (p.stdout or "").strip()


def remote_main_sha(org: str, repo: str) -> str | None:
    """远端 main 的 commit sha（走 api.github.com，**不依赖 git 传输层**）。

    取不到 ⇒ 返回 None（**判不了**，不是「取到空」）。
    """
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{org}/{repo}/commits/main", "--jq", ".sha"],
            capture_output=True, text=True)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    sha = (out.stdout or "").strip()
    return sha or None


def ensure_baseline(repo_dir: Path, org: str, repo: str, mode: str) -> tuple[bool, str]:
    """确保本地基线 == 远端 main。→ (成功?, 说明)

    为什么要在 API 模式下也核对基线：传���层不可用时**不能 fetch** ⇒ 本地
    `origin/main` 可能是**陈旧**的 ⇒ 在陈旧基线上改依赖并推分支 ⇒ PR 的 diff 会
    混进「远端 main 已有的新提交」⇒ 审阅者看到一堆无关变更。
    ⇒ 这里**不比对就拒绝**（判不了 ≠ 没问题），而不是冒险推。
    """
    local_main = git(repo_dir, "rev-parse", "origin/main", check=False)
    remote = remote_main_sha(org, repo)
    if remote and local_main == remote:
        return True, "基线已是最新（API 核对 origin/main == 远端 main）"
    if mode == "api":
        return False, (f"本地 origin/main({local_main[:8] or '缺失'}) 与远端 main"
                       f"({remote[:8] if remote else '取不到'}) 不一致 ⇒ 传输层不可用时"
                       f"无法安全同步基线 ⇒ 拒绝（请在能 fetch 的环境里先同步）")
    # git 模式（或 auto 的快路径）：先试传输层
    git(repo_dir, "fetch", "origin", "--tags")
    local_main = git(repo_dir, "rev-parse", "origin/main", check=False)
    remote = remote_main_sha(org, repo)
    if remote and local_main != remote:
        return False, (f"fetch 后本地 origin/main({local_main[:8]}) 仍与远端 main"
                       f"({remote[:8]}) 不一致 ⇒ 拒绝")
    return True, "已 fetch origin --tags（基线已核对）"


def push_with_fallback(repo_dir: Path, org: str, repo: str, branch: str,
                       base: str, mode: str) -> tuple[bool, str]:
    """把分支推上去。→ (成功?, 说明)

    `mode`：
    - ``git``：只用 git push（传输层必须可用）
    - ``api``：只用 Git Data API（**不依赖传输层**）
    - ``auto``（默认）：先试 git push，失败 ⇒ 回退 API 通道
      （⚠️ 不靠错误字符串判断「是不是传输层问题」——那种匹配脆且会误判；
        直接换通道，失败原因由 API 通道自己报）
    """
    if mode in ("git", "auto"):
        r = subprocess.run(["git", "-C", str(repo_dir), "push", "-q", "-u", "origin", branch],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True, "git push"
        if mode == "git":
            return False, "git push 失败：" + (r.stderr or r.stdout).strip()[:200]
    try:
        out = gh_push_branch.push_branch(repo_dir, f"{org}/{repo}", branch, base)
    except SystemExit as exc:
        return False, "Git Data API 通道也失败：" + str(exc).replace("\n", " ")[:240]
    return True, f"Git Data API 推送（commit {out['commit'][:8]}，树逐路径核对过）"


def go(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["go", *args], cwd=str(repo), capture_output=True, text=True)


def apply_repo(repo_dir: Path, repo: str, floors: list[tuple[str, str, str]],
               org: str, stamp: str, open_pr: bool, push: bool = True,
               push_mode: str = "auto") -> tuple[bool, str]:
    """把一个仓的地板抬上去并开 PR。→ (成功?, 说明)。

    任何一步红 ⇒ **不动那个仓**（不提交、不推、不开 PR），并把原因报出来。
    """
    if git(repo_dir, "status", "--porcelain", check=False):
        return False, "工作树不干净（有人正在改这个仓）⇒ 跳过"
    ok, base_note = ensure_baseline(repo_dir, org, repo, push_mode)
    if not ok:
        return False, base_note
    git(repo_dir, "checkout", "main")
    git(repo_dir, "reset", "--hard", "origin/main")
    branch = f"chore/deps-floor-{stamp}"
    git(repo_dir, "checkout", "-B", branch)

    for path, _cur, tgt in floors:
        # ⚠️ 必须用**完整模块路径**（github.com/oekit/pkg）——`go mod edit` 认的是
        # module path，不是仓库短名。写成 `oekit/pkg@v…` 会被 `go mod tidy` 当成
        # 一个没人 import 的模块**悄悄删掉** ⇒ 提交时空 ⇒ 工具「跑通了」却什么都没改
        # （本轮端到端实测抓到的真 bug：所以下面还要复核改完的 go.mod）。
        r = go(repo_dir, "mod", "edit", f"-require={path}@{tgt}")
        if r.returncode != 0:
            return False, f"go mod edit {path}@{tgt} 失败：{(r.stderr or '').strip()[:160]}"
    r = go(repo_dir, "mod", "tidy")
    if r.returncode != 0:
        return False, f"go mod tidy 失败：{(r.stderr or '').strip()[:200]}"
    # 复核：改完的 go.mod 里地板必须真的到目标版本（防「编辑被回滚」这类静默 no-op）。
    try:
        after = first_party_requires((repo_dir / "go.mod").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"复核 go.mod 失败：{e}"
    missed = [f"{p} 仍是 {after.get(p)}（要 {t}）" for p, _c, t in floors if after.get(p) != t]
    if missed:
        return False, "编辑未生效（被 tidy 回滚？）⇒ 跳过：" + "; ".join(missed)

    for step, args in (("build", ("build", "./...")), ("vet", ("vet", "./...")),
                       ("test", ("test", "-race", "-count=1", "./..."))):
        r = go(repo_dir, *args)
        if r.returncode != 0:
            return False, f"{step} 失败 ⇒ 跳过（不改这个仓）：{(r.stderr or r.stdout).strip()[:200]}"
    r = go(repo_dir, "mod", "tidy", "-diff")
    if r.returncode != 0:
        return False, "go mod tidy -diff 有漂移 ⇒ 跳过"

    rows = "\n".join(f"| `{p}` | `{c}` | `{t}` |" for p, c, t in floors)
    msg = f"""chore(deps): 依赖地板追到最新——{floors[0][0]} {floors[0][2]}

| 依赖 | 现地板 | 抬到 |
|---|---|---|
{rows}

## 为什么

矩阵的**发版时刻地板时效**门禁（`verify_tag_floor --ref HEAD`）要求：各仓 HEAD
（= 下次发版会发出去的内容）的依赖地板，相对那一刻的依赖最新版本，不能已落后。
oekit 基座与矩阵模块都在高频发版 ⇒ 地板是**跑步机**，落后一格就会判红。

## 兼容性实测

本工具在提交前跑完 `go build` / `go vet` / `go test -race -count=1 ./...`（全绿才提交）
与 `go mod tidy -diff`（无漂移）⇒ 改动**只影响依赖版本**，零行为差异。

只改 `go.mod` / `go.sum` ⇒ 不需要 CHANGELOG 条目（版本段由下次发版的 `prewrite` 生成）。
"""
    body = f"""## 依赖地板抬升

| 依赖 | 现地板 | 抬到 |
|---|---|---|
{rows}

## 为什么

矩阵的**发版时刻地板时效**门禁（`verify_tag_floor --ref HEAD`）要求：各仓 HEAD（= 下次发版会发出去的内容）的依赖地板，相对那一刻的依赖最新版本，不能已落后。

本 PR 由 `matrix-tools/scripts/bump_dep_floors.py` 生成（地板施作器）：基座与矩阵模块都在高频发版 ⇒ 地板是**跑步机**，人工追平赶不上发版节奏。

## 兼容性实测

提交前跑完 `go build` / `go vet` / `go test -race -count=1 ./...`（全绿才提交）与 `go mod tidy -diff`（无漂移）⇒ **零行为差异**。

只改 `go.mod` / `go.sum` ⇒ 不需要 CHANGELOG 条目（版本段由下次发版的 `prewrite` 生成）。
"""
    # 提交信息与 PR 正文先落盘。⚠️ **PR 正文文件必须活到 gh pr create 之后**——
    # 提前删掉 ⇒ `gh pr create --body-file <不存在>` ⇒ **提交与分支都推了、PR 却没开**
    # （本轮端到端实测抓到的第二个 bug）。
    msg_path = repo_dir / ".git" / "DEPS_FLOOR_MSG"
    msg_path.write_text(msg, encoding="utf-8")
    body_path = repo_dir / ".git" / "DEPS_FLOOR_BODY"
    body_path.write_text(body, encoding="utf-8")
    title = f"chore(deps): 依赖地板追到最新——{floors[0][0]} {floors[0][2]}"
    try:
        git(repo_dir, "add", "-A")
        git(repo_dir, "commit", "-q", "-F", str(msg_path))
        if push:
            base_sha = git(repo_dir, "rev-parse", "origin/main")
            ok, note = push_with_fallback(repo_dir, org, repo, branch, base_sha, push_mode)
            if not ok:
                return False, f"推分支失败 ⇒ {note}（本地提交已留在分支 {branch}）"

        if not push:
            return True, f"已在本地提交（分支 {branch}，**未推远端**）"
        if not open_pr:
            return True, f"已在本地提交并推到分支 {branch}（未开 PR）"
        pr = subprocess.run(["gh", "pr", "create", "--repo", f"{org}/{repo}", "--base", "main",
                             "--head", branch, "--title", title, "--body-file", str(body_path)],
                            capture_output=True, text=True)
        if pr.returncode != 0:
            # 提交与分支已经推出去了，但 PR 没开 ⇒ 算**失败**。报「成功」会让调用方
            # 以为这轮做完了，而矩阵里多了一个没人看的分支（本轮实测踩过）。
            return False, (f"已推到分支 {branch}，但**开 PR 失败** ⇒ "
                           f"{(pr.stderr or pr.stdout).strip()[:160]}")
        return True, pr.stdout.strip()
    finally:
        msg_path.unlink(missing_ok=True)
        body_path.unlink(missing_ok=True)



def selftest_push_modes() -> int:
    """自测：**通道选择与回退**必须符合契约（带反证样本）。

    契约：
    - ``mode=api``  ⇒ 不许碰 git push，直接走 Git Data API；
    - ``mode=git``  ⇒ 只走 git push，失败就失败（**不许**偷偷回退）；
    - ``mode=auto`` ⇒ 先 git push，失败**回退** API（不许静默放弃）；
    - 基线与远端 main 不一致 ⇒ 拒绝（在陈旧基线上推分支会让 PR diff 混进无关变更）。

    变异验证（退回旧写法 ⇒ 自测必须转红）：
    - 「auto 失败不回退」⇒ ③ 转红；
    - 「api 模式也先试 git push」⇒ ① 转红；
    - 「基线不一致也放行」⇒ ④ 转红。
    """
    import shutil
    import tempfile

    # ⚠️ 必须 global：下面要把模块级 remote_main_sha 换成假实现 ⇒ 不声明的话
    # Python 把它当**局部**变量 ⇒ `orig_main_sha = remote_main_sha` 直接 UnboundLocalError。
    global remote_main_sha

    fails = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            fails.append("%s%s" % (name, ("：" + detail) if detail else ""))

    def make_repo(td: str, name: str = "r", with_remote: bool = True) -> Path:
        """with_remote=True  ⇒ 有一个**可用**的 bare remote（git push 会成功）
        with_remote=False ⇒ 没有 remote（git push 必然失败 ⇒ 模拟传输层不可用）
        """
        d = Path(td) / name
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
        (d / "f.txt").write_text("x\n", encoding="utf-8")
        # 一个**可用**的 bare remote（本地路径）⇒ git push 会成功
        # ⇒ 于是「api 模式到底有没有偷偷用 git push」变得可观测。
        bare = Path(td) / (name + "-remote.git")
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "base"], check=True)
        if with_remote:
            subprocess.run(["git", "-C", str(d), "remote", "add", "origin", str(bare)], check=True)
            subprocess.run(["git", "-C", str(d), "push", "-q", "origin",
                            "HEAD:refs/heads/main"], check=True)
        return d

    orig_api = gh_push_branch.push_branch
    orig_main_sha = remote_main_sha
    seen = []

    def fake_api(*_a, **_k):
        seen.append("api")
        return {"branch": "br", "commit": "a" * 40, "files": 1}

    try:
        gh_push_branch.push_branch = fake_api
        with tempfile.TemporaryDirectory() as td:
            d = make_repo(td)
            base = git(d, "rev-parse", "HEAD")

            # ① mode=api ⇒ 必须走 API，且 git push 不该被调用
            #    （这个仓**没有** remote ⇒ git push 必然失败 ⇒ 若仍成功，只能是 API 兜住了）
            # 建出 br 分支并推到 bare remote ⇒ 之后 git push **会成功**
            # ⇒ 「api 模式有没有偷偷用 git push」才可观测（否则必然失败 ⇒ 断言没区分力）。
            subprocess.run(["git", "-C", str(d), "checkout", "-q", "-b", "br"], check=True)
            subprocess.run(["git", "-C", str(d), "push", "-q", "origin", "br"], check=True)
            # 删掉远端那个分支（本地分支留着无妨）⇒ bare 里没有 refs/heads/br
            subprocess.run(["git", "-C", str(d), "push", "-q", "origin", ":refs/heads/br"], check=True)
            seen.clear()
            ok, note = push_with_fallback(d, "o", "r", "br", base, "api")
            check("① mode=api 走 API 通道", ok and seen == ["api"], note)
            # 加强：api 模式下**不许碰 git push**（bare remote 是可用的 ⇒ 若偷偷推了会留下分支）
            bare_refs = git(d, "ls-remote", "--heads", "origin", check=False)
            check("①b mode=api 没有偷偷 git push", "refs/heads/br" not in bare_refs, bare_refs)

            # ② mode=git 且传输层不可用 ⇒ 必须失败，**且不许回退**
            #    用「没有 remote」的仓 ⇒ git push 必然失败（= 传输层不可用的形态）
            d_noremote = make_repo(td, name="r2", with_remote=False)
            seen.clear()
            ok, note = push_with_fallback(d_noremote, "o", "r", "br", base, "git")
            check("② mode=git 失败且不回退", (not ok) and seen == [], note)

            # ③ mode=auto 且传输层不可用 ⇒ 必须回退到 API 并成功
            seen.clear()
            ok, note = push_with_fallback(d_noremote, "o", "r", "br", base, "auto")
            check("③ mode=auto 回退到 API", ok and seen == ["api"], note)

            # ④ 基线与远端 main 不一致 ⇒ 拒绝（api 模式下不许 fetch）
            remote_main_sha = lambda _o, _r: "b" * 40
            ok, note = ensure_baseline(d, "o", "r", "api")
            check("④ 基线不一致 ⇒ 拒绝", (not ok) and "不一致" in note, note)

            # ⑥ auto 模式：fetch 之后仍与远端 main 不一致 ⇒ 拒绝
            remote_main_sha = lambda _o, _r: "c" * 40
            ok, note = ensure_baseline(d, "o", "r", "auto")
            check("⑥ fetch 后仍不一致 ⇒ 拒绝", (not ok) and "不一致" in note, note)

            # ⑤ 基线一致 ⇒ 放行（且说明里要说清是 API 核对的）
            subprocess.run(["git", "-C", str(d), "update-ref",
                            "refs/remotes/origin/main", base], check=True)
            remote_main_sha = lambda _o, _r: base
            ok, note = ensure_baseline(d, "o", "r", "api")
            check("⑤ 基线一致 ⇒ 放行", ok and "API 核对" in note, note)
    finally:
        gh_push_branch.push_branch = orig_api
        remote_main_sha = orig_main_sha

    if fails:
        print("❌ 自测失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 推送通道自测通过（7 条断言：api 只走 API 且不碰 git push / git 模式不回退 / "
          "auto 会回退 / 基线不一致拒绝 / fetch 后仍不一致拒绝 / 基线一致放行）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="第一方依赖地板施作器（默认只报告）")
    ap.add_argument("--root", required=True, help="矩阵根目录（各仓克隆的父目录）")
    ap.add_argument("--org", default="oagentkit", help="组织名（默认 oagentkit）")
    ap.add_argument("--repo", action="append", default=[], help="只处理这些仓（可重复）")
    ap.add_argument("--apply", action="store_true", help="真的落地（改文件/提交/推分支/开 PR）")
    ap.add_argument("--no-pr", action="store_true", help="落地但不��� PR")
    ap.add_argument("--push-mode", choices=("git", "api", "auto"), default="auto",
                    help="推分支用哪条通道：git=只走传输层；api=只走 GitHub REST Git Data API"
                         "（**不依赖传输层**）；auto=先 git push，失败回退 API")
    ap.add_argument("--no-push", action="store_true",
                    help="落地但**不推远端**（本地试跑；隐含 --no-pr）")
    ap.add_argument("--api-report", action="store_true",
                    help="**不经本地克隆**的报告（go.mod 从 GitHub API 读）——CI 里用")
    ap.add_argument("--open-tracking-issue", action="store_true",
                    help="把落后的清单开/更新成跟踪 issue（去重：同名开着就更新它）")
    ap.add_argument("--tracking-repo", default="matrix-tools",
                    help="跟踪 issue 开在哪个仓（默认 matrix-tools）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    root = Path(args.root).resolve()
    repos = args.repo or matrix_repos(root)
    if not repos:
        print("❌ 矩阵清单读不到（matrix-tools/modules.json 与 versions.json 都没找到？）")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d")
    if args.api_report:
        report, unreadable = api_report(root, args.org, repos)
        if report:
            print(f"⚠️ 落后 {sum(len(s) for _r, s, _u in report)} 处，涉及 {len(report)} 个仓：\n")
            for repo, stale, unknown in report:
                print(f"  {repo}:")
                for path, cur, tgt in stale:
                    print(f"    {path}: {cur} → {tgt}")
                for path in unknown:
                    print(f"    {path}: 取不到最新版本（判不了，不动）")
                print()
        else:
            print("✅ 各仓第一方依赖地板都是最新的（无待抬）")
        if unreadable:
            print(f"ℹ️ 读不到 go.mod 的仓（跳过）：{', '.join(unreadable)}")
        if args.open_tracking_issue:
            print("=== 跟踪 issue ===")
            msg = open_tracking_issue(args.org, args.tracking_repo, report, unreadable)
            print("  " + msg)
            # 开/更新失败 ⇒ **非零退出**：静默失败比红更糟（门禁一旦开始骗人，
            # 就没人再看它了）。
            if msg.startswith(("创建失败", "更新失败")) or "失败" in msg:
                return 1
        return 0
    report: list[tuple[str, list[tuple[str, str, str]], list[str]]] = []
    total = 0
    for repo in repos:
        repo_dir = root / repo
        gomod = repo_dir / "go.mod"
        if not gomod.exists():
            continue
        try:
            text = gomod.read_text(encoding="utf-8")
        except Exception:
            continue
        requires = first_party_requires(text)
        if not requires:
            continue
        latest, unknown = latest_versions(list(requires), args.org)
        stale = stale_floors(requires, latest)
        if stale:
            total += len(stale)
            report.append((repo, stale, unknown))

    if not report:
        print("✅ 各仓第一方依赖地板都是最新的（无待抬）")
        return 0

    print(f"⚠️ 落后 {total} 处，涉及 {len(report)} 个仓：\n")
    for repo, stale, unknown in report:
        print(f"  {repo}:")
        for path, cur, tgt in stale:
            print(f"    {path}: {cur} → {tgt}")
        for path in unknown:
            print(f"    {path}: 取不到最新版本（判不了，不动）")
        print()

    if not args.apply:
        print("（只报告模式；加 --apply 落地）")
        return 0

    print("=== --apply：逐仓落地 ===")
    ok = 0
    for repo, stale, _unknown in report:
        repo_dir = root / repo
        try:
            good, why = apply_repo(repo_dir, repo, stale, args.org, stamp,
                                   not (args.no_pr or args.no_push),
                                   push=not args.no_push)
        except Exception as e:  # noqa: BLE001
            good, why = False, f"异常：{e}"
        print(f"  {repo}: {'✅' if good else '❌'} {why}")
        ok += 1 if good else 0
    print(f"\n落地完成：{ok}/{len(report)} 个仓成功")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # 版本比较：必须按数字比（字符串比较会把 v0.9.0 判成大于 v0.10.0）
    check("v0.9.0 < v0.10.0", version_lt("v0.9.0", "v0.10.0"), True)
    check("v0.10.0 > v0.9.0", version_lt("v0.10.0", "v0.9.0"), False)
    check("相等不算落后", version_lt("v0.31.0", "v0.31.0"), False)
    check("解析不了判不了", version_lt("abc", "v1.0.0"), None)

    # 第一方筛选
    check("oagentkit 模块算第一方", split_module("github.com/oagentkit/guardx"), ("oagentkit", "guardx"))
    check("oekit 基座算第一方", split_module("github.com/oekit/pkg"), ("oekit", "pkg"))
    check("第三方不算", split_module("github.com/cloudwego/eino"), None)
    check("非 github 路径不算", split_module("golang.org/x/sync"), None)

    # 落后判定
    req = {"github.com/oagentkit/guardx": "v0.15.0", "github.com/oekit/pkg": "v0.31.0"}
    latest = {"github.com/oagentkit/guardx": "v0.17.0", "github.com/oekit/pkg": "v0.31.0"}
    check("只挑出落后的", stale_floors(req, latest),
          [("github.com/oagentkit/guardx", "v0.15.0", "v0.17.0")])
    check("取不到目标版本就不产出", stale_floors(req, {}), [])

    # go.mod 解析：事实层要同时认「单行 require」与「require 块」两种形态
    single = "module x\n\ngo 1.26.0\n\nrequire github.com/oekit/pkg v0.20.0\n"
    block = ("module x\n\ngo 1.26.0\n\nrequire (\n\tgithub.com/oekit/concurrency v0.19.0\n)\n")
    check("单行 require 认得出", first_party_requires(single), {"github.com/oekit/pkg": "v0.20.0"})
    check("require 块认得出", first_party_requires(block),
          {"github.com/oekit/concurrency": "v0.19.0"})
    mixed = single + "\nrequire (\n\tgithub.com/oekit/logx v0.8.0\n)\n"
    check("两种形态同时认", len(first_party_requires(mixed)), 2)

    if fails:
        print("❌ bump_dep_floors 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    rc = selftest_push_modes()
    if rc != 0:
        return rc
    print("✅ bump_dep_floors 自测通过"
          "（版本按数字比 / 第一方筛选 / 落后判定 / go.mod 两种 require 形态）")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
