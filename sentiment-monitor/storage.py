"""M1 evidence storage. Standard library, bounded manual batches, no model calls."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import uuid

RULE = "exact-text-v1"
SCHEMA = "m1-v1"
P1_SCHEMA = "p1-v1"
P1_KINDS = ("title_only", "post_body", "source_summary")
P1_PRECISIONS = ("exact", "approximate", "unknown")
P1_KIND_SOURCE = {"title_only": "title", "post_body": "post_body", "source_summary": "source_summary"}
# Whole-database cap on distinct P1 candidate identities. Re-importing an already known
# identity never expands it; a new identity beyond the cap rejects the whole batch.
P1_CANDIDATE_LIMIT = 3
P1_DEFAULT_GROUP = "人工候选"
P1_RECORD_FIELDS = {"platform", "platform_id", "url", "title", "source_name", "evidence_kind",
                    "content", "observed_at", "published_at", "source_response_sha256",
                    "comment_count", "other_counts", "group_label"}
P1_REQUIRED_FIELDS = {"platform", "url", "title", "source_name", "evidence_kind", "content",
                      "observed_at", "source_response_sha256", "comment_count"}
# Names that mean "comment/reply count"; they belong in comment_count, never in other_counts.
P1_RESERVED_COUNTS = {"comment", "comments", "comment_count", "reply", "replies", "reply_count",
                      "评论", "评论数", "评论总数", "回复", "回复数"}
# Column order of p1_evidence, kept next to the DDL so the INSERT cannot drift.
P1_EVIDENCE_COLUMNS = ("id", "identity_key", "platform", "platform_id", "url", "canonical_url", "title",
                       "source_name", "evidence_kind", "content", "content_sha256", "content_raw_sha256",
                       "content_source", "fragment_start", "fragment_end", "observed_at", "observed_raw",
                       "published_at", "published_raw", "time_quality", "source_response_sha256",
                       "snapshot_sha256", "snapshot_path", "comment_value", "comment_precision",
                       "comment_source_field", "other_counts", "group_label", "imported_at")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalized_text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def publication(value):
    if value is None or value == "":
        return None, "missing"
    if not isinstance(value, str):
        raise ValueError("published_at 必须为字符串或 null")
    value = value.strip()
    if not value:
        return None, "missing"
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid"
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, "timezone_missing"
    try:
        return dt.astimezone(timezone.utc).isoformat(), "valid"
    except (ValueError, OverflowError):
        return None, "invalid"


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("url 必须为不超过 4000 字符的字符串")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise ValueError("url 格式错误") from exc
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
        raise ValueError("记录 url 必须为不含认证信息的 http(s) URL")
    host = host.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and not ((parts.scheme.lower() == "http" and port == 80) or
                     (parts.scheme.lower() == "https" and port == 443)):
        host += ":" + str(port)
    # Preserve query and path: do not guess that tracking-like parameters are disposable.
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS feeds(id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE NOT NULL, added_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS keywords(value TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS canonical_records(
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dedup_rule TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS collection_runs(
        id TEXT PRIMARY KEY, feed_id INTEGER REFERENCES feeds(id), kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
        started_at TEXT NOT NULL, finished_at TEXT, input_count INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
        error TEXT, input_sha256 TEXT, limit_n INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS records(
        id TEXT PRIMARY KEY, feed_id INTEGER NOT NULL REFERENCES feeds(id), source_id TEXT,
        title TEXT NOT NULL, summary TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL,
        published_at TEXT, published_raw TEXT, fetched_at TEXT NOT NULL,
        time_quality TEXT NOT NULL CHECK(time_quality IN ('valid','missing','invalid','timezone_missing','legacy_unverified')),
        canonical_id TEXT NOT NULL REFERENCES canonical_records(id), dedup_reason TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES collection_runs(id), sentiment TEXT NOT NULL,
        UNIQUE(feed_id, raw_sha256));
      CREATE INDEX IF NOT EXISTS records_url ON records(canonical_url);
      CREATE INDEX IF NOT EXISTS records_content ON records(content_sha256);
      CREATE INDEX IF NOT EXISTS records_published ON records(published_at);
      CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS p1_evidence(
        id TEXT PRIMARY KEY, identity_key TEXT NOT NULL, platform TEXT NOT NULL, platform_id TEXT,
        url TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL, source_name TEXT NOT NULL,
        evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('title_only','post_body','source_summary')),
        content TEXT NOT NULL, content_sha256 TEXT NOT NULL, content_raw_sha256 TEXT NOT NULL,
        content_source TEXT NOT NULL, fragment_start INTEGER NOT NULL, fragment_end INTEGER NOT NULL,
        observed_at TEXT NOT NULL, observed_raw TEXT NOT NULL,
        published_at TEXT, published_raw TEXT,
        time_quality TEXT NOT NULL CHECK(time_quality IN ('valid','missing','invalid','timezone_missing')),
        source_response_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, snapshot_path TEXT NOT NULL,
        comment_value INTEGER, comment_precision TEXT NOT NULL CHECK(comment_precision IN ('exact','approximate','unknown')),
        comment_source_field TEXT NOT NULL, other_counts TEXT NOT NULL, group_label TEXT NOT NULL,
        imported_at TEXT NOT NULL, UNIQUE(identity_key, snapshot_sha256));
      CREATE INDEX IF NOT EXISTS p1_identity ON p1_evidence(identity_key);
      CREATE INDEX IF NOT EXISTS p1_observed ON p1_evidence(observed_at);
      CREATE TABLE IF NOT EXISTS p1_watch_periods(
        watch_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL, evidence_id TEXT NOT NULL,
        entered_at TEXT NOT NULL, expires_at TEXT NOT NULL, entry_reason TEXT NOT NULL,
        exited_at TEXT, exit_reason TEXT);
      CREATE INDEX IF NOT EXISTS p1_watch_identity ON p1_watch_periods(identity_key);
    """)
    return db


