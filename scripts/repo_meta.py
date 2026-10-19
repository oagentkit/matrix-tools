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

import base64
import json
import re
import subprocess
import time
from pathlib import Path

# 主模块版本线的 tag：v1.2.3（不含嵌套模块的 audit/v1.2.3 形态）
MAIN_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

# 嵌套模块的 tag：<子目录>/v1.2.3
NESTED_TAG_RE = re.compile(r"^(?P<prefix>[^/]+/[\w.\-/]*?)v\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")


def gh_api(path: str, retries: int = 3) -> str | None:
    """调 `gh api <path>` 取 JSON 文本；取不到返回 None（**判不了 ≠ 没有**）。"""
    last = None
    for attempt in range(retries):
        try:
            p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        except OSError as e:      # gh 不在 PATH 上
            return None
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
        last = p.returncode
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_file(org: str, repo: str, path: str, ref: str = "main") -> str | None:
    """取仓里某文件的原文（base64 解码后）。取不到返回 None。"""
    raw = gh_api(f"repos/{org}/{repo}/contents/{path}?ref={ref}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return base64.b64decode(payload["content"]).decode("utf-8")
    except (ValueError, KeyError):
        return None


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


def _all_tags(org: str, repo: str) -> list[str]:
    raw = gh_api(f"repos/{org}/{repo}/tags?per_page=100")
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

    if fails:
        print("❌ repo_meta 自测失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("✅ repo_meta 自测通过（tag 过滤 / semver / module 路径 / 包目录）")
    return 0


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(description="远端仓库元信息取数层")
    # --selftest 是「默认就跑自测」的显式写法（与其他守卫脚本的参数习惯一致）
    _ap.add_argument("--selftest", action="store_true", help="离线自测（默认行为）")
    _ap.parse_args()
    raise SystemExit(selftest())
