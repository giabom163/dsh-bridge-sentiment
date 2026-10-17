#!/usr/bin/env python3
"""完整审查包：绑定历史 attempt，缺证、漂移或超限即拒绝。只读 store/worktree。
--out 必须显式指定到 store/仓库之外且不可覆盖。首次 --limit 3，限制候选文件数。
出包不代表执行停稳、R0 PASS 或人工接收。
"""
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    # bridge.json_bytes 的契约和快照口径。
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是对象：{path}")
    return value


def git(workspace, *args):
    strip = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_COMMON_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in strip}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(["git", "-C", str(workspace), *args], env=env,
                          capture_output=True, timeout=60)
    if proc.returncode:
        raise ValueError(f"git {args[0]} exit={proc.returncode}: " + proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


def git_attr(workspace, name):
    fields = git(workspace, "check-attr", "-z", "text", "eol", "filter", "working-tree-encoding", "--", name).split(b"\0")
    return {fields[i + 1].decode("utf-8", "strict"): fields[i + 2].decode("utf-8", "strict")
            for i in range(0, len(fields) - 2, 3)}


def clean_eol(data):
    if b"\0" in data or b"\r" in data.replace(b"\r\n", b""):
        return None
    if b"\r\n" in data and b"\n" in data.replace(b"\r\n", b""):
        return None
    return data.replace(b"\r\n", b"\n")


def expected_checkout(blob, attrs, autocrlf):
    if attrs.get("filter", "unspecified") != "unspecified" or attrs.get("working-tree-encoding", "unspecified") != "unspecified":
        return None
    text, eol = attrs.get("text", "unspecified"), attrs.get("eol", "unspecified")
    if text == "unset":
        return None
    if text not in ("set", "auto") and eol not in ("lf", "crlf") and autocrlf != "true":
        return None
    normalized = clean_eol(blob)
    if normalized is None:
        return None
    target = "crlf" if eol == "crlf" or (eol == "unspecified" and autocrlf == "true") else "lf"
    return normalized.replace(b"\n", b"\r\n") if target == "crlf" else normalized


def inside(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError(f"非法相对路径：{relative!r}")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or parts.as_posix() != relative:
        raise ValueError(f"非法相对路径：{relative!r}")
    target = root
    for part in parts.parts:
        target /= part
        if part.lower() == ".git" or target.is_symlink() or target.is_junction():
            raise ValueError(f"拒绝链接或 .git 路径：{relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"路径越界：{relative}")
    return target


def snapshot(workspace):
    names = git(workspace, "ls-files", "-c", "-o", "--exclude-standard", "-z").split(b"\0")
    if len(names) > 10001:
        raise ValueError("仓库文件超过 10000，拒绝自动扫描")
    found = {}
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8", "strict")
        target = inside(workspace, name)
        if target.is_file():
            if target.stat().st_size > 20 * 1024 * 1024:
                raise ValueError(f"文件超过 20 MB：{name}")
            found[name] = digest(target.read_bytes())
        elif target.exists():
            raise ValueError(f"不支持目录／子模块：{name}")
    return found


def fenced(text, language=""):
    fence = "`" * max(3, max((len(s) + 1 for s in re.findall(r"`+", text)), default=3))
    return f"{fence}{language}\n{text}\n{fence}"


def build(args):
    if min(args.limit, args.max_diff_kb, args.max_packet_kb) < 1:
        raise ValueError("--limit 和大小上限必须为正整数")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.task):
        raise ValueError("非法 task_id")
    store = Path(args.store).resolve(strict=True)
    task_dir = inside(store, "tasks/" + args.task)
    aid = args.attempt or load(task_dir / "state.json").get("current_attempt")
    if not isinstance(aid, str) or not re.fullmatch(r"attempt-[a-f0-9]{32}", aid):
        raise ValueError("非法或缺失 attempt_id")
    att_dir = inside(task_dir, "attempts/" + aid)
    inputs = {}
    tool_path = Path(__file__).resolve()
    inputs[tool_path] = digest(tool_path.read_bytes())

    def evidence(path):
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"JSON 必须是对象：{path}")
        inputs[path] = digest(raw)
        return value

    attempt = evidence(att_dir / "attempt.json")
    result = evidence(att_dir / "result.json")
    state = evidence(task_dir / "state.json")
    if state.get("task_id") != args.task:
        raise ValueError("state.task_id 与任务目录不一致")
    events_path = task_dir / "events.jsonl"
    event_bytes = events_path.read_bytes()
    inputs[events_path] = digest(event_bytes)
    events = [json.loads(line) for line in event_bytes.decode("utf-8").splitlines() if line.strip()]
    revision = attempt.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("attempt 缺少有效 revision；禁止回退到最新契约")
    contract_path = task_dir / f"contract.r{revision}.json"
    contract = evidence(contract_path)
    contract_raw_hash = inputs[contract_path]
    contract_hash = digest(canonical(contract))
    for label, record in (("attempt", attempt), ("result", result)):
        if record.get("attempt_id") != aid or record.get("task_id") != args.task:
            raise ValueError(f"{label} 身份与目录不一致")
        if record.get("revision") != revision or record.get("contract_sha256") != contract_hash:
            raise ValueError(f"{label} 契约版本／哈希不一致")
    if contract.get("task_id") != args.task:
        raise ValueError("契约 task_id 不一致")
    collected = [event for event in events if event.get("attempt_id") == aid and event.get("action") == "collected"]
    if not collected or collected[-1].get("result_sha256") != inputs[att_dir / "result.json"]:
        raise ValueError("events.jsonl 未绑定此 attempt 的原始 result 哈希")
    started = [event for event in events if event.get("attempt_id") == aid and event.get("action") == "starting"]
    if not started or not started[-1].get("plan_sha256"):
        raise ValueError("events.jsonl 缺少该 attempt 的计划记录")
    value = attempt.get("worktree")
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("attempt.worktree 无效；不回退到猜测路径")
    workspace = Path(value).resolve(strict=True)
    out = Path(args.out).absolute()
    if out.exists() or out.is_symlink() or out.is_junction():
        raise ValueError(f"拒绝覆盖已有输出：{out}")
    for protected in (store, workspace, Path(contract["repo"]).resolve(), Path(__file__).resolve().parent.parent):
        if out.resolve().is_relative_to(protected):
            raise ValueError(f"输出不得位于 store、工作区或源码仓库：{out}")
    if not out.parent.is_dir():
        raise ValueError("输出目录必须预先存在")
    marker = workspace / ".git"
    if not marker.is_file() or digest(marker.read_bytes()) != result.get("git_marker_sha256"):
        raise ValueError("工作区 .git 指针缺失或哈希不一致")
    base = attempt.get("base_commit")
    if not isinstance(base, str) or not re.fullmatch(r"[a-f0-9]{40}", base):
        raise ValueError("缺失有效基线 commit")
    if result.get("base_commit") != base or git(workspace, "rev-parse", "HEAD").decode().strip() != base:
        raise ValueError("基线／工作区 HEAD 不一致")
    top = Path(git(workspace, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    if top != workspace:
        raise ValueError("记录的工作区不是实际 Git 根目录")
    common = Path(git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir").decode("utf-8").strip()).resolve()
    expected_common = (Path(contract["repo"]).resolve(strict=True) / ".git").resolve(strict=True)
    if common != expected_common:
        raise ValueError("attempt.worktree 不属于契约指定仓库")
    before = evidence(att_dir / "before.json")
    after = evidence(att_dir / "after.json")
    if digest(canonical(after)) != result.get("after_snapshot_sha256"):
        raise ValueError("after.json 与 result 快照哈希不一致")
    current = snapshot(workspace)
    if current != after:
        raise ValueError("当前工作区与历史 after 快照不一致")
    actual = [{"path": p, "before_sha256": before.get(p), "after_sha256": after.get(p)}
              for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    recorded = result.get("changes")
    if not isinstance(recorded, list) or any(not isinstance(c, dict) for c in recorded):
        raise ValueError("result.changes 缺失或无效")
    if sorted(recorded, key=lambda c: c.get("path", "")) != actual:
        raise ValueError("完整变更集合与 result.changes 不一致")
    if len(actual) > args.limit:
        raise ValueError(f"变更文件数 {len(actual)} 超过 --limit {args.limit}")
    tracked = {}
    for entry in git(workspace, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if entry:
            meta, name = entry.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("不支持符号链接／子模块基线")
            tracked[name.decode("utf-8", "strict")] = oid.decode("ascii")
    if set(before) != set(tracked):
        raise ValueError("before 快照与基线文件集合不一致")
    autocrlf_proc = subprocess.run(["git", "-C", str(workspace), "config", "--get", "core.autocrlf"],
                                   capture_output=True, timeout=60)
    if autocrlf_proc.returncode not in (0, 1):
        raise ValueError("读取 core.autocrlf 失败：" + autocrlf_proc.stderr.decode("utf-8", "replace"))
    autocrlf = autocrlf_proc.stdout.decode("ascii", "strict").strip().lower() if autocrlf_proc.returncode == 0 else "false"
    git_content = []
    for name, oid in tracked.items():
        inside(workspace, name)
        blob = git(workspace, "cat-file", "blob", oid)
        blob_hash = digest(blob)
        attrs = git_attr(workspace, name)
        relation = "exact"
        if blob_hash != before[name]:
            expected = expected_checkout(blob, attrs, autocrlf)
            if expected is None or digest(expected) != before[name]:
                raise ValueError(f"before 原始字节与基线不符，且无纯 LF/CRLF 转换证据：{name}")
            relation = "proven_lf_crlf_checkout"
        git_content.append({"path": name, "worktree_before_raw_sha256": before[name],
                            "git_blob_oid": oid, "git_blob_raw_sha256": blob_hash,
                            "relation": relation, "attributes": attrs, "core_autocrlf": autocrlf})
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all").decode("utf-8", "strict")
    diff_args = ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", "--")
    diff = git(workspace, *diff_args)
    changed_tracked = {p.decode("utf-8", "strict") for p in
                       git(workspace, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--").split(b"\0") if p}
    untracked_names = {p.decode("utf-8", "strict") for p in
                       git(workspace, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p}
    changed_names = changed_tracked | untracked_names
    ignored_names = [p.decode("utf-8", "replace") for p in
                     git(workspace, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split(b"\0") if p]
    if ignored_names:
        raise ValueError("存在无法与历史快照比较的忽略文件，拒绝声称变更集合完整：" + repr(ignored_names[:20]))
    hash_paths = {c["path"] for c in actual}
    if (changed_names - hash_paths) - set(tracked) or hash_paths - changed_names:
        raise ValueError(f"Git 变更清单与哈希快照不一致：git={sorted(changed_names)}, hash={sorted(hash_paths)}")
    if len(changed_names) > args.limit:
        raise ValueError(f"Git 变更路径数 {len(changed_names)} 超过 --limit {args.limit}")
    additions = []
    attachment_data = {}
    attachment_manifest = []

    def attach_candidate(name, data):
        file_hash = digest(data)
        filename = file_hash + ".bin"
        attachment_data.setdefault(filename, data)
        attachment_manifest.append({"path": name, "sha256": file_hash, "bytes": len(data),
                                    "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                                    "controlled_attachment": f"{out.name}.attachments/{filename}"})

    for name in sorted(set(after) - set(tracked)):
        data = inside(workspace, name).read_bytes()
        try:
            content = data.decode("utf-8", "strict")
        except UnicodeError:
            content = None
        if content is not None and "\0" in content:
            content = None
        if content is None or len(data) > args.max_diff_kb * 1024:
            attach_candidate(name, data)
        else:
            additions.append(f"### 未跟踪文件 {json.dumps(name, ensure_ascii=False)}\n" + fenced(content))
    diff_section = fenced(diff.decode("utf-8", "strict"), "diff") + "\n\n" + "\n\n".join(additions)
    if len(diff_section.encode("utf-8")) > args.max_diff_kb * 1024:
        # 受控降级：保留 Git stat 与每个变更后文件的外置内容、SHA-256、字节数和类型。
        attachment_manifest.clear()
        for name in sorted(changed_names):
            if name in after:
                attach_candidate(name, inside(workspace, name).read_bytes())
        stat = git(workspace, "diff", "--stat", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", "--").decode("utf-8", "strict")
        diff_section = ("（完整 diff 超限，已改用受控附件；基线、状态、变更哈希与附件清单均保留）\n" +
                        fenced(stat) + "\n\n" + fenced(json.dumps(attachment_manifest, ensure_ascii=False, indent=2), "json"))
    sections = ["# 审查包（输入一致性材料；不代表 R0 PASS）",
                f"任务：{args.task}\nattempt：{aid}\n工作区：{workspace}\n契约：{contract_path}\n"
                f"契约原始字节 SHA-256：{contract_raw_hash}\n契约规范化 SHA-256：{contract_hash}\n"
                f"attempt.contract_sha256 绑定依据：{attempt.get('contract_sha256')}\n基线：{base}",
                "## 完整契约\n" + fenced(json.dumps(contract, ensure_ascii=False, indent=2), "json"),
                "## 完整变更与磁盘哈希\n" + fenced(json.dumps(actual, ensure_ascii=False, indent=2), "json"),
                "## 基线 Git blob 与工作树换行证据\n" + fenced(json.dumps(git_content, ensure_ascii=False, indent=2), "json"),
                "## 受控候选附件\n" + fenced(json.dumps(attachment_manifest, ensure_ascii=False, indent=2), "json"),
                "## Git 状态\n" + fenced(status), "## 完整候选内容\n" + diff_section,
                "## 原始执行结果（未知保留 null）\n" + fenced(json.dumps(result, ensure_ascii=False, indent=2), "json"),
                "## 证据文件原始 SHA-256\n" + fenced(json.dumps({str(p): h for p, h in inputs.items()}, ensure_ascii=False, indent=2), "json"),
                "## 使用边界\n只读审查，不执行契约中的命令。Git 忽略文件不在快照范围内。"
                "审查前另行确认旧执行停稳／隔离、原生会话可追踪及一次发送预算。"
                "费用字段仅为原始记录，不代表账单支出。"]
    data = ("\n\n".join(sections) + "\n").encode("utf-8")
    if len(data) > args.max_packet_kb * 1024:
        raise ValueError("完整审查包超限；拒绝截断或先写后报错")
    # 一致性复读不替代原生停稳证明。
    if any(digest(p.read_bytes()) != h for p, h in inputs.items()) or snapshot(workspace) != current:
        raise ValueError("生成期间证据／工作区变化，拒绝出包")
    if digest(marker.read_bytes()) != result["git_marker_sha256"] or git(workspace, "rev-parse", "HEAD").decode().strip() != base:
        raise ValueError("生成期间 Git 基线变化")
    if git(workspace, *diff_args) != diff:
        raise ValueError("生成期间 diff 变化")
    attachment_dir = out.with_name(out.name + ".attachments")
    pending_dir = None
    if attachment_data:
        if attachment_dir.exists() or attachment_dir.is_symlink() or attachment_dir.is_junction():
            raise ValueError(f"拒绝覆盖已有附件目录：{attachment_dir}")
        pending_dir = out.with_name(out.name + "." + uuid.uuid4().hex + ".attachments.pending")
        pending_dir.mkdir()
        for filename, content in attachment_data.items():
            path = pending_dir / filename
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if digest(path.read_bytes()) != filename.removesuffix(".bin"):
                raise ValueError(f"附件回读 SHA-256 不一致，未发布：{path}")
        os.rename(pending_dir, attachment_dir)
    pending = out.with_name(out.name + "." + uuid.uuid4().hex + ".pending")
    # 先写 pending；I/O 失败时不发布正式包，保留 pending 供人工检查。
    with pending.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if digest(pending.read_bytes()) != digest(data):
        raise ValueError(f"输出回读哈希不一致；未发布，残留文件：{pending}")
    if os.name == "nt":
        os.rename(pending, out)  # Windows rename 拒绝覆盖并发出现的目标。
    else:
        os.link(pending, out)  # 原子且不覆盖；保留同内容 pending，不自动清理。
    return {"packet": str(out), "sha256": digest(data), "bytes": len(data), "attempt": aid,
            "revision": revision, "changed_files": len(changed_names),
            "attachments": str(attachment_dir) if attachment_data else None,
            "attachment_hashes": [filename.removesuffix(".bin") for filename in sorted(attachment_data)],
            "r0_acceptance": "BLOCKED",
            "note": "仅输入一致性；停止事实与独立验收尚需核证"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--attempt")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--max-diff-kb", type=int, default=60)
    ap.add_argument("--max-packet-kb", type=int, default=120)
    args = ap.parse_args()
    try:
        result = build(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