def start_run(db, feed_id, kind, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 的整数")
    if db.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone() is None:
        raise ValueError("来源不存在")
    run_id = "run-" + uuid.uuid4().hex
    with db:
        db.execute("INSERT INTO collection_runs(id,feed_id,kind,status,started_at,limit_n) VALUES(?,?,?,'running',?,?)",
                   (run_id, feed_id, kind, now(), limit))
    return run_id


def fail_run(db, run_id, error):
    with db:
        db.execute("UPDATE collection_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",
                   (now(), str(error)[:1000], run_id))


def save_snapshot(root, payload):
    raw = json_bytes(payload)
    sha = digest(raw)
    relative = Path("snapshots") / (sha + ".json")
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        pass
    if digest(path.read_bytes()) != sha:
        raise OSError("原文快照哈希回读不一致")
    return sha, relative.as_posix()


def import_batch(db, root, run_id, rows, classify):
    run = db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["status"] != "running":
        raise ValueError("批次不存在或已结束")
    try:
        if not isinstance(rows, list) or len(rows) > run["limit_n"]:
            raise ValueError("记录必须为数组，且不得超过批次 limit")
        prepared = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("每条记录必须为对象")
            if set(row) - {"title", "summary", "url", "source_id", "published_at", "raw_xml"}:
                raise ValueError("记录含未声明字段")
            title, summary = row.get("title"), row.get("summary", "")
            if not isinstance(title, str) or not isinstance(summary, str) or not normalized_text(title):
                raise ValueError("title 必须为非空文本，summary 必须为文本")
            if len(title) > 10000 or len(summary) > 100000:
                raise ValueError("记录文本超过限制")
            source_id = row.get("source_id")
            if source_id is not None and (not isinstance(source_id, str) or len(source_id) > 2000):
                raise ValueError("source_id 必须为不超过 2000 字符的字符串或 null")
            if "raw_xml" in row and (not isinstance(row["raw_xml"], str) or len(row["raw_xml"]) > 2000000):
                raise ValueError("raw_xml 格式或长度错误")
            url = canonical_url(row.get("url"))
            published_at, quality = publication(row.get("published_at"))
            title, summary = normalized_text(title), normalized_text(summary)
            content_hash = digest(json_bytes([title, summary]))
            prepared.append((row, title, summary, url, content_hash, published_at, quality, source_id))
        input_hash = digest(json_bytes(rows))
        added = duplicates = 0
        # Snapshots are immutable content-addressed evidence. A failed transaction may leave
        # an unreferenced snapshot; no destructive cleanup or partial record commit occurs.
        with db:
            for row, title, summary, url, content_hash, published_at, quality, source_id in prepared:
                raw_hash, raw_path = save_snapshot(root, row)
                if db.execute("SELECT id FROM records WHERE feed_id=? AND raw_sha256=?",
                              (run["feed_id"], raw_hash)).fetchone():
                    duplicates += 1
                    continue
                # Exact text clusters only. Same URL with edited text remains a separate
                # version and is linked by canonical_url rather than merging clusters.
                existing = db.execute("""SELECT r.canonical_id FROM records r JOIN canonical_records c
                    ON c.id=r.canonical_id WHERE r.content_sha256=? AND c.dedup_rule=?
                    ORDER BY r.fetched_at,r.id LIMIT 1""", (content_hash, RULE)).fetchone()
                if existing:
                    canonical_id, reason = existing[0], "exact_text"
                else:
                    canonical_id, reason = "canonical-" + uuid.uuid4().hex, "independent"
                    db.execute("INSERT INTO canonical_records VALUES(?,?,?)", (canonical_id, now(), RULE))
                record_id = "record-" + uuid.uuid4().hex
                db.execute("""INSERT INTO records(id,feed_id,source_id,title,summary,url,canonical_url,
                    content_sha256,raw_sha256,raw_path,published_at,published_raw,fetched_at,time_quality,
                    canonical_id,dedup_reason,run_id,sentiment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (record_id, run["feed_id"], source_id, title, summary, row["url"], url,
                            content_hash, raw_hash, raw_path, published_at, row.get("published_at"), now(),
                            quality, canonical_id, reason, run_id, classify(title + " " + summary)))
                added += 1
            db.execute("""UPDATE collection_runs SET status='succeeded',finished_at=?,input_count=?,
                added=?,duplicates=?,input_sha256=? WHERE id=? AND status='running'""",
                       (now(), len(rows), added, duplicates, input_hash, run_id))
        return dict(db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone())
    except Exception as exc:
        db.rollback()
        fail_run(db, run_id, exc)
        raise


def recover_runs(db):
    # Called once at startup only; do not interrupt live requests on every connection.
    with db:
        db.execute("UPDATE collection_runs SET status='interrupted',finished_at=?,error='服务上次退出，未自动重试' WHERE status='running'", (now(),))


def data(db):
    feeds = [dict(row) for row in db.execute("""SELECT f.*,
      (SELECT count(*) FROM records r WHERE r.feed_id=f.id) item_count,
      (SELECT max(finished_at) FROM collection_runs c WHERE c.feed_id=f.id AND c.status='succeeded') last_success_at,
      (SELECT id FROM collection_runs c WHERE c.feed_id=f.id ORDER BY started_at DESC,id DESC LIMIT 1) last_run_id
      FROM feeds f ORDER BY f.id DESC""")]
    items = [dict(row) for row in db.execute("""SELECT r.*,r.url link,r.published_at published,f.name feed_name
      FROM records r JOIN feeds f ON f.id=r.feed_id
      ORDER BY r.published_at IS NULL,r.published_at DESC,r.fetched_at DESC,r.id LIMIT 500""")]
    stats = dict(db.execute("""SELECT count(*) record_count,count(DISTINCT canonical_id) canonical_count,
      coalesce(sum(published_at IS NULL),0) unknown_time_count FROM records""").fetchone())
    stats["unknown_time_ratio"] = stats["unknown_time_count"] / stats["record_count"] if stats["record_count"] else None
    stats["time_quality_counts"] = {r[0]: r[1] for r in db.execute("SELECT time_quality,count(*) FROM records GROUP BY time_quality")}
    legacy = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='items'").fetchone()[0]
    legacy_count = db.execute("SELECT count(*) FROM items").fetchone()[0] if legacy else 0
    return {"schema_version": SCHEMA, "feeds": feeds, "items": items,
            "keywords": [r[0] for r in db.execute("SELECT value FROM keywords ORDER BY value")],
            "stats": stats, "items_limit": 500,
            "legacy_items_pending": legacy_count,
            "coverage": "sample_only", "analysis_status": "unreviewed_dictionary",
            "runs": [dict(r) for r in db.execute("SELECT * FROM collection_runs ORDER BY started_at DESC,id DESC LIMIT 100")]}


# --- P1-A: bounded manual gender-topic evidence store (no network, no model) --------------------

class P1LimitExceeded(ValueError):
    """Whole-database P1 candidate identity cap would be exceeded; the batch is rejected."""


def p1_identity(platform, platform_id, canonical):
    # Identity is platform + platform_id when present, otherwise the canonical URL.
    # Never clustered by equal/similar title or body.
    if platform_id:
        return platform + "\x1fid\x1f" + platform_id
    return platform + "\x1furl\x1f" + canonical


def p1_hex(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} 必须为 64 位小写十六进制 SHA-256")
    return value


def p1_count(value, field):
    if not isinstance(value, dict) or set(value) != {"value", "precision", "source_field"}:
        raise ValueError(f"{field} 必须且仅含 value/precision/source_field")
    raw, precision, source_field = value["value"], value["precision"], value["source_field"]
    if precision not in P1_PRECISIONS:
        raise ValueError(f"{field}.precision 必须为 exact/approximate/unknown")
    if raw is None:
        if precision != "unknown":
            raise ValueError(f"{field} 未知计数必须 precision=unknown 且 value=null")
        amount = None
    else:
        # type(x) is int rejects bool; negatives and floats are rejected; unknown must stay null.
        if type(raw) is not int or raw < 0:
            raise ValueError(f"{field}.value 必须为非负整数或 null")
        if precision == "unknown":
            raise ValueError(f"{field} precision=unknown 时 value 必须为 null，不得转换成 0")
        amount = raw
    if not isinstance(source_field, str) or not source_field.strip() or len(source_field) > 200:
        raise ValueError(f"{field}.source_field 必须为非空文本（最多 200 字符）")
    return {"value": amount, "precision": precision, "source_field": source_field.strip()}


def p1_other_counts(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 20:
        raise ValueError("other_counts 必须为不超过 20 个计数的对象")
    result = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("other_counts 名称必须为非空文本（最多 100 字符）")
        key = name.strip()
        if key.lower() in P1_RESERVED_COUNTS:
            raise ValueError(f"other_counts 不得包含评论/回复计数，应放入 comment_count：{key}")
        result[key] = p1_count(entry, f"other_counts.{key}")
    return result


def p1_prepare(record):
    if not isinstance(record, dict):
        raise ValueError("每条记录必须为对象")
    unknown = set(record) - P1_RECORD_FIELDS
    if unknown:
        raise ValueError(f"记录含未声明字段（含评论/回复正文一律拒绝）：{'、'.join(sorted(unknown))}")
    missing = P1_REQUIRED_FIELDS - set(record)
    if missing:
        raise ValueError(f"记录缺少字段：{'、'.join(sorted(missing))}")
    platform = record["platform"]
    if not isinstance(platform, str) or not platform.strip() or len(platform) > 100:
        raise ValueError("platform 必须为非空文本（最多 100 字符）")
    platform = platform.strip()
    platform_id = record.get("platform_id")
    if platform_id is not None:
        if not isinstance(platform_id, str) or len(platform_id) > 2000:
            raise ValueError("platform_id 必须为不超过 2000 字符的字符串或 null")
        platform_id = platform_id.strip() or None
    if not isinstance(record["url"], str):
        raise ValueError("url 必须为字符串")
    canonical = canonical_url(record["url"])
    title = record["title"]
    if not isinstance(title, str) or not normalized_text(title) or len(title) > 10000:
        raise ValueError("title 必须为非空文本（最多 10000 字符）")
    title = normalized_text(title)
    source_name = record["source_name"]
    if not isinstance(source_name, str) or not normalized_text(source_name) or len(source_name) > 200:
        raise ValueError("source_name 必须为非空文本（最多 200 字符）")
    source_name = normalized_text(source_name)
    kind = record["evidence_kind"]
    if kind not in P1_KINDS:
        raise ValueError("evidence_kind 必须为 title_only/post_body/source_summary")
    content_raw = record["content"]
    if not isinstance(content_raw, str) or len(content_raw) > 100000:
        raise ValueError("content 必须为不超过 100000 字符的字符串")
    content = normalized_text(content_raw)
    if kind == "title_only" and content:
        raise ValueError("title_only 的 content 必须为空字符串")
    if kind != "title_only" and not content:
        raise ValueError("post_body/source_summary 必须提供非空 content")
    observed_raw = record["observed_at"]
    if not isinstance(observed_raw, str):
        raise ValueError("observed_at 必须为带时区的有效时间字符串")
    observed_at, observed_quality = publication(observed_raw)
    if observed_quality != "valid":
        raise ValueError("observed_at 必须为带时区的有效时间")
    published_at, published_quality = publication(record.get("published_at"))
    source_response_sha256 = p1_hex(record["source_response_sha256"], "source_response_sha256")
    comment = p1_count(record["comment_count"], "comment_count")
    other = p1_other_counts(record.get("other_counts"))
    group_label = record.get("group_label")
    if group_label is None or group_label == "":
        group_label = P1_DEFAULT_GROUP
    elif not isinstance(group_label, str) or len(group_label) > 200:
        raise ValueError("group_label 必须为不超过 200 字符的字符串或 null")
    else:
        group_label = normalized_text(group_label) or P1_DEFAULT_GROUP
    return {
        "record": record, "identity": p1_identity(platform, platform_id, canonical),
        "platform": platform, "platform_id": platform_id, "url": record["url"], "canonical_url": canonical,
        "title": title, "source_name": source_name, "evidence_kind": kind, "content": content,
        "content_sha256": digest(json_bytes(content)),
        "content_raw_sha256": digest(json_bytes(content_raw)),
        "content_source": P1_KIND_SOURCE[kind], "fragment_start": 0, "fragment_end": len(content),
        "observed_at": observed_at, "observed_raw": observed_raw,
        "published_at": published_at, "published_raw": record.get("published_at"),
        "time_quality": published_quality, "source_response_sha256": source_response_sha256,
        "comment": comment, "other": other, "group_label": group_label,
    }


def p1_import(db, root, records, limit):
    # Batch is capped by min(server --limit, 3); the whole database keeps <= 3 identities.
    cap = min(limit, P1_CANDIDATE_LIMIT) if type(limit) is int else P1_CANDIDATE_LIMIT
    if not isinstance(records, list) or len(records) > cap:
        raise ValueError(f"records 必须为不超过 {cap} 条的数组")
    prepared = [p1_prepare(record) for record in records]  # validate everything before any write
    identities = {p["identity"] for p in prepared}
    existing = {row[0] for row in db.execute("SELECT DISTINCT identity_key FROM p1_evidence")}
    if len(existing | identities) > P1_CANDIDATE_LIMIT:
        raise P1LimitExceeded(
            f"全库 P1 候选身份总数不得超过 {P1_CANDIDATE_LIMIT}；重复导入同一身份不会扩容")
    added, duplicates, evidence_ids = 0, 0, []
    with db:
        for p in prepared:
            snapshot_sha256, snapshot_path = save_snapshot(root, p["record"])
            evidence_id = "p1ev-" + digest(json_bytes([p["identity"], snapshot_sha256]))
            # Same identity + same evidence snapshot is a replay: no new row, history preserved.
            if db.execute("SELECT 1 FROM p1_evidence WHERE identity_key=? AND snapshot_sha256=?",
                          (p["identity"], snapshot_sha256)).fetchone():
                duplicates += 1
                continue
            fields = {
                "id": evidence_id, "identity_key": p["identity"], "platform": p["platform"],
                "platform_id": p["platform_id"], "url": p["url"], "canonical_url": p["canonical_url"],
                "title": p["title"], "source_name": p["source_name"], "evidence_kind": p["evidence_kind"],
                "content": p["content"], "content_sha256": p["content_sha256"],
                "content_raw_sha256": p["content_raw_sha256"], "content_source": p["content_source"],
                "fragment_start": p["fragment_start"], "fragment_end": p["fragment_end"],
                "observed_at": p["observed_at"], "observed_raw": p["observed_raw"],
                "published_at": p["published_at"], "published_raw": p["published_raw"],
                "time_quality": p["time_quality"], "source_response_sha256": p["source_response_sha256"],
                "snapshot_sha256": snapshot_sha256, "snapshot_path": snapshot_path,
                "comment_value": p["comment"]["value"], "comment_precision": p["comment"]["precision"],
                "comment_source_field": p["comment"]["source_field"],
                "other_counts": json_bytes(p["other"]).decode(), "group_label": p["group_label"],
                "imported_at": now(),
            }
            names = P1_EVIDENCE_COLUMNS
            db.execute("INSERT INTO p1_evidence(%s) VALUES(%s)" % (",".join(names), ",".join("?" * len(names))),
                       tuple(fields[name] for name in names))
            added += 1
            evidence_ids.append(evidence_id)
    return {"schema_version": P1_SCHEMA, "status": "succeeded", "added": added, "duplicates": duplicates,
            "input_count": len(records), "identity_total": len(existing | identities),
            "candidate_limit": P1_CANDIDATE_LIMIT, "evidence_ids": evidence_ids,
            "network_requests": 0, "feeds_touched": 0}


def p1_entry(row):
    return {"evidence_id": row["id"], "identity_key": row["identity_key"], "platform": row["platform"],
            "platform_id": row["platform_id"], "url": row["url"], "canonical_url": row["canonical_url"],
            "title": row["title"], "source_name": row["source_name"], "evidence_kind": row["evidence_kind"],
            "content_sha256": row["content_sha256"], "observed_at": row["observed_at"],
            "imported_at": row["imported_at"], "group_label": row["group_label"],
            "comment_count": {"value": row["comment_value"], "precision": row["comment_precision"],
                              "source_field": row["comment_source_field"]},
            "other_counts": json.loads(row["other_counts"]), "snapshot_sha256": row["snapshot_sha256"],
            "source_response_sha256": row["source_response_sha256"]}


def p1_overview(db, top_k):
    if top_k is None:
        top_k = P1_CANDIDATE_LIMIT
    elif isinstance(top_k, str) and top_k.strip().isdigit():
        top_k = int(top_k.strip())
    if type(top_k) is not int or not 1 <= top_k <= P1_CANDIDATE_LIMIT:
        raise ValueError(f"top_k 必须为 1–{P1_CANDIDATE_LIMIT} 的整数")
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM p1_evidence ORDER BY observed_at DESC, imported_at DESC, id DESC")]
    latest = {}
    for row in rows:
        latest.setdefault(row["identity_key"], row)  # first occurrence is the newest actual observation
    groups = {}
    for row in latest.values():
        groups.setdefault(row["group_label"], []).append(row)
    output = []
    for label in sorted(groups):
        members = groups[label]
        exact = sorted([m for m in members if m["comment_precision"] == "exact" and m["comment_value"] is not None],
                       key=lambda m: (-m["comment_value"], m["identity_key"]))
        others = sorted([m for m in members if m["comment_precision"] != "exact"], key=lambda m: m["identity_key"])
        output.append({
            "group_label": label, "identity_count": len(members), "exact_count": len(exact),
            "top": [{**p1_entry(m), "rank": i + 1} for i, m in enumerate(exact[:top_k])],
            "unranked": [{**p1_entry(m), "rank_reason": ("comment_count_approximate" if m["comment_precision"] == "approximate"
                                                          else "comment_count_unknown")} for m in others],
        })
    return {"schema_version": P1_SCHEMA, "top_k": top_k, "candidate_limit": P1_CANDIDATE_LIMIT,
            "identity_total": len(latest), "group_count": len(output), "groups": output,
            "exact_only": True, "empty_state": not latest,
            "note": "仅对精确评论计数降序排名；近似与未知计数不参与排名并单列；view/like/danmaku 等不计入评论数；"
                    "此处为各身份最新观测的单点当前值；带时区的同帖历史与观察周期见 p1_history / p1_watchlist"}


def p1_detail(db, root, evidence_id):
    row = db.execute("SELECT * FROM p1_evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    path = (Path(root) / row["snapshot_path"]).resolve()
    snapshots_root = (Path(root) / "snapshots").resolve()
    if not path.is_relative_to(snapshots_root):
        raise ValueError("证据快照路径越界")
    raw = path.read_bytes()
    if digest(raw) != row["snapshot_sha256"]:
        raise OSError("证据快照哈希不一致")
    return {"evidence": row, "snapshot": json.loads(raw), "snapshot_sha256_verified": True,
            "scope": "manual_evidence_only", "source_response_verified": False}


# --- P1-B: bounded watch periods + real per-post observation history (no network, no model) ------
#
# p1_evidence stays append-only: p1_import never overwrites an observation. Watch periods are a
# separate append-only ledger of explicit human joins; exiting or expiring never deletes evidence or
# history and never renews automatically. All timestamps are real server UTC or the imported,
# already timezone-checked observed_at; nothing is fabricated.

WATCH_SCHEMA = "p1-watch-v1"
WATCH_LIMIT = 3
WATCH_HOURS = 24
HISTORY_DEFAULT_LIMIT = 500
HISTORY_MAX_LIMIT = 500
REASON_MAX = 200


class P1NotFound(ValueError):
    """Referenced evidence_id / watch_id does not exist (server maps to 404)."""


class P1Conflict(ValueError):
    """Live watch conflict: already active, not active, or active cap full (server maps to 409)."""


class P1Invalid(ValueError):
    """Field type or business constraint invalid (server maps to 422)."""


def utc_moment(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(timezone.utc)


def p1_bound(value, field):
    """Optional from/to: must be a timezone-aware ISO-8601 string; returns normalized UTC or None."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise P1Invalid(f"{field} 必须为带时区的 ISO-8601 时间字符串")
    normalized, quality = publication(value)
    if quality != "valid":
        raise P1Invalid(f"{field} 必须为带时区的有效时间（from 含、to 不含）")
    return normalized


def watch_reason(value, field="reason"):
    if not isinstance(value, str):
        raise P1Invalid(f"{field} 必须为人工填写的短文本（1–{REASON_MAX} 字符）")
    text = re.sub(r"\s+", " ", value).strip()
    if not 1 <= len(text) <= REASON_MAX:
        raise P1Invalid(f"{field} 必须为 1–{REASON_MAX} 字符的人工填写原因")
    return text


def watch_status(row, moment=None):
    """Derived at read time from the real server UTC now. Expiry is not a background job."""
    moment = moment or datetime.now(timezone.utc)
    if row["exited_at"]:
        return "exited"
    if utc_moment(row["expires_at"]) <= moment:
        return "expired"
    return "active"


def watch_period(db, watch_id):
    return db.execute("SELECT * FROM p1_watch_periods WHERE watch_id=?", (watch_id,)).fetchone()


def watch_item(db, row, moment):
    # evidence_id is the identity's newest real evidence row; it does not follow TopK ranking.
    latest = db.execute(
        "SELECT * FROM p1_evidence WHERE identity_key=? ORDER BY observed_at DESC, imported_at DESC, id DESC LIMIT 1",
        (row["identity_key"],)).fetchone()
    first = db.execute(
        "SELECT observed_at FROM p1_evidence WHERE identity_key=? ORDER BY observed_at ASC, imported_at ASC, id ASC LIMIT 1",
        (row["identity_key"],)).fetchone()
    return {
        "watch_id": row["watch_id"],
        "evidence_id": latest["id"] if latest else row["evidence_id"],
        "title": latest["title"] if latest else "",
        "url": latest["url"] if latest else "",
        "source_name": latest["source_name"] if latest else "",
        "status": watch_status(row, moment),
        "entered_at": row["entered_at"], "expires_at": row["expires_at"],
        "entry_reason": row["entry_reason"], "exited_at": row["exited_at"], "exit_reason": row["exit_reason"],
        "first_observed_at": first["observed_at"] if first else None,
        "latest_observed_at": latest["observed_at"] if latest else None,
    }


def p1_watchlist(db, moment=None):
    """All watch periods (active + expired + exited). Never deletes or rewrites history."""
    moment = moment or datetime.now(timezone.utc)
    rows = list(db.execute("SELECT * FROM p1_watch_periods ORDER BY entered_at DESC, watch_id DESC"))
    items = [watch_item(db, row, moment) for row in rows]
    active = [item for item in items if item["status"] == "active"]
    active.sort(key=lambda item: item["latest_observed_at"] or "", reverse=True)
    return {"schema_version": WATCH_SCHEMA, "active_limit": WATCH_LIMIT,
            "active": active[:WATCH_LIMIT], "history": items,
            "empty_state": not items}


class P1TransactionRequired(RuntimeError):
    """immediate_write 需要无事务连接才能取写锁；调用方已开启事务时明确拒绝，不退回 SAVEPOINT。"""


@contextmanager
def immediate_write(db):
    """取得 SQLite 写锁后再做 cap 检查，避免跨连接把上限读空后分别插入。

    仅 `with db` 不会在前置 SELECT 之前开启写事务，因此重复检查与活跃计数会和插入
    分处不同事务、被并发连接同时读到尚有余量。这里在检查前显式 `BEGIN IMMEDIATE`，
    成功统一 commit、异常统一 rollback。

    本函数要求连接处于**无事务**状态。若调用方已开启事务（`db.in_transaction`），既有
    的 SAVEPOINT 既不能重新取得写锁，也不能证明该事务已是 writer transaction，故不再
    退回 SAVEPOINT，而是**明确拒绝**该调用：抛出 P1TransactionRequired，不进入检查、不
    插入，也不提交或回滚调用方事务（调用方自行处理其事务）。
    """
    if db.in_transaction:
        raise P1TransactionRequired(
            "immediate_write 需要无事务连接以取得 SQLite 写锁；检测到调用方已开启事务，"
            "无法在既有事务中保证跨连接原子性，已拒绝调用且未提交/回滚调用方事务")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def p1_watch_add(db, evidence_id, reason, moment=None):
    """Explicit human join. 24h period from real server UTC now; atomic cap 3; never renews."""
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise P1Invalid("evidence_id 必须为非空文本")
    reason = watch_reason(reason, "reason")
    moment = moment or datetime.now(timezone.utc)
    row = db.execute("SELECT * FROM p1_evidence WHERE id=?", (evidence_id.strip(),)).fetchone()
    if row is None:
        raise P1NotFound("evidence 不存在")
    with immediate_write(db):
        periods = list(db.execute("SELECT * FROM p1_watch_periods WHERE identity_key=?", (row["identity_key"],)))
        if any(watch_status(period, moment) == "active" for period in periods):
            raise P1Conflict("该原帖已有活跃观察周期；已拒绝重复加入且未延长 24h")
        active_count = sum(1 for period in db.execute("SELECT * FROM p1_watch_periods")
                           if watch_status(period, moment) == "active")
        if active_count >= WATCH_LIMIT:
            raise P1Conflict(f"活跃观察周期已达上限 {WATCH_LIMIT}；已原子拒绝，未静默扩容")
        watch_id = "p1w-" + uuid.uuid4().hex
        db.execute("""INSERT INTO p1_watch_periods(watch_id,identity_key,evidence_id,entered_at,expires_at,
            entry_reason,exited_at,exit_reason) VALUES(?,?,?,?,?,?,NULL,NULL)""",
                   (watch_id, row["identity_key"], row["id"], moment.isoformat(),
                    (moment + timedelta(hours=WATCH_HOURS)).isoformat(), reason))
    return watch_item(db, watch_period(db, watch_id), moment)


def p1_watch_exit(db, watch_id, reason, moment=None):
    """Explicit human exit. Appends a state change only; never deletes periods, evidence or history."""
    if not isinstance(watch_id, str) or not watch_id.strip():
        raise P1Invalid("watch_id 必须为非空文本")
    reason = watch_reason(reason, "reason")
    moment = moment or datetime.now(timezone.utc)
    row = watch_period(db, watch_id.strip())
    if row is None:
        raise P1NotFound("观察周期不存在")
    status = watch_status(row, moment)
    if status != "active":
        raise P1Conflict("该观察周期已" + ("退出" if status == "exited" else "到期") + "，不能再退出")
    with db:
        db.execute("UPDATE p1_watch_periods SET exited_at=?,exit_reason=? WHERE watch_id=? AND exited_at IS NULL",
                   (moment.isoformat(), reason, watch_id.strip()))
    return watch_item(db, watch_period(db, watch_id.strip()), moment)


def p1_history(db, evidence_id, from_raw=None, to_raw=None, limit=HISTORY_DEFAULT_LIMIT, offset=0):
    """Real append-only observation history for one identity, ordered by observed_at.

    Comparison is decided on the complete ordered history first (so a window's first point may
    reference a real out-of-window previous point), then the display window is applied. Same UTC
    instant + same count + same source_field merges into one bucket referencing all its evidence;
    a same-instant conflict is never resolved and blocks later points from borrowing older values.
    """
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        raise P1Invalid("evidence_id 必须为非空文本")
    if type(limit) is not int or not 1 <= limit <= HISTORY_MAX_LIMIT:
        raise P1Invalid(f"limit 必须为 1–{HISTORY_MAX_LIMIT} 的整数")
    if type(offset) is not int or offset < 0:
        raise P1Invalid("offset 必须为非负整数")
    window_from = p1_bound(from_raw, "from")
    window_to = p1_bound(to_raw, "to")
    if window_from and window_to and utc_moment(window_from) >= utc_moment(window_to):
        raise P1Invalid("to 必须晚于 from（from 含、to 不含）")
    anchor = db.execute("SELECT identity_key FROM p1_evidence WHERE id=?", (evidence_id.strip(),)).fetchone()
    if anchor is None:
        raise P1NotFound("evidence 不存在")
    rows = [dict(row) for row in db.execute(
        "SELECT * FROM p1_evidence WHERE identity_key=? ORDER BY observed_at ASC, imported_at ASC, id ASC",
        (anchor["identity_key"],))]
    buckets, index = [], {}
    for row in rows:
        bucket = index.get(row["observed_at"])
        if bucket is None:
            bucket = {"observed_at": row["observed_at"], "observed_raw": row["observed_raw"],
                      "evidence_ids": [], "counts": set(), "value": None, "precision": "unknown",
                      "source_field": ""}
            index[row["observed_at"]] = bucket
            buckets.append(bucket)
        bucket["evidence_ids"].append(row["id"])
        bucket["counts"].add((row["comment_value"], row["comment_precision"], row["comment_source_field"]))
    points, previous = [], None
    for bucket in buckets:
        conflict = len(bucket["counts"]) > 1
        if not conflict:
            bucket["value"], bucket["precision"], bucket["source_field"] = next(iter(bucket["counts"]))
        point = {"evidence_ids": bucket["evidence_ids"], "observed_at": bucket["observed_at"],
                 "observed_raw": bucket["observed_raw"],
                 "comment_count": {"value": bucket["value"], "precision": bucket["precision"],
                                   "source_field": bucket["source_field"]},
                 "previous_observed_at": None, "interval_seconds": None, "delta": None,
                 "rate_per_hour": None, "comparison": None}
        if conflict:
            point["comparison"] = "same_time_conflict"
            previous = None                      # later points must not borrow across a bad bucket
        elif bucket["precision"] == "unknown" or bucket["value"] is None:
            point["comparison"] = "unknown"      # never补 0，不作为比较前值
        elif bucket["precision"] == "approximate":
            point["comparison"] = "approximate"  # 不补 0，不参与数值比较
        elif previous is None:
            point["comparison"] = "single_point"
            previous = bucket
        elif previous["source_field"] != bucket["source_field"]:
            point["comparison"] = "source_changed"   # 口径变化不比较
            previous = bucket
        else:
            seconds = (utc_moment(bucket["observed_at"]) - utc_moment(previous["observed_at"])).total_seconds()
            point["previous_observed_at"] = previous["observed_at"]
            point["interval_seconds"] = seconds
            if seconds <= 0:
                point["comparison"] = "invalid_interval"   # 时间异常：不算速率
            else:
                delta = bucket["value"] - previous["value"]
                point["delta"] = delta
                point["rate_per_hour"] = delta * 3600.0 / seconds
                point["comparison"] = "retraction" if delta < 0 else "comparable"
                previous = bucket
        points.append(point)
    windowed = []
    for point in points:
        if window_from and utc_moment(point["observed_at"]) < utc_moment(window_from):
            continue
        if window_to and utc_moment(point["observed_at"]) >= utc_moment(window_to):
            continue
        windowed.append(point)
    page = windowed[offset:offset + limit]
    return {"evidence_id": evidence_id.strip(),
            "filter": {"from": window_from, "to": window_to, "time_basis": "observed_at"},
            "points": page, "limit": limit, "offset": offset,
            "has_more": offset + len(page) < len(windowed)}
