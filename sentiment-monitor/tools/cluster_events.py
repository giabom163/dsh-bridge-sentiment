#!/usr/bin/env python
"""S4 事件聚类：把已入库的热榜标题聚成「事件」，供后续立场标注与日报使用。

只读硬约束：**只读数据库**（只 SELECT `records`/`feeds`），不写 records、不改表结构、
不建新表；不碰任何平台，不发任何平台写动作。

复用清单（先复用，后新写；详见 docs/S4-clustering.md 第一节）：

1. `tools/annotate_relevance.py`（本任务的 anchor）——按 importlib 载入其模块，直接复用：
     - `open_readonly()`：只读 URI 打开（无未检查点数据时 `mode=ro&immutable=1`，存在
       非空 `-wal` 时退回 `mode=ro`）；**本文件 `mode=ro` / `immutable=1` 的取值来自它，
       不另写一套只读打开**；
     - `AnnotationError`：带 stage / exit_code / extra 的失败类型；
     - `utc_stamp()`、`clip()`、`_utf8_stdout()`（Windows 控制台默认 GBK）。
2. `storage.py`——沿用证据库约定：`now()`（UTC）、`digest()`（SHA-256）、
   `json_bytes()`（jsonl 行按项目既有规范序列化：键排序、紧凑、`ensure_ascii=False`）。
   刻意不用 `storage.connect()`：它会 `CREATE TABLE IF NOT EXISTS` 并切 WAL 日志模式，属于写动作。
3. `analysis.py`——HTTP 细节全部取自其常量，不做任何复制：`PRODUCTION_ENDPOINT`、
   **裸模型 id** `PRODUCTION_MODEL`（`OPENCODE_GO_V1_MODEL` 可覆盖，从不加 provider 前缀）、
   `reasoning_effort="none"`（实测：不带时模型把预算烧在思考上、返回非 JSON）、
   三个必备头（`Authorization` 只来自环境变量 `PRODUCTION_KEY_ENV`、`PRODUCTION_USER_AGENT`、
   `x-opencode-session`）、`PRODUCTION_TIMEOUT`、`PRODUCTION_MAX_RESPONSE`、
   **错误保留** `analysis.ProductionHTTPError`（真实 HTTP 状态码 + 响应体前 `PRODUCTION_ERROR_BODY`＝200 字）。

唯一必须新写的部分及理由：

1. **聚类与相似度**：全仓没有标题相似度 / 连通分量的实现，本次新写（见第 3 节函数）。
2. **边界对判同提示词**：`analysis.production_messages(role, ...)` 把系统提示词硬绑在 A/B
   证据角色上（A 只输出 `claims`、B 只输出 `reviews`），`analysis.py` 不在本任务允许改动的
   路径内，无法参数化。故 `post_once()` 只重写「消息内容」，其**发送与解析逐行对齐
   `analysis.production_adapter()` 的既有行为**（请求体字段、请求头、`build_opener` 不加
   重定向、超时、响应上限、`choices[0].message.content` 解析、单 JSON 对象要求），并显式保留
   `OPENCODE_GO_V1_FIXTURE=1` 的夹具分支（有凭据用它；无凭据且显式夹具时不带 `Authorization`），
   使主线程可以离线夹具回放整条链路。
3. 参数解析、连通分量汇总、产物落盘与哈希回读。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analysis  # noqa: E402  复用已审查的生产适配器常量与错误类型
import storage   # noqa: E402  复用项目证据库既有约定


def _load_sibling(name):
    """按路径载入同目录工具模块（tools/ 不是包，无 __init__.py）。"""
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_s4_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法载入同目录工具：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_relevance = _load_sibling("annotate_relevance")   # 复用其只读打开与错误类型，不重写

TOOL = "s4-cluster"
DEFAULT_DB = ROOT / "sentiment.db"
OUT_DIR = ROOT / "events"

AnnotationError = _relevance.AnnotationError        # 复用失败类型（stage/exit_code/extra）
open_readonly = _relevance.open_readonly            # 复用只读打开（mode=ro / immutable=1）
usage_totals = _relevance.usage_totals              # 复用 usage 汇总（缺字段保留 null）

DEFAULT_LIMIT = 200
MAX_LIMIT = 200            # 自设上限，避免单次运行产生不可控规模
DEFAULT_THRESHOLD = 0.6    # 标题相似度阈值（规则阶段判同的硬门）
BOUNDARY_WIDTH = 0.15      # 边界带宽度：[threshold-0.15, threshold) 才交给模型
DEFAULT_MAX_MODEL_PAIRS = 20
MAX_MODEL_PAIRS = 50       # 契约硬上限
MODEL_BATCH_MAX = 10       # 契约硬约束：一次 ≤10 对
DEFAULT_MODEL_BATCH = 10
DEFAULT_MIN_INTERVAL = 8.0
MAX_INPUT_TITLE = 80       # 仅限制送入模型的标题长度，不改数据库原文

SIMILARITY_KINDS = ("dice", "jaccard")

# 归一化：去掉空白与标点（Python 3 中 \w 含 CJK，\W 即标点/空白），统一小写。
_PUNCT = re.compile(r"[\s\W_]+")
_CJK = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

NOTES = [
    "只读数据库：只 SELECT records/feeds，不写 records、不改表结构、不建新表。",
    "规则优先：canonical_url 完全相同、或标题相似度 >= --threshold 的对，直接判同事件，"
    "规则阶段零模型调用。",
    "模型只做边界裁量：仅对相似度落在 [threshold-0.15, threshold) 的边界对调用模型判同事件，"
    "其余一律按规则判为不同事件（保守）。",
    "成本纪律：模型调用批量进行（一次 ≤10 对），批次之间按 --min-interval 节流；"
    "总调用次数与 token 用量写进报告与 summary。",
    "失败保留真实 HTTP 状态码与响应体前 200 字；单次请求，不自动重试；"
    "该批边界对一律按「不同事件」保守处理。",
    "相似度是标题字面的字形/分词重合度，不是语义、立场或热度判断；不推断群体立场。",
    "本工具不写数据库、不写桥的状态文件、不启动服务、不订阅定时任务。",
]


# --- 标题归一化与相似度（本任务新写的唯一算法部分） ------------------------------------------

def normalize_title(title):
    """小写、去空白与标点；保留 CJK 与字母数字。空标题返回 ""。"""
    if not isinstance(title, str):
        return ""
    return _PUNCT.sub("", title.lower())


def char_bigrams(text):
    """字符级 Dice 的 n-gram 集合：长度 >=2 用相邻二元组，否则退化为单字符集。"""
    if len(text) < 2:
        return set(text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def title_tokens(text):
    """token Jaccard 的分词口径：字母数字连续串各算一个 token，每个 CJK 字算一个 token。"""
    return set(_CJK.findall(text))


def dice_similarity(a, b):
    """字符级 Dice：2*|A∩B| / (|A|+|B|)，A/B 为归一化标题的字符二元组集合。"""
    sa, sb = char_bigrams(a), char_bigrams(b)
    if not sa or not sb:
        return 0.0
    return 2.0 * len(sa & sb) / (len(sa) + len(sb))


def jaccard_similarity(a, b):
    """token Jaccard：|A∩B| / |A∪B|。"""
    sa, sb = title_tokens(a), title_tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


SIMILARITY_FUNCS = {"dice": dice_similarity, "jaccard": jaccard_similarity}


# --- 并查集（连通分量） ----------------------------------------------------------------------

class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, node):
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:      # 路径压缩
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if ra < rb:                            # 以较小下标为根，保证结果确定性
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb
        return True


# --- HTTP：单次请求，细节全部取自 analysis.py -------------------------------------------------

def post_once(messages):
    """最多一次真实 POST；成功返回 (output, usage)，失败抛出带真实错误原文的异常。

    与 `analysis.production_adapter()` 的发送/解析逐行对齐；不同之处只有 messages 内容。
    `OPENCODE_GO_V1_FIXTURE=1` 时允许不带凭据（离线夹具回放）；否则必须有凭据。
    """
    credential = os.environ.get(analysis.PRODUCTION_KEY_ENV)
    fixture_mode = os.environ.get("OPENCODE_GO_V1_FIXTURE") == "1"
    if not credential and not fixture_mode:
        raise AnnotationError(
            "env", f"缺少环境变量 {analysis.PRODUCTION_KEY_ENV}；未发送任何请求"
            "（离线回放请显式设置 OPENCODE_GO_V1_FIXTURE=1 并把端点指向本地夹具）", 2,
            {"credential_env": analysis.PRODUCTION_KEY_ENV})
    body = json.dumps({
        # 裸模型 id，调用时解析，OPENCODE_GO_V1_MODEL 可覆盖；从不在此加 provider 前缀。
        "model": os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL,
        "reasoning_effort": "none",
        "stream": False,
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": analysis.PRODUCTION_USER_AGENT,
        "x-opencode-session": "fixture-replay" if fixture_mode else analysis.PRODUCTION_SESSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(analysis.PRODUCTION_ENDPOINT, data=body, method="POST",
                                     headers=headers)
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


# --- 模型判同：提示词、消息、保守解析（新写「消息内容」） -------------------------------------

def judge_rules():
    return (
        "你是热榜事件归并裁判。判断给定的两个标题是否在讲同一件事（同一事件、同一话题来源）。"
        "只依据标题字面判断，不推断作者或群体立场，不接受也不输出任何统计数字。"
        "只输出一个 JSON 对象，且仅含一个键 pairs，值为与输入一一对应的数组；"
        "每条严格为 pair_id,same_event,reason 三个字段。"
        "pair_id 必须原样回填输入的 pair_id，不得新增、遗漏或重复。"
        "same_event 为布尔值，表示两个标题是否指同一事件；不确定时取 false。"
        "reason 为不超过 30 字的中文短语。"
    )


def judge_messages(pairs, records):
    payload = {"pairs": [{"pair_id": p["pair_id"],
                          "a": {"feed": records[p["i"]]["feed"],
                                "title": _relevance.clip(records[p["i"]]["title"], MAX_INPUT_TITLE)},
                          "b": {"feed": records[p["j"]]["feed"],
                                "title": _relevance.clip(records[p["j"]]["title"], MAX_INPUT_TITLE)}}
                         for p in pairs]}
    return [{"role": "system", "content": judge_rules()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def normalize_same(value):
    """same_event 只接受布尔真值；字符串 "true"/"1"/"yes"/"是"/"same" 视为 True，其余一律 False（保守）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "same", "是"):
        return True
    return False


