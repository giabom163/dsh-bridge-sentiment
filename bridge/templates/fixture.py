#!/usr/bin/env python3
"""离线夹具（record / serve / check / list）：让"没有凭据、没有网络、平台挂了"时也能真验证。

**重要前提（本会话独立复核后修正）**：
  1. 产品的凭据门在发送之前（`analysis.py` L299-301），所以**指向本地夹具也必须有 key**——
     除非显式设置 `OPENCODE_GO_V1_FIXTURE=1`（产品侧已加这个开关，默认关闭）。
  2. 匹配键**不能**用整包 body 的 SHA：产品请求体是 `{model, reasoning_effort, stream, messages(system+user)}`，
     与录制时的手写体永远不同。默认按 **messages 子集**匹配，另有 `--match model`（同名模型即命中）与
     `--match body`（严格整包）两种模式。

用法：
  # 1) 录一条（会发 1 次真实请求）
  python -B fixture.py record --name chat-basic --model deepseek-v4.1-flash --prompt "只输出 JSON"
  # 2) 起本地夹具服务（0 外网请求）
  python -B fixture.py serve --port 8799 [--match messages|model|body]
  # 3) 校验 / 列出
  python -B fixture.py check --name chat-basic
  python -B fixture.py list

客户端接法（产品侧）：
  set OPENCODE_GO_V1_ENDPOINT=http://127.0.0.1:8799/v1/chat/completions
  set OPENCODE_GO_V1_FIXTURE=1        # 显式开启夹具模式（跳过凭据门，仅用于离线验证）
"""
import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent
FIX = ROOT / "fixtures"
ZEN = "https://opencode.ai/zen/go/v1/chat/completions"


def key():
    p = pathlib.Path.home() / ".dsh" / ".credentials.yaml"
    if not p.is_file():
        return None
    m = re.search(r"OPENCODE_GO_V1_API_KEY\s*:\s*[\"']?([^\s\"']+)",
                  p.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def body_of(name, model, prompt):
    return {"model": model, "reasoning_effort": "none", "temperature": 0, "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}]}


def sig(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def cmd_record(args):
    k = key()
    if not k:
        print(json.dumps({"error": "没有 OPENCODE_GO_V1_API_KEY，无法录制"}, ensure_ascii=False))
        return 2
    body = body_of(args.name, args.model, args.prompt)
    headers = {"content-type": "application/json", "authorization": "Bearer " + k,
               "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
               "accept": "application/json", "x-opencode-session": "fixture-record"}
    req = urllib.request.Request(args.endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, ensure_ascii=False))
        return 1
    FIX.mkdir(parents=True, exist_ok=True)
    data = {"name": args.name, "sig": sig(body), "recorded_at": datetime.now(timezone.utc).isoformat(),
            "request": {"model": body["model"], "messages": body["messages"]},
            "http_status": status, "response_raw": raw}
    (FIX / f"{args.name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "name": args.name, "sig": data["sig"], "http": status,
                      "bytes": len(raw)}, ensure_ascii=False))
    return 0


def cmd_list(args):
    FIX.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(FIX.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items.append({"name": d.get("name"), "sig": d.get("sig"),
                      "model": (d.get("request") or {}).get("model"),
                      "http": d.get("http_status"), "recorded_at": d.get("recorded_at"),
                      "bytes": len(d.get("response_raw") or "")})
    print(json.dumps({"fixtures": items, "dir": str(FIX), "match_modes": ["messages", "model", "body"]},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_check(args):
    p = FIX / f"{args.name}.json"
    problems = []
    if not p.is_file():
        problems.append(f"夹具不存在：{p}")
    else:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            raw = d.get("response_raw") or ""
            json.loads(raw)
            if not (d.get("request") or {}).get("model"):
                problems.append("请求里的 model 缺失（serve 会跳过它）")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"内容不合法：{exc}")
    print(json.dumps({"name": args.name, "ok": not problems, "problems": problems}, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


def matches(fx, body, mode):
    """返回 (是否命中, 原因)。mode: messages（子集）/ model / body（严格整包 SHA）。"""
    req = fx.get("request") or {}
    model = req.get("model")
    if not model:
        return False, "夹具缺 request.model"
    if mode == "body":
        return (fx.get("sig") == sig(body)), "整包 SHA"
    if body.get("model") != model:
        return False, f"model 不同（夹具 {model} vs 请求 {body.get('model')}）"
    if mode == "model":
        return True, "model 相同"
    have = {(m.get("role"), m.get("content")) for m in (body.get("messages") or []) if isinstance(m, dict)}
    want = {(m.get("role"), m.get("content")) for m in (req.get("messages") or []) if isinstance(m, dict)}
    if not want:
        return False, "夹具没有 messages"
    ok = want <= have
    return ok, ("messages 子集命中" if ok else "messages 不匹配（请求里缺夹具记录的那几条）")


class Handler(BaseHTTPRequestHandler):
    fixtures = []
    mode = "messages"

    def log_message(self, *a):
        pass

    def _log(self, record):
        FIX.mkdir(parents=True, exist_ok=True)
        with (FIX / "replays.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = {}
        hit, why = None, None
        for fx in Handler.fixtures:
            ok, reason = matches(fx["data"], body, Handler.mode)
            if ok:
                hit, why = fx["data"], reason
                break
            why = reason
        now = datetime.now(timezone.utc).isoformat()
        if not hit:
            # 错请求必须 404，并且**也记日志**（否则"修后 404"没有正向证据）
            self._log({"at": now, "hit": False, "mode": Handler.mode, "why": why,
                       "request_model": body.get("model") if isinstance(body, dict) else None,
                       "request_sig": sig(body) if isinstance(body, dict) and body else None})
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "没有匹配的夹具", "why": why}},
                                        ensure_ascii=False).encode())
            return
        payload = hit["response_raw"].encode()
        self.send_response(hit.get("http_status", 200))
        self.send_header("content-type", "application/json")
        self.send_header("x-fixture", hit["name"])
        self.send_header("x-fixture-match", Handler.mode)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self._log({"at": now, "hit": True, "fixture": hit["name"], "mode": Handler.mode,
                   "why": why, "bytes": len(payload)})


def cmd_serve(args):
    FIX.mkdir(parents=True, exist_ok=True)
    skipped = []
    for p in sorted(FIX.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            skipped.append({"file": p.name, "why": "非法 JSON"})
            continue
        if not (d.get("request") or {}).get("model") or not d.get("response_raw"):
            skipped.append({"file": p.name, "why": "缺 request.model 或 response_raw"})
            continue
        Handler.fixtures.append({"data": d})
    if not Handler.fixtures:
        print(json.dumps({"error": "fixtures/ 里没有可用录制，先跑 record", "skipped": skipped},
                         ensure_ascii=False))
        return 1
    Handler.mode = args.match
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"serving": f"http://127.0.0.1:{args.port}/v1/chat/completions",
                      "fixtures": len(Handler.fixtures), "match": args.match,
                      "skipped": skipped, "external_requests": 0}, ensure_ascii=False))
    sys.stdout.flush()
    srv.serve_forever()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--name", required=True)
    r.add_argument("--model", default="deepseek-v4.1-flash")
    r.add_argument("--prompt", default='只输出一个 JSON：{"ok":true}')
    r.add_argument("--endpoint", default=ZEN)
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("--name", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8799)
    s.add_argument("--match", default="messages", choices=["messages", "model", "body"],
                   help="匹配模式：messages（默认，夹具 messages ⊆ 请求）/ model / body（严格整包 SHA）")
    args = ap.parse_args()
    return {"record": cmd_record, "list": cmd_list, "check": cmd_check, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
