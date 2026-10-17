#!/usr/bin/env python
"""S3-B 相关性/主题/性别相关标注器：只读 sentiment.db，分批调用生产适配器声明的模型端点。

用途：把「性别对立监测」的范围从全量采集记录收窄到真正相关的条目，为后续事件分析提供
带证据类型的候选集合。本工具**只读数据库**：只 SELECT records/feeds，不写 records、不改表
结构，不发起任何平台写动作。

复用清单（先复用，后新写）：

1. `analysis.py`（已并入并独立审查过的生产适配器）——本文件**不重写其 HTTP 逻辑**，全部
   细节直接读取 analysis 的常量，逐个对应：
     - 端点 `analysis.PRODUCTION_ENDPOINT`
     - **裸模型 id** `analysis.PRODUCTION_MODEL`（允许 `OPENCODE_GO_V1_MODEL` 覆盖，从不加前缀）
     - **`reasoning_effort="none"`**（实测：不带时模型把预算烧在思考上、返回非 JSON）
     - **三个必备头**：`Authorization`（凭据只来自环境变量 `analysis.PRODUCTION_KEY_ENV`）、
       `analysis.PRODUCTION_USER_AGENT`（Cloudflare 对非浏览器 UA 回 403 1010）、
       `x-opencode-session`（缺失回 400 MissingSessionID）
     - 超时 `analysis.PRODUCTION_TIMEOUT`、响应上限 `analysis.PRODUCTION_MAX_RESPONSE`
     - **错误保留** `analysis.ProductionHTTPError`（真实 HTTP 状态码 + 响应体前
       `analysis.PRODUCTION_ERROR_BODY` = 200 字），单次请求、不重试
   唯一无法直接调用的部分是 `analysis.production_adapter()`：它内部的 `production_messages()`
   把系统提示词硬绑在 A/B 证据角色上（A 输出 claims、B 输出 reviews），而本次要的是
   items 标注提示词；`analysis.py` 不在本任务允许改动的路径内，无法把提示词参数化。因此这里
   只重写「消息内容」，发送与解析逐行对齐 `analysis.production_adapter()` 的既有行为。
2. `storage.py`——沿用其证据库约定：`now()`（UTC）、`digest()`、`json_bytes()`（jsonl 行按项目既有规范序列化）。
   读取用只读连接（见 `open_readonly()`）；刻意不用 `storage.connect()`，因为后者会
   `CREATE TABLE IF NOT EXISTS` 并切 WAL 日志模式，属于写动作。
3. `tools/collect_opencli.py`——沿其做法：`--limit` / `--dry-run`、失败保留真实原文不静默降级、
   统一 JSON 报告与退出码。`_utf8_stdout()` 是同一做法的本地实现（Windows 控制台默认 GBK）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analysis  # noqa: E402  复用已审查的生产适配器常量与错误类型
import storage   # noqa: E402  复用项目证据库既有约定

TOOL = "s3b-relevance"
DEFAULT_DB = ROOT / "sentiment.db"
OUT_DIR = ROOT / "annotations"

DEFAULT_LIMIT = 25
MAX_LIMIT = 200          # 本工具自设上限，避免单次运行产生不可控的模型花费
BATCH_MAX = 25           # 契约硬约束：单批不超过 25 条
DEFAULT_BATCH = 25
DEFAULT_MIN_INTERVAL = 8.0

SUMMARY_MAX = 30         # 契约：summary ≤30 字
MAX_INPUT_TITLE = 200    # 仅用于限制送入模型的输入长度，不改数据库
MAX_INPUT_SUMMARY = 300

TOPICS = ("性别议题", "婚恋家庭", "职场", "公共安全", "教育", "医疗",
          "消费维权", "政策法规", "娱乐八卦", "其他")
STANCES = ("反对", "支持", "中立", "无关")
CONSERVATIVE_TOPIC = "其他"
CONSERVATIVE_RELEVANCE = 0
CONSERVATIVE_STANCE = "无关"

NOTES = [
    "只读数据库：只 SELECT records/feeds，不写 records、不改表结构、不建新表。",
    "单批最多 25 条；批次之间按 --min-interval 节流；每次调用最多一个真实 HTTP 请求，不重试。",
    "枚举越界（topic/relevance/stance）与类型越界一律降级为保守值，不抛错、不原样输出。",
    "结构性问题（items 缺失、id 覆盖不全、id 重复或不属于本批）会明确失败，不静默降级。",
    "模型给出的 relevance 是相关性判断，不是热度或评论数；不写回数据库。",
    "送入模型的 title/summary 超长时截断并加 “…” 标记，只影响输入，不改数据库原文。",
]


class AnnotationError(RuntimeError):
    """带阶段、真实错误原文与退出码的失败；用于保留 HTTP 状态码与响应体前 200 字。"""

    def __init__(self, stage, message, exit_code, extra=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.exit_code = exit_code
        self.extra = extra or {}


def _utf8_stdout():
    """Windows 控制台默认 GBK，中文/emoji 会让 print 崩掉；与 tools/collect_opencli.py 同一做法。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def utc_stamp(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clip(text, limit):
    """为控制送模型的 token，超长文本截断并加省略号；只影响输入，不改数据库原文。"""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


# --- 枚举/类型归一化（来源：analysis.normalize_output 的“先归一化再严格校验”思路） -------------
#
# analysis.normalize_output() 是同类归一化：它在 validate() 之前对齐引用偏移，但绝不放宽
# “引用必须是原文逐字子串”这一证据性要求。analysis.py 里没有枚举级归一化函数，故按同一思路
# 在本文件实现等价函数：越界枚举一律降级为保守值，且**不参与**结构完整性判断（结构问题仍硬失败）。

def normalize_relevance(value):
    """relevance ∈ {0,1,2}；其余（含 3、-1、1.5、True、"高"、缺失）一律降级为保守值 0。"""
    if type(value) is int and value in (0, 1, 2):   # type is int 排除 bool
        return value
    if isinstance(value, str) and value.strip() in ("0", "1", "2"):
        return int(value.strip())
    return CONSERVATIVE_RELEVANCE


def normalize_topic(value):
    """topic 枚举固定 10 项；越界/未知一律降级为「其他」。"""
    return value if value in TOPICS else CONSERVATIVE_TOPIC


def normalize_stance(value):
    """stance ∈ {反对,支持,中立,无关}；越界/未知一律降级为「无关」。"""
    return value if value in STANCES else CONSERVATIVE_STANCE


def normalize_gender(value):
    """gender_related 只接受布尔真值；字符串 "true"/"yes"/"1" 视为 True，其余一律 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"):
        return True
    return False


def normalize_summary(value):
    """summary 必须为字符串且 ≤30 字；非字符串视为空串，超长截断到 30 字。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:SUMMARY_MAX]


