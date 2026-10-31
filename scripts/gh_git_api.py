#!/usr/bin/env python3
"""GitHub **Git Data API** 的共享取数层 —— 推送侧与拉取侧共用同一套事实层。

## 为什么要有这一层（2026-10-31）

把本地状态与远端 GitHub 对齐是同一件事的两面：

- `gh_push_branch.py`（**推**）：本地 HEAD → 远端新分支；
- `gh_sync_commit.py`（**拉**）：远端 commit → 本地对象库。

两侧都要用到同一批**事实层**能力：取 token、走代理、读 GitHub REST、读本地 git 对象、
算树、**逐字重建 commit 对象**。不抽出来就会各写一份，然后**悄悄漂移**。

实测形态：本轮开工前，仓库外的临时脚本 `.agent-tools/push_via_api.py`（247 行）
与仓内 `gh_push_branch.py`（601 行）就是两份实现，行为已经不一样了
（临时版没有 blob/tree/commit 三层 sha 校验）⇒ 临时版被删，能力收进仓内。

## 本模块**不含判定**

它只做三件事：取事实、搬字节、做**字节级自校验**（sha 必须逐字相等）。
「这次对齐算不算成功」住在各自的脚本里（判定不可共享，见 `AGENTS.md`）。

## 逐字重建 commit 的规则（实测得出，踩坑记录）

GitHub 自己创建的 commit（squash / merge / web-flow 签名）**带 PGP 签名**，
其对象比普通 commit 多一个 `gpgsig` 头。要在本地重建出**同一个 sha**，必须拿到
API 的 `verification.payload` + `verification.signature` 两个字段：

- `payload` = 去掉 `gpgsig` 头后的 commit 正文；它**末尾没有 `\\n`**，
  而 GitHub 的 squash commit 对象**本身也没有末尾换行** ⇒ 重建时**不要**补换行；
- `signature` 按 `"\\n"` 切分（**保留末尾空元素**，它对应对象里的 `" "` 行）：
  首行前缀 `"gpgsig "`，其余行前缀 `" "`，**空行也写成 `" "`**（单空格）；
- ⚠️ `git cat-file commit` 的输出**会被 git 补一个末尾换行**
  ⇒ 判断真实对象字节数要用 `git cat-file -s`，不能拿输出长度当大小。

⚠️ **无签名的 commit 无法从 API 元信息重建**：API 只给 ISO 日期（UTC），
而 git 对象里要的是「时间戳 + 原始时区」⇒ 时区不可得 ⇒ sha 必然不同。
⇒ 本模块只支持带签名的 commit（那正是「本地 fetch 不到的那一批」）；
无签名的一律**拒绝**，而不是给你一个 sha 不同的弱对账。

## 用法

    from gh_git_api import api, git, sync_commit
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"


# ── 取数层：token / 代理 / REST ────────────────────────────────────────

def token(api_root: str = API_ROOT) -> str:
    """GitHub token：先看环境变量，再退到 `gh auth token`（gh 已登录时可用）。

    ⚠️ **本地自测的假服务不校验认证** ⇒ 那台机器上没登录 gh 也得能跑自测
    （实测：CI 上没有 gh 认证 ⇒ 自测直接崩 ⇒ 2026-10-31 的 CI 红的真因）。
    ⇒ 对本地 api_root 退化成占位 token；对真实 GitHub 仍然**必须**有 token。
    """
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    except OSError:  # gh 都没装
        p = None
    if p is not None and p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    host = urllib.parse.urlparse(api_root).hostname
    if host in ("127.0.0.1", "localhost", "::1"):
        return "local-selftest-token"
    raise SystemExit("拿不到 GitHub token：设 GITHUB_TOKEN，或先 `gh auth login`")


def opener(api_root: str = API_ROOT):
    """本地地址（自测的假服务）**必须绕过代理**。

    否则 HTTP_PROXY/HTTPS_PROXY 会把 127.0.0.1 的请求也塞给代理 ⇒ 请求到不了本地服务
    （自测第一版就栽在这：报 RemoteDisconnected）。
    非本地地址则用默认 opener ⇒ 读标准代理环境变量（生产环境要靠它出网）。
    """
    host = urllib.parse.urlparse(api_root).hostname
    if host in ("127.0.0.1", "localhost", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def api(method: str, path: str, body=None, api_root: str = API_ROOT,
        allow_404: bool = False):
    """调 GitHub REST。代理走标准环境变量（HTTPS_PROXY）⇒ urllib 默认生效。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(api_root + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token(api_root))
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener(api_root).open(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and allow_404:
            return None
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit("HTTP %s %s\n%s" % (exc.code, path, detail[:1500]))
    return json.loads(raw) if raw else None


# ── 本地 git 事实层 ────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("git %s 失败：%s" % (" ".join(args),
                                            (p.stderr or p.stdout).strip()[:300]))
    return (p.stdout or "").strip()