def normalize_judgements(sent, output):
    """把一批模型输出映射为 {pair_id: bool}。

    结构损坏（缺 pairs 数组）返回 None，由调用方按「整批保守判不同」处理；
    单条越界/缺失/重复不加倍放大错误，缺失即判 False（不同事件），符合「不确定就归不同」。
    """
    if not isinstance(output, dict) or not isinstance(output.get("pairs"), list):
        return None
    sent_ids = {p["pair_id"] for p in sent}
    seen = {}
    for raw in output["pairs"]:
        pid = raw.get("pair_id") if isinstance(raw, dict) else None
        if not isinstance(pid, str) or pid not in sent_ids:
            continue                       # 越批 id 直接忽略，不放大成结构性失败
        if pid in seen:
            continue                       # 重复 id 取首次出现，保守
        seen[pid] = normalize_same(raw.get("same_event"))
    return {p["pair_id"]: seen.get(p["pair_id"], False) for p in sent}


# --- 只读数据读取（复用 _relevance.open_readonly） --------------------------------------------

def load_records(db, limit):
    """按 fetched_at DESC 取最近 N 条；同刻用 id DESC 兜底排序保证确定性。"""
    sql = ("SELECT r.id, r.title, r.url, r.canonical_url, r.fetched_at, f.name AS feed "
           "FROM records r JOIN feeds f ON f.id = r.feed_id "
           "ORDER BY r.fetched_at DESC, r.id DESC LIMIT ?")
    try:
        rows = db.execute(sql, (limit,)).fetchall()
    except Exception as exc:  # noqa: BLE001  sqlite3.Error 不能跨连接类比较，统一转成项目失败类型
        raise AnnotationError("db", f"读取 records 失败：{exc}", 2, {"db_sql": sql}) from exc
    return [{"id": r["id"], "title": r["title"] or "", "url": r["url"],
             "canonical_url": r["canonical_url"], "fetched_at": r["fetched_at"],
             "feed": r["feed"]} for r in rows]


