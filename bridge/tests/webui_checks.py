"""--limit 3 离线检查本地控制台的页面、任务提交/规划和来源限制。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import webui


def request(url, method="GET", body=None, origin=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def fixture_repo(home):
    repo = home / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "input.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "input.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Bridge Fixture",
                    "-c", "user.email=bridge-fixture@example.invalid", "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return repo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须为 1–3")
    home = ROOT / "data" / ("webui-check-" + uuid.uuid4().hex[:12])
    home.mkdir(parents=True)
    store = home / "store"
    repo = fixture_repo(home)
    fake_dsh = home / "fake-dsh.js"
    fake_dsh.write_text(
        "const fs=require('fs');\n"
        "fs.writeFileSync('result.txt','fixture output\\n',{encoding:'utf8'});\n"
        "console.log(JSON.stringify({type:'final',text:'fixture complete'}));\n",
        encoding="utf-8")
    config = {"node": str(webui.bridge.adapters.NODE), "dsh": str(fake_dsh)}
    config_path = home / "webui_config.json"
    webui.bridge.atomic(config_path, config)
    server = webui.BridgeHTTPServer(("127.0.0.1", 0), store, config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    origin = base
    checks = []
    try:
        status, headers, page = request(base + "/")
        js_status, _, js = request(base + "/app.js")
        checks.append({"check":"static_console","status":"PASS" if status == 200 and js_status == 200 and "default-src 'self'" in headers.get("Content-Security-Policy", "") and b"Agent Bridge" in page and b"approve_start" in js else "FAIL"})
        contract = {"task_id":"webui-fixture","goal":"Web UI API fixture","repo":str(repo),"executor":"dsh",
                    "allowed_paths":["result.txt"],"artifacts":["result.txt"],"input_refs":["input.txt"],
                    "completion_criteria":["fixture only"],"timeout_seconds":30}
        status, _, created = request(base + "/api/tasks", "POST", {"contract":contract}, origin)
        item = json.loads(created)
        list_status, _, listing = request(base + "/api/tasks")
        detail_status, _, detail = request(base + "/api/tasks/webui-fixture")
        ok = status == 201 and item.get("status") == "ready" and list_status == 200 and detail_status == 200 and len(json.loads(listing)["tasks"]) == 1 and json.loads(detail)["state"]["status"] == "ready"
        checks.append({"check":"submit_list_detail","status":"PASS" if ok else "FAIL"})
        plan_status, _, plan_raw = request(base + "/api/tasks/webui-fixture/plan", "POST", {}, origin)
        plan = json.loads(plan_raw)
        bad_status, _, _ = request(base + "/api/tasks/webui-fixture/plan", "POST", {}, "http://example.invalid")
        ok = plan_status == 200 and len(plan.get("plan_sha256", "")) == 64 and bad_status == 403
        checks.append({"check":"plan_and_origin_guard","status":"PASS" if ok else "FAIL"})
    except Exception as error:
        checks.append({"check":"http_flow","status":"FAIL","exception":type(error).__name__})
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    checks = checks[:args.limit]
    record = {"checks":checks,"retained_fixture":str(home),"model_requests":0,"acceptance":"BLOCKED"}
    webui.bridge.atomic(home / "checks.json", record)
    for check in checks:
        print(json.dumps(check, ensure_ascii=False))
    print(json.dumps({"retained_fixture":str(home),"store":str(store),"config":str(config_path),
                      "acceptance":"BLOCKED"}, ensure_ascii=False))
    return 1 if any(item["status"] != "PASS" for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
