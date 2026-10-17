#!/usr/bin/env python
"""S5 立场标注：证据先行（A 出 claims → B 逐条复核 → 只由 supported claim 推导立场）。

只读硬约束：**只读数据库**（只 SELECT records/feeds），不写 records、不改表结构、不建新表；
不碰任何平台，不发任何平台写动作。

复用清单（先复用，后新写；详见 docs/S5-stance.md 第一节）：

1. `analysis.py`（已并入并独立审查的 A/B 证据协议）——**直接调用，一个字都不重写**：
   - `analysis.production_adapter(role, packet, a_output=None)`：生产 HTTP 客户端（裸模型 id、
     `reasoning_effort="none"`、三个必备头、错误保留 `analysis.ProductionHTTPError`、单次请求
     不重试），并自带 `OPENCODE_GO_V1_FIXTURE=1` 的离线夹具分支；
   - `analysis.normalize_output(packet, role, output)`：在 validate 之前对齐引用偏移，绝不
     放宽「引用必须是原文逐字子串」；
   - `analysis.validate(packet, role, output, a_output)`：A/B 结构与引用硬校验。
   本工具的立场**只**由通过 validate、且被 B 复核为 `supported` 的 claim 推导；模型不得直接
   输出立场。
2. `tools/cluster_events.py`（S4）——`--events-from-db` 时按 importlib 载入其模块，复用
   `load_records()` / `normalize_title()` / `SIMILARITY_FUNCS` / `rule_pass()` / `build_events()`
   （规则阶段零模型调用），事件定义（`event_id`、`size`、按规模排序）与 S4 产物一致；
   **不另写一套聚类**。
3. `tools/annotate_relevance.py`（本任务 anchor）——复用 `open_readonly()`
   （无未检查点数据时 `mode=ro&immutable=1`，存在非空 `-wal` 时退回 `mode=ro`；两种模式都不写库，
   任何写都会失败）、`AnnotationError`（stage / exit_code / extra）、`utc_stamp()`、`clip()`、
   `_utf8_stdout()`（Windows 控制台默认 GBK）、`usage_totals()`（usage 缺失保留 null）。
4. `storage.py`——`now()`（UTC）、`digest()`（SHA-256）、`json_bytes()`（jsonl 行按项目既有规范
   序列化：键排序、紧凑、`ensure_ascii=False`）。

唯一必须新写的部分及理由：

1. **事件输入的读取与成员解析**：S4 产物 `events-*.jsonl` 每条只有 `event_id/size/feeds/titles/
   urls/first_seen/method`——**没有** `record_id`、`summary`、`raw_sha256`，而证据 packet 必须带
   这三个字段。故按 `(url, title)` 回只读库关联 `records`（同键多候选按 `fetched_at, id` 升序
   确定性取用，重复成员按序消费）。该做法是新写的；聚类逻辑本身不重写。
2. **立场规则表**：只把「通过 validate 且 supported 的 claim」按 `target` 分布（`context` 作
   方向细化）映射为 `支持/反对/中立`，无 supported claim 一律 `无关`。规则表写在
   docs/S5-stance.md 第四节，**模型从不直接输出立场**。
3. **纯规则降级**（`--no-model`）：关键词 + 目标词表，输出强制标 `method="rules"`，绝不冒充
   模型结论。
4. 参数解析、预算控制、产物落盘与哈希回读。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analysis  # noqa: E402  复用已审查的 A/B 证据协议（production_adapter / normalize_output / validate）
import storage   # noqa: E402  复用项目证据库既有约定


def _load_sibling(name, alias):
    """按路径载入同目录工具模块（tools/ 不是包，无 __init__.py）。"""
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法载入同目录工具：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_relevance = _load_sibling("annotate_relevance", "_s5_annotate_relevance")
_cluster = _load_sibling("cluster_events", "_s5_cluster_events")

TOOL = "s5-stance"
DEFAULT_DB = ROOT / "sentiment.db"
EVENTS_DIR = ROOT / "events"
OUT_DIR = ROOT / "stance"

AnnotationError = _relevance.AnnotationError      # 复用失败类型（stage / exit_code / extra）
open_readonly = _relevance.open_readonly          # 复用只读打开（mode=ro / immutable=1）
usage_totals = _relevance.usage_totals            # 复用 usage 汇总（缺字段保留 null）
_utf8_stdout = _relevance._utf8_stdout            # 复用 Windows 控制台 UTF-8 切换

DEFAULT_MAX_EVENTS = 10
MAX_MAX_EVENTS = 50            # 契约硬上限
DEFAULT_MAX_MODEL_CALLS = 20   # 每次 A 或 B 各算 1 次
MAX_MODEL_CALLS = 100          # 自设上限：--max-events 上限 50 × 每事件最多 2 次
DEFAULT_MIN_INTERVAL = 8.0
DEFAULT_LIMIT = 200            # 仅用于 --events-from-db 的聚类输入规模
MAX_LIMIT = 200

STANCES = ("支持", "反对", "中立", "无关")
CONSERVATIVE_STANCE = "无关"   # 无 supported claim / 证据不足时的保守立场
METHOD_MODEL = "model"
METHOD_RULES = "rules"

# 纯规则降级的粗关键词表（只用于 --no-model；是降级，不是模型结论的替代）。
FEMALE_TERMS = ("女", "母亲", "妈妈", "妻子", "老婆", "女儿", "女孩", "姐姐", "妹妹",
                "奶奶", "女性", "女子", "闺蜜", "婆婆", "儿媳", "宝妈")
MALE_TERMS = ("男", "父亲", "爸爸", "丈夫", "老公", "儿子", "男孩", "哥哥", "弟弟",
              "爷爷", "男性", "男子")
SUPPORT_TERMS = ("支持", "力挺", "点赞", "赞同", "认可", "声援", "拥护")
REBUT_TERMS = ("反对", "反驳", "辟谣", "澄清", "否认", "假的", "不实", "造谣",
               "怒斥", "致歉", "道歉")

NOTES = [
    "只读数据库：只 SELECT records/feeds，不写 records、不改表结构、不建新表。",
    "立场只由「通过 analysis.validate 且被 B 复核为 supported 的 claim」按规则表推导；"
    "模型从不直接输出立场。",
    "无 supported claim（含 A/B 失败、预算不足、全部被丢弃）一律判「无关」，并按保守口径计数。",
    "validate 不过的 claim 逐条丢弃并计数，绝不放宽校验；引用必须是原文逐字子串。",
    "成本纪律：--max-model-calls 计 A/B 每次调用；超限即停并在汇总里明说；"
    "事件之间按 --min-interval 节流；单次请求、不重试。",
    "失败保留真实 HTTP 状态码与响应体前 200 字（复用 analysis.ProductionHTTPError）；"
    "单个事件的模型失败按「无证据→无关」保守降级并在 errors 留痕，不静默。",
    "--no-model 为纯规则降级，输出标 method=\"rules\"，不冒充模型结论。",
    "不推断群体立场：本工具的立场是事件文本内、有引用支撑的表达倾向；不是人群态度比例，"
    "不算评论立场，也不采评论正文。",
    "本工具不写数据库、不写桥的状态文件、不启动服务、不订阅定时任务。",
]


# --- 事件输入：S4 产物 jsonl 或复用 cluster_events 现场聚类 -----------------------------------

def latest_events_file(directory):
    files = sorted(Path(directory).glob("events-*.jsonl"))
    if not files:
        raise AnnotationError("input", f"未找到 S4 事件产物（{directory}\\events-*.jsonl）；"
                                       f"请用 --events 显式指定，或加 --events-from-db", 2,
                              {"events_dir": str(directory)})
    return files[-1]


def load_events_jsonl(path):
    """读 S4 产物：每条至少需 event_id / titles / urls，且 titles 与 urls 等长同序。"""
    path = Path(path)
    if not path.is_file():
        raise AnnotationError("input", f"事件产物不存在：{path}", 2, {"events": str(path)})
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationError("input", f"事件产物第 {number} 行不是合法 JSON：{exc}", 2,
                                  {"events": str(path)}) from exc
        if not isinstance(event, dict):
            raise AnnotationError("input", f"事件产物第 {number} 行不是对象", 2, {"events": str(path)})
        titles, urls = event.get("titles"), event.get("urls")
        if not isinstance(event.get("event_id"), str) or not isinstance(titles, list) \
                or not isinstance(urls, list) or len(titles) != len(urls) or not titles:
            raise AnnotationError("input", f"事件产物第 {number} 行缺 event_id/titles/urls 或长度不一致", 2,
                                  {"events": str(path)})
        events.append({"event_id": event["event_id"], "size": len(titles),
                       "titles": titles, "urls": urls,
                       "declared_size": event.get("size")})
    return events


def load_record_maps(db):
    """只读全量 records，一次查询同时建两种索引：

    - `index`：按 (url, title) → 候选列表，同键多候选按 (fetched_at, id) 升序（确定性）；
    - `details`：按 record id → 记录，供 --events-from-db 直接用 id 取 summary/raw_sha256。

    只 SELECT、不写库；不改表结构、不建新表。
    """
    sql = ("SELECT id, url, title, summary, raw_sha256, canonical_id, published_at, fetched_at "
           "FROM records ORDER BY fetched_at ASC, id ASC")
    try:
        rows = db.execute(sql).fetchall()
    except Exception as exc:  # noqa: BLE001  sqlite3.Error 不能跨连接类比较，统一转项目失败类型
        raise AnnotationError("db", f"读取 records 失败：{exc}", 2, {"db_sql": sql}) from exc
    index = {}
    details = {}
    for row in rows:
        record = {"id": row["id"], "title": row["title"] or "", "summary": row["summary"] or "",
                  "raw_sha256": row["raw_sha256"], "canonical_id": row["canonical_id"],
                  "published_at": row["published_at"], "fetched_at": row["fetched_at"],
                  "url": row["url"]}
        index.setdefault((record["url"], record["title"]), []).append(record)
        details[record["id"]] = record
    return index, details


def resolve_members(index, event):
    """把 S4 事件成员（title/url 序列）关联回只读库记录；解析不到的成员**硬失败**，不伪造证据。"""
    counters = {}
    records = []
    ambiguous = 0
    missing = []
    for title, url in zip(event["titles"], event["urls"]):
        key = (url, title)
        candidates = index.get(key)
        if not candidates:
            missing.append({"event_id": event["event_id"], "title": title, "url": url})
            continue
        order = counters.get(key, 0)
        counters[key] = order + 1
        if len(candidates) > 1:
            ambiguous += 1
        # 同键候选按序消费；候选不足时复用最后一个（同一内容的重复成员），保持成员数不缩水。
        records.append(candidates[order] if order < len(candidates) else candidates[-1])
    if missing:
        raise AnnotationError(
            "input", f"事件 {event['event_id']} 有 {len(missing)} 个成员无法按 (url,title) 关联回库；"
                     f"事件产物与证据库可能不是同一快照，拒绝伪造 packet", 2,
            {"missing": missing[:5]})
    return records, ambiguous


def events_from_db(db, limit, details):
    """复用 cluster_events 的规则阶段（零模型调用）现场聚类；边界对按「不同事件」保守处理。"""
    records = _cluster.load_records(db, limit)       # 仅为聚类：id/title/url/fetched_at/feed
    for record in records:
        record["_norm"] = _cluster.normalize_title(record["title"])
    similarity = _cluster.SIMILARITY_FUNCS["dice"]
    uf, _stats, _boundary = _cluster.rule_pass(records, similarity, _cluster.DEFAULT_THRESHOLD)
    rule_roots = [uf.find(i) for i in range(len(records))]
    canonical = _cluster.build_events(records, uf, rule_roots)     # 权威 event_id/size
    by_id = {event["event_id"]: event for event in canonical}
    groups = {}
    for i in range(len(records)):
        groups.setdefault(uf.find(i), []).append(i)
    events = []
    for members in groups.values():
        members.sort(key=lambda i: (records[i]["fetched_at"], records[i]["id"]))
        ids = sorted(records[i]["id"] for i in members)
        event_id = "evt-" + storage.digest(storage.json_bytes(ids))[:16]
        if event_id not in by_id:      # 与 cluster_events 定义不一致时明确失败，不静默
            raise AnnotationError("input", f"现场聚类事件 ID 与 cluster_events.build_events 不一致：{event_id}", 2)
        member_ids = [records[i]["id"] for i in members]
        missing = [mid for mid in member_ids if mid not in details]
        if missing:                    # 聚类用的 id 必须能在同库取到完整字段，否则拒绝伪造 packet
            raise AnnotationError("input", f"事件 {event_id} 的成员记录在本库取不到完整字段", 2,
                                  {"missing": missing[:5]})
        events.append({"event_id": event_id, "size": len(members),
                       "titles": [details[mid]["title"] for mid in member_ids],
                       "urls": [details[mid]["url"] for mid in member_ids],
                       "declared_size": len(members),
                       "records": [details[mid] for mid in member_ids],
                       "ambiguous": 0})
    return events


# --- packet 与 A/B 证据协议（协议本身全部调用 analysis.py，不重写） ----------------------------

def build_packet(records):
    """与 analysis.evidence() 同形的 packet：records 带 id/raw_sha256/title/summary，供引用校验。"""
    return {
        "records": [{"id": r["id"], "raw_sha256": r["raw_sha256"], "title": r["title"],
                     "summary": r["summary"], "canonical_id": r["canonical_id"],
                     "published_at": r["published_at"]} for r in records],
        "coverage": "sample_only",
        "prompt_version": analysis.PROMPT,
        "statistics": {"record_count": len(records),
                       "canonical_count": len({r["canonical_id"] for r in records}),
                       "unknown_time_count": sum(1 for r in records if r["published_at"] is None)},
    }


def keep_valid_claims(packet, raw_output, drop):
    """A 输出：先检外层结构（仅 claims、1–20 条），再**逐条** normalize_output + validate。

    逐条校验使「validate 不过的 claim 丢弃并计数」成为可能；判据完全用 analysis.validate 原样，
    不放宽、不修补。丢弃原因写进 drop。
    """
    claims = raw_output.get("claims") if isinstance(raw_output, dict) else None
    if not isinstance(raw_output, dict) or set(raw_output) != {"claims"} or not isinstance(claims, list):
        drop.append({"reason": "a_structure", "detail": "模型输出必须为仅含 claims 数组的 JSON 对象"})
        return []
    if not 1 <= len(claims) <= 20:
        drop.append({"reason": "a_structure", "detail": f"claims 条数越界：{len(claims)}（契约 1–20）"})
        return []
    kept = []
    for claim in claims:
        single = {"claims": [claim]}                 # 单条容器：不改判据，仅缩小校验粒度
        try:
            analysis.normalize_output(packet, "A", single)
            analysis.validate(packet, "A", single)
        except ValueError as exc:
            drop.append({"reason": "a_validation", "detail": str(exc)[:200],
                         "claim_id": claim.get("id") if isinstance(claim, dict) else None})
            continue
        kept.append(single["claims"][0])
    return kept


def keep_reviews(packet, a_output, raw_output, drop):
    """B 输出：normalize_output + validate（逐条覆盖 A 全部主张）；不过则整批丢弃并计数。"""
    reviews = raw_output.get("reviews") if isinstance(raw_output, dict) else None
    if not isinstance(raw_output, dict) or set(raw_output) != {"reviews"} or not isinstance(reviews, list):
        drop.append({"reason": "b_structure", "detail": "模型输出必须为仅含 reviews 数组的 JSON 对象"})
        return None
    single = {"reviews": reviews}
    try:
        analysis.normalize_output(packet, "B", single)
        analysis.validate(packet, "B", single, a_output)
    except ValueError as exc:
        drop.append({"reason": "b_validation", "detail": str(exc)[:200]})
        return None
    return single["reviews"]


def admission(a_claims, reviews, drop):
    """只保留 B 复核为 supported 的 claim 作为证据；其余计入丢弃原因（保守）。"""
    supported = []
    by_id = {review["claim_id"]: review for review in reviews}
    for claim in a_claims:
        review = by_id.get(claim["id"])
        if review is None:
            drop.append({"reason": "b_missing_review", "claim_id": claim["id"]})
            continue
        if review["verdict"] == "supported":
            supported.append(claim)
        else:
            drop.append({"reason": "b_" + review["verdict"], "claim_id": claim["id"]})
    return supported


# --- 立场规则表（保守）：只由 supported claim 的 target 分布推导；context 作方向细化 -------------

def stance_from_claims(supported):
    """规则表（写死、可审计）：

    1. 无 supported claim                      → 无关
    2. supported 全部 target=unspecified        → 无关（没有可支撑的性别指向）
    3. 含 both，或同时含 male 与 female          → 中立（两侧都在场，保守不选边）
    4. 仅单侧（male 或 female）：
       4a. 该侧 context=assertion 多于 rebuttal → 支持
       4b. 该侧 context=rebuttal 多于 assertion → 反对
       4c. 其余（quotation/uncertain 居多或各半）→ 中立
    """
    if not supported:
        return CONSERVATIVE_STANCE
    counts = {"male": 0, "female": 0, "both": 0, "unspecified": 0}
    for claim in supported:
        counts[claim["target"]] += 1
    if counts["male"] == 0 and counts["female"] == 0 and counts["both"] == 0:
        return CONSERVATIVE_STANCE
    if counts["both"] > 0 or (counts["male"] > 0 and counts["female"] > 0):
        return "中立"
    side = "male" if counts["male"] > 0 else "female"
    assertive = sum(1 for c in supported if c["target"] == side and c["context"] == "assertion")
    rebutting = sum(1 for c in supported if c["target"] == side and c["context"] == "rebuttal")
    if assertive > rebutting:
        return "支持"
    if rebutting > assertive:
        return "反对"
    return "中立"


def rules_stance(records):
    """--no-model 的纯规则降级：关键词 + 目标词表。是降级，不冒充模型结论。"""
    text = " ".join((r["title"] or "") + " " + (r["summary"] or "") for r in records).lower()
    has_female = any(term in text for term in FEMALE_TERMS)
    has_male = any(term in text for term in MALE_TERMS)
    if not (has_female or has_male):
        return CONSERVATIVE_STANCE
    if has_female and has_male:
        return "中立"
    rebut = any(term in text for term in REBUT_TERMS)
    support = any(term in text for term in SUPPORT_TERMS)
    if rebut and not support:
        return "反对"
    if support and not rebut:
        return "支持"
    return "中立"


def evidence_of(claim):
    return {"claim_id": claim["id"], "targets": [claim["target"]],
            "quotes": [ref["quote"] for ref in claim["citations"]]}


# --- 逐个事件处理 ----------------------------------------------------------------------------

def process_event(event, packet, drop, budget, args, errors, call_log):
    """返回 (stance, method, supported, a_claims_count)。模型失败按「无证据→无关」保守降级。"""
    if args.no_model:
        return rules_stance(event["records"]), METHOD_RULES, [], 0

    if budget["used"] >= budget["max"]:
        raise _BudgetExhausted()
    if call_log:
        time.sleep(args.min_interval)
    try:
        raw_a, usage_a = analysis.production_adapter("A", packet, None)
    except Exception as exc:  # noqa: BLE001  逐事件降级：保留真实失败原文，不重试
        budget["used"] += 1
        errors.append(_error_entry(event, "A", exc))
        call_log.append({"event_id": event["event_id"], "role": "A", "usage": None,
                         "error": True})
        return CONSERVATIVE_STANCE, METHOD_MODEL, [], 0
    budget["used"] += 1
    call_log.append({"event_id": event["event_id"], "role": "A", "usage": usage_a, "error": False})

    a_claims = keep_valid_claims(packet, raw_a, drop)
    if not a_claims:
        return CONSERVATIVE_STANCE, METHOD_MODEL, [], 0

    if budget["used"] >= budget["max"]:
        budget["exhausted"] = True
        drop.append({"reason": "b_skipped_budget", "event_id": event["event_id"]})
        return CONSERVATIVE_STANCE, METHOD_MODEL, [], len(a_claims)
    a_output = {"claims": a_claims}
    try:
        raw_b, usage_b = analysis.production_adapter("B", packet, a_output)
    except Exception as exc:  # noqa: BLE001
        budget["used"] += 1
        errors.append(_error_entry(event, "B", exc))
        call_log.append({"event_id": event["event_id"], "role": "B", "usage": None, "error": True})
        return CONSERVATIVE_STANCE, METHOD_MODEL, [], len(a_claims)
    budget["used"] += 1
    call_log.append({"event_id": event["event_id"], "role": "B", "usage": usage_b, "error": False})

    reviews = keep_reviews(packet, a_output, raw_b, drop)
    if reviews is None:
        return CONSERVATIVE_STANCE, METHOD_MODEL, [], len(a_claims)
    supported = admission(a_claims, reviews, drop)
    return stance_from_claims(supported), METHOD_MODEL, supported, len(a_claims)


class _BudgetExhausted(Exception):
    """模型调用预算已用尽：超限即停（事件级）。"""


def _error_entry(event, role, exc):
    entry = {"event_id": event["event_id"], "role": role, "type": type(exc).__name__,
             "message": str(exc)[:300]}
    if isinstance(exc, analysis.ProductionHTTPError):    # 真实状态码 + 响应体前 200 字
        entry["status"] = exc.status
        entry["body_prefix"] = exc.body_prefix
    return entry


def precheck_model(args):
    """进入模型阶段前的前置条件：缺凭据（且未显式夹具）直接失败，不产生半成品。"""
    if args.no_model:
        return
    credential = os.environ.get(analysis.PRODUCTION_KEY_ENV)
    fixture = os.environ.get("OPENCODE_GO_V1_FIXTURE") == "1"
    if not credential and not fixture:
        raise AnnotationError(
            "env", f"缺少环境变量 {analysis.PRODUCTION_KEY_ENV}；未发送任何请求"
                   "（纯规则请加 --no-model；离线夹具回放请设 OPENCODE_GO_V1_FIXTURE=1）", 2,
            {"credential_env": analysis.PRODUCTION_KEY_ENV})


# --- 产物 ------------------------------------------------------------------------------------

def write_outputs(records, summary, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary["timestamp"]
    jsonl = out_dir / f"stance-{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as stream:
        for entry in records:
            stream.write(storage.json_bytes(entry).decode("utf-8") + "\n")
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
    parser.add_argument("--events", type=Path, default=None,
                        help="S4 事件产物 jsonl；缺省取 <仓库>/events/events-*.jsonl 中最新一个")
    parser.add_argument("--events-from-db", action="store_true",
                        help="不用 S4 产物，改为 import cluster_events 复用其规则阶段现场聚类（零模型调用）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="既有证据库路径，默认 <仓库>/sentiment.db（只读打开，用于取 summary/raw_sha256）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"--events-from-db 的聚类输入条数（1-{MAX_LIMIT}，默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS,
                        help=f"最多处理的事件数（1-{MAX_MAX_EVENTS}，默认 {DEFAULT_MAX_EVENTS}）")
    parser.add_argument("--max-model-calls", type=int, default=DEFAULT_MAX_MODEL_CALLS,
                        help=f"模型调用总上限（A/B 各算 1 次；0-{MAX_MODEL_CALLS}，默认 {DEFAULT_MAX_MODEL_CALLS}）")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help=f"事件之间的最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}）")
    parser.add_argument("--no-model", action="store_true",
                        help="完全跳过模型，纯规则降级（输出标 method=\"rules\"）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将处理多少事件、预计多少次模型调用；0 次网络请求、不写输出")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help="产物目录，默认 <仓库>/stance（不在 allowed_paths 内，仅运行期产物）")
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


def load_selected_events(db_handle, args):
    """返回 (events, source, total_events)。events 按 (size DESC, event_id) 取前 --max-events。"""
    index, details = load_record_maps(db_handle)
    if args.events_from_db:
        events = events_from_db(db_handle, args.limit, details)
        source = {"kind": "events-from-db", "events_file": None, "events_sha256": None,
                  "limit": args.limit}
    else:
        path = Path(args.events) if args.events else latest_events_file(EVENTS_DIR)
        raw_events = load_events_jsonl(path)
        events = []
        for event in raw_events:
            members, ambiguous = resolve_members(index, event)
            events.append({**event, "records": members, "ambiguous": ambiguous})
        source = {"kind": "events-jsonl", "events_file": str(path),
                  "events_sha256": storage.digest(Path(path).read_bytes()),
                  "limit": None}
    events.sort(key=lambda e: (-e["size"], e["event_id"]))
    return events[:args.max_events], source, len(events)


def main(argv=None):
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    context = {"max_events": args.max_events, "max_model_calls": args.max_model_calls,
               "no_model": args.no_model, "dry_run": args.dry_run, "db": str(args.db)}
    db_handle = None
    try:
        if not 1 <= args.max_events <= MAX_MAX_EVENTS:
            raise AnnotationError("args", f"--max-events 必须在 1-{MAX_MAX_EVENTS} 之间，收到 {args.max_events}", 2)
        if not 0 <= args.max_model_calls <= MAX_MODEL_CALLS:
            raise AnnotationError("args", f"--max-model-calls 必须在 0-{MAX_MODEL_CALLS} 之间，收到 {args.max_model_calls}", 2)
        if not 1 <= args.limit <= MAX_LIMIT:
            raise AnnotationError("args", f"--limit 必须在 1-{MAX_LIMIT} 之间，收到 {args.limit}", 2)
        if args.min_interval < 0:
            raise AnnotationError("args", f"--min-interval 不得为负，收到 {args.min_interval}", 2)
        if args.events and args.events_from_db:
            raise AnnotationError("args", "--events 与 --events-from-db 互斥", 2)

        db_handle, access_mode = open_readonly(args.db)
        events, source, total_events = load_selected_events(db_handle, args)

        plan = {
            "tool": TOOL, "mode": "dry-run" if args.dry_run else "live",
            "db": {"path": str(args.db), "present": True, "access": access_mode, "touched": False},
            "input": {**source, "total_events_available": total_events,
                      "events_selected": len(events), "max_events": args.max_events},
            "events": [{"event_id": e["event_id"], "size": e["size"],
                        "records": len(e["records"]), "ambiguous_members": e["ambiguous"]}
                       for e in events],
            "no_model": args.no_model, "min_interval_seconds": args.min_interval,
            "out_dir": str(args.out_dir), "notes": NOTES,
        }
        if args.dry_run:
            per_event = 0 if args.no_model else 2
            plan["network_requests"] = 0
            plan["model"] = {"enabled": not args.no_model, "max_model_calls": args.max_model_calls,
                             "per_event_max_calls": per_event,
                             "total_max_calls": per_event * len(events),
                             "planned_max_calls": min(per_event * len(events), args.max_model_calls)}
            plan["would_write"] = ["stance/stance-<UTC时间戳>.jsonl", "stance/summary-<UTC时间戳>.json"]
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0

        precheck_model(args)
        drop = []
        errors = []
        call_log = []
        budget = {"used": 0, "max": args.max_model_calls, "exhausted": False}
        results = []
        skipped = []
        for index, event in enumerate(events):
            if not args.no_model and budget["used"] >= budget["max"]:
                skipped = [{"event_id": e["event_id"], "size": e["size"],
                            "reason": "max_model_calls"} for e in events[index:]]
                budget["exhausted"] = True
                break
            packet = build_packet(event["records"])
            before = len(drop)
            try:
                stance, method, supported, a_count = process_event(
                    event, packet, drop, budget, args, errors, call_log)
            except _BudgetExhausted:
                skipped = [{"event_id": e["event_id"], "size": e["size"],
                            "reason": "max_model_calls"} for e in events[index:]]
                budget["exhausted"] = True
                break
            results.append({
                "event_id": event["event_id"], "size": event["size"], "stance": stance,
                "method": method, "supported_claims": len(supported),
                "dropped_claims": len(drop) - before,
                "evidence": [evidence_of(claim) for claim in supported],
            })
        stamp = _relevance.utc_stamp()
        stance_counts = {name: 0 for name in STANCES}
        method_counts = {METHOD_MODEL: 0, METHOD_RULES: 0}
        drop_reasons = {}
        for entry in results:
            stance_counts[entry["stance"]] += 1
            method_counts[entry["method"]] += 1
        for item in drop:
            drop_reasons[item["reason"]] = drop_reasons.get(item["reason"], 0) + 1
        summary = {
            "tool": TOOL, "generated_at": storage.now(), "timestamp": stamp,
            "db": {"path": str(args.db), "read_only": True, "access": access_mode},
            "input": {**source, "total_events_available": total_events},
            "params": {"max_events": args.max_events, "max_model_calls": args.max_model_calls,
                       "min_interval_seconds": args.min_interval, "no_model": args.no_model},
            "events_selected": len(events), "events_processed": len(results),
            "events_skipped": skipped,
            "stance_distribution": stance_counts, "method_distribution": method_counts,
            "supported_claim_total": sum(e["supported_claims"] for e in results),
            "dropped_claim_total": len(drop), "drop_reasons": drop_reasons, "drops": drop,
            "model": {
                "enabled": not args.no_model,
                "model_id": os.environ.get(analysis.PRODUCTION_MODEL_ENV) or analysis.PRODUCTION_MODEL,
                "endpoint": analysis.PRODUCTION_ENDPOINT, "reasoning_effort": "none",
                "calls": budget["used"], "max_calls": args.max_model_calls,
                "budget_exhausted": budget["exhausted"],
                "errors": errors, "calls_detail": call_log,
                "tokens": {"per_call": [c["usage"] for c in call_log if not c["error"]],
                           "totals": usage_totals([c["usage"] for c in call_log if not c["error"]])},
            },
            "notes": NOTES,
        }
        files = write_outputs(results, summary, args.out_dir)

        plan["status"] = "ok"
        plan["degraded"] = bool(errors)
        plan["events_processed"] = len(results)
        plan["events_skipped"] = skipped
        plan["stance_distribution"] = stance_counts
        plan["method_distribution"] = method_counts
        plan["supported_claim_total"] = summary["supported_claim_total"]
        plan["dropped_claim_total"] = summary["dropped_claim_total"]
        plan["drop_reasons"] = drop_reasons
        plan["model"] = {"enabled": not args.no_model, "calls": budget["used"],
                         "max_calls": args.max_model_calls, "budget_exhausted": budget["exhausted"],
                         "errors": errors, "tokens": summary["model"]["tokens"]["totals"]}
        plan["outputs"] = files
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except AnnotationError as exc:
        return emit_error(exc, context)
    except analysis.ProductionHTTPError as exc:
        # 兜底：模型阶段外的意外 HTTP 失败也保留真实状态码与响应体前 200 字，不重试。
        return emit_error(AnnotationError("http", str(exc), 3,
                                          {"status": exc.status, "body_prefix": exc.body_prefix}), context)
    finally:
        if db_handle is not None:
            db_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