def git_bytes(repo: Path, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if p.returncode != 0:
        raise SystemExit("git %s 失败：%s" % (" ".join(args),
                                            p.stderr.decode("utf-8", "replace")[:300]))
    return p.stdout


def has_object(repo: Path, sha: str) -> bool:
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha],
                       capture_output=True)
    return p.returncode == 0


def tree_of(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", rev + "^{tree}")


def local_tree_map(repo: Path, rev: str) -> dict:
    """{path: (mode, sha)}"""
    out: dict = {}
    for line in git(repo, "ls-tree", "-r", rev).splitlines():
        meta, path = line.split("\t", 1)
        mode, _kind, sha = meta.split()
        out[path] = (mode, sha)
    return out


# ── commit 对象：逐字重建 ──────────────────────────────────────────────

def commit_object_bytes(payload: str, signature: str) -> bytes:
    """用 GitHub 的 verification 字段**逐字**重建原 commit 对象。

    ⚠️ 三个坑（缺一即 sha 对不上）：
      ① `payload` 末尾**没有** `\\n`，且 GitHub 的 squash commit 对象**本身也没有**
         末尾换行 ⇒ 重建时**不要**补；
      ② `signature` 按 `"\\n"` 切分要**保留末尾空元素**（对应对象里的 `" "` 行）；
      ③ 空行写成**单空格** `" "`，不是真空行。
    """
    head, rest = payload.split("\n\n", 1)
    sig_lines = signature.split("\n")
    block = ["gpgsig " + sig_lines[0]] + [(" " + ln) if ln else " " for ln in sig_lines[1:]]
    return (head + "\n" + "\n".join(block) + "\n\n" + rest).encode("utf-8")


def sync_commit(repo_dir: Path, owner_repo: str, sha: str,
                api_root: str = API_ROOT) -> dict:
    """把远端 commit **逐字**搬回本地对象库。→ 摘要。

    前提（不满足就**拒绝**，不给弱对账）：
      · 该 commit **带签名**（否则时区不可得 ⇒ 无法逐字重建）；
      · 它的 **parent 本地已有**（否则本地无法接上这条历史链）。
    树缺失时自动补：下载缺 blob → 临时索引构造树 → **校验 tree sha 逐字相同**。
    """
    if has_object(repo_dir, sha):
        return {"sha": sha, "skipped": True, "reason": "本地已有该对象"}

    info = api("GET", "/repos/%s/git/commits/%s" % (owner_repo, sha), api_root=api_root)
    ver = info.get("verification") or {}
    payload, signature = ver.get("payload"), ver.get("signature")
    if not payload or not signature:
        raise SystemExit(
            "远端 commit %s 没有 verification 签名 ⇒ 无法逐字重建"
            "（无签名 commit 的原始时区不可得，sha 必然不同）——拒绝弱对账" % sha)
    for parent in info.get("parents", []):
        if not has_object(repo_dir, parent["sha"]):
            raise SystemExit(
                "parent %s 本地不存在 ⇒ 无法逐字重建 %s（请按从旧到新的顺序同步）"
                % (parent["sha"], sha))

    tree_sha = info["tree"]["sha"]
    ensure_tree(repo_dir, owner_repo, tree_sha, api_root)

    raw = commit_object_bytes(payload, signature)
    got = subprocess.run(["git", "-C", str(repo_dir), "hash-object", "-t", "commit",
                          "-w", "--stdin"], input=raw, capture_output=True)
    got_sha = got.stdout.decode().strip()
    if got_sha != sha:
        raise SystemExit("commit sha 不符：期望 %s 实得 %s" % (sha, got_sha))
    return {"sha": sha, "skipped": False, "tree": tree_sha}


def ensure_tree(repo_dir: Path, owner_repo: str, tree_sha: str,
                api_root: str = API_ROOT) -> None:
    """保证本地有该 tree 对象（缺 blob 就下载，构造后校验 sha 逐字相同）。"""
    if has_object(repo_dir, tree_sha):
        return
    data = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (owner_repo, tree_sha),
               api_root=api_root)
    entries = [e for e in data.get("tree", []) if e["type"] in ("blob", "commit")]
    if data.get("truncated"):
        raise SystemExit("tree %s 被 API 截断，无法完整重建" % tree_sha)

    index_path = str(repo_dir / ".git" / ("oagentkit-sync-index-%d" % os.getpid()))
    try:
        subprocess.run(["git", "-C", str(repo_dir), "read-tree", "--empty"],
                       env={**os.environ, "GIT_INDEX_FILE": index_path}, check=True)
        for e in entries:
            mode, sha, path = e["mode"], e["sha"], e["path"]
            if not has_object(repo_dir, sha):
                blob = api("GET", "/repos/%s/git/blobs/%s" % (owner_repo, sha),
                           api_root=api_root)
                if blob.get("encoding") != "base64":
                    raise SystemExit("blob %s 编码异常: %r" % (sha, blob.get("encoding")))
                content = base64.b64decode(blob["content"])
                got = subprocess.run(["git", "-C", str(repo_dir), "hash-object", "-t",
                                      "blob", "-w", "--stdin"], input=content,
                                     capture_output=True, check=True).stdout.decode().strip()
                if got != sha:
                    raise SystemExit("blob sha 不符：期望 %s 实得 %s" % (sha, got))
            subprocess.run(["git", "-C", str(repo_dir), "update-index", "--add",
                            "--cacheinfo", mode, sha, path],
                           env={**os.environ, "GIT_INDEX_FILE": index_path}, check=True)
        tree = subprocess.run(["git", "-C", str(repo_dir), "write-tree"],
                              env={**os.environ, "GIT_INDEX_FILE": index_path},
                              capture_output=True, check=True).stdout.decode().strip()
        if tree != tree_sha:
            raise SystemExit("tree sha 不符：期望 %s 实得 %s" % (tree_sha, tree))
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)


