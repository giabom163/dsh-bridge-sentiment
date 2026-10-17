"""B1 桥协同图的三组定向离线检查（--limit 1–3）。

组 1：状态/验收/未知与 null —— 接口原值保真 + 前端映射文字完整。
组 2：依赖选择与断线 —— 依赖边只来自 task.dependencies、缺失依赖可见、唯一 5 秒刷新、断线停止动画。
组 3：静态路由与原 UI 兼容 —— 白名单路由、CSP、逐项 UI 路由响应体哈希、路径穿越拒绝、原界面元素仍在。

--limit N 在执行前截断：只运行前 N 组（不是运行全部再裁输出）。
--serve-ui-fixture api-fail|empty 配合 --port：仅供主线程用真实浏览器人工核验 F1 的隔离夹具，
  绑定 127.0.0.1 指定端口持续提供真实 UI，使用独立临时 store，不请求外部或模型，不碰真实 store。
  普通检查模式不会启动它，也不会持久驻留服务。

只做 GET 与本地文件读取：不发送模型请求、不触发 native-status、不持久启动服务（进程内起停）。
"""
import argparse
import hashlib
import http.client
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import webui

# 实际加载的 UI 路由 -> runtime.loaded_sha256 中的键（逐项核对响应体字节）。
UI_ROUTES = (
    ("/", "web/index.html"),
    ("/app.js", "web/app.js"),
    ("/style.css", "web/style.css"),
    ("/task-graph.js", "web/task-graph.js"),
    ("/vendor/cytoscape.min.js", "web/vendor/cytoscape.min.js"),
)

STATUS_PAIRS = [
    ("ready", "待规划"), ("prepared", "空会话待确认"), ("running", "执行中"),
    ("awaiting_review", "待审查"), ("awaiting_user", "待用户验收"), ("accepted", "已接收"),
    ("rework", "返修待规划"), ("failed", "失败"), ("cancelled", "已取消"),
    ("stop_unconfirmed", "停止未确认"),
]
ACCEPTANCE_PAIRS = [("PASS", "通过"), ("FAIL", "失败"), ("BLOCKED", "阻塞")]


