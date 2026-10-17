#!/usr/bin/env python3
"""桥 store 清理（prune）：回收失败/取消 attempt 的 worktree。

**安全设计（本会话独立复核后重写）**：
  - **默认什么都不删**：必须显式 `--include-failed`（失败/取消/preparation_error 的 attempt）
    或 `--include-superseded`（accepted 任务里**非当前**的旧 attempt）才会进入候选；
  - **未知/不可读状态一律保留并告警**（fail-closed，与项目既有原则一致）；
  - `--protect <子串>` 可保护指定路径（如本会话自己的 workspace），命中即保留；
  - `--apply` 时逐条**核实删除结果**（路径确实不存在才计数），不再无条件 +1；
  - 只动 `attempts/<att>/workspace`，不动 result.json / 证据文件。

用法：
  python -B prune_stores.py --store <store> [--store ...]                       # 只报告
  python -B prune_stores.py --store <store> --include-failed                     # 报告候选
  python -B prune_stores.py --store <store> --include-failed --apply             # 真删（逐条核实）
"""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILED_STATUS = {"failed", "cancelled", "preparation_error"}
REVIEW_STATUS = {"accepted", "awaiting_review", "awaiting_user", "rework"}


def run(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 127, "", f"{type(exc).__name__}: {exc}"


def dir_size(path):
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", action="append", required=True)
    ap.add_argument("--include-failed", action="store_true", help="允许清理失败/取消任务的 attempt")
    ap.add_argument("--include-superseded", action="store_true", help="允许清理已接收任务里非当前的旧 attempt")
    ap.add_argument("--protect", action="append", default=[], help="路径包含该子串则保留（可多次）")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    candidates, kept = [], []
    for store in args.store:
        st = pathlib.Path(store)
        for state_path in sorted(st.glob("tasks/*/state.json")):
            task_dir = state_path.parent
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state_ok = isinstance(state, dict)
            except Exception:  # noqa: BLE001
                state, state_ok = {}, False
            status = state.get("status")
            current = state.get("current_attempt")
            for att_dir in sorted(task_dir.glob("attempts/*")):
                ws = att_dir / "workspace"
                if not ws.is_dir():
                    continue
                if any(pat and pat in str(ws) for pat in args.protect):
                    kept.append({"path": str(ws), "why": "命中 --protect"})
                    continue
                if not state_ok:
                    kept.append({"path": str(ws), "why": "state.json 不可读 → 保留（fail-closed）"})
                    continue
                is_current = att_dir.name == current
                if is_current:
                    kept.append({"path": str(ws), "why": "当前 attempt"})
                    continue
                if status in REVIEW_STATUS:
                    if args.include_superseded:
                        candidates.append({"path": str(ws), "task": task_dir.name,
                                           "attempt": att_dir.name, "status": status,
                                           "reason": "已接收任务的旧 attempt（--include-superseded）",
                                           "bytes": dir_size(ws)})
                    else:
                        kept.append({"path": str(ws), "why": f"已接收/待审任务（{status}），默认保留"})
                    continue
                if status in FAILED_STATUS:
                    if args.include_failed:
                        candidates.append({"path": str(ws), "task": task_dir.name,
                                           "attempt": att_dir.name, "status": status,
                                           "reason": "失败/取消（--include-failed）",
                                           "bytes": dir_size(ws)})
                    else:
                        kept.append({"path": str(ws), "why": f"失败/取消任务（{status}），默认保留证据"})
                    continue
                kept.append({"path": str(ws), "why": f"未知状态 {status!r} → 保留并告警"})

    total = sum(c["bytes"] for c in candidates)
    result = {"dry_run": not args.apply, "candidates": len(candidates),
              "candidate_mb": round(total / 1048576, 2),
              "items": candidates, "kept": len(kept), "kept_detail": kept,
              "flags": {"include_failed": args.include_failed,
                        "include_superseded": args.include_superseded,
                        "protect": args.protect}}

    if args.apply:
        removed, failed = 0, []
        for item in candidates:
            ws = pathlib.Path(item["path"])
            gitfile = ws.parent / ".git"
            if gitfile.is_file():
                txt = gitfile.read_text(encoding="utf-8", errors="replace")
                for line in txt.splitlines():
                    if line.startswith("gitdir:"):
                        gd = line.split(":", 1)[1].strip().rstrip("/\\")
                        repo = pathlib.Path(gd).parents[1]
                        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(ws)])
            try:
                if ws.exists():
                    shutil.rmtree(ws)
            except Exception as exc:  # noqa: BLE001
                failed.append({"path": item["path"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            if not ws.exists():          # 逐条核实，不无条件计数
                removed += 1
            else:
                failed.append({"path": item["path"], "error": "删除后路径仍存在"})
        result["removed"] = removed
        result["failed"] = failed
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
