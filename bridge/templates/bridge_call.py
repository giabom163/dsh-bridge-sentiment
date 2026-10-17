#!/usr/bin/env python3
"""桥命令包装（防吞错误）：跑桥 CLI 并**完整打印** stdout + stderr + 退出码。

为什么要有它：本会话出现过「reject 命令其实失败了，但我用 Select-String 过滤输出，
错误没匹配上就被丢掉」→ 账本以为打回了、其实没打回。这个包装器强制：**不过滤、不截断，
非 0 退出时把 stderr 原文完整打出**。

用法：
  python -B bridge_call.py --store <store> [--pyz <agent-bridge.pyz>] -- <桥命令与参数…>
  python -B bridge_call.py --store <store> -- status sentiment-s1-... 
  python -B bridge_call.py --store <store> -- review <task> --decision reject --reason "..."

退出码 = 桥命令的退出码（原样透传）。
"""
import argparse
import json
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PYZ = r"C:\Users\Lenovo\Documents\Codex\2026-10-14\seeker-agent-agent-agent\outputs\agent-bridge-installed\agent-bridge.pyz"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--pyz", default=DEFAULT_PYZ)
    ap.add_argument("--json-summary", action="store_true", help="额外打印一行摘要（从 JSON 里挑 status/acceptance）")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        print(json.dumps({"error": "缺少桥命令，例如：-- status <task>"}, ensure_ascii=False))
        return 64
    full = [sys.executable, "-B", args.pyz, "--limit", "3", "--store", args.store] + cmd
    p = subprocess.run(full, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out, err = p.stdout or "", p.stderr or ""
    print("=== exit:", p.returncode, "===")
    print("=== stdout ===")
    print(out if out.strip() else "(空)")
    print("=== stderr ===")
    print(err if err.strip() else "(空)")
    if args.json_summary:
        try:
            data = json.loads(out)
            keep = {k: data.get(k) for k in ("task_id", "status", "acceptance", "revision", "reviewable")}
            keep["product_review_verdict"] = (data.get("product_review") or {}).get("verdict")
            keep["human_decision"] = (data.get("human_accept") or {}).get("decision")
            print("=== summary ===")
            print(json.dumps(keep, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            print("=== summary === (stdout 不是 JSON，无法摘要)")
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