def request(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def raw_path_status(base, path):
    """用原始请求行发送路径，避免客户端库对 ../ 做归一化，真实检验白名单。"""
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": f"{parts.hostname}:{parts.port}"})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def write_task(store, task, status, acceptance="BLOCKED"):
    """直接落盘一份可被 bridge.load_task 读取的夹具（contract 哈希须自洽）。"""
    directory = store / "tasks" / task["task_id"]
    directory.mkdir(parents=True, exist_ok=True)
    webui.bridge.atomic(directory / "contract.r1.json", task)
    state = {"task_id": task["task_id"], "revision": 1,
             "contract_sha256": webui.bridge.digest(webui.bridge.json_bytes(task)),
             "status": status, "acceptance": acceptance, "current_attempt": None,
             "created_at": webui.bridge.now()}
    webui.bridge.atomic(directory / "state.json", state)


def fixtures(store):
    base = {"repo": "C:/fixture/repo", "executor": "opencode",
            "allowed_paths": ["x.txt"], "artifacts": ["x.txt"], "completion_criteria": ["fixture"]}
    write_task(store, {**base, "task_id": "graph-alpha", "goal": "无依赖且未记录模型"}, "ready")
    write_task(store, {**base, "task_id": "graph-beta", "goal": "依赖 alpha 的运行任务",
                       "model": "opencode-go/deepseek-v4.1-flash", "dependencies": ["graph-alpha"]},
               "running")
    write_task(store, {**base, "task_id": "graph-gamma", "goal": "未知状态与未知验收",
                       "dependencies": ["graph-beta", "graph-missing"]}, "weave-unknown", "UNKNOWN")
    write_task(store, {**base, "task_id": "graph-delta", "goal": "已接收且验收通过"}, "accepted", "PASS")


def load_text(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def check_status_and_null(base, tasks):
    graph_js = load_text("web", "task-graph.js")
    app_js = load_text("web", "app.js")
    by_id = {row["task_id"]: row for row in tasks}
    alpha = by_id["graph-alpha"]
    gamma = by_id["graph-gamma"]
    beta = by_id["graph-beta"]
    mapping_ok = all(code in graph_js and label in graph_js for code, label in STATUS_PAIRS)
    mapping_ok = mapping_ok and all(code in graph_js and label in graph_js for code, label in ACCEPTANCE_PAIRS)
    unknown_ok = "未知（" in graph_js and "未知（" in app_js
    null_ok = ("model" not in alpha["task"] or alpha["task"].get("model") is None) and "未记录" in graph_js
    preserved = gamma["state"]["status"] == "weave-unknown" and gamma["state"]["acceptance"] == "UNKNOWN"
    model_kept = beta["task"].get("model") == "opencode-go/deepseek-v4.1-flash"
    return {
        "check": "status_acceptance_unknown_null",
        "status": "PASS" if (mapping_ok and unknown_ok and null_ok and preserved and model_kept) else "FAIL",
        "detail": {"status_labels_10": mapping_ok, "unknown_kept": unknown_ok, "null_model_kept": null_ok,
                   "raw_values_preserved": preserved, "explicit_model_kept": model_kept},
    }


def check_dependencies_disconnect(base, tasks):
    graph_js = load_text("web", "task-graph.js")
    app_js = load_text("web", "app.js")
    by_id = {row["task_id"]: row for row in tasks}
    beta_dep = by_id["graph-beta"]["task"].get("dependencies") == ["graph-alpha"]
    gamma_dep = "graph-missing" in by_id["graph-gamma"]["task"].get("dependencies", [])
    edge_source = "dependencies" in graph_js and "缺失依赖" in graph_js and "breadthfirst" in graph_js
    one_poller = app_js.count("setInterval(") == 1 and "setInterval(()=>refresh(),5000)" in app_js
    no_graph_poller = "setInterval" not in graph_js
    disconnect = ("断连" in app_js and "setConnection" in app_js and "cy.stop" in graph_js
                  and "最后成功" in app_js)
    select_wired = "onSelect" in graph_js and "onSelect" in app_js and 'cy.on("tap", "node"' in graph_js
    # F1：从未成功加载（未知）与成功加载且为空必须区分；图、文本列表、表格都不把未知当空。
    unknown_vs_empty = ("hasLoadedTasks" in app_js and "hasLoaded" in graph_js
                        and "update(state.tasks, state.hasLoadedTasks)" in app_js
                        and "无法读取任务" in app_js and "无法读取任务" in graph_js)
    ok = (beta_dep and gamma_dep and edge_source and one_poller and no_graph_poller
          and disconnect and select_wired and unknown_vs_empty)
    return {
        "check": "dependency_selection_disconnect",
        "status": "PASS" if ok else "FAIL",
        "detail": {"dependency_edge_beta": beta_dep, "missing_dependency_seen": gamma_dep,
                   "edges_from_dependencies_only": edge_source, "single_5s_poller": one_poller,
                   "graph_has_no_poller": no_graph_poller, "disconnect_stops_animation": disconnect,
                   "node_click_wired": select_wired, "unknown_vs_empty_distinct": unknown_vs_empty},
    }


def check_static_routes(base):
    runtime_status, _, runtime_raw = request(base + "/api/runtime")
    runtime = json.loads(runtime_raw) if runtime_status == 200 else {}
    loaded = runtime.get("loaded_sha256", {})

    # 逐一取回每个实际加载的 UI 路由响应体。
    responses = {}
    for route, _key in UI_ROUTES:
        status, headers, body = request(base + route)
        responses[route] = {"status": status, "headers": headers, "body": body}

    page_status = responses["/"]["status"]
    csp = responses["/"]["headers"].get("Content-Security-Policy", "")
    page = responses["/"]["body"]
    js = responses["/task-graph.js"]
    cy = responses["/vendor/cytoscape.min.js"]
    lic_status, _, lic_body = request(base + "/vendor/cytoscape-LICENSE")

    # F3：对每个实际服务的 UI 路由，比较 runtime 记录哈希 == HTTP 响应体 SHA-256 == 本地磁盘 SHA-256。
    route_hashes = {}
    for route, key in UI_ROUTES:
        body = responses[route]["body"]
        http_hash = hashlib.sha256(body).hexdigest()
        disk_hash = hashlib.sha256(ROOT.joinpath(*key.split("/")).read_bytes()).hexdigest()
        runtime_hash = loaded.get(key)
        route_hashes[route] = {"runtime_key": key, "runtime_sha256": runtime_hash,
                               "http_sha256": http_hash, "disk_sha256": disk_hash,
                               "match": runtime_hash == http_hash == disk_hash}
    ui_hashes_ok = all(item["match"] for item in route_hashes.values())

    build_ok = isinstance(runtime.get("build"), str) and runtime["build"] != "shared-session-20261015"
    routes_ok = (js["status"] == 200 and "javascript" in js["headers"].get("Content-Type", "")
                 and b"BridgeTaskGraph" in js["body"]
                 and cy["status"] == 200 and "javascript" in cy["headers"].get("Content-Type", "")
                 and lic_status == 200 and b"Permission is hereby granted" in lic_body)
    csp_ok = (page_status == 200 and "default-src 'self'" in csp and "script-src 'self'" in csp
              and "unsafe-inline" not in csp)
    ui_ok = all(marker in page for marker in (b"id=\"taskRows\"", b"id=\"searchInput\"", b"id=\"detailPanel\"",
                                              b"id=\"taskDialog\"", b"/vendor/cytoscape.min.js", b"/task-graph.js"))
    no_cdn = b"http://" not in page and b"https://" not in page
    traversals = [raw_path_status(base, p) for p in ("/../bridge.py", "/vendor/../app.js", "/%2e%2e/bridge.py")]
    traversal_ok = all(code == 404 for code in traversals)
    ok = (runtime_status == 200 and ui_hashes_ok and build_ok and routes_ok and csp_ok
          and ui_ok and no_cdn and traversal_ok)
    return {
        "check": "static_routes_and_ui_compat",
        "status": "PASS" if ok else "FAIL",
        "detail": {"runtime_ok": runtime_status == 200, "ui_response_hashes_match": ui_hashes_ok,
                   "ui_route_hashes": route_hashes, "build_updated": build_ok,
                   "whitelist_routes": routes_ok, "csp_self_only": csp_ok,
                   "original_ui_present": ui_ok, "no_cdn_reference": no_cdn,
                   "traversal_rejected": traversal_ok, "traversal_codes": traversals},
    }


# ---- 仅供主线程真实浏览器人工核验 F1 的隔离 UI 夹具（显式 --serve-ui-fixture 才启用）----

MARKER_CSS = (b"\n.fixture-marker{position:fixed;top:0;left:0;right:0;z-index:99999;"
              b"background:#b14e47;color:#fff;font:700 12px/1.6 'DM Mono',monospace;"
              b"text-align:center;padding:7px 10px;letter-spacing:.4px}\n")


def fixture_marker_html(mode):
    text = ("⚠ 样本夹具（%s）：真实 UI + 固定 /api/tasks；由 tests/graph_checks.py "
            "--serve-ui-fixture 提供；独立临时 store，非真实 store，无模型请求、无外部访问。" % mode)
    return ("<div class=\"fixture-marker\">" + text + "</div>").encode("utf-8")


class UiFixtureHandler(webui.BridgeHandler):
    """真实 UI + 固定 /api/tasks 的隔离夹具，用于浏览器人工核验 F1 未知/空态。"""

    def do_GET(self):
        mode = getattr(self.server, "fixture_mode", None)
        path = urlsplit(self.path).path
        if path == "/api/tasks":
            if mode == "api-fail":
                # 固定 503，正文含 fixture；不访问真实 store。
                return self._send(503, {"error": "fixture api-fail: /api/tasks 固定 503（样本，不访问真实 store）"})
            return self._send(200, {"tasks": []})
        if path == "/":
            page = (webui.WEB_ROOT / "index.html").read_bytes()
            page = page.replace(b"<body>", b"<body>" + fixture_marker_html(mode or "empty"), 1)
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/style.css":
            css = (webui.WEB_ROOT / "style.css").read_bytes() + MARKER_CSS
            return self._send(200, css, "text/css; charset=utf-8")
        return super().do_GET()


class UiFixtureServer(webui.BridgeHTTPServer):
    def __init__(self, address, store, mode):
        super().__init__(address, store, {})
        self.fixture_mode = mode
        self.RequestHandlerClass = UiFixtureHandler


def serve_ui_fixture(mode, port):
    """显式夹具模式：绑定 127.0.0.1 指定端口持续提供真实 UI，独立临时 store。"""
    store = ROOT / "data" / ("ui-fixture-" + uuid.uuid4().hex[:12])
    store.mkdir(parents=True, exist_ok=True)
    server = UiFixtureServer(("127.0.0.1", port), store, mode)
    print(json.dumps({
        "fixture": mode, "url": f"http://127.0.0.1:{port}/", "store": str(store),
        "marker": "顶部醒目红色横幅（样本夹具）",
        "api_tasks": "固定 503，正文含 fixture" if mode == "api-fail" else "成功空列表 {\"tasks\": []}",
        "model_requests": 0, "external_requests": 0, "acceptance": "BLOCKED",
        "note": "仅在显式 --serve-ui-fixture 时启动；真实浏览器人工核验 F1，Ctrl+C 结束。",
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--serve-ui-fixture", choices=("api-fail", "empty"), default=None,
                        help="仅供浏览器人工核验 F1：绑定 127.0.0.1 --port 持续提供真实 UI + 固定 /api/tasks")
    parser.add_argument("--port", type=int, default=None,
                        help="--serve-ui-fixture 时的本机端口（1–65535）")
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须为 1–3")
    if args.serve_ui_fixture is not None:
        if args.port is None or not 1 <= args.port <= 65535:
            parser.error("--serve-ui-fixture 需要 --port 指定 1–65535 的本机端口")
        return serve_ui_fixture(args.serve_ui_fixture, args.port)

    home = ROOT / "data" / ("graph-check-" + uuid.uuid4().hex[:12])
    home.mkdir(parents=True)
    store = home / "store"
    fixtures(store)

    server = webui.BridgeHTTPServer(("127.0.0.1", 0), store, {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks = []
    executed = []
    try:
        _, _, listing = request(base + "/api/tasks")
        tasks = json.loads(listing)["tasks"]
        plan = (
            ("status_acceptance_unknown_null", lambda: check_status_and_null(base, tasks)),
            ("dependency_selection_disconnect", lambda: check_dependencies_disconnect(base, tasks)),
            ("static_routes_and_ui_compat", lambda: check_static_routes(base)),
        )
        for name, run in plan[:args.limit]:  # 执行前按 limit 截断：只运行前 N 组
            executed.append(name)
            checks.append(run())
    except Exception as error:  # 保真实错误，不伪造通过
        checks.append({"check": "graph_checks_flow", "status": "FAIL",
                       "exception": type(error).__name__, "message": str(error)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    record = {"limit": args.limit, "executed_checks": executed, "checks": checks,
              "retained_fixture": str(home), "model_requests": 0, "acceptance": "BLOCKED"}
    webui.bridge.atomic(home / "checks.json", record)
    for check in checks:
        print(json.dumps(check, ensure_ascii=False))
    print(json.dumps({"limit": args.limit, "executed_checks": executed,
                      "retained_fixture": str(home), "store": str(store),
                      "model_requests": 0, "acceptance": "BLOCKED"}, ensure_ascii=False))
    return 1 if any(item["status"] != "PASS" for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
