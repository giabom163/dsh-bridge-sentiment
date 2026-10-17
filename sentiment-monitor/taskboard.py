#!/usr/bin/env python3
"""任务流水线（独立进程，默认 8791）：只读汇总桥 store 的任务，与舆情观察台完全独立。

- 不读产品库、不碰 sentiment.db；只读各 store 的 state.json / attempt.json / result.json。
- 只服务：`/`（tasks.html）、`/api/tasks`、`/api/tasks/<store_key>/<task_id>`。
- store 清单默认内置，可用环境变量 SENTIMENT_TASK_STORES（JSON 数组）覆盖。

用法：python -B taskboard.py [--port 8791] [--limit N]
"""
import argparse
import hashlib
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8791

# 任务流水线：桥 store 的只读汇总。默认清单可用环境变量 SENTIMENT_TASK_STORES 覆盖，
# 值必须是 JSON 数组，元素形如 {"key": "_s1", "label": "S1", "path": "D:\\...\\_s1\\store"}。
# 缺失的 store 只在响应里记 error，不影响其它 store，也不写任何 store 文件。
TASK_SCHEMA_VERSION = 1
TASK_STORES_ENV = "SENTIMENT_TASK_STORES"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_TASK_STORES = (
    {"key": "_p0", "label": "P0",
     "path": r"D:/DSH/output/2026-10-16/2026-10-16-18-03-28/_p0/store"},
    {"key": "_s1", "label": "S1 · 多源采集",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s1\store"},
    {"key": "_s3", "label": "S3 · 相关性标注",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s3\store"},
    {"key": "_s4", "label": "S4 · 事件聚类",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s4\store"},
    {"key": "_s5", "label": "S5 · 立场分析",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_s5\store"},
    {"key": "_e2e", "label": "E2E · 端到端验收",
     "path": r"D:\DSH\output\2026-10-16\2026-10-16-18-03-28\_e2e\store"},
    {"key": "bridge", "label": "Bridge · 桥任务",
     "path": r"C:\Users\Lenovo\Documents\Codex\bridge-work\data"},
)


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def task_store_list():
    """返回生效的 store 清单；环境变量覆盖时必须是结构合法的 JSON 数组。"""
    raw = os.environ.get(TASK_STORES_ENV, "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_TASK_STORES]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TASK_STORES_ENV} 必须是 JSON 数组：{exc}") from exc
    if not isinstance(value, list) or not value:
        raise ValueError(f"{TASK_STORES_ENV} 必须是非空 JSON 数组")
    stores = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{TASK_STORES_ENV} 的每个元素必须是对象")
        key = str(item.get("key") or "").strip()
        path = str(item.get("path") or "").strip()
        if not key or not path:
            raise ValueError(f"{TASK_STORES_ENV} 的每个元素都需要非空 key 与 path")
        stores.append({"key": key, "label": str(item.get("label") or key), "path": path})
    return stores


def read_json(path):
    """只读解析 JSON；文件不存在返回 (None, None)，解析失败返回 (None, 错误说明)。"""
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except OSError as exc:
        return None, f"{path.name} 读取失败：{exc}"
    except ValueError as exc:
        return None, f"{path.name} 不是合法 JSON：{exc}"


def task_detail_version(task_dir, attempt_id):
    """由 state/attempt/result 的实际字节 SHA 派生稳定版本号。

    只读取真实文件字节，不用 mtime/size；内容不变则版本不变。缺失的文件用固定占位，
    因此“未执行/未收尾”的任务也有稳定版本。"""
    paths = [task_dir / "state.json"]
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        paths.extend([attempt_dir / "attempt.json", attempt_dir / "result.json"])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        try:
            data = path.read_bytes() if path.is_file() else None
        except OSError:
            data = None
        if data is None:
            digest.update(b"\x00missing\x00")
        else:
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        digest.update(b"\x01")
    return digest.hexdigest()


def task_summary(task_dir):
    """汇总一个任务的 state.json 与当前 attempt 的 attempt.json / result.json。"""
    state, problem = read_json(task_dir / "state.json")
    if not isinstance(state, dict):
        state = {}
        problem = problem or "state.json 不是 JSON 对象"
    attempt_id = state.get("current_attempt")
    attempt = result = None
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        attempt, attempt_error = read_json(attempt_dir / "attempt.json")
        result, result_error = read_json(attempt_dir / "result.json")
        # attempt.json / result.json 缺失属正常（尚未执行或未收尾），只有解析失败才算问题。
        problems = [x for x in (attempt_error, result_error) if x]
        if problems:
            problem = "；".join([x for x in (problem, *problems) if x])[:300]
    else:
        attempt_id = None
    review = state.get("product_review")
    human = state.get("human_accept")
    return {
        "id": task_dir.name,
        "status": state.get("status"),
        "acceptance": state.get("acceptance"),
        "revision": state.get("revision"),
        "product_review": review.get("verdict") if isinstance(review, dict) else None,
        "human": human.get("decision") if isinstance(human, dict) else None,
        "human_dismissed": bool(human.get("dismissed")) if isinstance(human, dict) else False,
        "attempt": attempt_id,
        "started": (attempt or {}).get("started_at") or state.get("started_at"),
        "finished": state.get("finished_at") or (result or {}).get("finished_at"),
        "cost": (result or {}).get("native_cost"),
        "detail_version": task_detail_version(task_dir, attempt_id),
    }, problem


def task_overview():
    stores = []
    for store in task_store_list():
        root = Path(store["path"])
        entry = {"key": store["key"], "label": store["label"], "path": store["path"],
                 "error": None, "tasks": []}
        if not root.is_dir():
            entry["error"] = "store 目录不存在"
            stores.append(entry)
            continue
        tasks_dir = root / "tasks"
        if not tasks_dir.is_dir():
            entry["error"] = "tasks 目录不存在"
            stores.append(entry)
            continue
        try:
            task_dirs = sorted((p for p in tasks_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError as exc:
            entry["error"] = f"tasks 目录读取失败：{exc}"
            stores.append(entry)
            continue
        problems = []
        for task_dir in task_dirs:
            summary, problem = task_summary(task_dir)
            entry["tasks"].append(summary)
            if problem:
                problems.append(f"{task_dir.name}: {problem}")
        if problems:
            entry["error"] = "；".join(problems)[:500]
        stores.append(entry)
    return {"schema_version": TASK_SCHEMA_VERSION, "updated_at": now_iso(), "stores": stores}


def task_detail(store_key, task_id):
    """原样只读回传 state.json / attempt.json / result.json；返回 (payload, http_code)。"""
    store = next((item for item in task_store_list() if item["key"] == store_key), None)
    if store is None:
        return {"error": "store 不存在", "store": store_key}, 404
    tasks_dir = Path(store["path"]) / "tasks"
    if not tasks_dir.is_dir():
        return {"error": "store 的 tasks 目录不存在", "store": store_key}, 404
    try:
        task_dir = next((p for p in tasks_dir.iterdir() if p.is_dir() and p.name == task_id), None)
    except OSError as exc:
        return {"error": f"tasks 目录读取失败：{exc}", "store": store_key}, 500
    if task_dir is None:
        return {"error": "任务不存在", "store": store_key, "task_id": task_id}, 404
    state, state_error = read_json(task_dir / "state.json")
    if state_error:
        return {"error": state_error, "store": store_key, "task_id": task_id}, 500
    if state is None:
        return {"error": "state.json 不存在", "store": store_key, "task_id": task_id}, 404
    attempt_id = state.get("current_attempt") if isinstance(state, dict) else None
    attempt = result = None
    if isinstance(attempt_id, str) and TASK_ID_PATTERN.match(attempt_id) and ".." not in attempt_id:
        attempt_dir = task_dir / "attempts" / attempt_id
        attempt, attempt_error = read_json(attempt_dir / "attempt.json")
        result, result_error = read_json(attempt_dir / "result.json")
        errors = "；".join(x for x in (attempt_error, result_error) if x)
        if errors:
            return {"error": errors, "store": store_key, "task_id": task_id}, 500
    return {"state": state, "attempt": attempt, "result": result,
            "detail_version": task_detail_version(task_dir, attempt_id)}, 200




class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, value, kind="application/json; charset=utf-8"):
        data = value if isinstance(value, (bytes, bytearray)) else json.dumps(
            value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            return self._get()
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        except OSError as exc:
            return self.send(500, {"error": str(exc)})

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/tasks":
            return self.send(200, task_overview())
        if path.startswith("/api/tasks/"):
            segments = [urllib.parse.unquote(x) for x in path[len("/api/tasks/"):].split("/")]
            if len(segments) != 2 or not all(segments):
                return self.send(404, {"error": "任务详情路径应为 /api/tasks/<store_key>/<task_id>"})
            payload, code = task_detail(segments[0], segments[1])
            return self.send(code, payload)
        if path in ("/", "/tasks", "/tasks.html"):
            return self.send(200, (ROOT / "tasks.html").read_bytes(), "text/html; charset=utf-8")
        return self.send(404, {"error": "not found"})


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    PORT = args.port
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"任务流水线：http://{HOST}:{PORT}/（只读；Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
