"""A（两条 GET 失败路径）真实错误验收夹具：隔离库 + 真实 repo 服务与真实前端。

仅标准库。本助手用于给 v7-A 的两条 GET 失败路径补足**真实 HTTP 状态与原始响应体**证据：

  * detail   模式：隔离库内把唯一一行 p1_evidence 的 snapshot_path 指向一个**从未创建过**的
              snapshots/missing-fixture-<hash>.json（旧快照保留）。真实 GET /api/p1/overview 仍 200；
              真实 GET /api/p1/evidence/<id> 走真实 p1_detail，读不存在文件抛 OSError → 409。
  * overview 模式：隔离库内只对 p1_evidence 做一次
              ALTER TABLE ... RENAME COLUMN imported_at TO fixture_missing_imported_at。
              storage.connect 幂等建表仍成功、GET /api/data 仍 200；
              GET /api/p1/overview?top_k=3 因 ORDER BY imported_at 触发真实 sqlite3 错误 → 500。

硬约束（不得违反）：
  * 生产代码零改动：不修改、不提交仓库任何文件；只在 --evidence-dir 内写隔离产物。
  * 不联网、不调用模型、不读凭据、不接触真实 sentiment.db，不删除任何文件或表。
  * 保留真实 Handler._get 与真实 GET 错误捕获；不伪造 HTTP 错误、不替换 API 函数、
    不注入前端、不做 route mock。仅有的两处运行时覆盖：
      - POST 一律返回 405（禁用写入口与联网刷新来源，避免污染来源）；
      - send 以与原 send **相同序列化** tee 实际 path/status/原始响应 body 到本次账本。
  * 单次服务最多 180 秒自动 shutdown（标准库 threading.Timer），无浏览器、无父子进程控制。
  * 从当前 repo 真实 import server/storage/analysis，真实复制 index.html/app.js/style.css 原始字节。

本文件只是夹具，**自身不启动任何测试**；由主线程稍后手动执行一次 `--limit 3`。
它不代表产品验收，也不代表 A 复审通过。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

TASK_ID = "gender-ui-v7-a-error-fixture-20261016"
FIXTURE_MARKER = "隔离验收样本，不是真实来源"
MAX_SECONDS = 180
ALLOWED_PORTS = (8791, 8792)
FRONTEND_FILES = ("index.html", "app.js", "style.css")
SOURCE_FILES = ("server.py", "storage.py", "analysis.py")
REPO_ROOT = Path(__file__).resolve().parents[1]


def utcnow():
    return datetime.now(timezone.utc)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Ledger:
    """本次运行的 HTTP 错误账本：逐条追加真实 path/status/原始响应体（与原 send 同序列化）。"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.write_text("", encoding="utf-8")

    def record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def entries(self) -> list:
        rows = []
        if not self.path.exists():
            return rows
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows


def build_handler(module, ledger: Ledger):
    """真实 Handler 子类。不覆盖 _get/do_GET，只 tee 响应并把写入入口关闭为 405。"""

    class FixtureHandler(module.Handler):
        def do_POST(self):
            return self.send(405, {"error": "fixture: POST 已禁用，仅保留真实 GET 读取路径"})

        def send(self, code, value, kind="application/json; charset=utf-8"):
            # 与真实 send 完全相同的序列化口径，确保账本里的 body 就是真正写出的字节。
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            ledger.record({
                "at": utcnow().isoformat(),
                "method": self.command,
                "path": self.path,
                "status": int(code),
                "content_type": kind,
                "body_sha256": sha256_hex(data),
                "body_length": len(data),
                "body": data.decode("utf-8", "replace"),
            })
            return module.Handler.send(self, code, value, kind)

    return FixtureHandler


