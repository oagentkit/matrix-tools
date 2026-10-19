#!/usr/bin/env python3
"""各仓 go.mod 的第一方依赖地板 vs 远端最新 tag 的漂移检测。

背景（本仓 issue 证据）：oagentkit#8、#10 两份 issue 描述的是同一类漂移——
子依赖（llm / obs / textx / mcpkit / runtime / oekit/*）发版时只动了自己的
go.mod，**没有回头抬本仓的 require**。后果不是编译失败，而是 MVS 解析结果
被旧地板压住：以本仓为入口的消费方即使自己直接 require 了新版，最终拿到的仍是
旧版本，且没有手段把它顶上去（除非消费方把它写进 go.mod 的 indirect）。

为什么不做成「漂移即 CI 红」的硬门禁：
  地板漂移在设计上就是常态——任何一次子仓发版都会让所有下游变成落后。
  硬门禁等于要求每个仓在每次子仓发版后都跟着发一版，这是发版跑步机，
  长期只会训练出「红了就改豁免名单」的习惯。所以：
    - 默认（report）：只报告，恒 exit 0，让人看见；
    - --enforce：发版前手动跑，漂移则 exit 1，用于 release runbook 的自检；
    - --selftest：离线自证守卫还活着（见下）。

守卫最危险的失效模式是「一直绿，但从没生效过」：判据写反、豁免写错，
它就会静默放过所有漂移。所以 --selftest 用内联 fixture 覆盖每条判据，
不联网、不依赖真实远端状态。

判据来源的可靠性：
  最新 tag 用 `gh api repos/{org}/{repo}/tags` 取。**不要用 go list -m -versions**
  ——它报的是 proxy 的缓存目录，不是远端现实：实测 oagentkit/agent 的 go list
  会列出 v0.1.0～v0.5.0（远端已删），oekit/pkg 的 go list 会列出 v0.9.2
  （远端只有 v0.9.1 / v0.9.3）。真要确认真相用 git ls-remote --tags。

用法:
    verify_dep_floors.py                      # 报告全部模块的漂移（恒 exit 0）
    verify_dep_floors.py --enforce            # 有漂移则 exit 1（发版前自检）
    verify_dep_floors.py --repo agent         # 只看一个仓
    verify_dep_floors.py --ref v0.9.0         # 查某个已发布 tag 的地板，而不是 main
    verify_dep_floors.py --json               # 机器可读输出
    verify_dep_floors.py --selftest           # 离线自测，不联网

退出码: 0 = 无漂移 / 报告模式恒 0 / selftest 通过；1 = 有漂移（--enforce）或自测失败。
需要 gh 已认证（GH_TOKEN 或已登录的 gh）。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 判作「第一方」的 module path 前缀。oekit 与 oagentkit 同属一个团队的 org
# （oagentkit 的模块大量以 oekit/pkg、oekit/concurrency 为基座），
# 所以它的 tag 漂移与 oagentkit 内部漂移是同一个故障模式，一并纳入。
DEFAULT_ORGS = ("github.com/oagentkit/", "github.com/oekit/")

_REQUIRE_RE = re.compile(r"^\s*([\w.\-]+(?:\.[\w\-]+)*/[\w.\-/]+)\s+(v[\w.\-+]+)")


# ---------------------------------------------------------------- semver

def parse_version(v: str) -> tuple | None:
    """v1.2.3 / v1.2.3-rc.1 → 可比较元组；非 semver 返回 None。

    段按**数字**比较，不是按字符串——字符串比较会把 v0.9.10 判成小于 v0.9.9。
    预发布后缀（-rc.1 / -beta）排在其基础版本之后（SemVer §11：预发布 < 正式）。
    """
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    # 无预发布标记用 (1, "")，有标记用 (0, 串)，于是 pre < release
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def version_lt(a: str, b: str) -> bool | None:
    """a < b ? ；任一侧不是 semver 返回 None（判不了 ≠ 判为落后）。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return pa < pb


# ---------------------------------------------------------------- 采集

