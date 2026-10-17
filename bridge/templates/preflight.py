#!/usr/bin/env python3
"""开工自检（preflight）：把"开工才发现"的问题提前到 0 成本发现。

默认 **1 次模型请求**（凭据有效性，`--no-credential` 可关掉 → 0 网络）：仓库干净度、产品库与数据量、
冷却账、桥账本、OpenCLI 扩展是否在线。加 `--network` 会再逐个平台查登录态（最多 `--limit` 个，间隔 25 秒）。

用法：
  python -B preflight.py --repo <产品仓库> --output <本会话产物目录>
  python -B preflight.py --repo <产品仓库> --network --limit 3     # 查 6 平台登录态

输出：JSON（verdict=ready/degraded/blocked + 每项结果 + 建议动作）。退出码 0=ready 1=degraded 2=blocked。
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows 控制台默认 gbk：统一把 stdout/stderr 切到 UTF-8，否则打印中文/符号会崩。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

NODE = r"C:\Program Files\nodejs\node.exe"
CLI = r"C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\dist\src\main.js"
PLATFORMS = [("weibo", "hot"), ("zhihu", "hot"), ("toutiao", "hot"),
             ("tieba", "hot"), ("bilibili", "hot"), ("xiaohongshu", "feed")]


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 127, "", f"{type(exc).__name__}: {exc}"


def check_repo(repo):
    out = {"id": "repo", "ok": False, "detail": {}}
    if not (repo / ".git").exists():
        out["detail"]["why"] = "不是 git 仓库"
        return out
    code, head, _ = run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
    code2, dirty, _ = run(["git", "-C", str(repo), "status", "--porcelain"])
    out["detail"] = {"head": head.strip(), "dirty": [l for l in dirty.splitlines() if l.strip()]}
    out["ok"] = code == 0 and not out["detail"]["dirty"]
    return out


def check_db(repo):
    out = {"id": "product_db", "ok": False, "detail": {}}
    db = repo / "sentiment.db"
    if not db.is_file():
        out["detail"]["why"] = "sentiment.db 不存在"
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        total = con.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
        feeds = {r["name"]: r["n"] for r in con.execute(
            "SELECT f.name, COUNT(*) n FROM records r JOIN feeds f ON f.id=r.feed_id GROUP BY f.name")}
        con.close()
        out["detail"] = {"records": total, "feeds": feeds, "db_bytes": db.stat().st_size}
        out["ok"] = total > 0
    except Exception as exc:  # noqa: BLE001
        out["detail"]["why"] = f"{type(exc).__name__}: {exc}"
    return out


def check_throttle(repo):
    """冷却账：告诉开工时哪些平台还在冷却。"""
    out = {"id": "throttle", "ok": True, "detail": {"platforms": {}, "note": ""}}
    path = repo / ".collector-throttle.json"
    if not path.is_file():
        out["detail"]["note"] = "无冷却账（等价于全部平台可采）"
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        for plat, entry in (data.get("platforms") or {}).items():
            raw = entry.get("last_attempt_at") if isinstance(entry, dict) else None
            if not raw:
                out["detail"]["platforms"][plat] = {"state": "坏账（实现应 fail-closed）"}
                continue
            try:
                last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                mins = round((now - last).total_seconds() / 60)
                out["detail"]["platforms"][plat] = {"last_attempt_min_ago": mins,
                                                   "cooldown": "冷却中" if mins < 30 else "已过冷却"}
            except ValueError:
                out["detail"]["platforms"][plat] = {"state": "时间戳不可解析"}
    except Exception as exc:  # noqa: BLE001
        out["detail"]["note"] = f"账不可读（fail-closed）：{type(exc).__name__}"
        out["ok"] = False
    return out


def check_bridge(output):
    """桥账本：按 store 汇总任务状态（只看本地文件，不调 CLI）。"""
    out = {"id": "bridge_stores", "ok": True, "detail": {}}
    for store in sorted(glob.glob(str(output / "_*" / "store"))):
        name = Path(store).parent.name
        counts = {}
        for sp in glob.glob(store + r"\tasks\*\state.json"):
            try:
                s = json.loads(Path(sp).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            counts[s.get("status", "?")] = counts.get(s.get("status", "?"), 0) + 1
        out["detail"][name] = counts
    return out


def check_doctor():
    out = {"id": "opencli_doctor", "ok": False, "detail": {}}
    code, stdout, stderr = run([NODE, CLI, "doctor"], timeout=120)
    text = (stdout + stderr)
    out["detail"] = {"exit": code, "excerpt": " ".join(text.split())[:400]}
    low = text.lower()
    # 注意坑："not connected" 里也含 "connected"，必须看否定标记而不是关键词命中。
    out["ok"] = (code == 0
                 and "[fail]" not in low
                 and "[missing]" not in low
                 and "not connected" not in low
                 and "[ok] extension: connected" in low)
    return out


def check_logins(limit):
    """逐个平台查登录态；每次之间间隔 25 秒，最多 6 次请求。"""
    out = {"id": "platform_logins", "ok": True, "detail": {}, "requests": 0}
    targets = PLATFORMS[:max(1, limit)]          # --limit 真正生效（此前形参未用，永远 6 次）
    out["detail"]["_limit"] = len(targets)
    for site, sub in targets:
        code, stdout, stderr = run([NODE, CLI, site, "whoami", "-f", "json"], timeout=90)
        out["requests"] += 1
        text = (stdout + stderr)
        status = "ok" if code == 0 else "需要登录/不可用"
        out["detail"][site] = {"exit": code, "status": status, "sample": " ".join(text.split())[:160]}
        if code != 0:
            out["ok"] = False
        import time as _t
        if out["requests"] < len(targets):
            _t.sleep(25)
    return out


def check_credential(repo):
    """凭据有效性：真发一次 max_tokens=1 的请求（唯一 1 次外网请求）。

    教训：桥任务没有凭据时只能写 BLOCKED（S3-B），所以开工时先确认 key 能用，
    以免排到"实现完了才发现跑不了真实调用"。
    """
    import re
    import urllib.error
    import urllib.request

    out = {"id": "model_credential", "ok": False, "detail": {}, "requests": 0}
    creds_path = Path.home() / ".dsh" / ".credentials.yaml"
    key = None
    if creds_path.is_file():
        try:
            text = creds_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"OPENCODE_GO_V1_API_KEY\s*:\s*[\"']?([^\s\"']+)", text)
            key = m.group(1) if m else None
        except OSError:
            pass
    if not key:
        out["detail"]["why"] = "未找到 OPENCODE_GO_V1_API_KEY（模型任务无法做真实调用，只能靠桩/夹具）"
        return out

    payload = {"model": "deepseek-v4.1-flash", "max_tokens": 1, "reasoning_effort": "none",
               "messages": [{"role": "user", "content": "hi"}]}
    headers = {"content-type": "application/json", "authorization": "Bearer " + key,
               "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
               "accept": "application/json", "x-opencode-session": "preflight-check"}
    req = urllib.request.Request("https://opencode.ai/zen/go/v1/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers, method="POST")
    out["requests"] = 1
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        out["ok"] = bool(body.get("choices"))
        out["detail"] = {"http": resp.status, "model": body.get("model"),
                         "usage": body.get("usage")}
    except urllib.error.HTTPError as exc:
        excerpt = exc.read().decode("utf-8", "replace")[:200]
        out["detail"] = {"http": exc.code, "excerpt": excerpt}
    except Exception as exc:  # noqa: BLE001
        out["detail"] = {"why": f"{type(exc).__name__}: {str(exc)[:160]}"}

    # 顺带静态核对：适配器里三头是否齐（免得"凭据没问题但代码漏头"）
    adapter = repo / "analysis.py"
    if adapter.is_file():
        try:
            code = adapter.read_text(encoding="utf-8", errors="replace")
            out["detail"]["adapter_headers"] = {h: (h in code) for h in
                                                ("Authorization", "User-Agent", "x-opencode-session")}
        except OSError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--output", default=str(Path(__file__).resolve().parents[0]))
    ap.add_argument("--network", action="store_true", help="逐平台查登录态（会发最多 6 次请求，间隔 25s）")
    ap.add_argument("--skip-doctor", action="store_true")
    ap.add_argument("--no-credential", action="store_true", help="跳过凭据有效性检查（少发 1 次请求）")
    ap.add_argument("--limit", type=int, default=3, help="--network 时最多查几个平台（默认 3）")
    ap.add_argument("--save", default=None, help="把完整 JSON 报告写到该路径（留证据用）")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    checks = [check_repo(repo), check_db(repo), check_throttle(repo), check_bridge(output)]
    if not args.skip_doctor:
        checks.append(check_doctor())
    if not args.no_credential:
        checks.append(check_credential(repo))
    if args.network:
        checks.append(check_logins(args.limit))

    blocked = [c for c in checks if not c["ok"] and c["id"] in ("repo", "product_db")]
    degraded = [c for c in checks if not c["ok"]]
    verdict = "blocked" if blocked else ("degraded" if degraded else "ready")
    advice = {
        "blocked": "先处理 block 项：仓库不干净 / 产品库不可用。",
        "degraded": "可以开工，但下列项有风险：" + "、".join(c["id"] for c in degraded),
        "ready": "环境就绪，可直接开工。",
    }[verdict]
    report = {"at": datetime.now(timezone.utc).isoformat(), "repo": str(repo),
              "verdict": verdict, "advice": advice,
              "checks": checks,
              "network_requests": sum(int(c.get("requests") or 0) for c in checks)}
    if args.save:
        Path(args.save).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return {"ready": 0, "degraded": 1, "blocked": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
