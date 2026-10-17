#!/usr/bin/env python3
"""桥任务自检骨架（交付前由执行方自己跑；0 网络请求、只读、标准库）。

设计目标：把本会话里真实踩过的失败模式在**交付前**拦掉——
  1. 改了允许路径之外的文件；
  2. 产物缺失；
  3. 硬编码密钥/cookie；
  4. Python 语法错（AST 解析失败）；
  5. 类型专属：端点白名单/写动作拒绝、模型 id 带 provider 前缀、只读打开、枚举降级缺失。

用法（在 worktree 根目录）：
  python -B <本文件> --kind endpoint --allow tools/,docs/ --artifacts tools/collect_opencli.py,docs/S1-collector.md
  python -B <本文件> --kind adapter  --allow analysis.py --artifacts analysis.py
  python -B <本文件> --kind annotator --allow tools/,docs/ --artifacts tools/annotate_relevance.py
  python -B <本文件> --kind docs     --allow docs/ --artifacts docs/xxx.md

输出：一段 JSON（verdict=pass/fail、每条检查的 ok/detail、violations 列表）。退出码 pass=0 / fail=1。
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "疑似 API key（sk- 前缀）"),
    (r"Bearer\s+[A-Za-z0-9._-]{20,}", "疑似写死的 Bearer token"),
    (r"(?:api[_-]?key|cookie|token)\s*[:=]\s*[\"'][^\"']{12,}[\"']", "疑似写死的密钥/凭据字面量"),
]


def run(cmd, cwd=None):
    """子进程一律按 UTF-8 解码并容错：Windows 默认 gbk 会把中文输出解码成异常。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return 127, "", f"{type(exc).__name__}: {exc}"


def changed_paths(worktree):
    code, out, err = run(["git", "status", "--porcelain"], cwd=str(worktree))
    if code != 0:
        return None, f"git status 失败（{code}）：{err.strip()[:160]}"
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:                     # 重命名
            path = path.split(" -> ")[-1]
        paths.append(path.strip('"'))
    return paths, None


def check_changed(worktree, allow, paths):
    violations = []
    for path in paths or []:
        if not any(path == a.rstrip("/") or path.startswith(a.rstrip("/") + "/") for a in allow):
            violations.append(path)
    return {"id": "changed_paths", "ok": not violations,
            "detail": {"changed": len(paths or []), "outside_allow": violations}}


def check_artifacts(worktree, artifacts):
    missing = [a for a in artifacts if not (worktree / a).is_file()]
    return {"id": "artifacts", "ok": not missing, "detail": {"missing": missing}}


def check_secrets(worktree, paths):
    hits = []
    for path in paths or []:
        f = worktree / path
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            for m in re.finditer(pattern, text):
                hits.append({"file": path, "why": label, "sample": m.group(0)[:12] + "…"})
    return {"id": "no_secrets", "ok": not hits, "detail": {"hits": hits[:10]}}


def check_python(worktree, paths):
    bad = []
    for path in paths or []:
        if not path.endswith(".py"):
            continue
        f = worktree / path
        if not f.is_file():
            continue
        try:
            ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            bad.append({"file": path, "error": f"{exc.msg} @{exc.lineno}"})
    return {"id": "python_ast", "ok": not bad, "detail": {"bad": bad}}


def read(worktree, path):
    f = worktree / path
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""


def check_endpoint(worktree, paths):
    """端点类：白名单拒绝 + 非法参数拒绝 + dry-run 不建库（全部离线，0 上游请求）。"""
    scripts = [p for p in (paths or []) if p.endswith(".py") and p.startswith("tools/")]
    if not scripts:
        return {"id": "endpoint_contract", "ok": False, "detail": {"why": "未找到 tools/ 下的脚本"}}
    script = worktree / scripts[0]
    results = []
    before = (worktree / "sentiment.db").exists()
    for argv, label in ((["--endpoint", "definitely-not-an-endpoint", "--dry-run", "--limit", "1"], "未知端点必须被拒"),
                        (["--endpoint", "weibo-publish", "--dry-run", "--limit", "1"], "疑似写动作必须被拒"),
                        (["--endpoint", "hot", "--dry-run", "--limit", "0"], "limit 0 必须报错")):
        code, out, err = run([sys.executable, "-B", str(script)] + argv, cwd=str(worktree))
        combined = (out or "") + (err or "")          # argparse 的 error 行通常在末尾，不能只看开头
        low = combined.lower()
        has_error = ("error" in low) or any(k in combined for k in ("未知", "不支持", "拒绝", "白名单"))
        crashed = "traceback (most recent call last)" in low
        blocked = code != 0 and has_error and not crashed   # 被拒（argparse 或错误信封）且不是崩溃
        line = next((ln.strip() for ln in combined.splitlines()
                     if "error" in ln.lower() or "拒绝" in ln or "不支持" in ln), combined.strip()[:120])
        results.append({"case": label, "exit": code, "ok": blocked, "sample": line[:160]})
    after = (worktree / "sentiment.db").exists()
    results.append({"case": "dry-run 不建库", "db_before": before, "db_after": after, "ok": before == after})
    return {"id": "endpoint_contract", "ok": all(r["ok"] for r in results), "detail": results}