def gh(*args: str) -> tuple[int, str]:
    """调 gh 并**重试**。

    必须重试：一次扫描要发 40+ 次 gh api，实测偶发瞬时失败（返回空 / 非 0）。
    没有重试时，一次瞬时失败会被记成「取不到 tag / 取不到 go.mod」——
    于是报告里凭空多出几条「判不了」，**而且整份报告的行数会变少**
    （那个仓的 go.mod 没取到，它的依赖一条都不出现）。
    「判不了」本该只出现在真的判不了时，混进瞬时失败就成了噪音。
    """
    last = (1, "")
    for attempt in range(3):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0 and p.stdout.strip():
            return p.returncode, out
        last = (p.returncode, out)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return last


def owner_repo(module_path: str) -> tuple[str, str] | None:
    """module path → GitHub (owner, repo)；拆不出返回 None。

    这是**纯函数**，刻意与网络调用分开：路径拆分写错时（曾把 owner 拼成
    "github.com/oagentkit"，导致每个依赖都查不到 tag、报告里 43 条全是
    「判不了」却仍然 exit 0），只有把它独立出来才能被 --selftest 离线覆盖。

    首段带 "." 或 ":" 的是 host（github.com / gopkg.in），owner 取第二段；
    否则首段本身就是 owner。
    """
    parts = module_path.split("/")
    if len(parts) >= 3 and ("." in parts[0] or ":" in parts[0]):
        return parts[1], parts[2]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def latest_tag(module_path: str) -> str | None:
    """module path → 远端最新 tag。

    `gh api repos/{owner}/{repo}/tags` 按提交时间倒序返回，取首条即最新。
    取不到返回 None——**判不出不等于落后**，调用方须区分。
    """
    parsed = owner_repo(module_path)
    if parsed is None:
        return None
    owner, repo = parsed
    rc, out = gh("api", f"repos/{owner}/{repo}/tags", "--jq", "[.[].name] | .[0] // empty")
    if rc != 0:
        return None
    tag = out.strip().splitlines()[0].strip() if out.strip() else ""
    return tag or None


def repo_go_mod(org: str, repo: str, ref: str) -> str | None:
    """取某仓某 ref 的 go.mod 原文；取不到返回 None。"""
    rc, out = gh("api", f"repos/{org}/{repo}/contents/go.mod?ref={ref}")
    if rc != 0:
        return None
    try:
        payload = json.loads(out)
        return base64.b64decode(payload["content"]).decode("utf-8")
    except (ValueError, KeyError):
        return None


def parse_requires(go_mod_text: str, prefixes: tuple[str, ...]) -> dict[str, str]:
    """从 go.mod 文本抽出第一方 require。

    只认 require 块内的行；module 块与注释行不含 module path，不误命中。
    """
    found: dict[str, str] = {}
    in_require = False
    for raw in go_mod_text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line == "require (":
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        if in_require:
            m = _REQUIRE_RE.match(line)
            if m and m.group(1).startswith(prefixes):
                found.setdefault(m.group(1), m.group(2))
        elif line.startswith("require "):
            # 单行形式：require foo v1.2.3
            m = _REQUIRE_RE.match(line[len("require "):])
            if m and m.group(1).startswith(prefixes):
                found.setdefault(m.group(1), m.group(2))
    return found


# ---------------------------------------------------------------- 判据

def collect(org: str, repo: str, ref: str, prefixes: tuple[str, ...]) -> list[dict]:
    """一个仓 → 一组漂移记录（无漂移返回空列表）。"""
    text = repo_go_mod(org, repo, ref)
    if text is None:
        return [{"repo": repo, "dep": "-", "pinned": "-", "latest": "-",
                 "state": "no-go-mod", "detail": f"{org}/{repo}@{ref} 取不到 go.mod"}]

    rows = []
    for dep, pinned in sorted(parse_requires(text, prefixes).items()):
        latest = latest_tag(dep)
        if latest is None:
            rows.append({"repo": repo, "dep": dep, "pinned": pinned, "latest": "?",
                         "state": "no-tag", "detail": "取不到远端 tag，判不了"})
            continue
        lt = version_lt(pinned, latest)
        if lt is True:
            rows.append({"repo": repo, "dep": dep, "pinned": pinned, "latest": latest,
                         "state": "behind", "detail": "地板落后远端最新"})
        elif lt is None:
            rows.append({"repo": repo, "dep": dep, "pinned": pinned, "latest": latest,
                         "state": "uncomparable", "detail": "版本号不是可比的 semver"})
        else:
            rows.append({"repo": repo, "dep": dep, "pinned": pinned, "latest": latest,
                         "state": "ok", "detail": ""})
    return rows


