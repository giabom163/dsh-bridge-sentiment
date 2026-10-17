#!/usr/bin/env python3
"""契约体检（check_contract）：在 submit 之前把"包太大/漏字段/无自检"拦住。

硬规则（本会话踩出来的）：
  - 单契约改动文件数 > 2 → 建议拆包（3 次 900s 超时多因包大）；
  - 产物数 > 3 → 建议拆包；
  - timeout_seconds 必须存在且 120 ≤ t ≤ 900；
  - goal 必须包含"复用"与"selfcheck"（交付前自检）；
  - completion_criteria ≥ 3 条；
  - 文件不得带 BOM、不得有 CRLF（桥的输入哈希按磁盘字节算）。

用法：
  python -B check_contract.py <contract.json> [--max-files 2] [--max-artifacts 3] [--max-timeout 900]

退出码：0=pass，1=warn，2=fail。
"""
import argparse
import json
import pathlib
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REQUIRED = ["title", "goal", "repo", "executor", "allowed_paths", "artifacts",
            "completion_criteria", "timeout_seconds"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-files", type=int, default=2)
    ap.add_argument("--max-artifacts", type=int, default=3)
    ap.add_argument("--max-timeout", type=int, default=900)
    ap.add_argument("--max-goal-chars", type=int, default=4000, help="goal 字符上限（防命令行过长）")
    ap.add_argument("--max-bytes", type=int, default=20000, help="契约文件字节上限")
    args = ap.parse_args()

    p = pathlib.Path(args.path)
    problems, warnings, checks = [], [], []

    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("文件带 UTF-8 BOM（桥按磁盘字节算哈希，可能不一致）")
    if b"\r\n" in raw:
        problems.append("文件含 CRLF（仓库 eol=lf 时会造成哈希不一致）")

    try:
        contract = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"path": str(p), "verdict": "fail",
                          "problems": [f"JSON 解析失败：{exc}"]}, ensure_ascii=False, indent=1))
        return 2

    is_template = "_template" in contract
    missing = [k for k in REQUIRED if k not in contract]
    if not is_template and "task_id" not in contract:
        missing.append("task_id")
    checks.append({"id": "required_fields", "ok": not missing, "detail": {"missing": missing}})
    if missing:
        problems.append(f"缺字段：{missing}")

    allowed = contract.get("allowed_paths") or []
    artifacts = contract.get("artifacts") or []
    code_files = [a for a in artifacts if not a.startswith("docs/")]
    checks.append({"id": "package_size", "ok": len(code_files) <= args.max_files,
                   "detail": {"code_artifacts": len(code_files), "max": args.max_files,
                              "artifacts_total": len(artifacts)}})
    if len(code_files) > args.max_files:
        problems.append(f"代码产物 {len(code_files)} 个 > 上限 {args.max_files}：建议拆包（本会话超时多因包大）")
    if len(artifacts) > args.max_artifacts:
        warnings.append(f"产物 {len(artifacts)} 个 > {args.max_artifacts}，考虑精简")

    timeout = contract.get("timeout_seconds")
    t_ok = isinstance(timeout, int) and 120 <= timeout <= args.max_timeout
    checks.append({"id": "timeout", "ok": t_ok,
                   "detail": {"timeout_seconds": timeout, "range": [120, args.max_timeout]}})
    if not t_ok:
        problems.append(f"timeout_seconds={timeout} 不在 120–{args.max_timeout} 之间（按任务类型设，别一律 900）")

    goal = contract.get("goal") or ""
    checks.append({"id": "goal_reuse_and_selfcheck",
                   "ok": ("复用" in goal) and ("selfcheck" in goal.lower()),
                   "detail": {"has_reuse": "复用" in goal, "has_selfcheck": "selfcheck" in goal.lower()}})
    if "复用" not in goal:
        warnings.append("goal 里没有「复用优先」要求（模板里有，建议保留）")
    if "selfcheck" not in goal.lower():
        warnings.append("goal 里没有要求执行方先跑 selfcheck.py（会降低首次通过率）")

    crit = contract.get("completion_criteria") or []
    checks.append({"id": "criteria", "ok": len(crit) >= 3,
                   "detail": {"count": len(crit)}})
    if len(crit) < 3:
        warnings.append("completion_criteria 少于 3 条")

    # 载荷体积门：预防 Windows 命令行过长（WinError 206）与审查方超时。
    # 实测事故：R0 审查第 2 轮因内联输入过长，进程 2 秒就死在 CreateProcess。
    goal_chars = len(goal)
    packet_bytes = len(raw)
    checks.append({"id": "payload_size",
                   "ok": goal_chars <= args.max_goal_chars and packet_bytes <= args.max_bytes,
                   "detail": {"goal_chars": goal_chars, "max_goal_chars": args.max_goal_chars,
                              "contract_bytes": packet_bytes, "max_bytes": args.max_bytes}})
    if goal_chars > args.max_goal_chars:
        problems.append(f"goal 长 {goal_chars} 字符 > 上限 {args.max_goal_chars}："
                        "派发/审查时极可能触发 Windows 命令行过长或审查超时；请把细节移到 docs/ 与证据文件，goal 只留契约要点")
    if packet_bytes > args.max_bytes:
        problems.append(f"契约文件 {packet_bytes} 字节 > 上限 {args.max_bytes}：同上")

    verdict = "fail" if problems else ("warn" if warnings else "pass")
    print(json.dumps({"path": str(p), "template": is_template, "verdict": verdict,
                      "checks": checks, "problems": problems, "warnings": warnings,
                      "next": ("改完再 submit" if verdict == "fail" else
                               "可以 submit" if verdict == "pass" else
                               "可以 submit（但有建议）")}, ensure_ascii=False, indent=1))
    return {"pass": 0, "warn": 1, "fail": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