# --- 聚类主流程 ------------------------------------------------------------------------------

def rule_pass(records, similarity, threshold):
    """规则阶段：canonical_url 相同 + 相似度 >= threshold 做连通分量；返回 (并查集, 统计, 边界对)。

    规则阶段**不调用模型**。边界对（相似度在 [threshold-0.15, threshold)）仅收集，待模型裁量。
    """
    lower = max(0.0, threshold - BOUNDARY_WIDTH)
    uf = UnionFind(len(records))
    stats = {"total_pairs": 0, "canonical_same": 0, "similarity_same": 0,
             "boundary": 0, "different": 0}
    boundary = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            stats["total_pairs"] += 1
            if records[i]["canonical_url"] == records[j]["canonical_url"]:
                uf.union(i, j)
                stats["canonical_same"] += 1
                continue
            score = similarity(records[i]["_norm"], records[j]["_norm"])
            if score >= threshold:
                uf.union(i, j)
                stats["similarity_same"] += 1
            elif score >= lower:
                stats["boundary"] += 1
                boundary.append({"i": i, "j": j, "similarity": round(score, 6)})
            else:
                stats["different"] += 1
    # 已被规则连通的边界对不再送模型（省预算）；只保留跨规则分量的对。
    candidates = [p for p in boundary if uf.find(p["i"]) != uf.find(p["j"])]
    candidates.sort(key=lambda p: (-p["similarity"], p["i"], p["j"]))   # 确定性
    return uf, stats, candidates


