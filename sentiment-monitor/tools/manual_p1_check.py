"""P1-A three-group isolated check. No HTTP service, no network, no product DB.

Groups:
  1. identity / replay dedup / same-title-but-different-id not merged /
     snapshot hash read-back and corruption rejection / extra (comment body) field rejected.
  2. exact vs approximate vs unknown counts, TopK order on the 120-30-80 offline sample,
     candidate-identity cap of 3, rejection of missing-zone or invalid observed_at.
  3. whole-batch rollback on one bad record, empty-store single-point + detail boundary,
     no feeds / no network on import, minimal legacy M1 regression.

Substantive pure-function checks only; server.py is memory-compiled and AST-mapped,
never imported as a running service. This is not GUI or product acceptance.
"""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import storage  # noqa: E402

GROUP1_LABEL = "P1 首次候选"
SAMPLE_SHA = "26e1a5ab8e3d64496557ae1c5ffc4be53853b969947dd61f54f28c918b7eca7f"
OTHER_SHA = "0123456789abcdef" * 4


def rec(**over):
    base = {
        "platform": "bilibili", "platform_id": "BV1mZpn6YE5R",
        "url": "https://www.bilibili.com/video/BV1mZpn6YE5R/",
        "title": "女教师在学校打拳，男孩一定要保护好自己", "source_name": "B站视频",
        "evidence_kind": "title_only", "content": "",
        "observed_at": "2026-10-16T08:55:55.128092+08:00",
        "published_at": "2026-10-12T11:05:00+08:00",
        "source_response_sha256": SAMPLE_SHA,
        "comment_count": {"value": 522, "precision": "exact", "source_field": "stat.reply"},
        "other_counts": {"view": {"value": 90455, "precision": "exact", "source_field": "stat.view"},
                         "like": {"value": 8660, "precision": "exact", "source_field": "stat.like"},
                         "danmaku": {"value": 75, "precision": "exact", "source_field": "stat.danmaku"}},
        "group_label": GROUP1_LABEL,
    }
    base.update(over)
    return base


def count(value, precision, field="stat.reply"):
    return {"value": value, "precision": precision, "source_field": field}


def new_db(run, name):
    return storage.connect(run / name)


