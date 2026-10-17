"""v7-B 真实 GUI 夹具：隔离库 + 真实 repo 服务与真实前端 + 本地 ECharts vendor。

用途：让主线程在专用验收标签页里看到**真实 Handler / 真实 storage / 真实前端**驱动的
观察清单与同帖历史双图，而不接触真实 `sentiment.db`、凭据、配置或既有 8790 服务。

硬约束：
  * 只创建 `--evidence-dir` 指定的**全新**目录；不覆盖、不删除、不清理任何已有内容。
  * 只从当前 repo 读取真实 `server.py` / `storage.py` / `analysis.py` 与前端原始字节；vendor 目录按原字节复制。
  * 3 个**明确标注为合成**的 identity（`platform=fixture.invalid`），多时点、含 30/60 分钟区间、
    单点等待与 unknown 示例；全部通过真实 `storage.p1_import()` 写入，并由真实 `p1_watch_add()` 加入观察。
    时刻与计数只是夹具，不是任何来源实测值。
  * 只开放真实 GET / 真实 API 与观察清单 add/exit 两个 POST；其余 POST（含 /api/p1/import、/api/refresh）一律 405。
  * 只监听 127.0.0.1 的固定白名单端口 8791/8792；最多 180 秒自动停止；保留请求账本；不联网、不调用模型。

命令形状：`python tools/serve_b_fixture.py --limit 3 --evidence-dir <全新隔离目录> --port 8791 --seconds 180`
本助手本身不启动任何产品服务，也不代表产品验收或人工接收。
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
import urllib.parse

TASK_ID = "gender-ui-v7-b-fixture-20261016"
FIXTURE_MARKER = "隔离合成样本，不是真实来源"
MAX_SECONDS = 180
ALLOWED_PORTS = (8791, 8792)
FRONTEND_FILES = ("index.html", "app.js", "style.css")
VENDOR_FILES = ("echarts/echarts.min.js", "echarts/LICENSE", "echarts/NOTICE",
                "echarts/provenance.json", "echarts/licenses/LICENSE-d3")
SOURCE_FILES = ("server.py", "storage.py", "analysis.py")
WATCH_POST_PATHS = ("/api/p1/watchlist", "/api/p1/watchlist/exit")
REPO_ROOT = Path(__file__).resolve().parents[1]
GROUP_LABEL = "隔离合成样本（不是真实来源）"


def utcnow():
    return datetime.now(timezone.utc)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rec(pid, observed_at, value, precision="exact", field="fixtureCount"):
    return {
        "platform": "fixture.invalid", "platform_id": pid,
        "url": f"https://example.invalid/{pid}",
        "title": f"{FIXTURE_MARKER}：{pid}",
        "source_name": FIXTURE_MARKER,
        "evidence_kind": "title_only", "content": "",
        "observed_at": observed_at, "published_at": None,
        "source_response_sha256": sha256_hex(f"{pid}|{observed_at}|{value}|{precision}|{field}".encode("utf-8")),
        "comment_count": {"value": value, "precision": precision, "source_field": field},
        "other_counts": {}, "group_label": GROUP_LABEL,
    }


class Ledger:
    """真实响应账本：与真实 send 同序列化，逐条记录 path/status 与**完整**响应体。

    响应体按内容哈希完整写入独立 artifact 目录（不截断、不打截断标记），账本只引用其
    sha256 / 长度 / 路径，避免大响应（例如本地 ECharts）无法从账本恢复。
    """

    def __init__(self, path: Path):
        self.path = path
        self.bodies_dir = path.parent / "http-bodies"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.path.write_text("", encoding="utf-8")

    def _store_body(self, data: bytes) -> str:
        sha = sha256_hex(data)
        target = self.bodies_dir / f"{sha}.body"
        if not target.exists():
            target.write_bytes(data)
        return str(target)

    def record(self, entry: dict, data: bytes) -> None:
        body_path = self._store_body(data)
        payload = {**entry, "body_sha256": sha256_hex(data), "body_length": len(data),
                   "body_path": body_path, "body_complete": True}
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def entries(self) -> list:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_handler(module, ledger: Ledger):
    class FixtureHandler(module.Handler):
        def _post(self):
            # 只放行观察清单 add/exit；其余写入口（导入 / 刷新 / 分析等）在 Origin 校验前即拒绝。
            path = urllib.parse.urlsplit(self.path).path
            if path not in WATCH_POST_PATHS:
                return self.send(405, {"error": "fixture：仅开放观察清单 add/exit，其余 POST 一律 405"})
            return module.Handler._post(self)

        def send(self, code, value, kind="application/json; charset=utf-8"):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            ledger.record({"at": utcnow().isoformat(), "method": self.command, "path": self.path,
                           "status": int(code), "content_type": kind}, data)
            return module.Handler.send(self, code, value, kind)

    return FixtureHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, required=True, help="必填；只接受 3（夹具固定三身份，首次即 --limit 3）")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="必填；必须是不存在的新目录")
    parser.add_argument("--port", type=int, choices=ALLOWED_PORTS, required=True, help="只允许 8791 或 8792")
    parser.add_argument("--seconds", type=int, default=MAX_SECONDS, help="自动停止秒数，1..180，默认 180")
    args = parser.parse_args()
    if args.limit != 3:
        parser.error("--limit 只接受 3；夹具固定三个合成身份，1/2 会在半初始化后失败，故在创建目录前拒绝")
    if not 1 <= args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds 只接受 1..{MAX_SECONDS}")
    if args.evidence_dir.exists():
        parser.error("--evidence-dir 必须是不存在的新目录（拒绝覆盖已有内容）")
    for name in FRONTEND_FILES + SOURCE_FILES:
        if not (REPO_ROOT / name).is_file():
            parser.error(f"当前 repo 缺少 {name}，无法建立真实夹具")
    for name in VENDOR_FILES:
        if not (REPO_ROOT / "vendor" / name).is_file():
            parser.error(f"当前 repo 缺少 vendor/{name}，无法建立真实夹具")

    started = utcnow()
    deadline = started + timedelta(seconds=args.seconds)
    args.evidence_dir.mkdir(parents=True, exist_ok=False)
    evidence_dir = args.evidence_dir.resolve()
    isolation_root = evidence_dir / "root"
    isolation_root.mkdir(parents=True, exist_ok=False)
    db_path = evidence_dir / "fixture.db"
    ledger_path = evidence_dir / "http-ledger.jsonl"

    # 1) 前端与本地 vendor 按原字节复制到隔离 root（真实 <script src> 指向固定文件，不走 CDN）。
    source_sha256 = {name: sha256_hex((REPO_ROOT / name).read_bytes()) for name in FRONTEND_FILES + SOURCE_FILES}
    copied = {}
    for name in FRONTEND_FILES:
        data = (REPO_ROOT / name).read_bytes()
        (isolation_root / name).write_bytes(data)
        copied[name] = sha256_hex(data)
    vendor_sha256 = {}
    for name in VENDOR_FILES:
        source = (REPO_ROOT / "vendor" / name).read_bytes()
        target = isolation_root / "vendor" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
        vendor_sha256[name] = sha256_hex(source)
    if copied != {name: source_sha256[name] for name in FRONTEND_FILES}:
        raise RuntimeError("前端副本字节与 repo 源不一致，夹具拒绝继续")
    provenance = json.loads((REPO_ROOT / "vendor" / "echarts" / "provenance.json").read_text(encoding="utf-8"))
    echarts_expected = {entry["path"].replace("vendor/echarts/", "echarts/"): entry["sha256"] for entry in provenance["files"]}
    if vendor_sha256.get("echarts/echarts.min.js") != echarts_expected.get("echarts/echarts.min.js"):
        raise RuntimeError("本地 ECharts 文件与 provenance.json 不一致，夹具拒绝继续")

    # 2) 真实 import 当前 repo 的 server/storage/analysis（server 只定义，不启动 __main__）。
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import storage
    import analysis
    import server

    server.ROOT = isolation_root
    server.DB = db_path
    server.HOST = "127.0.0.1"
    server.PORT = args.port
    server.LIMIT = args.limit

    # 3) 真实 storage.connect + analysis.init 建全 schema；真实 p1_import 写入 3 个合成身份。
    base = (started - timedelta(hours=4)).replace(second=0, microsecond=0)

    def at(minutes):
        return (base + timedelta(minutes=minutes)).astimezone(timezone(timedelta(hours=8))).isoformat()

    samples = {
        "fixture-b-A": [rec("fixture-b-A", at(-60), None, "unknown"), rec("fixture-b-A", at(0), 100),
                        rec("fixture-b-A", at(30), 130)],
        "fixture-b-B": [rec("fixture-b-B", at(0), 400), rec("fixture-b-B", at(60), 430), rec("fixture-b-B", at(120), 460)],
        "fixture-b-C": [rec("fixture-b-C", at(0), 300)],
    }
    db = storage.connect(db_path)
    analysis.init(db)
    for pid, records in samples.items():
        for record in records:
            storage.p1_import(db, isolation_root, [record], args.limit)

    # 4) 3 帖全部通过真实 p1_watch_add() 加入观察（24h 周期，人工原因）。
    latest = {}
    for pid in samples:
        row = db.execute("SELECT id FROM p1_evidence WHERE platform_id=? ORDER BY observed_at DESC, imported_at DESC, id DESC LIMIT 1",
                         (pid,)).fetchone()
        latest[pid] = row["id"]
        storage.p1_watch_add(db, row["id"], f"夹具：人工加入观察 {pid}（非真实来源）")
    watch = storage.p1_watchlist(db)
    overview = storage.p1_overview(db, 2)
    top2 = [entry["evidence_id"] for group in overview["groups"] for entry in group["top"]]
    db.close()

    # 5) RUNTIME 提供隔离身份（真实 /api/runtime 路由读取）。
    server.RUNTIME = {
        "fixture": FIXTURE_MARKER, "task_id": TASK_ID, "pid": os.getpid(),
        "started_at": started.isoformat(), "deadline": deadline.isoformat(),
        "entry": f"http://127.0.0.1:{args.port}/", "isolated_root": str(isolation_root),
        "database": str(db_path), "real_db_touched": False, "manual_limit": args.limit,
        "loaded_sha256": {name: source_sha256[name] for name in SOURCE_FILES},
        "echarts_sha256": vendor_sha256.get("echarts/echarts.min.js"),
        "production_adapter": "not_configured",
    }

    ready = {
        "task_id": TASK_ID, "status": "READY", "fixture_marker": FIXTURE_MARKER,
        "url": f"http://127.0.0.1:{args.port}/", "host": "127.0.0.1", "port": args.port, "pid": os.getpid(),
        "limit": args.limit, "started_at": started.isoformat(), "deadline": deadline.isoformat(), "seconds": args.seconds,
        "evidence_dir": str(evidence_dir), "isolated_root": str(isolation_root), "isolated_db": str(db_path),
        "ledger": str(ledger_path), "real_db_touched": False, "network_requests": 0,
        "source_sha256": source_sha256, "copied_frontend_sha256": copied, "vendor_sha256": vendor_sha256,
        "evidence_ids": latest, "active_watch": [item["watch_id"] for item in watch["active"]],
        "active_watch_count": len(watch["active"]), "top2_evidence_ids": top2,
        "fall_out_of_top2": sorted(set(latest.values()) - set(top2)),
        "single_point_or_unknown": ["fixture-b-C"],
        "allowed_post": list(WATCH_POST_PATHS), "blocked_post_examples": ["/api/p1/import", "/api/refresh"],
        "note": "隔离合成样本，不是真实来源；全部数据由真实 storage.p1_import/p1_watch_add 写入；真实 GET 与观察清单 add/exit POST；"
                "其余 POST 405；到期限自动 shutdown；未删除任何文件，未接触真实 sentiment.db，未联网。",
    }
    (evidence_dir / "fixture.json").write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) 真实 ThreadingHTTPServer（真实 Handler 子类）+ 上限自动 shutdown。
    ledger = Ledger(ledger_path)
    httpd = ThreadingHTTPServer((server.HOST, server.PORT), build_handler(server, ledger))
    stopper = threading.Timer(args.seconds, httpd.shutdown)
    stopper.daemon = True
    stopper.start()

    print(json.dumps(ready, ensure_ascii=False), flush=True)
    print(f"[fixture] 已就绪：{ready['url']}；活跃观察 {ready['active_watch_count']}/3；Top2 落榜 {ready['fall_out_of_top2']}；"
          f"{args.seconds}s 后自动停止。账本：{ledger_path}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopper.cancel()
        httpd.server_close()

    entries = ledger.entries()
    summary = {
        "task_id": TASK_ID, "status": "STOPPED", "fixture_marker": FIXTURE_MARKER,
        "stopped_at": utcnow().isoformat(), "response_count": len(entries),
        "observed_statuses": sorted({int(entry.get("status", 0)) for entry in entries}),
        "ledger": str(ledger_path), "isolated_db": str(db_path),
        "files_kept": sorted(path.name for path in evidence_dir.iterdir()),
        "real_db_touched": False, "network_requests": 0,
        "note": "服务已按上限自动停止；未删除任何文件。真实请求账本见 http-ledger.jsonl。",
    }
    (evidence_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