def select_pairs(candidates, max_pairs):
    selected = candidates[:max_pairs]
    for order, pair in enumerate(selected):
        pair["pair_id"] = f"p{order + 1:04d}"
    return selected, len(candidates) - len(selected)


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def model_pass(uf, records, selected, batch_size, min_interval, errors):
    """模型阶段：分批裁量边界对；HTTP/解析失败按「整批判不同」保守处理，保留真实原文，不重试。"""
    decisions = {}
    batches_meta = []
    usages = []
    for index, batch in enumerate(chunk(selected, batch_size)):
        if index and min_interval:
            time.sleep(min_interval)
        started = storage.now()
        usage = None                    # 该批未成功取到 usage 时保持 None（不冒充 0）
        try:
            output, usage = post_once(judge_messages(batch, records))
            judged = normalize_judgements(batch, output)
        except analysis.ProductionHTTPError as exc:
            judged = None
            errors.append({"batch": index + 1, "stage": "http", "status": exc.status,
                           "body_prefix": exc.body_prefix, "pair_ids": [p["pair_id"] for p in batch]})
        except AnnotationError as exc:
            judged = None
            errors.append({"batch": index + 1, "stage": exc.stage, "exit_code": exc.exit_code,
                           "message": exc.message, "extra": exc.extra,
                           "pair_ids": [p["pair_id"] for p in batch]})
        if judged is None:
            # 结构损坏也走「不确定→不同事件」的保守分支，并在 errors 中留痕；不静默。
            # 注意：HTTP 200 且模型输出结构损坏时 usage 仍有效（该次调用确实发生了），予以保留。
            if not errors or errors[-1]["batch"] != index + 1:
                errors.append({"batch": index + 1, "stage": "parse",
                               "message": "模型输出缺少 pairs 数组，整批按不同事件处理",
                               "pair_ids": [p["pair_id"] for p in batch]})
            judged = {p["pair_id"]: False for p in batch}
        same = 0
        for pair in batch:
            verdict = judged.get(pair["pair_id"], False)   # 缺项保守判 False
            decisions[pair["pair_id"]] = verdict
            if verdict and uf.union(pair["i"], pair["j"]):
                same += 1
        batches_meta.append({"index": index + 1, "count": len(batch), "same": same,
                             "started_at": started, "finished_at": storage.now(),
                             "pair_ids": [p["pair_id"] for p in batch], "usage": usage})
        usages.append(usage)
    return decisions, batches_meta, usages


