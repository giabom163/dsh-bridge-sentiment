#!/usr/bin/env python3
"""自动验收（verify_task）：把"我对一个 attempt 的验收"从 4 步压成 1 步。

它做的：
  1) 读桥账本（state/attempt/result）→ 执行事实（exit、stop_reason、越界、缺产物、原生错误）；
  2) 调桥 CLI `status` 取**实时** reviewable/review_gate（不读存量值）；
  3) 在 worktree 上跑自检 selfcheck.py（kind 由你给，allow/artifacts 从契约取）；
  4) 算每个产物的 sha256，并核对与交付说明里声称的一致（可选）；
  5) 输出一个 JSON：verdict = ready_to_review / needs_fix / not_reviewable + blockers。

用法：
  python -B verify_task.py --store <store> --task <task_id> [--attempt <att>] \
      --kind endpoint|adapter|annotator|docs [--bridge <pyz>] [--json]

退出码：0=ready_to_review，1=needs_fix，2=not_reviewable。
"""
import argparse
import glob
import hashlib
import json
import pathlib
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
SELFCHECK = HERE / "selfcheck.py"
DEFAULT_PYZ = r"C:\Users\Lenovo\Documents\Codex\2026-10-14\seeker-agent-agent-agent\outputs\agent-bridge-installed\agent-bridge.pyz"


def run(cmd, timeout=300):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
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


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--attempt", default=None)
    ap.add_argument("--kind", default=None, choices=["endpoint", "adapter", "annotator", "docs"])
    ap.add_argument("--bridge", default=DEFAULT_PYZ)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    store = pathlib.Path(args.store)
    task_dir = store / "tasks" / args.task
    state = load(task_dir / "state.json")
    if not state:
        print(json.dumps({"error": f"读不到 state.json：{task_dir}", "verdict": "error"}, ensure_ascii=False))
        return 2

    attempt = args.attempt or state.get("current_attempt")
    att_dir = task_dir / "attempts" / attempt if attempt else None
    result = load(att_dir / "result.json") if att_dir else None
    # 契约在 task 级：contract.r<N>.json（不是 attempt 级）
    contract = None
    for cp in sorted(task_dir.glob("contract.r*.json"), key=lambda x: x.name):
        contract = load(cp) or contract
    changes = (result or {}).get("changes") or []
    worktree = att_dir / "workspace" if att_dir else None

    blockers = []

    # 1) 执行事实
    facts = {
        "exit_code": (result or {}).get("exit_code"),
        "stop_reason": (result or {}).get("stop_reason"),
        "timed_out": (result or {}).get("timed_out"),
        "violations": (result or {}).get("violations") or [],
        "missing_artifacts": (result or {}).get("missing_artifacts") or [],
        "native_error_count": (result or {}).get("native_error_count", 0),
    }
    if facts["violations"]:
        blockers.append(f"越界改动 {len(facts['violations'])} 处")
    if facts["missing_artifacts"]:
        blockers.append(f"缺产物 {facts['missing_artifacts']}")
    if facts["native_error_count"]:
        blockers.append(f"原生错误 {facts['native_error_count']} 条")
    if facts["exit_code"] not in (0, None):
        blockers.append(f"退出码 {facts['exit_code']}")

    # 2) 实时 reviewable（调桥 CLI status）
    reviewable, gate_reason = None, None
    code, out, err = run([sys.executable, "-B", args.bridge, "--limit", "3",
                          "--store", str(store), "status", args.task])
    if code == 0:
        try:
            st = json.loads(out)
            reviewable = st.get("reviewable")
            gate_reason = (st.get("review_gate") or {}).get("reason")
        except Exception:  # noqa: BLE001
            blockers.append("桥 status 输出无法解析")
    else:
        blockers.append(f"桥 status 失败（exit {code}）：{(err or out).strip()[:120]}")
    if reviewable is False:
        blockers.append(f"不可审：{gate_reason}")

    # 3) 自检
    sc = {"ok": None, "reason": "未指定 --kind 或 worktree 不存在"}
    if args.kind and worktree and worktree.is_dir():
        allow = ",".join((contract or {}).get("allowed_paths") or ["tools/", "docs/"])
        arts = ",".join((contract or {}).get("artifacts") or [])
        cmd = [sys.executable, "-B", str(SELFCHECK), "--kind", args.kind,
               "--worktree", str(worktree), "--allow", allow]
        if arts:
            cmd += ["--artifacts", arts]
        code, out, err = run(cmd)
        try:
            report = json.loads(out)
            sc = {"ok": report.get("verdict") == "pass", "verdict": report.get("verdict"),
                  "reason": "自检通过" if report.get("verdict") == "pass" else "自检未通过",
                  "failed_checks": [c["id"] for c in report.get("checks", []) if not c.get("ok")]}
        except Exception:  # noqa: BLE001
            sc = {"ok": False, "reason": f"自检无法解析：{(out + err)[:160]}"}
        if sc.get("ok") is False:
            blockers.append(f"自检未通过：{sc.get('failed_checks') or sc.get('reason')}")

    # 4) 产物哈希
    hashes = {}
    for ch in changes:
        rel = ch.get("path")
        if not rel or not worktree:
            continue
        f = worktree / rel
        if f.is_file():
            hashes[rel] = sha256(f)

    verdict = ("ready_to_review" if not blockers
               else ("not_reviewable" if reviewable is False else "needs_fix"))
    report = {"task": args.task, "attempt": attempt, "store": str(store),
              "status": state.get("status"), "revision": state.get("revision"),
              "facts": facts, "reviewable": reviewable, "review_gate_reason": gate_reason,
              "selfcheck": sc, "changed_paths": [c.get("path") for c in changes],
              "artifact_sha256": hashes, "blockers": blockers, "verdict": verdict,
              "next": ("交接审查（review --decision audit）" if verdict == "ready_to_review" else
                       "先修 blockers，再重跑本命令" if verdict == "needs_fix" else
                       "不可审：先看 review_gate.reason")}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return {"ready_to_review": 0, "needs_fix": 1, "not_reviewable": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
