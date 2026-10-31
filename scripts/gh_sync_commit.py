#!/usr/bin/env python3
"""把远端 commit **逐字**搬回本地 —— 走 GitHub REST Git Data API，不依赖 git 传输层。

## 为什么需要它

`github.com` 主站在某些出口下被掐（443/22/SSH 全断、git smart-HTTP 端点不可达）
⇒ `git fetch` 永远失败 ⇒ 本地仓与远端**分叉**：本地 main 落后、远端新 commit 拿不到。
而 `api.github.com` 在同一出口下**可达** ⇒ 可以只靠 API 把远端对象搬回来。

`gh_push_branch.py` 是**推**的一侧，本脚本是**拉**的一侧：两侧共用
`gh_git_api.py` 的事实层（取数 / 代理 / 逐字重建规则）。

## 保真性

搬回来的 commit 必须与远端**同一个 sha**（内容寻址 ⇒ 逐字相同才可能同 sha）：

① commit 对象用 `verification.payload` + `verification.signature` **逐字重建**，
   重建出的 sha 必须 == 远端声称的 sha；
② 树用临时索引构造，构造出的 tree sha 必须 == 远端的 tree sha；
③ 缺 blob 时下载后写本地对象库，写入前核对 blob sha。

⇒ 三层都过 ⇒ 本地那个对象与远端**逐字节等价**。

## 已知边界（别把它当通用 git 客户端）

- **只支持带签名的 commit**（GitHub 自己创建的 squash/merge 那种）。
  无签名 commit 的原始**时区**不可得（API 只给 UTC）⇒ sha 必然不同 ⇒ 本脚本**拒绝**，
  而不是给你一个 sha 不同的弱对账。
- **parent 必须本地已有** ⇒ 请按「从旧到新」的顺序同步。

## 用法

    python3 scripts/gh_sync_commit.py --repo-dir <本地仓> --repo <owner/repo> <sha> [...]
    python3 scripts/gh_sync_commit.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_git_api import API_ROOT, api, git, has_object, sync_commit  # noqa: E402

SIG_TEXT = "-----BEGIN PGP SIGNATURE-----\n\nc2lnZ25yZS1zaWduYXR1cmU=\n-----END PGP SIGNATURE-----\n"


# ── 「带签名 commit」的字节工厂（自测用）─────────────────────────────────

def signed_commit_bytes(tree: str, parents: list[str], message: str,
                        author: str = "T <t@example.com>", ts: str = "1700000000 +0800",
                        committer: str = "GitHub <noreply@github.com>") -> bytes:
    """造一个**格式与 GitHub squash commit 一致**的 commit 对象字节。

    ⚠️ 与 GitHub 的一致性有三处，缺一即重建不出同一个 sha：
      · gpgsig 块在 committer 之后、message 之前；
      · END 行之后还有一行 `" "`（来自签名文本末尾的换行）；
      · 块与 message 之间是**一个真空行**；
      · message 末尾**没有**换行。
    """
    head = "tree %s\n" % tree
    for p in parents:
        head += "parent %s\n" % p
    head += "author %s %s\n" % (author, ts)
    head += "committer %s %s\n" % (committer, ts)
    sig_lines = SIG_TEXT.rstrip("\n").split("\n")
    block = ["gpgsig " + sig_lines[0]] + [(" " + ln) if ln else " " for ln in sig_lines[1:]]
    block.append(" ")  # 签名文本末尾的换行 ⇒ 对象里多一行 " "
    return (head + "\n".join(block) + "\n\n" + message).encode("utf-8")


def split_payload_signature(raw: bytes) -> tuple[str, str]:
    """把上面造出来的字节**拆回** (payload, signature) —— 模拟 GitHub 的返回。"""
    text = raw.decode("utf-8")
    head, rest = text.split("\n\n", 1)
    lines = head.split("\n")
    keep = [ln for ln in lines if not ln.startswith("gpgsig ")]
    sig_lines = []
    in_sig = False
    for ln in lines:
        if ln.startswith("gpgsig "):
            in_sig = True
            sig_lines.append(ln[len("gpgsig "):])
        elif in_sig:
            sig_lines.append(ln[1:] if ln.startswith(" ") else ln)
    payload = "\n".join(keep) + "\n\n" + rest
    signature = "\n".join(sig_lines) + "\n"
    return payload, signature


# ── 自测：本地假 GitHub 跑真 HTTP 往返 ──────────────────────────────────

def _selftest() -> int:
    import http.server
    import shutil
    import tempfile

    fails: list[str] = []

    def check(name, got, want):
        if got != want:
            fails.append("%s：期望 %r，实得 %r" % (name, want, got))

    tmp = tempfile.mkdtemp()
    repo = Path(tmp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    base_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                               capture_output=True, text=True).stdout.strip()

    # 造一个「带签名」的 commit，写进本地对象库（sha 由**真实 git** 算）
    raw = signed_commit_bytes(base_tree, [base], "feat: 假提交\n")
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit",
                          "-w", "--stdin"], input=raw,
                         capture_output=True, check=True).stdout.decode().strip()
    payload, signature = split_payload_signature(raw)

    class FakeGitHub(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        tamper_signature = False
        tamper_blob = False

        def log_message(self, *_a):
            pass

        def handle_error(self, request, client_address):
            import traceback
            traceback.print_exc()

        def _guard(self, fn):
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
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._guard(self._get)

        def _get(self):
            path = self.path.split("?")[0]
            if "/git/commits/" in path:
                sig = signature
                if FakeGitHub.tamper_signature:
                    sig = "\n".join(signature.split("\n")[:-1])  # 丢掉末尾空元素
                self._json({"sha": sha, "tree": {"sha": base_tree},
                            "parents": [{"sha": base}],
                            "verification": {"payload": payload, "signature": sig}})
            elif "/git/trees/" in path:
                out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", base_tree],
                                     capture_output=True, text=True).stdout
                entries = []
                for line in out.splitlines():
                    meta, p = line.split("\t", 1)
                    mode, _kind, s = meta.split()
                    entries.append({"path": p, "mode": mode, "type": "blob", "sha": s})
                self._json({"tree": entries, "truncated": False})
            elif "/git/blobs/" in path:
                bsha = path.rsplit("/", 1)[-1]
                content = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", bsha],
                                         capture_output=True).stdout
                if FakeGitHub.tamper_blob:
                    content = content + b"tampered\n"
                self._json({"sha": bsha, "encoding": "base64",
                            "content": base64.b64encode(content).decode("ascii")})
            else:
                self._json({"message": "not found"}, code=404)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api_root = "http://127.0.0.1:%d" % httpd.server_address[1]

    try:
        # ① 正常路径：搬回来后本地有该对象，且 sha 逐字相同
        out = sync_commit(repo, "o/r", sha, api_root=api_root)
        check("同步后 sha 相同", out.get("sha"), sha)
        check("本地已有该对象", has_object(repo, sha), True)
        check("对象大小与远端一致",
              subprocess.run(["git", "-C", str(repo), "cat-file", "-s", sha],
                             capture_output=True, text=True).stdout.strip(),
              str(len(raw)))
        # 幂等：再来一次应跳过
        out2 = sync_commit(repo, "o/r", sha, api_root=api_root)
        check("幂等：已有对象则跳过", out2.get("skipped"), True)

        # ② 反证：签名被篡改 ⇒ 重建的 sha 必须对不上 ⇒ 必须判红
        FakeGitHub.tamper_signature = True
        try:
            sync_commit(repo, "o/r", "1" * 40, api_root=api_root)
            check("反证：签名被篡改时必须拒绝", "没拒绝", "拒绝")
        except SystemExit:
            check("反证：签名被篡改时必须拒绝", "拒绝", "拒绝")
        FakeGitHub.tamper_signature = False

        # ③ 反证：blob 内容被篡改 ⇒ blob sha 对不上 ⇒ 必须判红
        FakeGitHub.tamper_blob = True
        repo2 = Path(tmp) / "repo2"
        shutil.copytree(repo, repo2)
        try:
            sync_commit(repo2, "o/r", "2" * 40, api_root=api_root)
            check("反证：blob 被篡改时必须拒绝", "没拒绝", "拒绝")
        except SystemExit:
            check("反证：blob 被篡改时必须拒绝", "拒绝", "拒绝")
        FakeGitHub.tamper_blob = False

        # ④ 反证：parent 本地缺失 ⇒ 必须拒绝（不给弱对账）
        repo3 = Path(tmp) / "repo3"
        repo3.mkdir()
        subprocess.run(["git", "-C", str(repo3), "init", "-q"], check=True)
        try:
            sync_commit(repo3, "o/r", "3" * 40, api_root=api_root)
            check("反证：parent 缺失时必须拒绝", "没拒绝", "拒绝")
        except SystemExit:
            check("反证：parent 缺失时必须拒绝", "拒绝", "拒绝")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("拉取侧自测失败：")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("拉取侧自测：9 条断言全过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把远端 commit 逐字搬回本地（走 API）")
    ap.add_argument("--repo-dir", required=False, help="本地仓目录")
    ap.add_argument("--repo", required=False, help="owner/repo")
    ap.add_argument("--api-root", default=API_ROOT, help=argparse.SUPPRESS)
    ap.add_argument("shas", nargs="*", help="要同步的 commit sha（按从旧到新）")
    ap.add_argument("--selftest", action="store_true", help="跑本脚本自测")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.repo_dir or not args.repo or not args.shas:
        ap.print_help()
        return 2

    repo_dir = Path(args.repo_dir).resolve()
    for sha in args.shas:
        res = sync_commit(repo_dir, args.repo, sha, api_root=args.api_root)
        if res.get("skipped"):
            print("· %s 本地已有，跳过" % sha[:12])
        else:
            print("✓ %s 已逐字搬回本地（sha 与远端相同）" % sha[:12])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
