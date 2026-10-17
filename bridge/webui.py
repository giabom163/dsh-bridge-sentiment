"""Loopback-only browser console for the local Agent Bridge."""
import argparse
import base64
import json
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 部署目录里 bridge 只存在于 agent-bridge.pyz 内；Python 可直接从 zip 导入。
ARCHIVE = ROOT / "agent-bridge.pyz"
if ARCHIVE.is_file() and str(ARCHIVE) not in sys.path:
    sys.path.insert(0, str(ARCHIVE))

import bridge

WEB_ROOT = ROOT / "web"
MAX_BODY = 1024 * 1024


def open_folder(target):
    """用系统文件管理器打开一个目录：固定可执行入口，不拼 shell 字符串、不解释用户输入。"""
    path = Path(target).resolve()
    if not path.is_dir():
        raise FileNotFoundError("目录不存在：" + str(path))
    if sys.platform == "win32":
        subprocess.Popen(["explorer.exe", str(path)], close_fds=True)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return str(path)


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, store, config, reports=None):
        super().__init__(address, BridgeHandler)
        self.store = Path(store).resolve()
        self.reports = Path(reports).resolve() if reports else None
        self.config = config
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        # 固定启动时加载的字节，磁盘之后变化不会冒充已热加载。
        loaded = {module.__name__: bridge.digest(module.__loader__.get_data(module.__file__))
                  for module in (bridge, bridge.adapters)}
        loaded.update({name: bridge.digest((ROOT / name).read_bytes())
                       for name in ("webui.py", "web/app.js", "web/index.html", "web/style.css",
                                    "web/task-graph.js", "web/vendor/cytoscape.min.js")})
        self.runtime = {"pid": __import__("os").getpid(), "started_at": bridge.now(), "store": str(self.store),
                        "loaded_sha256": loaded, "bridge_module": bridge.__file__,
                        "build": "shared-session-graph-20261015"}


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AgentBridgeUI/1.0"

    def log_message(self, fmt, *args):
        # Avoid writing task content, query strings, or local paths into access logs.
        return

    def _send(self, status, value, content_type="application/json; charset=utf-8"):
        if isinstance(value, bytes):
            body = value
        else:
            body = bridge.scrub(json.dumps(value, ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # style-src 允许 inline：Cytoscape 会注入一个 <style> 元素（style-src-elem），实测被拦。
        # 本控制台仅回环、Host/Origin 强校验、静态资源白名单、无用户 HTML 注入，故只对样式放宽，脚本仍限 'self'。
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": bridge.scrub(str(message))})

    def _body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length 非法")
        if size < 1 or size > MAX_BODY:
            raise ValueError("请求体为空或超过 1 MiB")
        try:
            value = json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体必须是 UTF-8 JSON")
        if not isinstance(value, dict):
            raise ValueError("JSON 顶层必须为对象")
        return value

    def _safe_task_id(self, value):
        task_id = unquote(value)
        if not bridge.ID.fullmatch(task_id):
            raise ValueError("任务编号非法")
        return task_id

    def _attempts(self, directory):
        """每次 attempt 的时间与结果，供「流程」页画 Timeline 水位条（只读，缺文件就留空）。"""
        rows = []
        attempts_dir = directory / "attempts"
        if not attempts_dir.is_dir():
            return rows
        for entry in sorted(attempts_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.is_symlink():
                continue
            row = {"attempt_id": entry.name}
            for name, keys in (("attempt.json", ("started_at", "executor", "model", "worktree", "base_commit")),
                               ("result.json", ("finished_at", "exit_code", "stop_reason", "violations",
                                                "missing_artifacts", "native_errors", "review_gate", "acceptance",
                                                "execution_facts"))):
                if (entry / name).is_file():
                    try:
                        data = bridge.read(entry / name)
                    except (OSError, ValueError):
                        continue
                    for key in keys:
                        if key in data:
                            row[key] = data[key]
            if (entry / "interrupt.json").is_file():
                row["interrupted"] = True
            rows.append(row)
        rows.sort(key=lambda r: str(r.get("started_at") or ""))
        return rows

    def _task_data(self, task_id):
        directory = bridge.task_dir(self.server.store, task_id)
        task, state = bridge.load_task(directory)
        current = state.get("current_attempt")
        result = None
        native = attempt_info = observation = None
        activity = []
        if current:
            attempt = directory / "attempts" / current
            result_path = attempt / "result.json"
            attempt_info = bridge.read(attempt / "attempt.json")
            if (attempt / "native-session.json").is_file():
                native = bridge.read(attempt / "native-session.json")
            if (attempt / "native-observation.json").is_file():
                observation = bridge.read(attempt / "native-observation.json")
            if result_path.is_file():
                result = bridge.read(result_path)
            log = attempt / "stdout.log"
            if log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 65536))
                    lines = stream.read(65536).decode("utf-8", "replace").splitlines()
                for line in lines:
                    try:
                        item = json.loads(line)
                        part = item.get("part") or {}
                        if item.get("type") in ("text", "tool_use", "error", "step_finish"):
                            activity.append({"type": item.get("type"), "tool": part.get("tool"),
                                             "status": (part.get("state") or {}).get("status"),
                                             "text": str(part.get("text") or item.get("error") or "")[-4000:]})
                    except (ValueError, AttributeError, TypeError):
                        continue
            # 详情页的准入用**实时复算**：state.json 里存的是采集当时的值，代码修好后二者会不一致
            # （发生过：新建文件被误判 artifacts_incomplete）。复算失败就退回存量值并标注来源。
            try:
                live = bridge.reviewability(attempt, state, result=result)
                state = {**state, "review_gate": live, "reviewable": live["reviewable"],
                         "review_gate_source": "live"}
            except Exception:
                state = {**state, "review_gate_source": "stored"}
        with self.server.jobs_lock:
            job = self.server.jobs.get(task_id)
        return {"task": task, "state": state, "result": result, "job": job,
                "native": native, "native_observation": observation, "attempt": attempt_info,
                "activity": activity[-12:], "commands": self._commands(task_id),
                "paths": self._paths(task_id, state), "attempts": self._attempts(directory)}
    def _commands(self, task_id):
        """把常用 CLI 命令下发到界面，省掉另开命令行窗口。"""
        base = '& "%s" -B "%s" --limit 3 --store "%s"' % (sys.executable, ARCHIVE, self.server.store)
        return [
            {"label": "查看状态", "command": "%s status %s" % (base, task_id)},
            {"label": "只读派生视图（全部任务）", "command": "%s doctor" % base},
            {"label": "只读审查（verdict=pass）",
             "command": '%s review %s --decision audit --verdict pass --audit-evidence "<审计文件>"' % (base, task_id)},
            {"label": "人工接收（需审计 + 用户证据）",
             "command": '%s review %s --decision accept --audit-evidence "<审计文件>" --user-evidence "<你的证据文件>"' % (base, task_id)},
            {"label": "存量回填（写历史，先逐文件备份）",
             "command": "%s backfill %s --approve-backfill" % (base, task_id)},
        ]

    def _paths(self, task_id, state):
        """界面「打开目录」可用的实际路径；只由 store 与 attempt 记录推导，不接受请求方给的路径。"""
        directory = bridge.task_dir(self.server.store, task_id)
        paths = {"store": str(self.server.store), "task": str(directory)}
        attempt_id = state.get("current_attempt")
        if attempt_id:
            attempt_dir = directory / "attempts" / attempt_id
            paths["attempt"] = str(attempt_dir)
            paths["worktree"] = str(bridge.recorded_worktree(attempt_dir))
            paths["evidence"] = str(attempt_dir / "web-evidence")
        if self.server.reports:
            paths["reports"] = str(self.server.reports)
        return paths

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/runtime":
                return self._send(200, self.server.runtime)
            # 浏览器会自动请求这两个；显式回 204，避免控制台日志里永远挂着 404 噪音。
            if path in ("/favicon.ico", "/robots.txt"):
                return self._send(204, b"")
            if path == "/":
                page = (WEB_ROOT / "index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/app.js":
                page = (WEB_ROOT / "app.js").read_bytes()
                return self._send(200, page, "text/javascript; charset=utf-8")
            if path == "/style.css":
                page = (WEB_ROOT / "style.css").read_bytes()
                return self._send(200, page, "text/css; charset=utf-8")
            # 静态资源逐项显式白名单：绝不按请求路径拼接文件路径。
            if path == "/task-graph.js":
                page = (WEB_ROOT / "task-graph.js").read_bytes()
                return self._send(200, page, "text/javascript; charset=utf-8")
            if path == "/vendor/cytoscape.min.js":
                page = (WEB_ROOT / "vendor" / "cytoscape.min.js").read_bytes()
                return self._send(200, page, "text/javascript; charset=utf-8")
            if path == "/vendor/cytoscape-LICENSE":
                page = (WEB_ROOT / "vendor" / "cytoscape-LICENSE").read_bytes()
                return self._send(200, page, "text/plain; charset=utf-8")
            if path == "/vendor/NOTICE.md":
                page = (WEB_ROOT / "vendor" / "NOTICE.md").read_bytes()
                return self._send(200, page, "text/markdown; charset=utf-8")
            if path == "/api/tasks":
                tasks_dir = self.server.store / "tasks"
                rows = []
                if tasks_dir.is_dir():
                    for entry in sorted(tasks_dir.iterdir(), key=lambda p: p.name):
                        if not bridge.ID.fullmatch(entry.name) or entry.is_symlink():
                            continue
                        try:
                            data = self._task_data(entry.name)
                            rows.append({"task_id": entry.name, "task": data["task"],
                                         "state": data["state"], "job": data["job"]})
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
                return self._send(200, {"tasks": rows})
            match = re.fullmatch(r"/api/tasks/([^/]+)", path)
            if match:
                return self._send(200, self._task_data(self._safe_task_id(match.group(1))))
            return self._error(404, "接口不存在")
        except FileNotFoundError:
            return self._error(404, "任务或界面文件不存在")
        except (ValueError, OSError, KeyError, TypeError) as error:
            return self._error(400, error)

    def _check_origin(self):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host", "").lower() != expected_host.lower():
            raise PermissionError("仅允许通过本机 127.0.0.1 地址操作")
        origin = self.headers.get("Origin")
        if origin != f"http://{expected_host}":
            raise PermissionError("写入操作必须来自本地控制台页面")

    def _save_evidence(self, directory, label, item):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"缺少 {label} 证据文件")
        name = Path(item["name"]).name
        if not name or len(name) > 120:
            raise ValueError(f"{label} 证据文件名非法")
        try:
            content = base64.b64decode(item.get("content_base64", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError(f"{label} 证据文件编码非法")
        if not content or len(content) > 256 * 1024:
            raise ValueError(f"{label} 证据文件为空或超过 256 KiB")
        target_dir = directory / "web-evidence"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{label}-{uuid.uuid4().hex[:12]}-{name}"
        with target.open("xb") as stream:
            stream.write(content)
        return target

    def do_POST(self):
        try:
            self._check_origin()
            body = self._body()
            path = urlsplit(self.path).path
            if path == "/api/open":
                # 打开目录：路径只从 store / attempt 推导（或启动时显式配置的报告目录），请求方不能指定任意路径。
                scope = body.get("scope")
                task_id = body.get("task_id")
                paths = self._paths(self._safe_task_id(task_id), bridge.load_task(bridge.task_dir(self.server.store,
                                                                                               self._safe_task_id(task_id)))[1]) if task_id else {}
                if scope == "store":
                    target = self.server.store
                elif scope == "reports":
                    if not self.server.reports:
                        raise ValueError("本控制台启动时未配置报告目录（--reports）")
                    target = self.server.reports
                elif scope in ("task", "attempt", "worktree", "evidence"):
                    if scope not in paths:
                        raise ValueError("该任务没有这一层目录（可能还没有尝试）")
                    target = paths[scope]
                else:
                    raise ValueError("scope 只支持 store / reports / task / attempt / worktree / evidence")
                resolved = Path(target).resolve()
                if scope != "reports" and resolved != self.server.store and not resolved.is_relative_to(self.server.store):
                    raise PermissionError("只允许打开本 store 内的目录")
                return self._send(200, {"opened": open_folder(resolved), "scope": scope})
            if path == "/api/tasks":
                task = bridge.validate(body.get("contract"))
                submissions = self.server.store / "web-submissions"
                submissions.mkdir(parents=True, exist_ok=True)
                source = submissions / f"{task['task_id']}-{uuid.uuid4().hex[:12]}.json"
                bridge.atomic(source, task)
                return self._send(201, bridge.submit(self.server.store, source))
            match = re.fullmatch(r"/api/tasks/([^/]+)/(plan|prepare|run|native-status|cancel|revise|review)", path)
            if not match:
                return self._error(404, "接口不存在")
            task_id, action = self._safe_task_id(match.group(1)), match.group(2)
            if action == "plan":
                return self._send(200, bridge.plan(self.server.store, task_id, self.server.config))
            if action == "native-status":
                return self._send(200, bridge.native_status(self.server.store, task_id, self.server.config))
            if action in ("prepare", "run"):
                plan_hash = body.get("plan_sha256")
                if not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
                    raise ValueError("需要有效的计划 SHA-256")
                if body.get("approve_start") is not True or body.get("ack_unmetered") is not True:
                    raise PermissionError("启动须在界面明确批准计划及未实证预算限制")
                task, _ = bridge.load_task(bridge.task_dir(self.server.store, task_id))
                visible = body.get("confirm_visible") is True
                if action == "run" and bridge.adapters.shared(task) and not visible:
                    raise PermissionError("先在真实 OpenCode 客户端找到空会话，再确认可见并发送提示词")
                with self.server.jobs_lock:
                    active = [key for key, value in self.server.jobs.items() if value.get("running")]
                    if active:
                        raise ValueError("已有前端任务运行中；请等待结束或核对状态")
                    self.server.jobs[task_id] = {"running": True, "started_at": bridge.now()}

                def execute():
                    try:
                        result = bridge.run(self.server.store, task_id, self.server.config,
                                            plan_hash, True, True, action == "prepare", visible)
                        job = {"running": False, "finished_at": bridge.now(), "result": result}
                    except Exception as error:
                        job = {"running": False, "finished_at": bridge.now(),
                               "error": bridge.scrub(str(error)), "acceptance": "BLOCKED"}
                    with self.server.jobs_lock:
                        self.server.jobs[task_id] = job

                threading.Thread(target=execute, name="agent-bridge-run", daemon=True).start()
                return self._send(202, {"task_id": task_id, "running": True,
                                        "note": "执行器已由前端批准启动；额度、原生重试与请求数无法由桥硬限制"})
            if action == "cancel":
                if body.get("approve_cancel") is not True:
                    raise PermissionError("结束任务进程树须在界面明确批准")
                return self._send(202, bridge.cancel(self.server.store, task_id,
                                                    body.get("attempt_id"), True))
            if action == "revise":
                task = bridge.validate(body.get("contract"))
                submissions = self.server.store / "web-submissions"
                submissions.mkdir(parents=True, exist_ok=True)
                source = submissions / f"{task_id}-rev{uuid.uuid4().hex[:12]}.json"
                bridge.atomic(source, task)
                return self._send(200, bridge.revise(self.server.store, task_id, source))
            if action == "review":
                directory = bridge.task_dir(self.server.store, task_id)
                decision = body.get("decision")
                if decision not in ("audit", "accept", "reject", "dismiss", "void"):
                    raise ValueError("decision 只支持 audit / accept / reject / dismiss / void")
                audit = self._save_evidence(directory, "audit", body.get("audit_evidence")) if body.get("audit_evidence") else None
                if decision == "audit" and audit is None:
                    raise ValueError("审计阶段需要只读审查证据")
                user = None
                if decision in ("accept", "reject") and body.get("user_evidence"):
                    user = self._save_evidence(directory, "user", body["user_evidence"])
                if decision == "accept" and user is None:
                    raise ValueError("接收必须同时附只读审计和用户真实路径证据")
                verdict = body.get("verdict")
                if verdict not in (None, "pass", "fail", "blocked"):
                    raise ValueError("verdict 只支持 pass / fail / blocked")
                return self._send(200, bridge.review(self.server.store, task_id, decision, audit, user,
                                                     verdict=verdict or "blocked", reason=body.get("reason")))
            return self._error(404, "接口不存在")
        except PermissionError as error:
            return self._error(403, error)
        except FileExistsError as error:
            return self._error(409, error)
        except (ValueError, OSError, KeyError, TypeError) as error:
            return self._error(400, error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--store", type=Path, default=ROOT / "data")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reports", type=Path, default=None,
                        help="可选：界面上「打开报告目录」指向的目录（本轮产物所在处）")
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须为 1–3")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535")
    config = bridge.read(args.config) if args.config else {}
    server = BridgeHTTPServer((args.host, args.port), args.store, config, args.reports)
    print(json.dumps({"url": f"http://{args.host}:{args.port}/", "store": str(server.store),
                      "reports": str(server.reports) if server.reports else None,
                      "model_requests": 0, "acceptance": "BLOCKED",
                      "security": "loopback only; mutating requests require same-origin browser"}, ensure_ascii=False),
          flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