def build_events(records, uf, rule_roots):
    """连通分量 → 事件；method = rule（只用规则）/ model（有模型边把两个规则分量并起来）。"""
    groups = {}
    for i in range(len(records)):
        groups.setdefault(uf.find(i), []).append(i)
    events = []
    for members in groups.values():
        members.sort(key=lambda i: (records[i]["fetched_at"], records[i]["id"]))
        ids = [records[i]["id"] for i in members]
        event = {
            "event_id": "evt-" + storage.digest(storage.json_bytes(sorted(ids)))[:16],
            "size": len(members),
            "feeds": sorted({records[i]["feed"] for i in members}),
            "titles": [records[i]["title"] for i in members],
            "urls": [records[i]["url"] for i in members],
            "first_seen": min(records[i]["fetched_at"] for i in members),
            "method": "model" if len({rule_roots[i] for i in members}) > 1 else "rule",
        }
        events.append(event)
    events.sort(key=lambda e: (-e["size"], e["event_id"]))
    return events


def build_summary(events, records, stats, model_meta, tokens, args, stamp, access_mode, files):
    feed_distribution = {}
    for record in records:
        slot = feed_distribution.setdefault(record["feed"], {"records": 0, "events": 0})
        slot["records"] += 1
    for event in events:
        for feed in event["feeds"]:
            feed_distribution[feed]["events"] += 1
    sizes = [event["size"] for event in events]
    return {
        "tool": TOOL,
        "generated_at": storage.now(),
        "timestamp": stamp,
        "db": {"path": str(args.db), "read_only": True, "access": access_mode},
        "params": {
            "limit": args.limit, "threshold": args.threshold, "similarity": args.similarity,
            "boundary_width": BOUNDARY_WIDTH, "max_model_pairs": args.max_model_pairs,
            "model": (not args.no_model and args.max_model_pairs > 0), "model_batch": args.model_batch,
            "min_interval_seconds": args.min_interval,
        },
        "model": {
            "enabled": (not args.no_model and args.max_model_pairs > 0),
            "model_id": os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL,
            "endpoint": analysis.PRODUCTION_ENDPOINT,
            "reasoning_effort": "none",
            "calls": model_meta["calls"],
            "pairs_sent": model_meta["pairs_sent"],
            "pairs_same": model_meta["pairs_same"],
            "pairs_different": model_meta["pairs_different"],
            "pairs_skipped_over_cap": model_meta["pairs_skipped_over_cap"],
            "errors": model_meta["errors"],
            "batches": model_meta["batches"],
            "tokens": {"per_batch": model_meta["usages"], "totals": tokens},
        },
        "record_count": len(records),
        "event_count": len(events),
        "max_event_size": max(sizes) if sizes else 0,
        "singleton_events": sum(1 for s in sizes if s == 1),
        "event_size_distribution": {str(s): sizes.count(s) for s in sorted(set(sizes))},
        "feed_distribution": feed_distribution,
        "pairs": {
            "total": stats["total_pairs"],
            "rule_same": stats["canonical_same"] + stats["similarity_same"],
            "rule_same_by_canonical_url": stats["canonical_same"],
            "rule_same_by_similarity": stats["similarity_same"],
            "rule_different": stats["different"],
            "boundary_total": stats["boundary"],
            "boundary_selected": len(model_meta["selected"]),
            "boundary_skipped_over_cap": model_meta["pairs_skipped_over_cap"],
            "boundary_skipped_no_model": model_meta["pairs_skipped_no_model"],
        },
        "outputs": files,
        "notes": NOTES,
    }


