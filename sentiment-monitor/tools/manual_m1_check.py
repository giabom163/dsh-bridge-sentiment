"""User-run three-case M1 acceptance. Never touches the product DB or network."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.limit != 3:
        parser.error("首次且仅支持 --limit 3，三个样本/三个检查项")
    work = args.work_dir.resolve()
    # Parent may be existing; create a unique run directory without cleanup.
    run = work / datetime.now(timezone.utc).strftime("m1-%Y%m%dT%H%M%S.%fZ")
    run.mkdir(parents=True, exist_ok=False)
    db = storage.connect(run / "samples.db")
    checks = []
    status = "FAIL"
    error = None
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
              for name in ("server.py", "storage.py")}
    try:
        with db:
            db.executemany("INSERT INTO feeds(id,name,url,added_at) VALUES(?,?,?,?)",
                           [(1, "样本甲", "https://example.invalid/a", storage.now()),
                            (2, "样本乙", "https://example.invalid/b", storage.now())])
        samples = [
            {"title": "样本共同内容", "summary": "完全相同的正文", "url": "https://example.invalid/1", "published_at": None},
            {"title": "样本共同内容", "summary": "完全相同的正文", "url": "https://example.invalid/2", "published_at": "2026-10-15T10:00:00+08:00"},
            {"title": "独立内容", "summary": "另一条独立正文", "url": "https://example.invalid/3", "published_at": "2026-10-15T03:00:00Z"},
        ]
        batches = [(1, samples[:1]), (2, samples[1:])]
        for feed, rows in batches:
            rid = storage.start_run(db, feed, "sample", 3)
            storage.import_batch(db, run, rid, rows, lambda text: "中性/待判")
        stats = storage.data(db)["stats"]
        if (stats["record_count"], stats["canonical_count"], stats["unknown_time_count"]) != (3, 2, 1):
            raise AssertionError(stats)
        if db.execute("SELECT count(*) FROM canonical_records").fetchone()[0] != 2:
            raise AssertionError("canonical table has unexpected rows")
        missing = db.execute("SELECT * FROM records WHERE feed_id=1").fetchone()
        if missing["published_at"] is not None or missing["time_quality"] != "missing" or not missing["fetched_at"]:
            raise AssertionError("missing publication time was not preserved")
        if db.execute("SELECT count(DISTINCT feed_id) FROM records").fetchone()[0] != 2:
            raise AssertionError("source provenance was lost")
        for row in db.execute("SELECT raw_path,raw_sha256 FROM records"):
            if storage.digest((run / row["raw_path"]).read_bytes()) != row["raw_sha256"]:
                raise AssertionError("snapshot mismatch")
        checks.append({"case": "three_samples_and_provenance", "status": "PASS", "stats": stats})
        for feed, rows in batches:
            rid = storage.start_run(db, feed, "replay", 3)
            result = storage.import_batch(db, run, rid, rows, lambda text: "中性/待判")
            if result["added"] != 0 or result["duplicates"] != len(rows):
                raise AssertionError(result)
        if storage.data(db)["stats"] != stats:
            raise AssertionError("replay changed record statistics")
        checks.append({"case": "idempotent_replay", "status": "PASS"})
        rid = storage.start_run(db, 1, "rollback", 3)
        calls = []

        def fail_second(text):
            calls.append(text)
            if len(calls) == 2:
                raise ValueError("intentional second-record write failure")
            return "中性/待判"

        rows = [{"title": "回滚样本一", "url": "https://example.invalid/rollback/1"},
                {"title": "回滚样本二", "url": "https://example.invalid/rollback/2"}]
        try:
            storage.import_batch(db, run, rid, rows, fail_second)
        except ValueError as exc:
            if str(exc) != "intentional second-record write failure":
                raise
        else:
            raise AssertionError("fault injection did not fail")
        if (len(calls) != 2 or storage.data(db)["stats"] != stats or
                db.execute("SELECT count(*) FROM canonical_records").fetchone()[0] != 2):
            raise AssertionError("partial record/canonical transaction persisted")
        failed = dict(db.execute("SELECT * FROM collection_runs WHERE id=?", (rid,)).fetchone())
        if failed["status"] != "failed":
            raise AssertionError(failed)
        checks.append({"case": "midwrite_transaction_rollback", "status": "PASS", "run": failed})
        status = "PASS"
    except Exception:
        error = traceback.format_exc()
    finally:
        db.close()
    result = {"status": status, "scope": "local_fixture_only", "product_acceptance": "BLOCKED",
              "source_sha256": hashes, "checks": checks, "error": error,
              "tests_limit": args.limit, "network_requests": 0, "product_db_touched": False}
    evidence = run / "result.json"
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": status, "evidence": str(evidence)}, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
