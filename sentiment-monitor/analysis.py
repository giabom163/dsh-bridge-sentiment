"""M3 bounded evidence workflow. The production HTTP adapter is implemented but not wired; production stays blocked."""
import json
import math
import os
from pathlib import Path
from statistics import median
import urllib.error
import urllib.request
import storage

RULE = "evidence-ab-v1"
PROMPT = "gender-expression-v1"
MODEL = "not-configured"


def init(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS analysis_jobs(
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL, input_hash TEXT NOT NULL,
        packet TEXT NOT NULL, rule_version TEXT NOT NULL, model_version TEXT NOT NULL,
        status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
        UNIQUE(group_id,input_hash,rule_version,model_version));
      CREATE TABLE IF NOT EXISTS model_requests(
        job_id TEXT NOT NULL REFERENCES analysis_jobs(id), role TEXT NOT NULL CHECK(role IN ('A','B')),
        status TEXT NOT NULL, output TEXT, error TEXT, usage TEXT,
        PRIMARY KEY(job_id,role));
      CREATE TABLE IF NOT EXISTS alert_candidates(
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL, rule_version TEXT NOT NULL,
        level TEXT NOT NULL, metrics TEXT NOT NULL, status TEXT NOT NULL,
        UNIQUE(group_id,rule_version,level));
      CREATE TABLE IF NOT EXISTS review_decisions(
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES analysis_jobs(id),
        decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
    """)


def evidence(db, root, ids, limit):
    if not isinstance(ids, list) or not ids or len(ids) > limit or any(not isinstance(x, str) for x in ids):
        raise ValueError("record_ids 必须为限额内的非空 ID 数组")
    if len(set(ids)) != len(ids):
        raise ValueError("record_ids 不得重复")
    rows = []
    for record_id in sorted(ids):
        r = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if r is None:
            raise ValueError("证据记录不存在")
        path = (Path(root) / r["raw_path"]).resolve()
        if not path.is_relative_to((Path(root) / "snapshots").resolve()):
            raise ValueError("证据快照路径越界")
        raw = path.read_bytes()
        if storage.digest(raw) != r["raw_sha256"]:
            raise ValueError("证据快照哈希不一致")
        snapshot = json.loads(raw)
        for field in ("title", "summary"):
            if storage.normalized_text(snapshot.get(field, "")) != r[field]:
                raise ValueError("证据字段与快照不一致")
        entry = {k: r[k] for k in ("id", "feed_id", "canonical_id", "raw_sha256", "title", "summary", "url", "published_at", "time_quality")}
        # Citation offsets refer to original JSON field text, before HTML/space normalization.
        entry.update(title=snapshot['title'], summary=snapshot.get('summary', ''))
        rows.append(entry)
    return {"records": rows, "coverage": "sample_only", "prompt_version": PROMPT,
            "statistics": {"record_count": len(rows), "canonical_count": len({r['canonical_id'] for r in rows}),
                           "unknown_time_count": sum(r['published_at'] is None for r in rows)}}


def create_job(db, root, group_id, ids, limit):
    if not isinstance(group_id, str) or not group_id.strip() or len(group_id) > 100:
        raise ValueError("group_id 必须是人工指定的非空分组（最多 100 字符）")
    packet = evidence(db, root, ids, limit)
    sha = storage.digest(storage.json_bytes(packet))
    job_id = "analysis-" + storage.digest(storage.json_bytes([group_id, sha, RULE, MODEL]))
    with db:
        db.execute("""INSERT OR IGNORE INTO analysis_jobs VALUES(?,?,?,?,?,?,?, ?,?)""",
                   (job_id, group_id, sha, storage.json_bytes(packet).decode(), RULE, MODEL,
                    "blocked", "adapter_not_configured：生产客户端、认证与模型尚未实证；未发送请求", storage.now()))
    return job(db, job_id)


def job(db, job_id):
    row = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("分析批次不存在")
    value = dict(row)
    value["packet"] = json.loads(value["packet"])
    value["requests"] = [dict(r) for r in db.execute("SELECT * FROM model_requests WHERE job_id=? ORDER BY role", (job_id,))]
    for r in value["requests"]:
        for field in ("output", "usage"):
            r[field] = json.loads(r[field]) if r[field] else None
    value["budget"] = {"maximum": 2, "reserved": len(value["requests"]), "cost": None}
    return value


def citation(packet, ref):
    if not isinstance(ref, dict) or set(ref) != {"record_id", "raw_sha256", "field", "start", "end", "quote"}:
        raise ValueError("引用字段不符合契约")
    record = next((r for r in packet["records"] if r["id"] == ref["record_id"]), None)
    start, end = ref["start"], ref["end"]
    if record is None or ref["raw_sha256"] != record["raw_sha256"] or ref["field"] not in ("title", "summary"):
        raise ValueError("引用记录、哈希或字段无效")
    text = record[ref["field"]]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != ref["quote"]:
        raise ValueError("引用字符区间与原文不一致")


def validate(packet, role, output, a_output=None):
    if not isinstance(output, dict):
        raise ValueError("模型输出必须为对象")
    key = "claims" if role == "A" else "reviews"
    if set(output) != {key} or not isinstance(output[key], list) or not 1 <= len(output[key]) <= 20:
        raise ValueError("模型输出结构或条数不符合契约")
    seen = set()
    for item in output[key]:
        fields = {"id", "text", "target", "context", "citations", "uncertainty"} if role == "A" else {"claim_id", "verdict", "reason", "counterevidence"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("主张或复核字段不符合契约")
        ident = item["id" if role == "A" else "claim_id"]
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("主张 ID 无效或重复")
        seen.add(ident)
        refs = item["citations" if role == "A" else "counterevidence"]
        if not isinstance(refs, list) or len(refs) > 20 or (role == "A" and not refs):
            raise ValueError("引用数量无效")
        for ref in refs:
            citation(packet, ref)
        if role == "A":
            if item['target'] not in ("male", "female", "both", "unspecified") or item['context'] not in ("assertion", "quotation", "rebuttal", "uncertain"):
                raise ValueError("对象或引述语境无效")
            if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 4000 for k in ("text", "uncertainty")):
                raise ValueError("主张与不确定项必须为有限非空文本")
        else:
            if item['verdict'] not in ("supported", "partially_supported", "unsupported", "unverifiable") or not isinstance(item['reason'], str) or not item['reason'].strip() or len(item['reason']) > 4000:
                raise ValueError("复核结论或理由无效")
            if item['verdict'] in ("partially_supported", "unsupported") and not refs:
                raise ValueError("质疑主张必须附反例引用")
    if role == "B" and (a_output is None or seen != {c['id'] for c in a_output['claims']}):
        raise ValueError("B 必须逐条复核 A 的全部主张")


def normalize_output(packet, role, output):
    """在 validate() 之前对齐引用偏移；validate() 的判据本身不改。

    实测（产品侧真跑）：模型给出的 citations.quote 是原文逐字子串，但它自算的 start/end 有偏差。
    对每条引用，取对应 record 的 title/summary 原文，用 text.find(quote) 求真实位置；找到就把
    start/end 改写为 (pos, pos+len(quote))，找不到则保持原样，交给 validate() 照旧硬失败——绝不
    放宽“引用必须是原文逐字子串且 text[start:end] == quote”这一证据性要求。
    偏移口径与 citation() 一致：原始快照字段解码后的 Python code point，[start,end)；同一 quote
    在原文出现多次时取首次出现的位置。本函数只对齐偏移，不增删引用字段、不改写 quote。
    """
    key, refs_key = ("claims", "citations") if role == "A" else ("reviews", "counterevidence")
    if not isinstance(output, dict) or not isinstance(output.get(key), list):
        return output
    source = packet.get("records", []) if isinstance(packet, dict) else []
    records = {r["id"]: r for r in source
               if isinstance(r, dict) and isinstance(r.get("id"), str)}
    for item in output[key]:
        if not isinstance(item, dict) or not isinstance(item.get(refs_key), list):
            continue
        for ref in item[refs_key]:
            if not isinstance(ref, dict) or ref.get("field") not in ("title", "summary"):
                continue
            record = records.get(ref.get("record_id"))
            quote = ref.get("quote")
            if record is None or not isinstance(quote, str) or not quote:
                continue
            text = record[ref["field"]]
            if not isinstance(text, str):
                continue
            position = text.find(quote)
            if position >= 0:
                ref["start"], ref["end"] = position, position + len(quote)
    return output


def request_once(db, job_id, role, adapter):
    """Internal adapter seam only. Production API never wires an unverified sender.

    Adapter must issue at most ONE HTTP request, no retries/tools/continuations.
    Reservation commits before sending; uncertain outcomes are never replayed.
    """
    if role not in ("A", "B"):
        raise ValueError("角色无效")
    db.execute("BEGIN IMMEDIATE")
    try:
        current = job(db, job_id)
        requests = current['requests']
        if any(r['role'] == role for r in requests):
            raise ValueError("请求已占用；禁止自动重发")
        if len(requests) >= 2 or any(r['status'] != 'completed' for r in requests):
            raise ValueError("预算耗尽或存在不确定请求")
        a = next((r['output'] for r in requests if r['role'] == 'A'), None)
        if (role == 'A' and requests) or (role == 'B' and a is None):
            raise ValueError("必须先 A 后 B")
        if current['status'] not in ('blocked', 'awaiting_b') or (current['status'] == 'blocked' and requests):
            raise ValueError("当前状态禁止发送")
        db.execute("INSERT INTO model_requests(job_id,role,status) VALUES(?,?,'reserved')", (job_id, role))
        db.execute("UPDATE analysis_jobs SET status='running',error=NULL WHERE id=?", (job_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    try:
        output, usage = adapter(role, current['packet'], a)
    except Exception as exc:
        with db:
            db.execute("UPDATE model_requests SET status='uncertain',error=? WHERE job_id=? AND role=?", (str(exc)[:2000], job_id, role))
            db.execute("UPDATE analysis_jobs SET status='blocked',error='request_outcome_uncertain' WHERE id=?", (job_id,))
        raise
    try:
        output = normalize_output(current['packet'], role, output)
        validate(current['packet'], role, output, a)
        state = 'awaiting_b' if role == 'A' else ('human_review' if all(r['verdict'] == 'supported' for r in output['reviews']) else 'blocked')
        error = None if state != 'blocked' else 'review_not_fully_supported'
    except ValueError as exc:
        state, error = 'blocked', str(exc)
    with db:
        db.execute("UPDATE model_requests SET status='completed',output=?,usage=?,error=? WHERE job_id=? AND role=?",
                   (storage.json_bytes(output).decode(), storage.json_bytes(usage).decode(), error, job_id, role))
        db.execute("UPDATE analysis_jobs SET status=?,error=? WHERE id=?", (state, error, job_id))
    return job(db, job_id)


# --- production model adapter: implemented, deliberately NOT wired ---------------------------------
#
# Seam contract: at most ONE real HTTP request per role slot, no retries, no redirect resend, no
# tool/continuation rounds, and never an automatic resend of a reserved or uncertain request.
# Production status therefore stays "blocked / not_configured" until the caller is wired with real
# credential and call evidence; this module only ever holds the environment variable NAME, never a
# credential value, and nothing here runs unless an explicit caller passes this adapter in.

# Endpoint is overridable so offline fixtures can replay a recorded response WITHOUT network or
# credentials (templates/fixture.py serves a local endpoint). Default behavior is unchanged.
PRODUCTION_ENDPOINT_ENV = "OPENCODE_GO_V1_ENDPOINT"
PRODUCTION_ENDPOINT = os.environ.get(PRODUCTION_ENDPOINT_ENV) or "https://opencode.ai/zen/go/v1/chat/completions"
# Model id: two different shapes exist and they are NOT interchangeable over raw HTTP.
#   * "opencode-go/deepseek-v4.1-flash" is only the provider-qualified name used on the local
#     OpenCode/bridge side (the task contract's `model` field is written in that form). The OpenCode
#     CLI strips the "<provider>/" prefix before it issues the request, so the wire never sees it.
#   * "deepseek-v4.1-flash" is the bare id the gateway itself accepts. This module speaks raw HTTP
#     (urllib) with no CLI in front of it, so it must send the bare id.
# Measured, two independent calls: with the "opencode-go/" prefix the gateway answers 400
# {"error":{"type":"server_error","message":"Upstream request failed: Model is unavailable."}},
# while the bare id answers 200. This module therefore uses the bare id: the default below, or
# OPENCODE_GO_V1_MODEL when that environment variable is set (so the model can be swapped without a
# code edit). The prefix is never re-added here.
PRODUCTION_MODEL = "deepseek-v4.1-flash"
PRODUCTION_MODEL_ENV = "OPENCODE_GO_V1_MODEL"
# Credential is read from the environment only; the value is never stored in the repository.
PRODUCTION_KEY_ENV = "OPENCODE_GO_V1_API_KEY"
# Cloudflare answers a non-browser User-Agent with 403 error code 1010.
PRODUCTION_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
# Without x-opencode-session the endpoint answers 400 MissingSessionID; a stable value is enough.
PRODUCTION_SESSION = "sentiment-s3-annotate-adapter"
PRODUCTION_TIMEOUT = 90
PRODUCTION_MAX_RESPONSE = 2_000_000
PRODUCTION_ERROR_BODY = 200


class ProductionHTTPError(RuntimeError):
    """A production HTTP attempt failed; the real status code and the first 200 characters of the
    response body are preserved. The failed attempt is never replayed automatically."""

    def __init__(self, status, body):
        self.status = status
        self.body_prefix = (body or "")[:PRODUCTION_ERROR_BODY]
        super().__init__(f"HTTP {status}: {self.body_prefix}")


def production_messages(role, packet, a_output):
    """One system rule block plus one JSON user payload; no tools, no multi-turn scaffold."""
    if role == "A":
        rules = ("你是性别议题证据标注器。只分析文本明确讨论的对象，不推断作者性别，不接受模型提供的统计数字。"
                 "只输出一个 JSON 对象，且仅含一个键 claims，值为 1–20 条主张。"
                 "每条主张严格为 id,text,target,context,citations,uncertainty。"
                 "target 取 male/female/both/unspecified；context 取 assertion/quotation/rebuttal/uncertain。"
                 "citations 为 {record_id,raw_sha256,field,start,end,quote} 数组，field 取 title/summary，"
                 "quote 必须是该字段原文的逐字子串，start/end 为其字符区间。")
    else:
        rules = ("你是性别议题主张复核器。只输出一个 JSON 对象，且仅含一个键 reviews，必须逐条覆盖 A 的全部主张。"
                 "每条复核严格为 claim_id,verdict,reason,counterevidence。"
                 "verdict 取 supported/partially_supported/unsupported/unverifiable；"
                 "partially_supported 或 unsupported 必须附反例引用，引用结构同 A。")
    payload = {"role": role, "packet": packet}
    if a_output is not None:
        payload["a_output"] = a_output
    return [{"role": "system", "content": rules},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def production_adapter(role, packet, a_output=None):
    """Minimal usable production client for one role slot: (output, usage) or a real exception.

    Credential comes from the environment variable PRODUCTION_KEY_ENV only. Returns the parsed single
    JSON object plus the server-provided usage verbatim (possibly None). Citation offsets are aligned
    later by normalize_output(), right before validate().
    """
    if role not in ("A", "B"):
        raise ValueError("角色无效")
    credential = os.environ.get(PRODUCTION_KEY_ENV)
    # Fixture mode is EXPLICIT and off by default: it exists only so an offline fixture server can
    # replay a recorded response without a credential. It must never silently weaken the real path.
    fixture_mode = os.environ.get("OPENCODE_GO_V1_FIXTURE") == "1"
    if not credential and not fixture_mode:
        raise RuntimeError(
            f"缺少环境变量 {PRODUCTION_KEY_ENV}；未发送任何请求"
            "（离线回放请显式设置 OPENCODE_GO_V1_FIXTURE=1 并把端点指向本地夹具）")
    body = json.dumps({
        # Bare id, resolved at call time so OPENCODE_GO_V1_MODEL can override it; never prefixed here.
        "model": os.environ.get(PRODUCTION_MODEL_ENV) or PRODUCTION_MODEL,
        # Fixed: without reasoning_effort=none the model burns the output budget on thinking
        # (measured 10.8s vs 3.5s) and returns non-JSON.
        "reasoning_effort": "none",
        "stream": False,
        "messages": production_messages(role, packet, a_output),
    }, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "User-Agent": PRODUCTION_USER_AGENT,
        "x-opencode-session": "fixture-replay" if fixture_mode else PRODUCTION_SESSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if credential:
        request_headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(PRODUCTION_ENDPOINT, data=body, method="POST",
                                     headers=request_headers)
    # A plain opener, no redirect handler: a 3xx must not silently forward the credential elsewhere.
    # urllib does not retry, so at most one connection attempt reaches the endpoint.
    try:
        with urllib.request.build_opener().open(request, timeout=PRODUCTION_TIMEOUT) as response:
            status = response.status
            raw = response.read(PRODUCTION_MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        raise ProductionHTTPError(exc.code, exc.read(PRODUCTION_MAX_RESPONSE).decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败：{exc.reason}") from exc
    if status != 200:
        raise ProductionHTTPError(status, raw.decode("utf-8", "replace"))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"响应不是合法 JSON 对象：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("响应必须是单个 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("响应缺少 choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    # Strict on purpose: only a single JSON object is accepted; model JSON is never patched.
    if not isinstance(content, str) or not content.strip():
        raise ValueError("响应缺少文本内容")
    try:
        output = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(output, dict):
        raise ValueError("模型输出必须是单个 JSON 对象")
    return output, payload.get("usage")


def recover(db):
    with db:
        db.execute("UPDATE analysis_jobs SET status='blocked',error='request_outcome_uncertain：上次请求未确认，禁止自动重发' WHERE id IN (SELECT job_id FROM model_requests WHERE status='reserved')")
        db.execute("UPDATE model_requests SET status='uncertain',error='服务中断，结果未知' WHERE status='reserved'")


def decide(db, job_id, decision, reason):
    if decision not in ('accept', 'reject') or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("决定需为 accept/reject 且理由非空（最多 2000 字符）")
    with db:
        cursor = db.execute("UPDATE analysis_jobs SET status=? WHERE id=? AND status='human_review'", ('accepted' if decision == 'accept' else 'rejected', job_id))
        if cursor.rowcount != 1:
            raise ValueError("仅完整支持且待人工复核的结论可作决定")
        db.execute("INSERT INTO review_decisions(job_id,decision,reason,created_at) VALUES(?,?,?,?)", (job_id, decision, reason, storage.now()))
    return job(db, job_id)


def heat_candidate(history, current, unknown_ratio):
    """Candidate parameters, not calibrated production thresholds; counts per complete day."""
    if not isinstance(history, list) or len(history) > 14 or any(type(x) is not int or x < 0 for x in history) or type(current) is not int or current < 0:
        raise ValueError("需要至多 14 个完整日非负计数")
    if unknown_ratio is None or not isinstance(unknown_ratio, (int, float)) or not math.isfinite(unknown_ratio) or not 0 <= unknown_ratio <= 1:
        return {"level": "quality", "reason": "unknown_denominator", "comparison_paused": True}
    result = {"current": current, "history": history, "unknown_ratio": unknown_ratio,
              "parameters": {"days": 14, "min_sample": 3, "max_unknown_ratio": 0.2, "mad_factor": 3}, "calibration": "unverified"}
    if unknown_ratio > 0.2:
        return {**result, "level": "quality", "reason": "time_quality", "comparison_paused": True}
    if len(history) < 14:
        return {**result, "level": "insufficient", "reason": "baseline_insufficient"}
    if current < 3 or sum(history) < 3 * 14:
        return {**result, "level": "insufficient", "reason": "low_sample"}
    base = median(history)
    mad = median([abs(x - base) for x in history])
    threshold = base + (3 * mad if mad else max(3, base))
    return {**result, "baseline": base, "mad": mad, "threshold": threshold,
            "level": "heat" if current > threshold else "none", "reason": "mad_zero" if mad == 0 else "mad"}


def overview(db):
    return {"adapter": "not_configured", "production_status": "BLOCKED", "request_budget": 2,
            "cost": None, "rule_version": RULE, "prompt_version": PROMPT,
            "jobs": [job(db, r[0]) for r in db.execute("SELECT id FROM analysis_jobs ORDER BY created_at DESC LIMIT 100")],
            "alerts": [dict(r) for r in db.execute("SELECT * FROM alert_candidates ORDER BY rowid DESC LIMIT 100")],
            "warning": "人工指定分组，不是自动事件聚类；候选阈值未校准；不发送通知"}


def candidate(db, job_id):
    current = job(db, job_id)
    stats = current['packet']['statistics']
    # Collection coverage is not attested for any complete day yet. Never invent
    # 14 zero days from absence of records; no rate comparison can be enabled.
    metrics = heat_candidate([], stats['canonical_count'], stats['unknown_time_count'] / stats['record_count'])
    metrics['denominator'] = stats['record_count']
    metrics['grouping'] = 'manual'
    key = storage.digest(storage.json_bytes([current['group_id'], RULE, metrics['level']]))
    with db:
        db.execute("INSERT OR IGNORE INTO alert_candidates VALUES(?,?,?,?,?,?)",
                   (key, current['group_id'], RULE, metrics['level'], storage.json_bytes(metrics).decode(), 'unreviewed'))
    return dict(db.execute("SELECT * FROM alert_candidates WHERE id=?", (key,)).fetchone())