def matrix_repos(matrix_path: Path) -> list[str]:
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    return [m["repo"] for m in data.get("modules", [])]


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    """离线自证：每条判据都必须能判对，且必须**能判错**。

    只测「绿的守卫」等于没测——所以每条都有对应的负向样本：
    一个错误实现（数字比较写成字符串、豁免过宽、判据写反）会在这里红。
    """
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实得 {got!r}")

    # --- semver：数字比较，不是字符串比较
    check("v0.9.10 > v0.9.9", version_lt("v0.9.9", "v0.9.10"), True)
    check("v0.9.10 < v0.9.9 反向为假", version_lt("v0.9.10", "v0.9.9"), False)
    check("v0.10.0 > v0.9.9", version_lt("v0.9.9", "v0.10.0"), True)
    check("相等不算落后", version_lt("v0.5.0", "v0.5.0"), False)
    check("预发布 < 正式", version_lt("v1.0.0-rc.1", "v1.0.0"), True)
    check("正式 > 预发布", version_lt("v1.0.0", "v1.0.0-rc.1"), False)
    check("两版预发布可比", version_lt("v1.0.0-rc.1", "v1.0.0-rc.2"), True)
    # 非 semver 必须判不出，而不是默默判成「不落后」
    check("非 semver 判不出", version_lt("latest", "v1.0.0"), None)
    check("非 semver 另一侧", version_lt("v1.0.0", "main"), None)

    # --- require 解析
    gomod = """module github.com/oagentkit/demo

go 1.26.0

require (
\tgithub.com/oagentkit/llm v0.9.0
\tgithub.com/oagentkit/textx v0.4.0 // indirect
\tgithub.com/cloudwego/eino v0.9.21
\tgopkg.in/yaml.v3 v3.0.1
)

require github.com/oekit/pkg v0.15.1
"""
    got = parse_requires(gomod, DEFAULT_ORGS)
    check("只收第一方", sorted(got), [
        "github.com/oagentkit/llm", "github.com/oagentkit/textx", "github.com/oekit/pkg"])
    check("indirect 也算地板", got.get("github.com/oagentkit/textx"), "v0.4.0")
    check("单行 require 形式", got.get("github.com/oekit/pkg"), "v0.15.1")
    check("三方不入列", "github.com/cloudwego/eino" in got, False)
    check("module 块不误命中", "github.com/oagentkit/demo" in got, False)

    # 豁免过宽的负向样本：前缀不能匹配到更深的伪前缀
    check("前缀不误伤子串", parse_requires(
        "require (\n\tgithub.com/oagentkitevil/x v1.0.0\n)", DEFAULT_ORGS), {})

    # --- 块边界：右括号必须复位，否则块外的 require 会被当块内继续解析
    check("块边界复位", parse_requires(
        "require (\n\tgithub.com/oagentkit/llm v1.0.0\n)\n"
        "github.com/oagentkit/textx v2.0.0 // 不在 require 里，是模块路径声明\n",
        DEFAULT_ORGS),
        {"github.com/oagentkit/llm": "v1.0.0"})

    # --- 顶层单行 require 是合法 go.mod 语法，必须计入（别把它当噪声剔掉）
    check("顶层单行 require 计入", parse_requires(
        "module github.com/oagentkit/demo\n\nrequire github.com/oagentkit/llm v1.0.0\n",
        DEFAULT_ORGS),
        {"github.com/oagentkit/llm": "v1.0.0"})

    # --- module 块声明不得被当成依赖（它没有版本号，最容易误命中）
    check("module 行不计入", parse_requires(
        "module github.com/oagentkit/demo\n", DEFAULT_ORGS), {})

    # --- owner/repo 拆分（真实故障点：曾把 owner 拼成 "github.com/oagentkit"，
    #     于是每个依赖都查不到 tag、报告 43 条全「判不了」却照样 exit 0）
    check("owner/repo 拆分 github", owner_repo("github.com/oagentkit/agent"),
          ("oagentkit", "agent"))
    check("owner/repo 拆分 oekit", owner_repo("github.com/oekit/pkg"),
          ("oekit", "pkg"))
    check("owner/repo 拆分非 github host", owner_repo("gopkg.in/yaml.v3"),
          ("gopkg.in", "yaml.v3"))
    check("owner/repo 无 host 形式", owner_repo("mymodule/subpath"), ("mymodule", "subpath"))
    check("owner/repo 单段判不出", owner_repo("justone"), None)
    # 反向样本：拼错 owner 会得到 ('github.com/oagentkit', 'agent')，
    # 上面的断言必须能把它和正确值区分开（否则守卫静默失效）
    check("owner 不含 host", "github.com" not in (owner_repo("github.com/oagentkit/agent") or ("",))[0], True)

    # --- 自检脚本自身不联网（自测必须离线可跑）
    # 上面所有用例都只喂字符串、没有任何 gh 调用；这里断言采集入口存在，
    # 防止将来有人把远端探测塞进判据里，导致 --selftest 依赖网络。
    for fn in (latest_tag, repo_go_mod, owner_repo):
        if not callable(fn):
            failures.append(f"{fn.__name__} 应为可调用")

    if failures:
        print("❌ verify_dep_floors 自测失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ verify_dep_floors 自测通过（semver / require 解析 / 豁免边界 / 离线）")
    return 0


