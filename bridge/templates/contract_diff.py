#!/usr/bin/env python3
"""契约 revision 差异（contract_diff）：看清打回重跑时到底改了什么。

用法：
  python -B contract_diff.py --store <store> --task <task_id>            # 对比最新两版
  python -B contract_diff.py --store <store> --task <task_id> --from 1 --to 2
"""
import argparse
import json
import pathlib
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FIELDS = ["title", "goal", "repo", "executor", "model", "allowed_paths", "artifacts",
          "input_refs", "completion_criteria", "timeout_seconds", "opencode_mode"]


def latest_revisions(task_dir):
    found = {}
    for p in task_dir.glob("contract.r*.json"):
        m = re.match(r"contract\.r(\d+)\.json$", p.name)
        if m:
            found[int(m.group(1))] = p
    return found


def load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--from", dest="rev_from", type=int, default=None)
    ap.add_argument("--to", dest="rev_to", type=int, default=None)
    args = ap.parse_args()

    task_dir = pathlib.Path(args.store) / "tasks" / args.task
    revs = latest_revisions(task_dir)
    if not revs:
        print(json.dumps({"error": f"{task_dir} 下没有 contract.r*.json"}, ensure_ascii=False))
        return 2
    keys = sorted(revs)
    frm = args.rev_from or (keys[-2] if len(keys) >= 2 else keys[0])
    to = args.rev_to or keys[-1]
    if frm not in revs or to not in revs:
        print(json.dumps({"error": f"revision 不存在：from={frm} to={to} 可用={keys}"}, ensure_ascii=False))
        return 2

    a, b = load(revs[frm]), load(revs[to])
    changes = []
    for key in FIELDS:
        av, bv = a.get(key), b.get(key)
        if av != bv:
            if isinstance(av, list) or isinstance(bv, list):
                added = [x for x in (bv or []) if x not in (av or [])]
                removed = [x for x in (av or []) if x not in (bv or [])]
                changes.append({"field": key, "added": added, "removed": removed})
            else:
                changes.append({"field": key, "from": av, "to": bv})
    print(json.dumps({"task": args.task, "from": f"r{frm}", "to": f"r{to}",
                      "changed_fields": [c["field"] for c in changes],
                      "changes": changes, "unchanged": [k for k in FIELDS if k not in
                                                        [c["field"] for c in changes]]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