def _selftest() -> int:
    """自测：逐字重建规则必须**真的**成立（含反证样本）。

    反证样本：把重建结果改动一个字节（末尾补换行 / 丢掉 signature 末尾空元素）
    ⇒ sha 必须**对不上** ⇒ 否则「sha 校验」就是摆设。
    """
    import json
    from pathlib import Path

    fixture = Path(__file__).resolve().parent / "fixtures" / "signed_commit.json"
    if not fixture.is_file():
        print("缺少 fixture %s" % fixture, file=sys.stderr)
        return 1
    data = json.loads(fixture.read_text(encoding="utf-8"))
    payload, signature, want = data["payload"], data["signature"], data["sha"]

    fails: list[str] = []

    def sha_of(raw: bytes) -> str:
        p = subprocess.run(["git", "hash-object", "-t", "commit", "--stdin"],
                           input=raw, capture_output=True)
        return p.stdout.decode().strip()

    def check(name, got, expect):
        if got != expect:
            fails.append("%s：期望 %r，实得 %r" % (name, expect, got))

    raw = commit_object_bytes(payload, signature)
    check("逐字重建的 sha", sha_of(raw), want)

    # 反证 ①：末尾补换行 ⇒ sha 必须不同
    check("反证：末尾补换行后 sha 必须不同", sha_of(raw + b"\n") == want, False)
    # 反证 ②：signature 丢掉末尾空元素 ⇒ sha 必须不同
    trimmed = "\n".join(signature.split("\n")[:-1])
    check("反证：丢掉 signature 末尾空元素后 sha 必须不同",
          sha_of(commit_object_bytes(payload, trimmed)) == want, False)
    # 反证 ③：payload 末尾补换行 ⇒ sha 必须不同
    check("反证：payload 末尾补换行后 sha 必须不同",
          sha_of(commit_object_bytes(payload + "\n", signature)) == want, False)
    # 反证 ④：payload 里去掉一个字符 ⇒ sha 必须不同
    check("反证：payload 改一个字符后 sha 必须不同",
          sha_of(commit_object_bytes(payload.replace("tree ", "tree  ", 1), signature)) == want,
          False)

    if fails:
        print("共享取数层自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("共享取数层自测：5 条断言全过")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="GitHub Git Data API 共享取数层")
    ap.add_argument("--selftest", action="store_true", help="跑本模块自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