def check_adapter(worktree, paths):
    """适配器类：裸模型 id、reasoning_effort、三个必备头、错误保留、引用对齐（静态）。"""
    text = "".join(read(worktree, p) for p in (paths or []) if p.endswith(".py"))
    checks = [
        ("请求体模型 id 不带 provider 前缀",
         not [m for m in (re.findall(r'"model"\s*:\s*"([^"]+)"', text)
                          + re.findall(r'(?:PRODUCTION_MODEL|MODEL)\s*=\s*"([^"]+)"', text)) if "/" in m]),
        ("带 reasoning_effort", "reasoning_effort" in text),
        ("带 Authorization / User-Agent / x-opencode-session",
         all(k in text for k in ("Authorization", "User-Agent", "x-opencode-session"))),
        ("HTTP 失败保留状态码与响应体摘要",
         bool(re.search(r"(status|code)\s*[:=].*?(body|excerpt|prefix)", text, re.S))),
        ("引用偏移对齐（text.find 或等价）", ("find(" in text) or ("index(" in text)),
        ("未把密钥写进仓库", not any(re.search(p, text) for p, _ in SECRET_PATTERNS)),
    ]
    detail = [{"check": name, "ok": ok} for name, ok in checks]
    return {"id": "adapter_contract", "ok": all(ok for _, ok in checks), "detail": detail}


def check_annotator(worktree, paths):
    """标注器类：只读打开 + 不写 records + 枚举降级 + dry-run 无网络（静态）。"""
    text = "".join(read(worktree, p) for p in (paths or []) if p.endswith(".py"))
    checks = [
        ("只读打开数据库（mode=ro / immutable）", ("mode=ro" in text) or ("immutable=1" in text)),
        ("没有对 records 的写操作",
         not re.search(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+records", text, re.I)),
        ("有枚举降级（conservative/fallback 之类）",
         bool(re.search(r"(conservative|fallback|降级)", text, re.I))),
        ("有 dry-run 且不发请求", ("dry_run" in text or "dry-run" in text)),
        ("未把密钥写进仓库", not any(re.search(p, text) for p, _ in SECRET_PATTERNS)),
    ]
    detail = [{"check": name, "ok": ok} for name, ok in checks]
    return {"id": "annotator_contract", "ok": all(ok for _, ok in checks), "detail": detail}


def check_docs(worktree, paths):
    ok = all(p.startswith("docs/") for p in (paths or []))
    return {"id": "docs_only", "ok": ok, "detail": {"changed": paths}}


KINDS = {"endpoint": check_endpoint, "adapter": check_adapter, "annotator": check_annotator, "docs": check_docs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=sorted(KINDS))
    ap.add_argument("--worktree", default=".")
    ap.add_argument("--allow", required=True, help="允许改动的路径前缀，逗号分隔")
    ap.add_argument("--artifacts", default="", help="必须存在的产物，逗号分隔")
    args = ap.parse_args()

    worktree = Path(args.worktree).resolve()
    allow = [a.strip() for a in args.allow.split(",") if a.strip()]
    artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()]

    paths, err = changed_paths(worktree)
    checks = []
    if err:
        checks.append({"id": "changed_paths", "ok": False, "detail": err})
    else:
        checks.append(check_changed(worktree, allow, paths))
    checks.append(check_artifacts(worktree, artifacts))
    checks.append(check_secrets(worktree, paths))
    checks.append(check_python(worktree, paths))
    checks.append(KINDS[args.kind](worktree, paths))

    verdict = "pass" if all(c["ok"] for c in checks) else "fail"
    report = {"kind": args.kind, "worktree": str(worktree), "allow": allow, "artifacts": artifacts,
              "changed": paths, "checks": checks, "verdict": verdict,
              "note": "离线自检：0 网络请求、只读、不改任何文件；它不能替代真实运行验收。"}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
