#!/usr/bin/env python3
"""成本看板（cost_report）：把桥账本里的花费与时长汇总成一页。

口径（如实标注）：
  - `native_cost` 只在 shared 模式有回读；standalone 没有 → 用**实测费率**估算（默认按 duration × rate）；
  - 费率默认从"有 native_cost 的 attempt"反推；也可 --rate 指定（USD/秒）；
  - 人民币换算 --cny 7.1（默认）。

用法：
  python -B cost_report.py --store <store> [--store <store2> ...] [--rate auto] [--cny 7.1] [--json]
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", action="append", required=True)
    ap.add_argument("--rate", default="auto")
    ap.add_argument("--cny", type=float, default=7.1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = []
    for store in args.store:
        st = pathlib.Path(store)
        pattern = "tasks/*/attempts/*"
        for att_dir in sorted(st.glob(pattern)):
            task = att_dir.parent.parent.name
            started = finished = None
            ap_path = att_dir / "attempt.json"
            if ap_path.is_file():
                try:
                    started = parse(json.loads(ap_path.read_text(encoding="utf-8")).get("started_at"))
                except Exception:  # noqa: BLE001
                    pass
            cost = tokens = None
            rp = att_dir / "result.json"
            if rp.is_file():
                try:
                    d = json.loads(rp.read_text(encoding="utf-8"))
                    finished = parse(d.get("finished_at"))
                    nt = d.get("native_cost") or {}
                    cost, tokens = nt.get("cost"), nt.get("tokens")
                except Exception:  # noqa: BLE001
                    pass
            secs = None
            if started and finished:
                try:
                    secs = (finished - started).total_seconds()   # 混用 naive/aware 会 TypeError
                except TypeError:
                    secs = None
            rows.append({"store": st.parent.name, "task": task, "attempt": att_dir.name[-12:],
                         "secs": secs, "cost": cost if isinstance(cost, (int, float)) else None,
                         "tokens": tokens})

    measured = [(r["secs"], r["cost"]) for r in rows if r["cost"] and r["secs"]]
    if args.rate not in ("auto", None):
        rate = float(args.rate)
    elif measured:
        rate = sum(c for _, c in measured) / sum(s for s, _ in measured)
    else:
        # 没有实测样本时不得凭空造数（项目规则：费用未实证则 null）
        rate = None

    total_secs = total_usd = 0.0
    for r in rows:
        if r["cost"] is not None:
            r["est_usd"] = r["cost"]
        elif rate is None:
            r["est_usd"] = None                     # 无法估算 → null，不写 0
        else:
            r["est_usd"] = (r["secs"] or 0) * rate
        total_secs += r["secs"] or 0
        total_usd += r["est_usd"] or 0.0
        tok = r["tokens"] or {}
        r["tok_in"] = tok.get("input")
        r["tok_out"] = tok.get("output")
        r["tok_reason"] = tok.get("reasoning")

    measured_usd = sum(r["est_usd"] or 0.0 for r in rows if r["cost"] is not None)
    estimated_usd = sum(r["est_usd"] or 0.0 for r in rows if r["cost"] is None)
    summary = {"attempts": len(rows), "measured": len(measured),
               "measured_attempts": sum(1 for r in rows if r["cost"] is not None),
               "estimated_attempts": sum(1 for r in rows if r["cost"] is None),
               "measured_usd": round(measured_usd, 6),
               "estimated_usd": round(estimated_usd, 6),
               "rate_usd_per_sec": (round(rate, 8) if rate is not None else None),
               "unestimable_attempts": sum(1 for r in rows if r["est_usd"] is None),
               "total_minutes": round(total_secs / 60, 1),
               "total_usd": round(total_usd, 6), "total_cny": round(total_usd * args.cny, 4),
               "by_store": {}}
    for r in rows:
        b = summary["by_store"].setdefault(r["store"], {"attempts": 0, "usd": 0.0})
        b["attempts"] += 1
        b["usd"] = round(b["usd"] + (r["est_usd"] or 0.0), 6)

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1))
        return 0

    print(f"# 成本看板（{len(rows)} 个 attempt）")
    print()
    if rate is None:
        print(f"- 实测计量的 attempt：**{len(measured)}** 个；**无法估算**（无实测样本 → 费用记 null，"
              "遵守「未实证不造数」；用 `--rate <USD/秒>` 可给出估算）")
        print(f"- 实测 $0.0000（0 个有 native_cost）｜ 估算 **null**（{summary['estimated_attempts']} 个）")
    else:
        print(f"- 实测计量的 attempt：**{len(measured)}** 个；费率反推 = ${rate:.8f}/s（含思考 token）")
        print(f"- **实测** ${measured_usd:.4f}（{summary['measured_attempts']} 个 attempt 有 native_cost）"
              f" ｜ **估算** ${estimated_usd:.4f}（{summary['estimated_attempts']} 个，按墙钟 × 实测费率；"
              f"墙钟含排队/人工等待，偏高）｜ 合计 ${total_usd:.4f} ≈ ¥{total_usd * args.cny:.2f}")
    print(f"- 总时长 {total_secs/60:.1f} 分钟")
    print()
    print("| 账本 | attempt | 估算(USD) |")
    print("|---|---|---|")
    for name, b in summary["by_store"].items():
        print(f"| {name} | {b['attempts']} | ${b['usd']:.4f} |")
    print()
    print("| 账本 | 任务 | attempt | 时长 | 实测/估算 | tokens(in/out/reas) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        mark = "实测" if r["cost"] is not None else "估算"
        tok = f"{r['tok_in'] or '-'}/{r['tok_out'] or '-'}/{r['tok_reason'] or '-'}"
        print(f"| {r['store']} | {r['task'][:34]} | {r['attempt']} | {round(r['secs']) if r['secs'] else '-'}s | "
              f"{mark} ${r['est_usd']:.4f} | {tok} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
