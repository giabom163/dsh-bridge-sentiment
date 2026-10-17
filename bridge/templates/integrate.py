#!/usr/bin/env python3
"""自动并入（integrate）：把"拷文件 + LF + 提交 + 零请求自检"压成 1 步。

安全门（不满足就拒绝，绝不硬来）：
  1) 任务状态必须是 accepted 且 acceptance == PASS（否则拒绝——只有验收过的才允许并入）；
  2) worktree 里每个产物必须存在；
  3) 并入后仓库必须干净、且仓库内文件哈希 == worktree 内（LF 规范化后）的哈希。

用法：
  python -B integrate.py --store <store> --task <task_id> --repo <产品仓库> [--message "..."] [--dry-run]

退出码：0=并入成功（或 dry-run 可并入），1=拒绝（给出原因），2=执行失败。
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TEXT_SUFFIXES = {".py", ".md", ".json", ".js", ".ts", ".css", ".html", ".yml", ".yaml", ".toml", ".txt", ".sql"}


def run(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 127, "", f"{type(exc).__name__}: {exc}"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--attempt", default=None)
    ap.add_argument("--message", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = pathlib.Path(args.store)
    repo = pathlib.Path(args.repo)
    task_dir = store / "tasks" / args.task
    state = {}
    try:
        state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    attempt = args.attempt or state.get("current_attempt")
    att_dir = task_dir / "attempts" / attempt if attempt else None

    refusals = []
    if state.get("status") != "accepted":
        refusals.append(f"任务状态是 {state.get('status')}，只有 accepted 才允许并入")
    if state.get("acceptance") != "PASS":
        refusals.append(f"acceptance 是 {state.get('acceptance')}，只有 PASS 才允许并入")
    if not att_dir or not (att_dir / "workspace").is_dir():
        refusals.append(f"找不到 worktree：{att_dir}")
    if not (repo / ".git").exists():
        refusals.append(f"不是 git 仓库：{repo}")
    if refusals:
        print(json.dumps({"task": args.task, "verdict": "refused", "refusals": refusals}, ensure_ascii=False, indent=1))
        return 1

    worktree = att_dir / "workspace"
    result = {}
    try:
        result = json.loads((att_dir / "result.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    changes = [c for c in (result.get("changes") or []) if c.get("path")]
    if not changes:
        print(json.dumps({"task": args.task, "verdict": "refused",
                          "refusals": ["result.json 里没有 changes，无法确定要并入哪些文件"]}, ensure_ascii=False, indent=1))
        return 1

    # 仓库必须干净（避免把别人的改动一起提交）
    code, dirty, err = run(["git", "status", "--porcelain"], cwd=str(repo))
    if code != 0:
        print(json.dumps({"task": args.task, "verdict": "failed", "error": f"git status 失败：{err[:160]}"},
                         ensure_ascii=False, indent=1))
        return 2
    if dirty.strip():
        print(json.dumps({"task": args.task, "verdict": "refused",
                          "refusals": [f"仓库不干净，先处理：{dirty.strip()[:300]}"]}, ensure_ascii=False, indent=1))
        return 1

    plan = []
    for ch in changes:
        rel = ch["path"]
        src = worktree / rel
        if not src.is_file():
            print(json.dumps({"task": args.task, "verdict": "refused",
                              "refusals": [f"worktree 缺文件：{rel}"]}, ensure_ascii=False, indent=1))
            return 1
        raw = src.read_bytes()
        fixed = raw.replace(b"\r\n", b"\n") if src.suffix.lower() in TEXT_SUFFIXES else raw
        plan.append({"path": rel, "bytes": len(fixed), "lf_normalized": fixed != raw,
                     "sha256": hashlib.sha256(fixed).hexdigest(), "raw": fixed})

    if args.dry_run:
        print(json.dumps({"task": args.task, "verdict": "dry_run_ok", "attempt": attempt,
                          "would_copy": [{k: v for k, v in p.items() if k != "raw"} for p in plan],
                          "message": args.message or f"feat: integrate {args.task}"}, ensure_ascii=False, indent=1))
        return 0

    for p in plan:
        dst = repo / p["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(p["raw"])

    code, out, err = run(["git", "add"] + [p["path"] for p in plan], cwd=str(repo))
    if code != 0:
        print(json.dumps({"task": args.task, "verdict": "failed", "error": f"git add 失败：{err[:200]}"},
                         ensure_ascii=False, indent=1))
        return 2
    message = args.message or f"feat: integrate {args.task} (accepted, {len(plan)} file(s))"
    code, out, err = run(["git", "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                          "commit", "-q", "-m", message], cwd=str(repo))
    if code != 0:
        # 没有可提交的内容（已并入过）也算成功，但要说清楚
        code2, head, _ = run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo))
        print(json.dumps({"task": args.task, "verdict": "no_changes", "note": "没有差异（可能已并入过）",
                          "head": head.strip(), "stderr": err.strip()[:200]}, ensure_ascii=False, indent=1))
        return 0

    code, head, _ = run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo))
    code2, dirty2, _ = run(["git", "status", "--porcelain"], cwd=str(repo))
    problems = []
    if dirty2.strip():
        problems.append(f"并入后仓库不干净：{dirty2.strip()[:200]}")
    for p in plan:
        dst = repo / p["path"]
        if p["sha256"] != sha256(dst):
            problems.append(f"哈希不一致：{p['path']}")
    print(json.dumps({"task": args.task, "verdict": "integrated" if not problems else "verify_failed",
                      "attempt": attempt, "head": head.strip(), "message": message,
                      "files": [{k: v for k, v in p.items() if k != "raw"} for p in plan],
                      "problems": problems}, ensure_ascii=False, indent=1))
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
