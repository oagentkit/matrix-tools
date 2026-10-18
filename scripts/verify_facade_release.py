#!/usr/bin/env python3
"""门面仓发布地板校验：agentkit 已发布的最新 tag，其 go.mod 必须钉在各模块仓的当前最新 tag。

背景（2026-10-18 实测）：
    门面仓 `agentkit` 是消费方的单一入口（README 推荐 `go get github.com/oagentkit/agentkit/...`）。
    Go 的 MVS 只看**被消费的那个版本**的 go.mod。所以
        go get github.com/oagentkit/agentkit@v1.2.0
    的模块图由 v1.2.0 处的 go.mod 决定，与 main 无关。

    而 v1.2.0 处（以及拆分以来的每一个 tag）都写着：
        github.com/oagentkit/agent    v0.1.0
        github.com/oagentkit/llm      v0.1.0
        ...13 个全部 v0.1.0

    后果：消费方从门面仓装走的是**拆分初期**的 13 个模块实现。
    这不报错、不告警、`go build` 照样过 —— 只是拿到的代码是几个月前的。
    「门面仓 v1.2.0 已发布」这件事反而成了问题本身。

    main 上已经修好了（go.mod 跟着每轮发 tag 跟进），但只要**没有重新发 tag**，
    线上流传的 v1.2.0 就一直是个陷阱。所以这条门禁守的是「tag」，不是 main。

用法:
    verify_facade_release.py --org oagentkit            # 联网读 tag（CI 用）
    verify_facade_release.py --tags-json t.json --facade-gomod go.mod   # 离线
    verify_facade_release.py --selftest                 # 自测

--tags-json 格式: {"textx": "v0.2.0", "llm": "v0.6.0", ...}
--facade-gomod : 门面仓**最新 tag 处**的 go.mod 文本（离线模式必填）

退出码: 0 = 门面接的是最新地板；1 = 有落后 / 漏 require / 一个仓都没检查到。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MATRIX = HERE.parent / "modules.json"

# 只认 require 块里的直接依赖行；注释行（// indirect）与空行忽略
REQUIRE = re.compile(r"^\s*github\.com/oagentkit/([a-z][a-z0-9]*)\s+(v[\d.]+)")


def parse_requires(gomod_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in gomod_text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        m = REQUIRE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def fetch_latest_tag(org: str, repo: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/tags", "--jq", "[.[].name] | .[0] // empty"],
        capture_output=True, text=True)
    return r.stdout.strip()


def fetch_file(org: str, repo: str, path: str, ref: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/contents/{path}?ref={ref}", "--jq", ".content"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    import base64
    return base64.b64decode("".join(r.stdout.split())).decode()


def semver_key(v: str) -> tuple:
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


# ---------- 自测 ----------

_GOOD_GOMOD = """module github.com/oagentkit/agentkit

go 1.26

require (
\tgithub.com/oagentkit/agent v0.5.0
\tgithub.com/oagentkit/llm v0.6.0
\tgithub.com/oagentkit/textx v0.2.0
\tgithub.com/oagentkit/queue v0.3.0 // indirect
)
"""

# 复刻线上 v1.2.0 的真实形态：13 个全部 v0.1.0
_STALE_GOMOD = """module github.com/oagentkit/agentkit

go 1.26