def raises(exc_types, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_types as exc:
        return True, f"{type(exc).__name__}: {exc}"
    return False, "no exception"


def find_identity(overview, platform_id):
    for group in overview["groups"]:
        for entry in group["top"] + group["unranked"]:
            if entry["platform_id"] == platform_id:
                return entry
    return None


def scenario_one(run):
    checks = []
    db = new_db(run, "group1.db")
    # 1a. identity + first import
    result_a = storage.p1_import(db, run, [rec()], 3)
    checks.append({"check": "identity_first_import", "status": "PASS" if result_a["added"] == 1 and result_a["identity_total"] == 1 else "FAIL",
                   "detail": {"added": result_a["added"], "identity_total": result_a["identity_total"]}})
    # 1b. same title, different platform_id -> separate identity, not merged
    b = rec(platform_id="BV1mZpn6YE5S", url="https://www.bilibili.com/video/BV1mZpn6YE5S/",
            observed_at="2026-10-16T09:10:00+08:00", source_response_sha256=OTHER_SHA,
            comment_count=count(80, "exact"), other_counts={"view": count(5000, "exact", "stat.view")})
    result_b = storage.p1_import(db, run, [b], 3)
    db1_rows = [dict(r) for r in db.execute("SELECT * FROM p1_evidence WHERE platform_id IN ('BV1mZpn6YE5R','BV1mZpn6YE5S')")]
    same_title = {row["title"] for row in db1_rows} == {"女教师在学校打拳，男孩一定要保护好自己"}
    distinct_ids = {row["identity_key"] for row in db1_rows}
    checks.append({"check": "same_title_different_id_not_merged",
                   "status": "PASS" if (len(db1_rows) == 2 and same_title and len(distinct_ids) == 2 and result_b["added"] == 1) else "FAIL",
                   "detail": {"rows": len(db1_rows), "distinct_identity_keys": len(distinct_ids), "same_title": same_title}})
    # 1c. replay identical snapshot -> no new row
    replay = storage.p1_import(db, run, [rec()], 3)
    checks.append({"check": "replay_dedup", "status": "PASS" if replay["added"] == 0 and replay["duplicates"] == 1 and replay["identity_total"] == 2 else "FAIL",
                   "detail": {"added": replay["added"], "duplicates": replay["duplicates"], "identity_total": replay["identity_total"]}})
    # 1d. same platform+platform_id, new observation -> new snapshot, identity stays
    a2 = rec(url="https://www.bilibili.com/video/BV1mZpn6YE5R/?spm_id_from=333",
             observed_at="2026-10-16T10:30:00+08:00", comment_count=count(600, "exact"),
             other_counts={"view": count(100000, "exact", "stat.view")})
    result_a2 = storage.p1_import(db, run, [a2], 3)
    overview = storage.p1_overview(db, None)
    latest = find_identity(overview, "BV1mZpn6YE5R")
    # observed_at is stored normalized to UTC (M1 convention); the raw input is kept in observed_raw.
    a2_row = dict(db.execute("SELECT * FROM p1_evidence WHERE id=?", (result_a2["evidence_ids"][0],)).fetchone())
    checks.append({"check": "identity_new_observation_latest_value",
                   "status": "PASS" if (result_a2["added"] == 1 and result_a2["identity_total"] == 2 and
                                        latest is not None and latest["comment_count"]["value"] == 600 and
                                        latest["observed_at"] == storage.publication(a2["observed_at"])[0] and
                                        a2_row["observed_raw"] == a2["observed_at"]) else "FAIL",
                   "detail": {"added": result_a2["added"], "identity_total": result_a2["identity_total"],
                              "latest_value": latest["comment_count"]["value"] if latest else None,
                              "observed_at_utc": latest["observed_at"] if latest else None,
                              "observed_raw": a2_row["observed_raw"]}})
    # 1e. snapshot read-back verification, then corruption rejection
    row = dict(db.execute("SELECT * FROM p1_evidence WHERE platform_id='BV1mZpn6YE5R' ORDER BY observed_at DESC LIMIT 1").fetchone())
    detail = storage.p1_detail(db, run, row["id"])
    ok_detail = (detail is not None and detail["snapshot_sha256_verified"] and
                 detail["snapshot"]["platform_id"] == "BV1mZpn6YE5R" and
                 detail["snapshot"]["observed_at"] == a2["observed_at"])
    snap_file = run / row["snapshot_path"]
    snap_file.write_bytes(b'{"tampered":true}')
    ok_corrupt, corrupt_detail = raises((OSError, ValueError), storage.p1_detail, db, run, row["id"])
    checks.append({"check": "snapshot_hash_and_corruption_409",
                   "status": "PASS" if (ok_detail and ok_corrupt) else "FAIL",
                   "detail": {"detail_ok": ok_detail, "corruption_rejected": ok_corrupt, "corruption_error": corrupt_detail}})
    # 1f. extra comment-body field rejected and nothing added
    before = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    bad = rec()
    bad["comment_text"] = "这是评论正文，必须被拒绝"
    ok_extra, extra_detail = raises(ValueError, storage.p1_import, db, run, [bad], 3)
    bad2 = rec(platform_id="BV1mZpn6YE5T")
    bad2["replies"] = [{"text": "二级回复正文"}]
    ok_extra2, extra_detail2 = raises(ValueError, storage.p1_import, db, run, [bad2], 3)
    after = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    checks.append({"check": "extra_comment_body_rejected",
                   "status": "PASS" if (ok_extra and ok_extra2 and before == after) else "FAIL",
                   "detail": {"extra_rejected": ok_extra, "nested_replies_rejected": ok_extra2,
                              "delta_rows": after - before, "error": extra_detail}})
    db.close()
    return checks


def scenario_two(run):
    checks = []
    # 2a. TopK order on the 120-30-80 offline sample, view must not drive comment ranking.
    db = new_db(run, "group2a.db")
    for pid, value, view in (("top-120", 120, 1), ("top-30", 30, 999999), ("top-80", 80, 500)):
        storage.p1_import(db, run, [rec(platform="example.invalid", platform_id=pid,
                                        url=f"https://example.invalid/{pid}", title=f"样本 {pid}",
                                        source_response_sha256=OTHER_SHA, comment_count=count(value, "exact"),
                                        other_counts={"view": count(view, "exact", "stat.view")},
                                        group_label="排序样本")], 3)
    top3 = [e["comment_count"]["value"] for e in storage.p1_overview(db, 3)["groups"][0]["top"]]
    top2 = [e["comment_count"]["value"] for e in storage.p1_overview(db, 2)["groups"][0]["top"]]
    top1 = [e["comment_count"]["value"] for e in storage.p1_overview(db, 1)["groups"][0]["top"]]
    checks.append({"check": "topk_exact_desc_120_30_80", "status": "PASS" if top3 == [120, 80, 30] else "FAIL",
                   "detail": {"top_k_3": top3, "top_k_2": top2, "top_k_1": top1, "insertion_order": [120, 30, 80]}})
    db.close()
    # 2b. approximate / unknown listed separately, never converted to zero, never ranked.
    db = new_db(run, "group2b.db")
    storage.p1_import(db, run, [
        rec(platform_id="p-exact", url="https://example.invalid/exact", title="精确",
            source_response_sha256=OTHER_SHA, comment_count=count(120, "exact"), group_label="精度样本"),
        rec(platform_id="p-approx", url="https://example.invalid/approx", title="约数",
            source_response_sha256=OTHER_SHA, comment_count=count(90, "approximate"), group_label="精度样本"),
        rec(platform_id="p-unknown", url="https://example.invalid/unknown", title="未知",
            source_response_sha256=OTHER_SHA, comment_count=count(None, "unknown"), group_label="精度样本"),
    ], 3)
    ov = storage.p1_overview(db, 3)
    group = ov["groups"][0]
    ranked_values = [e["comment_count"]["value"] for e in group["top"]]
    unranked = {e["platform_id"]: e for e in group["unranked"]}
    unknown_value = unranked["p-unknown"]["comment_count"]["value"]
    checks.append({"check": "precision_separation",
                   "status": "PASS" if (ranked_values == [120] and set(unranked) == {"p-approx", "p-unknown"} and
                                        unknown_value is None and
                                        unranked["p-approx"]["rank_reason"] == "comment_count_approximate" and
                                        unranked["p-unknown"]["rank_reason"] == "comment_count_unknown") else "FAIL",
                   "detail": {"ranked": ranked_values, "unranked_ids": sorted(unranked),
                              "unknown_value": unknown_value,
                              "approx_reason": unranked.get("p-approx", {}).get("rank_reason")}})
    db.close()
    # 2c. candidate identity cap of 3; a fourth identity rejects the whole batch.
    db = new_db(run, "group2c.db")
    for pid in ("cap-1", "cap-2", "cap-3"):
        storage.p1_import(db, run, [rec(platform_id=pid, url=f"https://example.invalid/{pid}",
                                        title=f"上限 {pid}", source_response_sha256=OTHER_SHA,
                                        comment_count=count(10, "exact"), group_label="上限样本")], 3)
    ok_cap, cap_detail = raises(storage.P1LimitExceeded, storage.p1_import, db, run,
                                [rec(platform_id="cap-4", url="https://example.invalid/cap-4", title="上限 cap-4",
                                     source_response_sha256=OTHER_SHA, comment_count=count(10, "exact"),
                                     group_label="上限样本")], 3)
    total = db.execute("SELECT count(DISTINCT identity_key) FROM p1_evidence").fetchone()[0]
    checks.append({"check": "candidate_identity_cap_3",
                   "status": "PASS" if (ok_cap and total == 3) else "FAIL",
                   "detail": {"limit_exceeded_raised": ok_cap, "identity_total": total, "error": cap_detail}})
    db.close()
    # 2d. missing-zone / invalid observed_at rejected.
    db = new_db(run, "group2d.db")
    cases = {"no_timezone": "2026-10-16T08:55:55", "invalid": "2026-13-40T99:99:99+08:00",
             "garbage": "not-a-time", "empty": ""}
    results = {}
    for name, value in cases.items():
        results[name] = raises(ValueError, storage.p1_import, db, run, [rec(observed_at=value)], 3)[0]
    checks.append({"check": "observed_at_requires_valid_zone",
                   "status": "PASS" if all(results.values()) else "FAIL", "detail": results})
    db.close()
    return checks


def scenario_three(run):
    checks = []
    # 3a. one bad record in a batch rejects the whole batch, nothing persisted.
    db = new_db(run, "group3a.db")
    good = rec(platform_id="tx-good", url="https://example.invalid/good", title="好记录",
               source_response_sha256=OTHER_SHA, comment_count=count(5, "exact"))
    bool_bad = rec(platform_id="tx-bool", url="https://example.invalid/bool", title="bool 计数",
                   source_response_sha256=OTHER_SHA, comment_count={"value": True, "precision": "exact", "source_field": "s"})
    ok_bool, bool_detail = raises(ValueError, storage.p1_import, db, run, [good, bool_bad], 3)
    rows_after_bool = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    neg_bad = rec(platform_id="tx-neg", url="https://example.invalid/neg", title="负数",
                  source_response_sha256=OTHER_SHA, comment_count=count(-5, "exact"))
    ok_neg, neg_detail = raises(ValueError, storage.p1_import, db, run, [good, neg_bad], 3)
    rows_after_neg = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    checks.append({"check": "whole_batch_rollback_one_bad",
                   "status": "PASS" if (ok_bool and ok_neg and rows_after_bool == 0 and rows_after_neg == 0) else "FAIL",
                   "detail": {"bool_rejected": ok_bool, "negative_rejected": ok_neg,
                              "rows_after_bool_batch": rows_after_bool, "rows_after_neg_batch": rows_after_neg,
                              "error": bool_detail}})
    db.close()
    # 3b. empty store single-point + detail boundary.
    db = new_db(run, "group3b.db")
    ov = storage.p1_overview(db, 3)
    detail = storage.p1_detail(db, run, "p1ev-does-not-exist")
    checks.append({"check": "empty_state_and_detail_boundary",
                   "status": "PASS" if (ov["identity_total"] == 0 and ov["groups"] == [] and ov["empty_state"] is True and detail is None) else "FAIL",
                   "detail": {"identity_total": ov["identity_total"], "groups": ov["groups"], "unknown_detail": detail}})
    db.close()
    # 3c. import touches no feeds and makes no network call.
    db = new_db(run, "group3c.db")
    with db:
        db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(1,'保留来源','https://example.invalid/feed',?)", (storage.now(),))
    feeds_before = db.execute("SELECT count(*) FROM feeds").fetchone()[0]
    records_before = db.execute("SELECT count(*) FROM records").fetchone()[0]
    runs_before = db.execute("SELECT count(*) FROM collection_runs").fetchone()[0]
    real_connect = socket.socket.connect
    real_urlopen = urllib.request.urlopen

    def blocked(*_args, **_kwargs):
        raise AssertionError("P1 导入触发了网络访问")

    socket.socket.connect = blocked
    urllib.request.urlopen = blocked
    try:
        result = storage.p1_import(db, run, [rec(source_response_sha256=OTHER_SHA)], 3)
    finally:
        socket.socket.connect = real_connect
        urllib.request.urlopen = real_urlopen
    feeds_after = db.execute("SELECT count(*) FROM feeds").fetchone()[0]
    records_after = db.execute("SELECT count(*) FROM records").fetchone()[0]
    runs_after = db.execute("SELECT count(*) FROM collection_runs").fetchone()[0]
    checks.append({"check": "no_feeds_no_network",
                   "status": "PASS" if (result["added"] == 1 and result["network_requests"] == 0 and
                                        feeds_after == feeds_before and records_after == records_before and
                                        runs_after == runs_before) else "FAIL",
                   "detail": {"added": result["added"], "network_requests": result["network_requests"],
                              "feeds": [feeds_before, feeds_after], "records": [records_before, records_after],
                              "runs": [runs_before, runs_after]}})
    db.close()
    # 3d. minimal legacy M1 regression in a fresh isolated DB.
    db = new_db(run, "group3d.db")
    with db:
        db.execute("INSERT INTO feeds(id,name,url,added_at) VALUES(1,'M1 回归','https://example.invalid/m1',?)", (storage.now(),))
    run_id = storage.start_run(db, 1, "m1_regression", 3)
    storage.import_batch(db, run, run_id, [{"title": "M1 最小回归标题", "url": "https://example.invalid/m1/1"}],
                         lambda _: "中性/待判")
    stats = storage.data(db)["stats"]
    run_row = dict(db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone())
    checks.append({"check": "legacy_m1_minimal_regression",
                   "status": "PASS" if (stats["record_count"] == 1 and stats["canonical_count"] == 1 and
                                        stats["unknown_time_count"] == 1 and run_row["status"] == "succeeded") else "FAIL",
                   "detail": {"stats": stats, "run_status": run_row["status"]}})
    db.close()
    return checks


def static_checks():
    checks = []
    # memory-only compile; never import server as a running service
    for name in ("server.py", "storage.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        try:
            compile(source, name, "exec")
            checks.append({"check": f"memory_compile_{name}", "status": "PASS", "detail": "compile() ok"})
        except SyntaxError as exc:
            checks.append({"check": f"memory_compile_{name}", "status": "FAIL", "detail": str(exc)})
    schema = json.loads((ROOT / "schemas" / "p1-evidence.schema.json").read_text(encoding="utf-8"))
    item = schema["properties"]["records"]["items"]
    ok_schema = (schema["additionalProperties"] is False and item["additionalProperties"] is False and
                 schema["properties"]["records"].get("maxItems") == 3 and
                 set(item["required"]) >= {"platform", "url", "title", "source_name", "evidence_kind",
                                           "content", "observed_at", "source_response_sha256", "comment_count"})
    checks.append({"check": "p1_schema_isolated_and_strict", "status": "PASS" if ok_schema else "FAIL",
                   "detail": {"additionalProperties_false": schema["additionalProperties"] is False,
                              "item_required": sorted(item["required"])}})
    # AST route mapping (static; not an HTTP acceptance)
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    listed, prefixed = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    listed.add(comp.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and
                isinstance(node.func.value, ast.Name) and node.func.value.id == "path" and node.args and
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            prefixed.add(node.args[0].value)
    routes_ok = all(r in listed for r in ("/api/p1/import", "/api/p1/overview")) and "/api/p1/evidence/" in prefixed
    checks.append({"check": "p1_route_mapping_ast", "status": "PASS" if routes_ok else "FAIL",
                   "detail": {"equality_routes": sorted(listed), "prefix_routes": sorted(prefixed),
                              "note": "静态 AST 映射，不等于 HTTP 验收"}})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.limit != 3:
        parser.error("首次且仅支持的定向检查为 --limit 3")
    work = args.work_dir.resolve()
    run = work / datetime.now(timezone.utc).strftime("p1-%Y%m%dT%H%M%S.%fZ")
    run.mkdir(parents=True, exist_ok=False)
    source_hashes = {name: storage.digest((ROOT / name).read_bytes())
                     for name in ("server.py", "storage.py", "schemas/p1-evidence.schema.json")}
    groups = [("group1_identity_snapshot", scenario_one), ("group2_counts_topk_limits", scenario_two),
              ("group3_transaction_boundaries", scenario_three)]
    report = {"status": "PASS", "scope": "isolated_local_fixture_only", "product_acceptance": "BLOCKED",
              "gui_acceptance": False, "network_requests": 0, "product_db_touched": False,
              "tests_limit": args.limit, "run_dir": str(run), "source_sha256": source_hashes, "groups": {}}
    static = static_checks()
    report["groups"]["group0_static"] = static
    for name, fn in groups:
        try:
            checks = fn(run)
        except Exception:
            checks = [{"check": f"{name}_unexpected_error", "status": "FAIL", "detail": traceback.format_exc()}]
        report["groups"][name] = checks
    failures = [c for group in report["groups"].values() for c in group if c["status"] != "PASS"]
    if failures:
        report["status"] = "FAIL"
        report["failures"] = [{"check": c["check"], "detail": c.get("detail")} for c in failures]
    (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for group_name, checks in report["groups"].items():
        (run / f"{group_name}.json").write_text(
            json.dumps({"group": group_name, "status": "FAIL" if any(c["status"] != "PASS" for c in checks) else "PASS",
                        "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "run_dir": str(run),
                      "summary": {k: {"total": len(v), "failed": sum(c["status"] != "PASS" for c in v)}
                                  for k, v in report["groups"].items()}}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
