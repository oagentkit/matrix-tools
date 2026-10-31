#!/usr/bin/env python3
"""把本地分支推到远端 —— 走 GitHub REST **Git Data API**，不依赖 git 传输层。

## 为什么需要它（issue matrix-tools#166）

`bump_dep_floors.py --apply` 这条施作器的收尾是「改 go.mod → 提交 → **推分支** → 开 PR」。
其中「推分支」走的是 `git push` ⇒ **依赖 git 传输层**（GitHub 的 git smart-HTTP 端点或 SSH）。
实测在某些出口下 `github.com` 主站的 TLS 被掐（443/22/SSH 全断）⇒ `git push` 永远失败
⇒ 施作器把「改完 go.mod」和「推得出去」耦合在一条命令里 ⇒ 前半段可用、后半段不可用。

而 `api.github.com` 在同一出口下**可达** ⇒ Git Data API 完全绕过传输层：
`POST /git/blobs` → `POST /git/trees` → `POST /git/commits` → `POST|PATCH /git/refs`。

## 保真性（内容寻址 ⇒ 可逐层自校验，任何一层不一致就中止）

① 上传 blob 后 GitHub 返回的 sha 必须 == 本地 `git hash-object`；
② 重建 tree 的 sha 必须 == 本地 `git rev-parse <c>^{tree}`；
③ 重建 commit 的 sha 必须 == 本地 commit sha（author/committer/时区/message 全同）；
④ 推完后用 `GET /git/trees/<head>?recursive=1` 与本地 `git ls-tree -r HEAD`
   **逐路径逐 sha** 比对。

⇒ 全部成立即证明「远端分支 == 本地 HEAD 逐字节等价」，且**不依赖任何传输层**。

## 已知边界（别把它当通用 git 客户端）

- **base 必须是远端已有、本地也有的 commit**（例如刚 fetch 过的 `origin/main`）。
  此时 parent 相同 ⇒ 内容寻址 ⇒ **远端 commit sha 与本地逐字相同**。
  若 base 只有远端有（本地没有对象），重建出的 commit sha 必然不同 ⇒ 本脚本**拒绝**，
  而不是给你一个「树相同但 sha 不同」的弱对账。
- **逐个 commit 重建，不压平**：base..HEAD 若有多个 commit，本脚本会**逐个**重建
  （parent 串成与本地一致的链）⇒ 每个 commit 的远端 sha 都与本地**逐字相同**。
  ⚠️ 压平成一个 commit 的话 parent 变了 ⇒ sha 必不同 ⇒ 最强对账失效
  （实测：这是本脚本初版在真实环境下的缺陷——自测只覆盖单 commit ⇒ 漏过；
  本轮推 3 个 commit 时被自己的 sha 校验拦下：拦得对，但功能不可用）。
- **只支持线性链**：合并提交（多个父）会被拒绝（明确报错，不静默改写历史）。
- **GitHub 的 squash/merge commit 带 PGP 签名** ⇒ 本脚本不去改写已有远端提交，
  只推「新分支」。⚠️（2026-10-31 更正）带签名的 commit 其实**能**逐字重建 ——
  用 API 的 `verification.payload` + `verification.signature`，规则见 `gh_git_api.py`；
  要把远端 commit 搬回本地时走 `gh_sync_commit.py`（拉的一侧）。
- `gh pr create` 本来就走 API ⇒ 不受传输层影响 ⇒ 本脚本只负责「把分支推上去」。

## 用法

    python3 scripts/gh_push_branch.py --repo-dir <本地仓> --repo <owner/repo> \
        --branch <分支> [--base <commit，默认 origin/main>] [--dry-run]
    python3 scripts/gh_push_branch.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 取数层（token / 代理 / REST）与本地 git 事实层**上移到共享模块**：
# 推送侧与拉取侧共用一份 ⇒ 两边不会各写一份后**悄悄漂移**。
from gh_git_api import API_ROOT, api, git, git_bytes, local_tree_map, tree_of  # noqa: E402

ROOT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


# ── 取数层 ──────────────────────────────────────────────────────────────

# ⚠️ 取数层已上移到 `gh_git_api.py`（见上方 import）：
#    token / 代理绕过 / REST 调用 / 本地 git 事实 / 树映射都在那里，
#    拉取侧的 `gh_sync_commit.py` 用的是同一份。

# （取数层的实现已全部上移到 gh_git_api.py：token / opener / api / git /
#  git_bytes / tree_of / local_tree_map —— 推送侧与拉取侧共用同一份。）


# ── 本地 git 事实层 ────────────────────────────────────────────────────

def commit_meta(repo: Path, sha: str) -> dict:
    """取 commit 的作者/提交者/消息。用 %aI/%cI（**带时区**的 ISO）——
    GitHub 保留传入的时区 ⇒ 重建出的 commit sha 与本地逐字相同。"""
    fmt = "%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
    raw = git(repo, "show", "-s", "--format=" + fmt, sha).split("\x00")
    an, ae, ai, cn, ce, ci, msg = raw[:7]
    return {
        "author": {"name": an, "email": ae, "date": ai},
        "committer": {"name": cn, "email": ce, "date": ci},
        "message": msg if msg.endswith("\n") else msg + "\n",
    }


def changed_paths(repo: Path, base: str, head: str) -> list:
    """base..head 的变更：[(status, path)]，status ∈ M/A/D/T。"""
    out = git(repo, "diff-tree", "-r", "--no-commit-id", "--name-status", base, head)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append((parts[0], parts[-1]))
    return rows


def file_mode(repo: Path, commit: str, path: str) -> str:
    return git(repo, "ls-tree", commit, "--", path).split()[0].strip()


# ── 推送通道 ────────────────────────────────────────────────────────────

def push_branch(repo_dir: Path, repo: str, branch: str, base: str,
                api_root: str = API_ROOT, dry_run: bool = False) -> dict:
    """把本地 HEAD（相对 base 的变更）推到远端 `branch`。→ 摘要。"""
    head = git(repo_dir, "rev-parse", "HEAD")

    # 0) base 必须远端已有（且本地也有对象，才能算变更集与建 tree）
    # 一次 GET 同时拿到 base 的 sha 与 tree（`/git/commits/<sha>` 两者都给）
    # ⇒ 既校验「base 远端存在」，又拿到建 tree 要用的 base_tree。
    base_info = api("GET", "/repos/%s/git/commits/%s" % (repo, base), api_root=api_root)
    if base_info["sha"] != base:
        raise SystemExit("远端 %s/%s 的 %s 解析成 %s —— 拒绝推送"
                         % (repo, branch, base, base_info["sha"]))
    base_tree = base_info["tree"]["sha"]
    if base_tree != tree_of(repo_dir, base):
        raise SystemExit("本地 %s 的树与远端不一致（本地=%s 远端=%s）—— 拒绝推送"
                         % (base, tree_of(repo_dir, base), base_tree))

    # 1) 取 base..HEAD 的 commit 链（从旧到新），**逐个**重建
    #
    # ⚠️ 必须逐个 commit 重建，不能把整段变更压成一个 commit：
    #    内容寻址下，commit 的 sha 取决于它的 parent ⇒ 压成一个 ⇒ parent 与本地不同
    #    ⇒ **sha 必然不同** ⇒「远端 sha == 本地 sha」这个最强对账永远失败。
    #    实测（2026-10-31）：本仓库修好这个缺陷前，推 3 个 commit 会被自己的
    #    sha 校验拦下（远端 sha ≠ 本地 sha）—— 拦得对，但功能不可用。
    #    ⇒ 逐个重建 ⇒ 链上的 parent 与本地**逐个相同** ⇒ 每个 commit 的 sha 都相同。
    commits = git(repo_dir, "rev-list", "--reverse", "%s..%s" % (base, head)).split()
    if not commits:
        commits = [head]

    prev_tree = base_tree
    prev_sha = base
    for c in commits:
        parents = git(repo_dir, "rev-list", "--parents", "-n", "1", c).split()[1:]
        if len(parents) != 1:
            raise SystemExit("commit %s 有 %d 个父（合并提交）——本通道只支持线性链"
                             % (c[:12], len(parents)))
        if parents[0] != prev_sha:
            raise SystemExit("commit %s 的父 %s 与链上前一个 %s 不符——链条不连续"
                             % (c[:12], parents[0][:12], prev_sha[:12]))

        # 1a) 逐个上传 blob，并核对 sha（内容寻址 ⇒ 必须逐字相等）
        entries = []
        for status, path in changed_paths(repo_dir, parents[0], c):
            if status == "D":
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            content = git_bytes(repo_dir, "show", c + ":" + path)
            local_sha = subprocess.run(
                ["git", "-C", str(repo_dir), "hash-object", "-t", "blob", "--stdin"],
                input=content, capture_output=True, check=True).stdout.decode().strip()
            resp = api("POST", "/repos/%s/git/blobs" % repo,
                       {"content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64"}, api_root=api_root)
            if resp["sha"] != local_sha:
                raise SystemExit("blob sha 不一致 %s：远端=%s 本地=%s"
                                 % (path, resp["sha"], local_sha))
            entries.append({"path": path, "mode": file_mode(repo_dir, c, path),
                            "type": "blob", "sha": resp["sha"]})

        # 1b) 建 tree（空提交 ⇒ GitHub 拒绝 `tree: []` ⇒ 沿用父树）
        if entries:
            tree_sha = api("POST", "/repos/%s/git/trees" % repo,
                           {"base_tree": prev_tree, "tree": entries},
                           api_root=api_root)["sha"]
        else:
            tree_sha = prev_tree
        want_tree = tree_of(repo_dir, c)
        if tree_sha != want_tree:
            raise SystemExit("tree sha 不一致（commit %s）：远端=%s 本地=%s"
                             % (c[:12], tree_sha, want_tree))

        # 1c) 建 commit（parent = 链上前一个 ⇒ sha 应与本地逐字相同）
        meta = commit_meta(repo_dir, c)
        commit_resp = api("POST", "/repos/%s/git/commits" % repo,
                          {"message": meta["message"], "tree": tree_sha,
                           "parents": [prev_sha], "author": meta["author"],
                           "committer": meta["committer"]}, api_root=api_root)
        if commit_resp["sha"] != c:
            raise SystemExit("commit sha 不一致：远端=%s 本地=%s"
                             % (commit_resp["sha"], c))
        prev_tree = tree_sha
        prev_sha = commit_resp["sha"]
    new_sha = prev_sha

    if dry_run:
        return {"branch": branch, "commit": new_sha, "dry_run": True}

    # 4) 落 ref（不存在则创建，存在则更新）
    exists = api("GET", "/repos/%s/git/ref/heads/%s" % (repo, branch),
                 api_root=api_root, allow_404=True)
    if exists is None:
        api("POST", "/repos/%s/git/refs" % repo,
            {"ref": "refs/heads/" + branch, "sha": new_sha}, api_root=api_root)
    else:
        api("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, branch),
            {"sha": new_sha, "force": True}, api_root=api_root)

    # 5) 独立判据：远端树 vs 本地 HEAD 树，逐路径逐 sha
    remote = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (repo, new_sha),
                 api_root=api_root)["tree"]
    remote_map = {e["path"]: (e["mode"], e["sha"]) for e in remote if e["type"] == "blob"}
    local_map = local_tree_map(repo_dir, "HEAD")
    if remote_map != local_map:
        only_remote = sorted(set(remote_map) - set(local_map))
        only_local = sorted(set(local_map) - set(remote_map))
        diff = sorted(p for p in set(local_map) & set(remote_map)
                      if local_map[p] != remote_map[p])
        raise SystemExit("远端树与本地 HEAD 不一致：仅远端=%s 仅本地=%s 内容不同=%s"
                         % (only_remote[:8], only_local[:8], diff[:8]))
    return {"branch": branch, "commit": new_sha, "files": len(local_map)}


# ── 自测（带反证样本）──────────────────────────────────────────────────

def _obj_exists(repo: Path, rev: str) -> bool:
    """本地是否有这个对象（假服务对未知 sha 必须回 404，不能崩）。"""
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", rev],
                       capture_output=True)
    return p.returncode == 0


def _selftest() -> int:
    """端到端自测：用**本地假 GitHub** 跑一遍真 HTTP 往返。

    为什么用真 HTTP 而不是 mock 掉 api()：本脚本最易错的地方正是
    「GitHub REST 的调用语义」（base_tree 怎么用、删除文件怎么写、ref 更新怎么判存废），
    mock 掉 HTTP 层就等于把最该验的部分绕过去了。

    假服务**委托本地 git** 算 sha ⇒ 客户端若算错，sha 就对不上 ⇒ 断言能抓住。
    """
    import http.server
    import threading
    import tempfile

    fails = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    class FakeGitHub(http.server.BaseHTTPRequestHandler):
        """最小 GitHub git-data 端点。sha 一律委托本地 git 算（= 真值）。"""

        repo_dir = None
        bad_blob_sha = False
        bad_tree_sha = False
        bad_commit_sha = False
        bad_tree_content = False
        bad_base_tree = False
        # ⚠️ 记录每次 POST /git/commits 的请求体 ⇒ 自测据此断言
        #    「**逐个** commit 重建、parent 链与本地一致」。
        #    （此前自测只有**单 commit** ⇒ 「把整段压成一个 commit」的缺陷漏过了。）
        commit_posts = []
        # ⚠️ 必须 HTTP/1.1：默认的 HTTP/1.0 每次响应后断连 ⇒ urllib 复用连接时报
        # RemoteDisconnected（自测第一版就栽在这）。
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):  # 静音
            pass

        def handle_error(self, request, client_address):
            import traceback
            traceback.print_exc()

        def _guard(self, fn):
            """包住 handler：**任何**异常（含 SystemExit）都要留下痕迹。

            ⚠️ socketserver 的 `handle_error` 只处理 `Exception` ⇒ `SystemExit`
            会**直接杀掉服务线程** ⇒ 客户端只看到「连接被关」，日志里什么都没有
            ⇒ 排查成本极高。⇒ 这里兜住全部异常并回 500。
            """
            import traceback
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001（含 SystemExit）
                traceback.print_exc()
                try:
                    self._json({"message": "fake server error: %r" % (exc,)}, code=500)
                except Exception:
                    pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._guard(self._do_GET)

        def _do_GET(self):
            path = self.path.split("?")[0]
            parts = [p for p in path.split("/") if p]
            if "commits" in parts and len(parts) >= 5:
                sha = parts[-1]
                if not _obj_exists(FakeGitHub.repo_dir, sha):
                    return self._json({"message": "Not Found"}, code=404)
                raw = git_bytes(FakeGitHub.repo_dir, "cat-file", "commit", sha)
                head = raw.decode("utf-8")
                tree = git(FakeGitHub.repo_dir, "rev-parse", sha + "^{tree}")
                if FakeGitHub.bad_base_tree:
                    tree = "5" * 40
                meta = commit_meta(FakeGitHub.repo_dir, sha)
                return self._json({
                    "sha": sha, "tree": {"sha": tree},
                    "parents": [{"sha": p} for p in git(
                        FakeGitHub.repo_dir, "rev-parse", sha + "^@").split()],
                    "message": meta["message"].rstrip("\n"),
                    "author": {"name": meta["author"]["name"],
                               "email": meta["author"]["email"],
                               "date": meta["author"]["date"]},
                    "committer": {"name": meta["committer"]["name"],
                                  "email": meta["committer"]["email"],
                                  "date": meta["committer"]["date"]},
                    "_raw": head,
                })
            if "trees" in parts and len(parts) >= 6:
                sha = parts[-1]
                if not _obj_exists(FakeGitHub.repo_dir, sha):
                    return self._json({"message": "Not Found"}, code=404)
                out = []
                for line in git(FakeGitHub.repo_dir, "ls-tree", "-r", sha).splitlines():
                    m, p = line.split("\t", 1)
                    mode, kind, s = m.split()
                    out.append({"path": p, "mode": mode, "type": kind, "sha": s})
                if FakeGitHub.bad_tree_content and out:
                    # 让**内容**与本地不同（sha 仍合法）⇒ 只可能被最后那道
                    # 「远端树 vs 本地 HEAD 树」逐路径比对抓住。
                    out[0] = dict(out[0], sha="4" * 40)
                return self._json({"sha": sha, "tree": out})
            if "refs" in parts:
                return self._json({"ref": "refs/heads/x", "object": {"sha": "0" * 40}},
                                  code=404)
            return self._json({"message": "not found"}, code=404)

        def do_POST(self):
            self._guard(self._do_POST)

        def _do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            path = self.path.split("?")[0]
            parts = [p for p in path.split("/") if p]
            if parts[-1] == "blobs":
                content = base64.b64decode(body["content"])
                sha = subprocess.run(
                    ["git", "-C", str(FakeGitHub.repo_dir), "hash-object", "-t", "blob", "--stdin"],
                    input=content, capture_output=True, check=True).stdout.decode().strip()
                if FakeGitHub.bad_blob_sha:
                    sha = "1" * 40
                return self._json({"sha": sha})
            if parts[-1] == "trees":
                # tree 是**增量**（base_tree + entries）⇒ 必须合并后再算 sha。
                # 用临时索引做（等价于 Git 内部做法）：
                #   read-tree base_tree → update-index 各条目 → write-tree
                idx = str(FakeGitHub.repo_dir / ".git" / "fake-index")
                if os.path.exists(idx):
                    os.remove(idx)
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                rd = subprocess.run(["git", "-C", str(FakeGitHub.repo_dir), "read-tree",
                                     body.get("base_tree") or ""], env=env, capture_output=True)
                if rd.returncode != 0:
                    return self._json({"message": "bad base_tree"}, code=422)
                for e in (body.get("tree") or []):
                    if e.get("sha") is None:
                        subprocess.run(["git", "-C", str(FakeGitHub.repo_dir), "update-index",
                                        "--force-remove", "--", e["path"]], env=env,
                                       capture_output=True)
                    else:
                        subprocess.run(["git", "-C", str(FakeGitHub.repo_dir), "update-index",
                                        "--add", "--cacheinfo",
                                        "%s,%s,%s" % (e["mode"], e["sha"], e["path"])],
                                       env=env, capture_output=True)
                wt = subprocess.run(["git", "-C", str(FakeGitHub.repo_dir), "write-tree"],
                                    env=env, capture_output=True)
                if wt.returncode != 0:
                    return self._json({"message": "bad tree"}, code=422)
                sha = wt.stdout.decode().strip()
                if FakeGitHub.bad_tree_sha:
                    sha = "2" * 40
                return self._json({"sha": sha})
            if parts[-1] == "commits":
                FakeGitHub.commit_posts.append(body)
                lines = ["tree %s" % body["tree"]]
                for p in body.get("parents", []):
                    lines.append("parent %s" % p)
                for role in ("author", "committer"):
                    d = body[role]
                    stamp = _iso_to_git(d["date"])
                    lines.append("%s %s <%s> %s" % (role, d["name"], d["email"], stamp))
                raw = ("\n".join(lines) + "\n\n" + body["message"]).encode()
                sha = subprocess.run(
                    ["git", "-C", str(FakeGitHub.repo_dir), "hash-object", "-t", "commit", "--stdin"],
                    input=raw, capture_output=True, check=True).stdout.decode().strip()
                if FakeGitHub.bad_commit_sha:
                    sha = "3" * 40
                return self._json({"sha": sha})
            if parts[-1] == "refs":
                return self._json({"ref": body["ref"], "object": {"sha": body["sha"]}})
            return self._json({"message": "not found"}, code=404)

        def do_PATCH(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            return self._json({"ref": self.path, "object": {"sha": body.get("sha")}})

    with tempfile.TemporaryDirectory() as td:
        repo_dir = Path(td) / "demo"
        repo_dir.mkdir()
        subprocess.run(["git", "-C", str(repo_dir), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
        (repo_dir / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "base"], check=True)
        base = git(repo_dir, "rev-parse", "HEAD")
        (repo_dir / "a.txt").write_text("two\n", encoding="utf-8")
        (repo_dir / "b.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "change"], check=True)
        # ⚠️ **再补两个 commit** ⇒ 推的是一条**链**（3 个 commit）
        #    （此前自测只有**单 commit** ⇒ 「把整段变更压成一个 commit」的缺陷漏过了：
        #      压平时 parent 与本地不同 ⇒ sha 必不同 ⇒ 真实环境里功能不可用）
        chain = [git(repo_dir, "rev-parse", "HEAD")]
        for i in (1, 2):
            (repo_dir / ("c%d.txt" % i)).write_text("chain-%d\n" % i, encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-q",
                            "-m", "chain %d" % i], check=True)
            chain.append(git(repo_dir, "rev-parse", "HEAD"))
        head = chain[-1]

        FakeGitHub.repo_dir = repo_dir
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        api_root = "http://127.0.0.1:%d" % port

        try:
            # ① 正常路径：推一条**3 个 commit 的链** ⇒ 远端 commit sha 必须 == 本地 HEAD sha
            FakeGitHub.commit_posts.clear()
            out = push_branch(repo_dir, "o/r", "feat/x", base, api_root=api_root)
            check("① 远端 commit sha == 本地 HEAD sha", out["commit"], head)
            # ①b ⚠️ 必须**逐个** commit 重建（不能压平成一个）⇒ parent 链与本地一致
            check("①b 逐个 commit 重建（不是压平成一个）",
                  len(FakeGitHub.commit_posts), len(chain))
            for i, body in enumerate(FakeGitHub.commit_posts):
                want_parent = base if i == 0 else chain[i - 1]
                check("①b 第 %d 个 commit 的 parent 与本地一致" % (i + 1),
                      body["parents"][0], want_parent)
                check("①b 第 %d 个 commit 的 tree 与本地一致" % (i + 1),
                      body["tree"], git(repo_dir, "rev-parse", chain[i] + "^{tree}"))

            # ② 反证：blob sha 对不上时必须**中止**（不能把错 blob 推上去）
            FakeGitHub.bad_blob_sha = True
            try:
                push_branch(repo_dir, "o/r", "feat/y", base, api_root=api_root)
                fails.append("② blob sha 不一致时没有中止")
            except SystemExit as exc:
                if "blob sha 不一致" not in str(exc):
                    fails.append("② 中止原因不对：%s" % str(exc)[:120])
            FakeGitHub.bad_blob_sha = False

            # ③ base 不存在（远端 404）⇒ 必须报错，不是静默
            try:
                push_branch(repo_dir, "o/r", "feat/z", "0" * 40, api_root=api_root)
                fails.append("③ base 不存在时没有报错")
            except SystemExit as exc:
                if "404" not in str(exc):
                    fails.append("③ base 不存在时的报错不对（应报 404）：%s" % str(exc)[:120])

            # ④ 反证：tree sha 对不上 ⇒ 必须中止
            FakeGitHub.bad_tree_sha = True
            try:
                push_branch(repo_dir, "o/r", "feat/t", base, api_root=api_root)
                fails.append("④ tree sha 不一致时没有中止")
            except SystemExit as exc:
                if "tree sha 不一致" not in str(exc):
                    fails.append("④ 中止原因不对：%s" % str(exc)[:120])
            FakeGitHub.bad_tree_sha = False

            # ⑤ 反证：commit sha 对不上 ⇒ 必须中止
            FakeGitHub.bad_commit_sha = True
            try:
                push_branch(repo_dir, "o/r", "feat/u", base, api_root=api_root)
                fails.append("⑤ commit sha 不一致时没有中止")
            except SystemExit as exc:
                if "commit sha 不一致" not in str(exc):
                    fails.append("⑤ 中止原因不对：%s" % str(exc)[:120])
            FakeGitHub.bad_commit_sha = False

            # ⑦ 反证：远端 base 的树与本地不同 ⇒ 预检必须中止（否则会在错误基线上推）
            FakeGitHub.bad_base_tree = True
            try:
                push_branch(repo_dir, "o/r", "feat/w", base, api_root=api_root)
                fails.append("⑦ 本地/远端 base 树不一致时没有中止")
            except SystemExit as exc:
                if "树与远端不一致" not in str(exc):
                    fails.append("⑦ 中止原因不对：%s" % str(exc)[:120])
            FakeGitHub.bad_base_tree = False

            # ⑥ 反证：远端树**内容**与本地不同（sha 合法）⇒ 逐路径比对必须抓住
            FakeGitHub.bad_tree_content = True
            try:
                push_branch(repo_dir, "o/r", "feat/v", base, api_root=api_root)
                fails.append("⑥ 远端树与本地不一致时没有中止")
            except SystemExit as exc:
                if "远端树与本地 HEAD 不一致" not in str(exc):
                    fails.append("⑥ 中止原因不对：%s" % str(exc)[:120])
            FakeGitHub.bad_tree_content = False
        finally:
            srv.shutdown()

    if fails:
        print("❌ 自测失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 自测通过（7 条断言：正常路径 sha 全等 / blob·tree·commit 三处 sha 不一致必中止 / "
          "base 不存在必报 404 / 本地与远端 base 树不一致必中止 / 远端树内容不一致必中止）")
    return 0


def _iso_to_git(iso: str) -> str:
    """ISO8601（带偏移）→ Git 的「<unix ts> <+0800>」。

    ⚠️ 用 `fromisoformat` 而不是正则挑格式：正则挑不中就抛异常 ⇒ 在**服务端**里
    抛 `SystemExit` ⇒ `handle_error` **不捕获**（它只处理 Exception）⇒ 服务线程直接死
    ⇒ 客户端只看到「连接被关」，日志里什么都没有（2026-10-31 的 CI 红的可疑路径）。
    """
    import datetime
    text = iso.strip()
    if text.endswith("Z"):          # fromisoformat 在 3.11 前不认 Z
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit("认不出的日期格式：%r（%s）" % (iso, exc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    off = dt.utcoffset() or datetime.timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%d %s%02d%02d" % (int(dt.timestamp()), sign, total // 3600, (total % 3600) // 60)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 GitHub Git Data API 推分支（不依赖 git 传输层）")
    ap.add_argument("--repo-dir", help="本地仓目录")
    ap.add_argument("--repo", help="owner/repo")
    ap.add_argument("--branch", help="要推的分支名")
    ap.add_argument("--base", default="origin/main", help="基线 commit（默认 origin/main）")
    ap.add_argument("--dry-run", action="store_true", help="只算不推")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not (args.repo_dir and args.repo and args.branch):
        ap.error("需要 --repo-dir / --repo / --branch（或用 --selftest）")
    repo_dir = Path(args.repo_dir).resolve()
    base = git(repo_dir, "rev-parse", args.base)
    out = push_branch(repo_dir, args.repo, args.branch, base, dry_run=args.dry_run)
    print("✅ 已推 %s@%s → %s（%d 个文件）"
          % (args.repo, out["branch"], out["commit"][:8], out.get("files", 0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