require (
\tgithub.com/oagentkit/agent v0.1.0
\tgithub.com/oagentkit/llm v0.1.0
\tgithub.com/oagentkit/textx v0.1.0
\tgithub.com/oagentkit/queue v0.1.0
)
"""

_TAGS = {"agent": "v0.5.0", "llm": "v0.6.0", "textx": "v0.2.0", "queue": "v0.3.0"}


def _audit(tag: str, gomod_text: str, tags: dict[str, str],
           declared: list[str], verbose: bool) -> int:
    req = parse_requires(gomod_text)
    print(f"门面仓最新 tag: {tag}")
    print(f"各模块仓最新 tag: {len(tags)} 个\n")

    bad = []
    for repo in declared:
        want = tags.get(repo, "")
        got = req.get(repo, "")
        if not got:
            bad.append((repo, want, "（门面 go.mod 未 require）"))
            continue
        if want and got != want:
            bad.append((repo, want, got))
        elif verbose:
            print(f"  {repo:<12} ✅ {got}")

    # 门面 require 了矩阵里没有的仓 —— 同样是漂移，只是方向相反
    for repo in sorted(set(req) - set(declared)):
        bad.append((repo, "(矩阵里没有这个仓)", req[repo]))

    if not declared:
        print("❌ modules.json 里一个仓都没有，拒绝判定。")
        return 1

    if bad:
        print(f"❌ 门面仓 tag {tag} 的依赖地板有 {len(bad)} 处落后：\n")
        for repo, want, got in bad:
            print(f"  {repo:<12} 应为 {want:<12} 实际 {got}")
        print("\n为什么这要当错误处理：")
        print("  门面仓是消费方的单一入口，而 MVS 只看被消费版本的 go.mod。")
        print("  门面 tag 钉着旧版本 ⇒ 消费方 go get 门面仓会静默拿到旧实现，")
        print("  不报错、不告警、go build 照样过 —— 只是代码是几个月前的。\n")
        print("修法：把门面仓 go.mod 跟到各仓最新 tag，合并后**重新给门面仓发 tag**。")
        print("     只改 main 不发 tag 等于没修：线上流传的仍是旧 tag。")
        return 1

    print(f"✅ 门面仓 tag {tag} 的依赖地板与 {len(declared)} 个模块仓的最新 tag 一致")
    return 0


def selftest() -> int:
    fails = []

    def check(name: str, got: int, want: int) -> None:
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {name}（期望 exit {want}，实际 {got}）")
        if not ok:
            fails.append(name)

    import io
    import contextlib

    def run(tag: str, gomod: str, tags: dict) -> int:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return _audit(tag, gomod, tags, sorted(tags), verbose=False)

    print("自测：")
    check("门面接最新地板 → 绿", run("v1.3.0", _GOOD_GOMOD, _TAGS), 0)
    check("门面接 v0.1.0（线上真实形态）→ 红", run("v1.2.0", _STALE_GOMOD, _TAGS), 1)
    check("漏 require 一个仓 → 红",
          run("v1.3.0", _GOOD_GOMOD.replace("\tgithub.com/oagentkit/textx v0.2.0\n", ""),
              _TAGS), 1)
    check("require 了矩阵外的仓 → 红",
          run("v1.3.0", _GOOD_GOMOD + "\tgithub.com/oagentkit/ghost v9.9.9\n", _TAGS), 1)

    p = parse_requires(_GOOD_GOMOD)
    ok_parse = (p.get("queue") == "v0.3.0"          # // indirect 仍算 require
                and "example.com" not in p)
    print(f"  {'✅' if ok_parse else '❌'} 解析忽略非 oagentkit 依赖")
    if not ok_parse:
        fails.append("parse")

    if fails:
        print(f"\n❌ 自测失败: {fails}")
        return 1
    print("\n✅ 自测全过：守卫确实能抓到线上那个真实缺陷（不是摆设）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", help="GitHub 组织（默认取 modules.json 的 defaultOrg）")
    ap.add_argument("--facade", default="agentkit", help="门面仓名")
    ap.add_argument("--matrix", default=None)
    ap.add_argument("--tags-json", help="离线：{repo: latest_tag} 的 JSON")
    ap.add_argument("--facade-gomod", help="离线：门面仓最新 tag 处的 go.mod 文本")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    matrix = Path(args.matrix) if args.matrix else DEFAULT_MATRIX
    if not matrix.is_file():
        print(f"❌ 找不到 modules.json: {matrix}")
        return 1
    data = json.loads(matrix.read_text(encoding="utf-8"))
    org = args.org or data.get("defaultOrg") or "oagentkit"
    declared = sorted(m["repo"] for m in data["modules"])

    if args.tags_json:
        tags = json.loads(Path(args.tags_json).read_text(encoding="utf-8"))
        if not args.facade_gomod:
            print("❌ 离线模式必须同时给 --facade-gomod")
            return 1
        gomod_text = Path(args.facade_gomod).read_text(encoding="utf-8")
        tag = "(离线 fixture)"
    else:
        tag = fetch_latest_tag(org, args.facade)
        if not tag:
            print(f"❌ 取不到 {org}/{args.facade} 的 tag")
            return 1
        gomod_text = fetch_file(org, args.facade, "go.mod", tag)
        if not gomod_text:
            print(f"❌ 取不到 {org}/{args.facade}@{tag} 的 go.mod")
            return 1
        tags = {}
        for r in declared:
            t = fetch_latest_tag(org, r)
            if not t:
                print(f"❌ 取不到 {org}/{r} 的 tag")
                return 1
            tags[r] = t

    if not tags:
        print("❌ 一个模块仓的 tag 都没取到，拒绝判定（避免「全绿但什么都没查」）")
        return 1

    return _audit(tag, gomod_text, tags, declared, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