# --- 提示词与消息构造（唯一必须新写的部分：analysis.production_messages 绑定 A/B 角色） ---------

def annotation_rules():
    topics = "|".join(TOPICS)
    stances = "|".join(STANCES)
    return (
        "你是性别议题舆情标注器。根据给定的记录（id/feed/title/summary）逐条判断它与"
        "「性别对立监测」的相关程度，并给出主题与立场。只依据文本明确表达的内容判断，"
        "不推断作者性别，不接受、也不输出任何统计数字。"
        "只输出一个 JSON 对象，且仅含一个键 items，值为与输入一一对应的数组；"
        "每条严格为 id,relevance,topic,stance,gender_related,summary 六个字段。"
        "id 必须原样回填输入的 id，不得新增、遗漏或重复。"
        f"relevance 取 0/1/2：2=直接讨论性别议题，1=间接相关，0=无关。"
        f"topic 取 {topics}。"
        f"stance 取 {stances}（无关时用 无关）。"
        "gender_related 为布尔值，表示是否与性别议题相关。"
        f"summary 为不超过 {SUMMARY_MAX} 字的中文短语。"
    )


def annotation_messages(records):
    payload = {"items": [{"id": r["id"], "feed": r["feed"],
                          "title": clip(r["title"], MAX_INPUT_TITLE),
                          "summary": clip(r["summary"], MAX_INPUT_SUMMARY)} for r in records]}
    return [{"role": "system", "content": annotation_rules()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


# --- HTTP：单次请求，细节全部取自 analysis.py -------------------------------------------------

def post_once(messages):
    """最多一次真实 POST；成功返回 (output, usage)，失败抛出带真实错误原文的异常。

    与 analysis.production_adapter() 的发送/解析逐行对齐；不同之处只有 messages 内容。
    """
    credential = os.environ.get(analysis.PRODUCTION_KEY_ENV)
    if not credential:
        raise AnnotationError(
            "env", f"缺少环境变量 {analysis.PRODUCTION_KEY_ENV}；未发送任何请求", 2,
            {"credential_env": analysis.PRODUCTION_KEY_ENV})
    body = json.dumps({
        "model": os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL,
        "reasoning_effort": "none",
        "stream": False,
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(analysis.PRODUCTION_ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {credential}",
        "User-Agent": analysis.PRODUCTION_USER_AGENT,
        "x-opencode-session": analysis.PRODUCTION_SESSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    # 不加重定向处理器：3xx 不得把凭据转发到别处。urllib 自身不重试，最多一次连接尝试。
    try:
        with urllib.request.build_opener().open(request, timeout=analysis.PRODUCTION_TIMEOUT) as response:
            status = response.status
            raw = response.read(analysis.PRODUCTION_MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        raise analysis.ProductionHTTPError(exc.code, exc.read(analysis.PRODUCTION_MAX_RESPONSE).decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise AnnotationError("network", f"连接失败：{exc.reason}", 3)
    if status != 200:
        raise analysis.ProductionHTTPError(status, raw.decode("utf-8", "replace"))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationError("parse", f"响应不是合法 JSON 对象：{exc}", 4,
                              {"body_prefix": raw.decode("utf-8", "replace")[:analysis.PRODUCTION_ERROR_BODY]}) from exc
    if not isinstance(payload, dict):
        raise AnnotationError("parse", "响应必须是单个 JSON 对象", 4)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AnnotationError("parse", "响应缺少 choices", 4)
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AnnotationError("parse", "响应缺少文本内容", 4)
    try:
        output = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnnotationError("parse", f"模型输出不是合法 JSON：{exc}", 4,
                              {"content_head": content[:analysis.PRODUCTION_ERROR_BODY]}) from exc
    if not isinstance(output, dict):
        raise AnnotationError("parse", "模型输出必须是单个 JSON 对象", 4)
    return output, payload.get("usage")


def normalize_items(sent, output):
    """把一批模型输出映射为最终标注条目。

    结构问题硬失败（不静默降级）；枚举/类型越界降级为保守值（不抛错、不原样输出）。
    """
    if not isinstance(output, dict) or not isinstance(output.get("items"), list):
        raise AnnotationError("parse", "模型输出必须为仅含 items 数组的 JSON 对象", 4,
                              {"output_head": json.dumps(output, ensure_ascii=False)[:300]})
    sent_ids = [r["id"] for r in sent]
    sent_set = set(sent_ids)
    index = {}
    for raw in output["items"]:
        ident = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(ident, str) or ident not in sent_set:
            raise AnnotationError("parse", "items 含本批之外的 id", 4,
                                  {"id": ident if isinstance(ident, str) else repr(ident)})
        if ident in index:
            raise AnnotationError("parse", f"items 中 id 重复：{ident}", 4, {"id": ident})
        index[ident] = raw
    missing = [i for i in sent_ids if i not in index]
    if missing:
        raise AnnotationError("parse", f"items 未覆盖本批全部记录，缺 {len(missing)} 条", 4,
                              {"missing_ids": missing[:25]})
    entries = []
    for record in sent:
        raw = index[record["id"]]
        entries.append({
            "record_id": record["id"],
            "feed": record["feed"],
            "title": record["title"],
            "relevance": normalize_relevance(raw.get("relevance")),
            "topic": normalize_topic(raw.get("topic")),
            "stance": normalize_stance(raw.get("stance")),
            "gender_related": normalize_gender(raw.get("gender_related")),
            "summary": normalize_summary(raw.get("summary")),
        })
    return entries


# --- 只读数据读取 -----------------------------------------------------------------------------

def open_readonly(path):
    """只读打开既有证据库，返回 (连接, 访问模式)；不建表、不切日志模式、任何写都会失败。

    实测（2026-10-16，SQLite 编译于 Python 3.13.14）：库处于 WAL 模式时，`mode=ro` 仍会在库
    目录旁创建 `-shm` 与 0 字节 `-wal`（WAL 记账），虽然**库文件字节不变**（SHA-256 前后一致），
    但会在目录里多出文件。故：
      - 不存在非空 `-wal` 时用 `mode=ro&immutable=1`：不产生任何旁文件、不加锁；
      - 存在非空 `-wal`（有尚未检查点的已提交数据）时退回 `mode=ro`：immutable 会假定文件不变
        而不读该 WAL，可能读到旧数据，正确性优先。
    契约建议指向一份只读复制件（本任务即如此），因此两种模式都不会改动库内容。
    """
    if not path.is_file():
        raise AnnotationError("db", f"证据库不存在：{path}", 2, {"db": str(path)})
    wal = path.with_name(path.name + "-wal")
    immutable = not (wal.is_file() and wal.stat().st_size > 0)
    mode = "mode=ro&immutable=1" if immutable else "mode=ro"
    try:
        db = sqlite3.connect(path.as_uri() + "?" + mode, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise AnnotationError("db", f"只读打开证据库失败：{exc}", 2, {"db": str(path)}) from exc
    db.row_factory = sqlite3.Row
    return db, mode


def load_records(db, feed, limit):
    """默认按 fetched_at DESC 取最近 N 条；--feed 按来源名精确过滤。同刻用 id 兜底排序保证确定性。"""
    sql = ["SELECT r.id, r.title, r.summary, r.fetched_at, f.name AS feed",
           "FROM records r JOIN feeds f ON f.id = r.feed_id"]
    params = []
    if feed:
        sql.append("WHERE f.name = ?")
        params.append(feed)
    sql.append("ORDER BY r.fetched_at DESC, r.id DESC LIMIT ?")
    params.append(limit)
    try:
        rows = db.execute(" ".join(sql), params).fetchall()
    except sqlite3.Error as exc:
        raise AnnotationError("db", f"读取 records 失败：{exc}", 2, {"db_sql": " ".join(sql)}) from exc
    return [{"id": r["id"], "feed": r["feed"], "title": r["title"],
             "summary": r["summary"] or "", "fetched_at": r["fetched_at"]} for r in rows]


def chunk(records, size):
    return [records[i:i + size] for i in range(0, len(records), size)]


def usage_totals(usages):
    """服务端 usage 原样保留（可能是 null）；只在字段存在且为整数时累加，否则该字段为 null。"""
    if all(u is None for u in usages):
        return None
    totals = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [u.get(key) for u in usages if isinstance(u, dict) and type(u.get(key)) is int]
        totals[key] = sum(values) if values else None
    return totals


def build_summary(entries, batches, batches_meta, usages, args, stamp, files):
    topics = {name: 0 for name in TOPICS}
    stances = {name: 0 for name in STANCES}
    relevance = {"0": 0, "1": 0, "2": 0}
    per_feed = {}
    for entry in entries:
        relevance[str(entry["relevance"])] += 1
        topics[entry["topic"]] += 1
        stances[entry["stance"]] += 1
        slot = per_feed.setdefault(entry["feed"], {"count": 0, "gender_related": 0,
                                                   "topic": {name: 0 for name in TOPICS}})
        slot["count"] += 1
        slot["topic"][entry["topic"]] += 1
        if entry["gender_related"]:
            slot["gender_related"] += 1
    total = len(entries)
    related = sum(1 for e in entries if e["gender_related"])
    return {
        "tool": TOOL,
        "generated_at": storage.now(),
        "timestamp": stamp,
        "model": os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL,
        "endpoint": analysis.PRODUCTION_ENDPOINT,
        "reasoning_effort": "none",
        "db": {"path": str(args.db), "read_only": True},
        "feed_filter": args.feed,
        "limit": args.limit,
        "batch_size": args.batch,
        "min_interval_seconds": args.min_interval,
        "record_count": total,
        "batch_count": len(batches_meta),
        "relevance_distribution": relevance,
        "topic_distribution": topics,
        "stance_distribution": stances,
        "gender_related": {
            "count": related,
            "total": total,
            "ratio": (related / total) if total else None,
        },
        "per_feed": per_feed,
        "tokens": {
            "batches": [{"index": i + 1, "count": len(b), "usage": u}
                        for i, (b, u) in enumerate(zip(batches, usages))],
            "totals": usage_totals(usages),
        },
        "outputs": files,
        "notes": NOTES,
    }


def write_outputs(entries, summary):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = summary["timestamp"]
    jsonl = OUT_DIR / f"relevance-{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as stream:
        for entry in entries:
            stream.write(storage.json_bytes(entry).decode("utf-8") + "\n")
    files = {"jsonl": str(jsonl), "jsonl_sha256": storage.digest(jsonl.read_bytes()),
             "jsonl_bytes": jsonl.stat().st_size}
    summary_path = OUT_DIR / f"summary-{stamp}.json"
    summary["outputs"] = files
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    files["summary"] = str(summary_path)
    files["summary_sha256"] = storage.digest(summary_path.read_bytes())
    # 回读校验：产物字节必须与报告里的哈希一致，避免把“写过了”当成“写对了”。
    if storage.digest(jsonl.read_bytes()) != files["jsonl_sha256"]:
        raise AnnotationError("write", "jsonl 回读哈希不一致", 7, {"path": str(jsonl)})
    return files


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"取最近 N 条 records（1-{MAX_LIMIT}，默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--feed", default=None, help="按来源名精确过滤（feeds.name，如 toutiao-hot）")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help=f"单批条数，不得超过 {BATCH_MAX}（默认 {DEFAULT_BATCH}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要发送的批次与条数，不发起任何网络请求、不写输出文件")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help=f"批次之间的最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="既有证据库路径，默认 <仓库>/sentiment.db")
    return parser


def emit_error(exc, context=None):
    payload = {"tool": TOOL,
               "status": "blocked" if exc.exit_code == 2 else "failed",
               "error": {"stage": exc.stage, "exit_code": exc.exit_code,
                         "type": type(exc).__name__, "message": exc.message, **exc.extra}}
    if context:
        payload.update(context)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exc.exit_code


def main(argv=None):
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    context = {"db": str(args.db), "feed": args.feed, "limit": args.limit,
               "batch": args.batch, "dry_run": args.dry_run}
    db_handle = None
    try:
        if not 1 <= args.limit <= MAX_LIMIT:
            raise AnnotationError("args", f"--limit 必须在 1-{MAX_LIMIT} 之间，收到 {args.limit}", 2)
        if not 1 <= args.batch <= BATCH_MAX:
            raise AnnotationError("args", f"--batch 必须在 1-{BATCH_MAX} 之间，收到 {args.batch}", 2)
        if args.min_interval < 0:
            raise AnnotationError("args", f"--min-interval 不得为负，收到 {args.min_interval}", 2)
        db_handle, access_mode = open_readonly(args.db)
        records = load_records(db_handle, args.feed, args.limit)
        batches = chunk(records, args.batch)
        model = os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL
        report = {
            "tool": TOOL, "status": "ok",
            "mode": "dry-run" if args.dry_run else "live",
            "network_requests": 0 if args.dry_run else len(batches),
            "db": {"path": str(args.db), "present": True, "access": access_mode, "touched": False},
            "feed": args.feed, "limit": args.limit, "batch_size": args.batch,
            "min_interval_seconds": args.min_interval,
            "model": model, "endpoint": analysis.PRODUCTION_ENDPOINT, "reasoning_effort": "none",
            "record_count": len(records), "batch_count": len(batches),
            "batches": [{"index": i + 1, "count": len(b),
                         "record_ids": [r["id"] for r in b],
                         "titles": [r["title"] for r in b]} for i, b in enumerate(batches)],
            "out_dir": str(OUT_DIR),
            "notes": NOTES,
        }
        if args.dry_run:
            report["would_write"] = [f"annotations/relevance-<UTC时间戳>.jsonl",
                                     f"annotations/summary-<UTC时间戳>.json"]
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        entries, usages, batches_meta = [], [], []
        for index, batch in enumerate(batches):
            if index and args.min_interval:
                time.sleep(args.min_interval)
            started = storage.now()
            output, usage = post_once(annotation_messages(batch))
            entries.extend(normalize_items(batch, output))
            usages.append(usage)
            batches_meta.append({"index": index + 1, "count": len(batch),
                                 "started_at": started, "finished_at": storage.now(),
                                 "usage": usage})
            report["batches"][index]["usage"] = usage
        stamp = utc_stamp()
        summary = build_summary(entries, batches, batches_meta, usages, args, stamp, {})
        files = write_outputs(entries, summary)
        report["status"] = "ok"
        report["outputs"] = files
        report["gender_related"] = summary["gender_related"]
        report["relevance_distribution"] = summary["relevance_distribution"]
        report["topic_distribution"] = summary["topic_distribution"]
        report["stance_distribution"] = summary["stance_distribution"]
        report["tokens"] = summary["tokens"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except AnnotationError as exc:
        return emit_error(exc, context)
    except analysis.ProductionHTTPError as exc:
        # 保留真实 HTTP 状态码与响应体前 200 字；不自动重试、不静默降级。
        return emit_error(AnnotationError("http", str(exc), 3,
                                         {"status": exc.status, "body_prefix": exc.body_prefix}), context)
    finally:
        if db_handle is not None:
            db_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