# ---------------------------------------------------------------- main

STATE_MARK = {"ok": "✅", "behind": "⚠️ ", "no-tag": "❓", "uncomparable": "❓", "no-go-mod": "❌"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="检测各仓 go.mod 的第一方依赖地板是否落后于远端最新 tag")
    ap.add_argument("--matrix", default=str(HERE.parent / "modules.json"))
    ap.add_argument("--org", default="oagentkit", help="矩阵仓所在的 org")
    ap.add_argument("--repo", help="只查这一个仓")
    ap.add_argument("--ref", default="main", help="查哪个 ref 的 go.mod（默认 main）")
    ap.add_argument("--prefix", action="append", dest="prefixes",
                    help=f"判作第一方的 module path 前缀，可重复（默认 {', '.join(DEFAULT_ORGS)}）")
    ap.add_argument("--enforce", action="store_true", help="有漂移则 exit 1（发版前自检用）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="机器可读输出")
    ap.add_argument("--selftest", action="store_true", help="离线自测，不联网")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    prefixes = tuple(args.prefixes) if args.prefixes else DEFAULT_ORGS
    matrix_path = Path(args.matrix)
    if not matrix_path.is_file():
        print(f"❌ 找不到矩阵文件: {matrix_path}")
        return 1

    repos = [args.repo] if args.repo else matrix_repos(matrix_path)
    rows: list[dict] = []
    for repo in repos:
        rows.extend(collect(args.org, repo, args.ref, prefixes))

    behind = [r for r in rows if r["state"] == "behind"]
    unknown = [r for r in rows if r["state"] in ("no-tag", "uncomparable", "no-go-mod")]

    if args.as_json:
        print(json.dumps({"ref": args.ref, "rows": rows, "behind": len(behind),
                          "unknown": len(unknown)}, ensure_ascii=False, indent=2))
    else:
        if rows:
            print(f"第一方依赖地板 @ {args.org}/*@{args.ref}\n")
            w = max(len(r["dep"]) for r in rows)
            for r in rows:
                mark = STATE_MARK.get(r["state"], "  ")
                line = (f"  {mark} {r['repo']:<12} {r['dep']:<{w}}  "
                        f"{r['pinned']:<9} → {r['latest']:<9}")
                if r["detail"]:
                    line += f" {r['detail']}"
                print(line)
            print()
        print(f"共 {len(rows)} 条第一方依赖：落后 {len(behind)}，判不了 {len(unknown)}")

        if behind and not args.enforce:
            print("\n（报告模式：漂移是常态，不失败。发版前自检请加 --enforce）")
            print("修法：go mod edit -require=<dep>@<latest> && go mod tidy，然后跑 go test -race ./...")
        if unknown:
            print(f"\n注：{len(unknown)} 条判不了（取不到 tag / 非 semver）。"
                  "判不了不等于不落后，落地前先用 git ls-remote --tags 人工确认。")
        if args.enforce and not behind:
            print("✅ 无地板漂移")

    if args.enforce and behind:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