def build_fixture_record(observed_at: str, source_response_sha256: str) -> dict:
    """隔离 fixture：标名“隔离验收样本，不是真实来源”，title_only/空正文，评论 1/exact。"""
    return {
        "platform": "fixture.invalid",
        "platform_id": "fixture-a-error-1",
        "url": "https://example.invalid/a-error-fixture",
        "title": "隔离验收样本：A 两条 GET 失败路径（非真实来源）",
        "source_name": FIXTURE_MARKER,
        "evidence_kind": "title_only",
        "content": "",
        "observed_at": observed_at,
        "published_at": None,
        "source_response_sha256": source_response_sha256,
        "comment_count": {"value": 1, "precision": "exact", "source_field": "fixtureCount"},
        "other_counts": {},
        "group_label": FIXTURE_MARKER,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, required=True, help="必填；只接受 1..3，首次使用 3")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="必填；必须是不存在的新目录")
    parser.add_argument("--mode", choices=("detail", "overview"), required=True, help="detail=409 路径；overview=500 路径")
    parser.add_argument("--port", type=int, choices=ALLOWED_PORTS, required=True, help="只允许 8791 或 8792")
    parser.add_argument("--seconds", type=int, default=MAX_SECONDS, help="自动停止秒数，1..180，默认 180")
    args = parser.parse_args()

    if not 1 <= args.limit <= 3:
        parser.error("--limit 只接受 1..3；首次使用 --limit 3")
    if not 1 <= args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds 只接受 1..{MAX_SECONDS}")
    if args.evidence_dir.exists():
        parser.error("--evidence-dir 必须是不存在的新目录（拒绝覆盖已有内容）")

    for name in FRONTEND_FILES + SOURCE_FILES:
        if not (REPO_ROOT / name).is_file():
            parser.error(f"当前 repo 缺少 {name}，无法建立真实夹具")

    started = utcnow()
    deadline = started + timedelta(seconds=args.seconds)

    try:
        args.evidence_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--evidence-dir 必须是不存在的新目录（拒绝覆盖已有内容）")

    evidence_dir = args.evidence_dir.resolve()
    isolation_root = evidence_dir / "root"
    isolation_root.mkdir(parents=True, exist_ok=False)
    db_path = evidence_dir / "fixture.db"
    ledger_path = evidence_dir / "http-ledger.jsonl"

    # 1) 真实复制当前 repo 前端原始字节并记录 hash（源 hash 与副本 hash 必须一致）。
    source_sha256 = {name: sha256_hex((REPO_ROOT / name).read_bytes()) for name in FRONTEND_FILES + SOURCE_FILES}
    copied_sha256 = {}
    for name in FRONTEND_FILES:
        data = (REPO_ROOT / name).read_bytes()
        (isolation_root / name).write_bytes(data)
        copied_sha256[name] = sha256_hex(data)
    if copied_sha256 != {name: source_sha256[name] for name in FRONTEND_FILES}:
        raise RuntimeError("前端副本字节与 repo 源不一致，夹具拒绝继续")

    # 2) 真实 import 当前 repo 的 server/storage/analysis（server 只定义，不启动 __main__）。
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import storage
    import analysis
    import server

    # 3) 运行时把真实服务指向隔离 root / 隔离库 / 指定回环端口；不改仓库文件。
    server.ROOT = isolation_root
    server.DB = db_path
    server.HOST = "127.0.0.1"
    server.PORT = args.port
    server.LIMIT = args.limit

    # 4) 合成 fixture 元数据（明确标合成），其字节 hash 作为 source_response_sha256。
    observed_at = started.isoformat()
    fixture_meta = {
        "synthetic": True,
        "marker": FIXTURE_MARKER,
        "task_id": TASK_ID,
        "note": "人工构造的隔离夹具元数据；不是任何真实平台响应，不声明真实来源",
        "generated_at": observed_at,
    }
    meta_bytes = json.dumps(fixture_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    source_response_sha256 = sha256_hex(meta_bytes)
    record = build_fixture_record(observed_at, source_response_sha256)

    # 5) 真实 storage.connect + analysis.init 建全 schema，真实 p1_import 写入 1 帖（产生快照，保留不删）。
    db = storage.connect(db_path)
    analysis.init(db)
    result = storage.p1_import(db, isolation_root, [record], args.limit)
    evidence_ids = result["evidence_ids"]
    if len(evidence_ids) != 1:
        raise RuntimeError(f"fixture 期望恰好写入 1 条证据，实际 {len(evidence_ids)}")
    evidence_id = evidence_ids[0]

    # 6) 按模式做**唯一一处**隔离库变异。
    mutation = {"mode": args.mode}
    if args.mode == "detail":
        row = dict(db.execute("SELECT id, snapshot_path FROM p1_evidence").fetchone())
        old_rel = row["snapshot_path"]
        old_abs = isolation_root / old_rel
        missing_hash = sha256_hex(("missing-fixture:" + row["id"]).encode("utf-8"))[:16]
        missing_rel = f"snapshots/missing-fixture-{missing_hash}.json"
        missing_abs = isolation_root / missing_rel
        if missing_abs.exists():
            raise RuntimeError(f"夹具要求目标快照从未创建，但已存在：{missing_rel}")
        with db:
            db.execute("UPDATE p1_evidence SET snapshot_path=? WHERE id=?", (missing_rel, row["id"]))
        mutation.update({
            "sql": "UPDATE p1_evidence SET snapshot_path=? WHERE id=?",
            "evidence_id": row["id"],
            "old_snapshot_path": old_rel,
            "old_snapshot_preserved": old_abs.is_file(),
            "new_missing_snapshot_path": missing_rel,
            "new_missing_snapshot_exists": missing_abs.exists(),
            "expected_endpoint": f"GET /api/p1/evidence/{row['id']}",
            "target_path_prefix": f"/api/p1/evidence/{row['id']}",
            "expected_status": 409,
        })
    else:
        columns_before = [r["name"] for r in db.execute("PRAGMA table_info(p1_evidence)")]
        indexes_before = sorted(r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='p1_evidence' AND name NOT LIKE 'sqlite_%'"))
        with db:
            db.execute("ALTER TABLE p1_evidence RENAME COLUMN imported_at TO fixture_missing_imported_at")
        columns_after = [r["name"] for r in db.execute("PRAGMA table_info(p1_evidence)")]
        indexes_after = sorted(r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='p1_evidence' AND name NOT LIKE 'sqlite_%'"))
        mutation.update({
            "sql": "ALTER TABLE p1_evidence RENAME COLUMN imported_at TO fixture_missing_imported_at",
            "columns_before": columns_before,
            "columns_after": columns_after,
            "indexes_before": indexes_before,
            "indexes_after": indexes_after,
            "expected_endpoint": "GET /api/p1/overview?top_k=3",
            "target_path_prefix": "/api/p1/overview",
            "expected_status": 500,
        })

    # 7) 回读隔离库事实（行数、sqlite 版本），并验证 storage.connect 幂等建表仍成功。
    probe = storage.connect(db_path)
    probe_rows = probe.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    probe_version = probe.execute("SELECT sqlite_version()").fetchone()[0]
    probe.close()
    isolation_row_count = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
    db.close()

    # 8) RUNTIME 提供隔离身份（真实 /api/runtime 路由读取），并写 fixture.json。
    server.RUNTIME = {
        "fixture": FIXTURE_MARKER,
        "task_id": TASK_ID,
        "mode": args.mode,
        "pid": os.getpid(),
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "entry": f"http://127.0.0.1:{args.port}/",
        "isolated_root": str(isolation_root),
        "database": str(db_path),
        "real_db_touched": False,
        "manual_limit": args.limit,
        "loaded_sha256": {name: source_sha256[name] for name in SOURCE_FILES},
        "production_adapter": "not_configured",
    }

    ready = {
        "task_id": TASK_ID,
        "status": "READY",
        "fixture_marker": FIXTURE_MARKER,
        "mode": args.mode,
        "url": f"http://127.0.0.1:{args.port}/",
        "entry": f"http://127.0.0.1:{args.port}/",
        "host": "127.0.0.1",
        "port": args.port,
        "pid": os.getpid(),
        "limit": args.limit,
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "seconds": args.seconds,
        "evidence_dir": str(evidence_dir),
        "isolated_root": str(isolation_root),
        "isolated_db": str(db_path),
        "ledger": str(ledger_path),
        "real_db_touched": False,
        "network_requests": 0,
        "source_sha256": source_sha256,
        "copied_frontend_sha256": copied_sha256,
        "fixture_evidence_id": evidence_id,
        "fixture_source_response_sha256": source_response_sha256,
        "fixture_source_response_sha256_synthetic": True,
        "isolation_row_count": isolation_row_count,
        "probe_row_count": probe_rows,
        "reconnect_ok": True,
        "sqlite_version": probe_version,
        "mutation": mutation,
        "expected_endpoint": mutation["expected_endpoint"],
        "expected_status": mutation["expected_status"],
        "note": "隔离验收样本，不是真实来源；真实 Handler._get 与真实 GET 错误捕获；POST 已禁用为 405；"
                "服务到期限自动 shutdown；未删除任何文件，未接触真实 sentiment.db。",
    }
    (evidence_dir / "fixture.json").write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")

    # 9) 启动真实 ThreadingHTTPServer（真实 Handler 子类）+ 180 秒上限自动 shutdown。
    ledger = Ledger(ledger_path)
    handler_class = build_handler(server, ledger)
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), handler_class)
    stopper = threading.Timer(args.seconds, httpd.shutdown)
    stopper.daemon = True
    stopper.start()

    print(json.dumps(ready, ensure_ascii=False), flush=True)
    print(f"[fixture] {args.mode} 模式已就绪：{ready['url']}；预计触发 {mutation['expected_endpoint']} → "
          f"HTTP {mutation['expected_status']}；{args.seconds}s 后自动停止。账本：{ledger_path}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopper.cancel()
        httpd.server_close()

    # 10) 收尾：真实统计账本并落错误账本（不删除任何产物）。
    entries = ledger.entries()
    errors = [e for e in entries if int(e.get("status", 0)) >= 400]
    summary = {
        "task_id": TASK_ID,
        "status": "STOPPED",
        "mode": args.mode,
        "fixture_marker": FIXTURE_MARKER,
        "stopped_at": utcnow().isoformat(),
        "response_count": len(entries),
        "error_count": len(errors),
        "observed_statuses": sorted({int(e.get("status", 0)) for e in entries}),
        "ledger": str(ledger_path),
        "expected_endpoint": mutation["expected_endpoint"],
        "expected_status": mutation["expected_status"],
        "expected_status_observed": any(int(e.get("status", 0)) == mutation["expected_status"] and
                                        mutation["target_path_prefix"] in e.get("path", "") for e in entries),
        "files_kept": sorted(p.name for p in evidence_dir.iterdir()),
        "real_db_touched": False,
        "note": "服务已按上限自动停止；未删除任何文件。真实 HTTP 状态与原始响应体见 http-ledger.jsonl / errors.json。",
    }
    (evidence_dir / "errors.json").write_text(
        json.dumps({"task_id": TASK_ID, "mode": args.mode, "fixture_marker": FIXTURE_MARKER,
                    "expected_status": mutation["expected_status"], "errors": errors},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