def write_outputs(events, summary, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary["timestamp"]
    jsonl = out_dir / f"events-{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as stream:
        for event in events:
            stream.write(storage.json_bytes(event).decode("utf-8") + "\n")
    files = {"jsonl": str(jsonl), "jsonl_sha256": storage.digest(jsonl.read_bytes()),
             "jsonl_bytes": jsonl.stat().st_size}
    summary_path = out_dir / f"summary-{stamp}.json"
    summary["outputs"] = files
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    files["summary"] = str(summary_path)
    files["summary_sha256"] = storage.digest(summary_path.read_bytes())
    # 回读校验：产物字节必须与报告里的哈希一致，避免把「写过了」当成「写对了」。
    if storage.digest(jsonl.read_bytes()) != files["jsonl_sha256"]:
        raise AnnotationError("write", "jsonl 回读哈希不一致", 7, {"path": str(jsonl)})
    return files


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"取最近 N 条 records（1-{MAX_LIMIT}，默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"标题相似度阈值（0-1，默认 {DEFAULT_THRESHOLD}）；>= 阈值判同事件")
    parser.add_argument("--similarity", choices=SIMILARITY_KINDS, default="dice",
                        help="标题相似度口径：字符级 dice（默认）或 token jaccard")
    parser.add_argument("--max-model-pairs", type=int, default=DEFAULT_MAX_MODEL_PAIRS,
                        help=f"最多送模型的边界对数（0-{MAX_MODEL_PAIRS}，默认 {DEFAULT_MAX_MODEL_PAIRS}）")
    parser.add_argument("--model-batch", type=int, default=DEFAULT_MODEL_BATCH,
                        help=f"单批模型对数，不得超过 {MODEL_BATCH_MAX}（默认 {DEFAULT_MODEL_BATCH}）")
    parser.add_argument("--no-model", action="store_true", help="完全跳过模型，纯规则聚类")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help=f"模型批次之间的最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将聚类多少条、会触发多少次模型调用；0 次网络请求、不写输出")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="既有证据库路径，默认 <仓库>/sentiment.db（只读打开）")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help="产物目录，默认 <仓库>/events（不在 allowed_paths 内，仅运行期产物）")
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
    _relevance._utf8_stdout()
    args = build_parser().parse_args(argv)
    context = {"limit": args.limit, "threshold": args.threshold, "similarity": args.similarity,
               "max_model_pairs": args.max_model_pairs, "no_model": args.no_model,
               "dry_run": args.dry_run, "db": str(args.db), "out_dir": str(args.out_dir)}
    db_handle = None
    try:
        if not 1 <= args.limit <= MAX_LIMIT:
            raise AnnotationError("args", f"--limit 必须在 1-{MAX_LIMIT} 之间，收到 {args.limit}", 2)
        if not 0.0 < args.threshold <= 1.0:
            raise AnnotationError("args", f"--threshold 必须在 (0,1] 之间，收到 {args.threshold}", 2)
        if not 0 <= args.max_model_pairs <= MAX_MODEL_PAIRS:
            raise AnnotationError("args", f"--max-model-pairs 必须在 0-{MAX_MODEL_PAIRS} 之间，收到 {args.max_model_pairs}", 2)
        if not 1 <= args.model_batch <= MODEL_BATCH_MAX:
            raise AnnotationError("args", f"--model-batch 必须在 1-{MODEL_BATCH_MAX} 之间，收到 {args.model_batch}", 2)
        if args.min_interval < 0:
            raise AnnotationError("args", f"--min-interval 不得为负，收到 {args.min_interval}", 2)

        db_handle, access_mode = open_readonly(args.db)
        records = load_records(db_handle, args.limit)
        for record in records:
            record["_norm"] = normalize_title(record["title"])
        similarity = SIMILARITY_FUNCS[args.similarity]

        # 规则阶段（零模型调用）。先留存规则分量根，供 method 归属判断。
        uf, stats, candidates = rule_pass(records, similarity, args.threshold)
        rule_roots = [uf.find(i) for i in range(len(records))]

        model_enabled = not args.no_model and args.max_model_pairs > 0
        if model_enabled:
            selected, over_cap = select_pairs(candidates, args.max_model_pairs)
            no_model_skipped = 0
        else:
            selected, over_cap, no_model_skipped = [], 0, len(candidates)
        call_count = len(chunk(selected, args.model_batch)) if selected else 0

        base = {
            "tool": TOOL, "mode": "dry-run" if args.dry_run else "live",
            "db": {"path": str(args.db), "present": True, "access": access_mode, "touched": False},
            "limit": args.limit, "threshold": args.threshold, "similarity": args.similarity,
            "boundary_width": BOUNDARY_WIDTH,
            "record_count": len(records), "event_count_if_rule_only": len({uf.find(i) for i in range(len(records))}),
            "pairs": {"total": stats["total_pairs"],
                      "rule_same": stats["canonical_same"] + stats["similarity_same"],
                      "rule_same_by_canonical_url": stats["canonical_same"],
                      "rule_same_by_similarity": stats["similarity_same"],
                      "rule_different": stats["different"], "boundary_total": stats["boundary"]},
            "model": {"enabled": model_enabled, "max_model_pairs": args.max_model_pairs,
                      "model_batch": args.model_batch, "min_interval_seconds": args.min_interval,
                      "boundary_selected": len(selected), "boundary_skipped_over_cap": over_cap,
                      "boundary_skipped_no_model": no_model_skipped,
                      "model_call_count": call_count},
            "out_dir": str(args.out_dir),
            "notes": NOTES,
        }

        if args.dry_run:
            base["network_requests"] = 0
            base["would_write"] = ["events/events-<UTC时间戳>.jsonl",
                                   "events/summary-<UTC时间戳>.json"]
            base["selected_boundary_pairs"] = [{"pair_id": p["pair_id"], "similarity": p["similarity"],
                                                "a": records[p["i"]]["id"], "b": records[p["j"]]["id"]}
                                               for p in selected]
            print(json.dumps(base, ensure_ascii=False, indent=2))
            return 0

        # 进入模型阶段前先判前置条件：缺凭据是配置错误，直接失败、不产生半成品（不静默降级成空结果）。
        if model_enabled and selected:
            credential = os.environ.get(analysis.PRODUCTION_KEY_ENV)
            fixture_mode = os.environ.get("OPENCODE_GO_V1_FIXTURE") == "1"
            if not credential and not fixture_mode:
                raise AnnotationError(
                    "env", f"缺少环境变量 {analysis.PRODUCTION_KEY_ENV}；未发送任何请求"
                    "（纯规则请加 --no-model；离线夹具回放请设 OPENCODE_GO_V1_FIXTURE=1）", 2,
                    {"credential_env": analysis.PRODUCTION_KEY_ENV, "boundary_selected": len(selected)})

        errors = []
        decisions, batches_meta, usages = model_pass(uf, records, selected, args.model_batch,
                                                     args.min_interval, errors) if selected \
            else ({}, [], [])
        pairs_same = sum(1 for v in decisions.values() if v)
        model_meta = {
            "calls": len(batches_meta), "pairs_sent": len(selected), "pairs_same": pairs_same,
            "pairs_different": len(selected) - pairs_same, "pairs_skipped_over_cap": over_cap,
            "pairs_skipped_no_model": no_model_skipped,
            "errors": errors, "batches": batches_meta, "usages": usages, "selected": selected,
        }
        tokens = usage_totals(usages)

        events = build_events(records, uf, rule_roots)
        stamp = _relevance.utc_stamp()
        summary = build_summary(events, records, stats, model_meta, tokens, args, stamp,
                                access_mode, {})
        files = write_outputs(events, summary, args.out_dir)

        base["status"] = "ok"
        base["degraded"] = bool(errors)
        base["event_count"] = summary["event_count"]
        base["max_event_size"] = summary["max_event_size"]
        base["feed_distribution"] = summary["feed_distribution"]
        base["model"]["model_call_count"] = model_meta["calls"]
        base["model"]["pairs_same"] = pairs_same
        base["model"]["pairs_different"] = model_meta["pairs_different"]
        base["model"]["errors"] = errors
        base["model"]["tokens"] = tokens
        base["outputs"] = files
        print(json.dumps(base, ensure_ascii=False, indent=2))
        return 0
    except AnnotationError as exc:
        return emit_error(exc, context)
    except analysis.ProductionHTTPError as exc:
        # 兜底：模型阶段外的意外 HTTP 失败也要保留真实状态码与响应体前 200 字，不重试。
        return emit_error(AnnotationError("http", str(exc), 3,
                                          {"status": exc.status, "body_prefix": exc.body_prefix}), context)
    finally:
        if db_handle is not None:
            db_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
