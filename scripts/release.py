#!/usr/bin/env python3
"""发版动作脚本：把「预写 → 提交 → 打 tag → 刷新矩阵源 → 同步站点」固化成命令。

## 为什么要有这个脚本

发版顺序是**顺序敏感**的，而顺序过去只写在文档里：

    预写待发版段 → 提交 CHANGELOG.md → 在那个提交上打 annotated tag
                 → 刷新 matrix-tools/versions.json → 拷到站点副本 → 两个仓一起提交

实测踩过的两类后果：

1. **「重生成会吞待发版段」**：`gen_changelogs.py` 无参重生成只按已存在的 tag 切段，
   于是预写段被一次重生成删掉（实测 agent 的 v0.10.1，距上一个重生成提交只隔 17 分钟），
   改动变成「既没有 tag 也没有 CHANGELOG 段」，而 4 道门禁同时全绿。
2. **「矩阵源没刷新」**：源 `versions.json` 的 facade latest 停在 v1.8.20，
   而真实 tag 已是 v1.8.21，站点副本反而是新的——没有任何门禁会红。

两者都不是「人不够仔细」，是**流程没有机器约束**。本脚本把每一步的前置条件写成代码：
条件不满足就**拒绝执行**，而不是执行到一半留下半成品。

## 设计：高内聚低耦合

- **高内聚**：发版这一族动作（预写 / 打 tag / 同步矩阵源）都在本脚本里，
  共享同一套前置检查与错误提示。
- **低耦合**：判定逻辑不重写——
  CHANGELOG 的生成交给 `gen_changelogs.py`（它是生成器，不是裁判），
  矩阵源的生成交给 `gen_versions.py`，版本地板的判定交给 `verify_dep_floors.py`。
  本脚本只负责**顺序、前置条件与提交/推送动作**。

## 通用性

不绑定 oagentkit：组织名从 `--org` 来，矩阵根从 `--root` 来，
站点路径从 `--site` 来。姊妹矩阵（oekit）换参数即可复用。

## 安全约束（每个子命令都有）

- 工作区不干净 ⇒ 拒绝（不碰别人的成果）。
- 每一步都支持 `--dry-run`，先看会发生什么再执行。
- 提交信息里的中文用 Python 写文件再 `git commit -F`——
  未加引号的 shell heredoc 会把中文里相邻的 ASCII 数字重编码成乱码
  （实测 `1.24` 变成 `¼`）。

用法:
    release.py prewrite --repo agent --version v0.10.1
    release.py tag      --repo agent --version v0.10.1
    release.py sync-matrix --version v0.10.1
    release.py doctor   --repo agent --version v0.10.1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_changelogs  # noqa: E402
from judge_report import Report, emit  # noqa: E402

DEFAULT_ORG = os.environ.get("MATRIX_ORG", "oagentkit")


# ---------------------------------------------------------------- 工具


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p


def git_out(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def git_out_opt(repo: Path, *args: str) -> str | None:
    """跑一条 git 命令，**失败不抛**——返回 None。

    用在「这条命令非零退出是正常情况」的探查上，
    例如 `git rev-parse --verify <branch>`：分支不存在时它就该返回非零，
    若按普通命令处理会把「分支不存在」抛成异常。
    """
    p = run(["git", "-C", str(repo), *args], check=False)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def commit_message_file(text: str) -> Path:
    """把提交信息写成 UTF-8 文件。

    为什么不用 shell heredoc：未加引号的 heredoc 会做变量展开，
    且中文与相邻 ASCII 数字相邻处可能被重编码（实测 `1.24` → `¼`）。
    Python 显式 utf-8 写入 + `git commit -F` 是可靠路径。
    """
    p = Path(os.environ.get("TMPDIR", "/tmp")) / f"release-msg-{os.getpid()}.txt"
    p.write_text(text, encoding="utf-8")
    raw = p.read_bytes()
    raw.decode("utf-8")          # 显式校验：写坏了要在这里炸
    bad = [c for c in raw.decode("utf-8") if 0x80 <= ord(c) <= 0x9F]
    if bad:
        raise RuntimeError(f"提交信息含控制区字符（U+0080–U+009F）：{bad!r}")
    return p


def repo_state(repo: Path) -> tuple[bool, str]:
    """→ (工作区是否干净, 当前分支)"""
    dirty = git_out(repo, "status", "--porcelain")
    branch = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return (not dirty), branch


# ---------------------------------------------------------------- prewrite


def cmd_prewrite(args) -> int:
    """预写待发版段：只改 CHANGELOG.md，单独成一个提交。"""
    root = Path(args.root).resolve()
    repo_dir = root / args.repo
    rep = Report(title=f"预写待发版段 {args.repo} {args.version}")

    if not (repo_dir / ".git").exists():
        rep.red(args.repo, f"不是 git 仓：{repo_dir}")
        return emit(rep)

    clean, branch = repo_state(repo_dir)
    if not clean:
        rep.red(args.repo, "工作区不干净，拒绝执行",
                "先提交或 stash 掉现有改动——发版动作不该混入别人的成果")
        return emit(rep)

    # 目标版本必须高于当前最高 tag
    tags = gen_changelogs.tags_in_order(repo_dir)
    if not tags:
        rep.red(args.repo, "仓里没有任何版本 tag（浅克隆？全新仓？）",
                "浅克隆请用完整克隆；全新仓应先发一个版本")
        return emit(rep)
    if gen_changelogs.semver_key(args.version) <= gen_changelogs.semver_key(tags[0]):
        rep.red(args.repo,
                f"版本 {args.version} 不高于当前最高 tag {tags[0]}",
                f"换一个高于 {tags[0]} 的版本号")
        return emit(rep)

    # 生成
    content = gen_changelogs.render(repo_dir, args.repo, args.version)
    changelog = repo_dir / "CHANGELOG.md"
    if not changelog.exists():
        rep.red(args.repo, "仓里没有 CHANGELOG.md",
                "先跑一次 gen_changelogs.py 生成初始 CHANGELOG")
        return emit(rep)

    # 自洽：写进去的内容必须与生成器再次生成的一致
    regenerated = gen_changelogs.render(repo_dir, args.repo, args.version)
    if regenerated != content:
        rep.red(args.repo, "生成结果自相矛盾（两次生成不一致）")
        return emit(rep)

    old = changelog.read_text(encoding="utf-8")
    if old == content:
        rep.info(args.repo, f"CHANGELOG 已是目标状态（{args.version} 段已在）",
                 "不需要预写")
        return emit(rep)

    branch_name = args.branch or f"chore/changelog-prewrite-{args.version}"
    if git_out_opt(repo_dir, "rev-parse", "--verify", branch_name):
        rep.red(args.repo, f"分支 {branch_name} 已存在",
                "换个分支名，或先把它删掉")
        return emit(rep)

    if args.dry_run:
        rep.info(args.repo, f"[dry-run] 将把 CHANGELOG.md 更新为含 {args.version} 段",
                 f"当前分支 {branch}")
        return emit(rep)

    changelog.write_text(content, encoding="utf-8")

    # **只**提交 CHANGELOG.md —— 混入别的文件会被 squash 合成一个提交，
    # 那就不再是「只改 CHANGELOG.md」，生成器的剔除判据（按实际改了什么文件）
    # 就会失效，预写段内容也会与事后重生成不一致。
    run(["git", "-C", str(repo_dir), "add", "CHANGELOG.md"])
    staged = git_out(repo_dir, "diff", "--cached", "--name-only")
    if staged.strip() != "CHANGELOG.md":
        run(["git", "-C", str(repo_dir), "reset", "--quiet"])
        rep.red(args.repo, f"暂存区不只 CHANGELOG.md（{staged}），已撤销暂存",
                "预写段必须单独成一个提交")
        return emit(rep)

    run(["git", "-C", str(repo_dir), "checkout", "-b", branch_name])
    msg = commit_message_file(
        f"docs(changelog): 预写 {args.version} 待发版段\n\n"
        f"由 release.py prewrite 生成。\n"
        f"本提交**只**改 CHANGELOG.md——混入别的文件会被 squash 合成一个提交，\n"
        f"生成器按「实际改了哪些文件」剔除零信息量提交的判据就会失效。\n"
    )
    run(["git", "-C", str(repo_dir), "commit", "-F", str(msg)])
    run(["git", "-C", str(repo_dir), "push", "-u", "origin", branch_name])

    rep.info(args.repo, f"已提交并推送分支 {branch_name}",
             f"合并后再执行：release.py tag --repo {args.repo} --version {args.version}")
    return emit(rep)


# ---------------------------------------------------------------- tag


def cmd_tag(args) -> int:
    """打 annotated tag：前置条件全满足才打。"""
    root = Path(args.root).resolve()
    repo_dir = root / args.repo
    rep = Report(title=f"打 tag {args.repo} {args.version}")

    if not (repo_dir / ".git").exists():
        rep.red(args.repo, f"不是 git 仓：{repo_dir}")
        return emit(rep)

    clean, branch = repo_state(repo_dir)
    if not clean:
        rep.red(args.repo, "工作区不干净，拒绝打 tag",
                "tag 会指向当前 HEAD——工作区有未提交改动时，tag 快照与提交版不一致")
        return emit(rep)
    if branch != "main":
        rep.red(args.repo, f"当前分支是 {branch}，不是 main",
                "tag 必须打在合并后的 main 上（预写段提交被 squash 后会改写 subject）")
        return emit(rep)

    # tag 不能已存在
    if git_out_opt(repo_dir, "tag", "--list", args.version):
        rep.red(args.repo, f"本地已有 tag {args.version}")
        return emit(rep)
    remote_tags = run(["git", "-C", str(repo_dir), "ls-remote", "--tags", "origin",
                       args.version], check=False).stdout.strip()
    if remote_tags:
        rep.red(args.repo, f"远端已有 tag {args.version}",
                "tag 受 ruleset 保护时不能重打——那会破坏下游 go.sum 的 h1")
        return emit(rep)

    # CHANGELOG 必须含该版本段
    changelog = repo_dir / "CHANGELOG.md"
    if not changelog.exists():
        rep.red(args.repo, "仓里没有 CHANGELOG.md")
        return emit(rep)
    if f"## [{args.version}]" not in changelog.read_text(encoding="utf-8"):
        rep.red(args.repo, f"CHANGELOG.md 里没有 {args.version} 版本段",
                f"先执行 release.py prewrite --repo {args.repo} --version {args.version}")
        return emit(rep)

    # **快照自洽**：tag 快照里的 CHANGELOG 必须与「事后重生成」一致。
    # 这条是「预写 → 提交 → 打 tag」三步里最容易错的一环：
    # 预写时看到的 subject 与合并后（squash 追加 (#N)）不同。
    regenerated = gen_changelogs.render(repo_dir, args.repo, args.version)
    if regenerated != changelog.read_text(encoding="utf-8"):
        rep.red(args.repo, "CHANGELOG.md 与「事后重生成」不一致，打 tag 会留下不一致的快照",
                "先跑：python3 scripts/gen_changelogs.py --repo "
                f"{args.repo} --next-version {args.version} 并提交差异")
        return emit(rep)

    if args.dry_run:
        rep.info(args.repo, f"[dry-run] 将在 {git_out(repo_dir, 'rev-parse', 'HEAD')[:8]} 上打 tag {args.version}")
        return emit(rep)

    msg = commit_message_file(f"{args.version}\n")
    run(["git", "-C", str(repo_dir), "tag", "-a", args.version, "-F", str(msg)])
    run(["git", "-C", str(repo_dir), "push", "origin", args.version])
    rep.info(args.repo, f"已打并推送 annotated tag {args.version}",
             "下一步：release.py sync-matrix 刷新矩阵源并同步站点副本")
    return emit(rep)


# ---------------------------------------------------------------- sync-matrix


def _floor_bumps(doc: dict) -> dict:
    """versions.json 里的 floorBumps 登记 → {仓名: 条目列表}。"""
    out: dict[str, list] = {}
    for section in ("modules", "facades"):
        for entry in doc.get(section, []):
            repo = entry.get("repo")
            if repo:
                out[repo] = entry.get("floorBumps") or []
    return out


def sync_site_modules(mt: Path, site: Path, rep: Report) -> str:
    """把矩阵源 modules.json 拷到站点 data/modules.json。

    为什么必须由工具做（以前是**纯手工动作**）：
    站点 AGENTS.md 写明「本仓 data/modules.json 是该文件的副本……内容必须逐字段一致」，
    CONTRIBUTING 发版第 4 步也写着「同步站点 data/modules.json」——
    但三处承诺一处机器校验都没有（2026-10-21 漏过一次：站点副本缺 llm/port，
    当时没有任何反馈会报出来）。⇒ 这一步交给发布工具，才不会「忘了也没人报」。

    纪律不变：**源先、副本后**——本函数只做本地拷贝，
    源仓的 PR 先合、副本仓的 PR 后合（由操作者分两次提交）。

    返回：copied / same / missing
    """
    src = mt / "modules.json"
    dst = site / "data" / "modules.json"
    if not src.is_file():
        rep.yellow("站点", f"找不到矩阵源 {src}")
        return "missing"
    if not dst.is_file():
        rep.yellow("站点", f"找不到站点副本 {dst}",
                   "站点仓还没建，或 data/ 目录被删了")
        return "missing"
    if dst.read_bytes() == src.read_bytes():
        rep.info("站点", "站点 data/modules.json 已与矩阵源一致")
        return "same"
    dst.write_bytes(src.read_bytes())
    rep.info("站点", "已把矩阵源 modules.json 拷到站点 data/modules.json",
             "两个仓一起提交（源先、副本后）")
    return "copied"


def cmd_sync_matrix(args) -> int:
    """刷新矩阵源 versions.json → 拷到站点副本 → 给出提交指引。

    这一步过去**不在任何 runbook 里**，而它缺失的后果是：
    源的 latest 陈旧没人查、站点副本与源漂移、跨仓门禁在时间窗里红。

    两个曾经漏掉的义务（issue matrix-tools#133）：
    ① floorBumps 必须**现算**（它要读各仓相邻两个 tag 处的 go.mod 差异），
       以前这里不传本地根 ⇒ 生成器只能「搬运既有值」⇒ 门面每次抬地板，
       矩阵源都会少一条「下游必须跑 go mod tidy」的登记。
    ② 站点 data/modules.json 必须跟着拷（以前是纯手工动作，漏了没人报）。
    """
    root = Path(args.root).resolve()
    mt = root / "matrix-tools"
    site = root / args.site
    rep = Report(title="同步矩阵源与站点副本")

    src = mt / "versions.json"
    if not src.is_file():
        rep.red("matrix-tools", f"找不到 {src}")
        return emit(rep)

    regen = mt / "versions.json.regen"
    p = run([sys.executable, str(mt / "scripts" / "gen_versions.py"),
             "--org", args.org, "--floor-root", str(root), "--out", str(regen)],
            check=False)
    if p.returncode != 0:
        regen.unlink(missing_ok=True)
        rep.red("matrix-tools", f"gen_versions.py 失败：\n{p.stdout}\n{p.stderr}")
        return emit(rep)

    before = json.loads(src.read_text(encoding="utf-8"))
    after = json.loads(regen.read_text(encoding="utf-8"))

    def latest_map(d):
        out = {}
        for m in d.get("modules", []):
            out[m["repo"]] = m.get("latest")
        for f in d.get("facades", []):
            out[f["repo"]] = f.get("latest")
        return out

    lb, la = latest_map(before), latest_map(after)
    changed = {k: (lb.get(k), la.get(k)) for k in set(lb) | set(la) if lb.get(k) != la.get(k)}

    # floorBumps 的来源：生成器把「现算 / 搬运」打在 stdout。
    # 「搬运」= 这次没算出来、沿用了旧值 ⇒ 旧值可能少条目（那正是 #133 的病根），
    # 所以它要报成**黄灯**而不是普通信息，让操作者看得见。
    fb_note = ""
    for line in (p.stdout or "").splitlines():
        if "floorBumps" in line and "来源" in line:
            fb_note = line.strip().lstrip("# ").strip()
    if not fb_note:
        rep.yellow("matrix-tools", "生成器没打印 floorBumps 来源说明",
                   "无法确认这次是现算还是搬运，先查 gen_versions.py")
    elif "搬运" in fb_note:
        rep.yellow("matrix-tools", fb_note,
                   "各仓克隆的 tag 历史不全 ⇒ 抬地板条目可能被少算；"
                   "把各仓 fetch --unshallow --tags 后重跑")
    else:
        rep.info("matrix-tools", fb_note)

    fb_before, fb_after = _floor_bumps(before), _floor_bumps(after)
    fb_changed = {k: (len(fb_before.get(k, [])), len(fb_after.get(k, [])))
                  for k in set(fb_before) | set(fb_after)
                  if fb_before.get(k, []) != fb_after.get(k, [])}

    site_copy = site / "docs" / "public" / "versions.json"
    site_latest = None
    if site_copy.is_file():
        site_latest = latest_map(json.loads(site_copy.read_text(encoding="utf-8")))

    if args.dry_run:
        rep.info("matrix-tools", f"[dry-run] latest 变化：{changed or '（无）'}")
        rep.info("matrix-tools", f"[dry-run] floorBumps 变化：{fb_changed or '（无）'}")
        rep.info("站点", f"[dry-run] 站点副本 latest：{site_latest}")
        regen.unlink(missing_ok=True)
        return emit(rep)

    # 写回条件：latest 变了**或** floorBumps 变了。
    # 只看 latest 是不够的 —— 那正是「工具算对了却被丢掉」的老形态：
    # 抬地板的登记差一条时 latest 可能没变（没发新版本），
    # 于是算出来的正确结果会被直接丢掉（本次白跑）。
    if changed or fb_changed:
        src.write_text(regen.read_text(encoding="utf-8"), encoding="utf-8")
        rep.info("matrix-tools", f"已刷新 versions.json：latest {changed or '（无）'}；"
                                 f"floorBumps {fb_changed or '（无）'}",
                 "提交它，并把刷新后的文件拷到站点 docs/public/versions.json")
    else:
        rep.info("matrix-tools", "versions.json 的 latest 与 floorBumps 均无变化")
    regen.unlink(missing_ok=True)

    if site_copy.is_file():
        shutil_copy = src.read_text(encoding="utf-8")
        if site_copy.read_text(encoding="utf-8") != shutil_copy:
            site_copy.write_text(shutil_copy, encoding="utf-8")
            rep.info("站点", "已把刷新后的 versions.json 拷到站点副本",
                     "两个仓一起提交（源先、副本后）")
        else:
            rep.info("站点", "站点副本已与源一致")
    else:
        rep.yellow("站点", f"找不到站点副本 {site_copy}",
                   "站点路径不对，或站点仓还没建")

    sync_site_modules(mt, site, rep)

    return emit(rep)


# ---------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    """只检查，不改任何东西：发版前的体检。"""
    root = Path(args.root).resolve()
    repo_dir = root / args.repo
    rep = Report(title=f"发版体检 {args.repo} {args.version}")

    if not (repo_dir / ".git").exists():
        rep.red(args.repo, f"不是 git 仓：{repo_dir}")
        return emit(rep)

    clean, branch = repo_state(repo_dir)
    rep.info(args.repo, f"分支 {branch}，工作区{'干净' if clean else '不干净'}")

    tags = gen_changelogs.tags_in_order(repo_dir)
    if tags:
        rep.info(args.repo, f"最高 tag {tags[0]}")
        if gen_changelogs.semver_key(args.version) <= gen_changelogs.semver_key(tags[0]):
            rep.red(args.repo, f"版本 {args.version} 不高于最高 tag {tags[0]}")
    else:
        rep.red(args.repo, "没有任何版本 tag")

    changelog = repo_dir / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        if f"## [{args.version}]" in text:
            rep.info(args.repo, f"CHANGELOG 已含 {args.version} 段")
            regenerated = gen_changelogs.render(repo_dir, args.repo, args.version)
            if regenerated == text:
                rep.info(args.repo, "CHANGELOG 与事后重生成一致（tag 快照会自洽）")
            else:
                rep.red(args.repo, "CHANGELOG 与事后重生成不一致",
                        f"先跑 gen_changelogs.py --repo {args.repo} "
                        f"--next-version {args.version} 并提交差异")
        else:
            rep.yellow(args.repo, f"CHANGELOG 还没有 {args.version} 段",
                       f"先跑 release.py prewrite --repo {args.repo} --version {args.version}")
    else:
        rep.red(args.repo, "没有 CHANGELOG.md")

    # 依赖地板时效（发版时刻的地板必须是当时的最新）。
    #
    # ⚠️ 只看**本仓**的依赖：地板报告器是全矩阵口径，
    # 而别的仓欠账不是本仓的阻塞项（否则每发一版都要先修完全矩阵的欠账，
    # 那正是「发版跑步机」）。
    # 用 --json 取机器可读结果再按仓过滤，而不是解析人类可读输出。
    dep = HERE / "verify_dep_floors.py"
    if dep.is_file():
        p = run([sys.executable, str(dep), "--org", args.org, "--json"], check=False)
        try:
            payload = json.loads(p.stdout)
        except ValueError:
            payload = None
        if payload is None:
            rep.yellow(args.repo, "依赖地板取数失败（判不了），未做地板校验",
                       "网络或脚本问题——判不了不能当成「地板没问题」")
        else:
            rows = [r for r in payload.get("rows", []) if r.get("repo") == args.repo]
            bad = [r for r in rows if r.get("state") != "ok"]
            for r in bad:
                rep.red(args.repo,
                        f"依赖 {r.get('dep')} 钉在 {r.get('pinned')}，远端最新是 {r.get('latest')}",
                        f"先发一版把地板抬到 {r.get('latest')}，再发本仓——"
                        "打 tag 时刻的地板必须是当时的最新")
            if not bad and rows:
                rep.info(args.repo, f"依赖地板满足发版条件（{len(rows)} 条第一方依赖）")
            elif not rows:
                rep.yellow(args.repo, "没有取到本仓的依赖地板记录（判不了）")

    return emit(rep)


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="发版动作脚本（预写 / 打 tag / 同步矩阵源）")
    ap.add_argument("--root", default=".", help="矩阵根目录")
    ap.add_argument("--org", default=DEFAULT_ORG, help="组织名")
    ap.add_argument("--dry-run", action="store_true", help="只演练，不改动")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prewrite", help="预写待发版段（只改 CHANGELOG.md）")
    p.add_argument("--repo", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--branch", help="分支名（默认 chore/changelog-prewrite-<version>）")
    p.set_defaults(func=cmd_prewrite)

    t = sub.add_parser("tag", help="打 annotated tag")
    t.add_argument("--repo", required=True)
    t.add_argument("--version", required=True)
    t.set_defaults(func=cmd_tag)

    s = sub.add_parser("sync-matrix", help="刷新矩阵源并同步站点副本")
    s.add_argument("--site", default="oagentkit.github.io")
    s.set_defaults(func=cmd_sync_matrix)

    d = sub.add_parser("doctor", help="发版前体检（只读）")
    d.add_argument("--repo", required=True)
    d.add_argument("--version", required=True)
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
