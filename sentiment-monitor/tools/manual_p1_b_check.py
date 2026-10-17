"""P1-B 定向隔离检查：观察清单生命周期 + 同帖真实观测历史与比较规则。

范围（一次性、小范围、隔离）：
  * 真实 `storage` + 真实 `server.Handler` 在**回环临时端口**上有限启停；所有写入都落在
    `--evidence-dir` 新建隔离目录内，绝不接触真实 `sentiment.db`、凭据、配置或 vendor。
  * 最多 3 个**明确标注为合成**的 identity；时刻与计数只是夹具，不是任何来源实测值。
  * 只覆盖定向断言：30/60 分钟速率、重复同刻同值合并、乱序导入排序、同刻不同计数冲突、
    unknown/approximate、回撤（负值）、source_changed、单点等待、零速率、日期窗口跨前点、
    观察周期（加入/重复/退出/到期后显式重加保留旧周期/活跃数自然≤3）、strict query/body、
    同源拒写与各错误状态、全库 3 身份上限（第 4 身份真实导入被拒）。
    只使用 A/B/C 三条真实 evidence 身份；不再伪造第 4 身份或重复活跃周期来强行触达
    “第 4 个活跃观察” cap 分支（合法状态下身份总数≤3，该分支无法由纯 API 状态自然到达）。
  * 不跑全量套件、不做压力/长时间测试、不清理产物、失败后保留原始 stdout/stderr。

用法：`python tools/manual_p1_b_check.py --limit 3 --evidence-dir <不存在的新目录>`
本脚本自身不启动产品 8790，不联网、不调用模型。它不代表产品验收或人工接收。
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import storage  # noqa: E402

FIXTURE_MARKER = "隔离合成样本（不是真实来源）"
GROUP_LABEL = "隔离合成分组"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def sr(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def rec(pid, observed_at, value, precision="exact", field="stat.reply"):
    return {
        "platform": "fixture.invalid", "platform_id": pid,
        "url": f"https://example.invalid/{pid}",
        "title": f"{FIXTURE_MARKER} {pid}",
        "source_name": FIXTURE_MARKER,
        "evidence_kind": "title_only", "content": "",
        "observed_at": observed_at, "published_at": None,
        "source_response_sha256": sr(pid, observed_at, value, precision, field),
        "comment_count": {"value": value, "precision": precision, "source_field": field},
        "other_counts": {}, "group_label": GROUP_LABEL,
    }


def raises(exc_types, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_types as exc:
        return True, f"{type(exc).__name__}: {exc}"
    return False, "no exception"


class Check:
    def __init__(self, name):
        self.name = name
        self.ok = True
        self.detail = {}

    def expect(self, label, condition, value=None):
        self.detail[label] = value if value is not None else bool(condition)
        if not condition:
            self.ok = False
        return condition

    def as_dict(self):
        return {"check": self.name, "status": "PASS" if self.ok else "FAIL", "detail": self.detail}


def latest_evidence(db, pid):
    return dict(db.execute("SELECT * FROM p1_evidence WHERE platform_id=? ORDER BY observed_at DESC, imported_at DESC, id DESC",
                           (pid,)).fetchone())


def utc_iso(value):
    """夹具期望值统一走真实归一（+08:00 → UTC），避免手写时区换算错误。"""
    return storage.publication(value)[0]


def points_of(db, evidence_id, **kwargs):
    return storage.p1_history(db, evidence_id, **kwargs)["points"]


def group_history(db, run):
    """同帖历史与比较规则（30/60 分钟、重复、乱序、冲突、unknown/approx、回撤、口径、单点、0、窗口跨前点）。"""
    out = []
    # --- A：多时点、30/60 分钟、重复同刻同值、回撤、unknown、口径变化、zero ---
    a = "fixture-A"
    for record in (
        rec(a, "2026-10-10T00:00:00+08:00", 100),
        rec(a, "2026-10-10T00:30:00+08:00", 130),
        rec(a, "2026-10-10T01:30:00+08:00", 190),
    ):
        storage.p1_import(db, run, [record], 3)
    c = Check("history_A_30_60min_rates")
    pts = points_of(db, latest_evidence(db, a)["id"])
    rates = [(p["comparison"], p["interval_seconds"], p["delta"], p["rate_per_hour"]) for p in pts]
    c.expect("points", len(pts) == 3, len(pts))
    c.expect("first_single_point", pts[0]["comparison"] == "single_point" and pts[0]["interval_seconds"] is None, pts[0]["comparison"])
    c.expect("30min_rate_60", pts[1]["comparison"] == "comparable" and pts[1]["interval_seconds"] == 1800
             and pts[1]["delta"] == 30 and abs(pts[1]["rate_per_hour"] - 60.0) < 1e-9, rates[1])
    c.expect("60min_rate_60", abs(pts[2]["rate_per_hour"] - 60.0) < 1e-9 and pts[2]["interval_seconds"] == 3600, rates[2])
    c.expect("observed_raw_preserved", pts[0]["observed_raw"] == "2026-10-10T00:00:00+08:00", pts[0]["observed_raw"])
    out.append(c)

    # 重复：同 UTC 时刻、同计数、同 source_field 的新快照 → 合并引用，不新增时间桶。
    c = Check("history_same_instant_same_count_merges")
    before = len(points_of(db, latest_evidence(db, a)["id"]))
    replay = rec(a, "2026-10-10T01:30:00+08:00", 190)
    replay["source_response_sha256"] = sr("replay-bucket", a)
    added = storage.p1_import(db, run, [replay], 3)["added"]
    pts = points_of(db, latest_evidence(db, a)["id"])
    bucket = [p for p in pts if p["observed_at"] == utc_iso("2026-10-10T01:30:00+08:00")][0]
    c.expect("new_row_appended", added == 1, added)
    c.expect("bucket_count_unchanged", len(pts) == before, {"before": before, "after": len(pts)})
    c.expect("bucket_references_both", len(bucket["evidence_ids"]) == 2, bucket["evidence_ids"])
    c.expect("bucket_not_double_counted", bucket["comment_count"]["value"] == 190 and bucket["delta"] == 60, bucket)
    out.append(c)

    # 回撤：计数减少 → retraction + 负变化，不叫增长。
    c = Check("history_retraction_negative")
    storage.p1_import(db, run, [rec(a, "2026-10-10T02:00:00+08:00", 150)], 3)
    pts = points_of(db, latest_evidence(db, a)["id"])
    last = pts[-1]
    c.expect("retraction", last["comparison"] == "retraction" and last["delta"] == -40 and last["rate_per_hour"] < 0,
             {"comparison": last["comparison"], "delta": last["delta"], "rate": last["rate_per_hour"]})
    out.append(c)

    # unknown 不补 0、不参与比较；后续 exact 不把 unknown 当作比较前值（跨过等待点）。
    c = Check("history_unknown_then_exact")
    storage.p1_import(db, run, [rec(a, "2026-10-10T03:00:00+08:00", None, "unknown")], 3)
    storage.p1_import(db, run, [rec(a, "2026-10-10T04:00:00+08:00", 160, "exact", "stat.other")], 3)
    pts = points_of(db, latest_evidence(db, a)["id"])
    unknown, changed = pts[-2], pts[-1]
    c.expect("unknown_point", unknown["comparison"] == "unknown" and unknown["comment_count"]["value"] is None
             and unknown["rate_per_hour"] is None and unknown["delta"] is None, unknown)
    c.expect("source_changed", changed["comparison"] == "source_changed" and changed["rate_per_hour"] is None,
             changed["comparison"])
    out.append(c)

    # 零速率：同口径、计数不变 → comparable 且 rate == 0（不是 unknown、不是 single_point）。
    c = Check("history_zero_rate_not_unknown")
    storage.p1_import(db, run, [rec(a, "2026-10-10T05:00:00+08:00", 160, "exact", "stat.other")], 3)
    pts = points_of(db, latest_evidence(db, a)["id"])
    z = pts[-1]
    c.expect("zero_rate", z["comparison"] == "comparable" and z["delta"] == 0 and z["rate_per_hour"] == 0.0
             and z["interval_seconds"] == 3600, z)
    out.append(c)

    # approximate 不参与数值比较、不补 0。
    c = Check("history_approximate")
    storage.p1_import(db, run, [rec(a, "2026-10-10T06:00:00+08:00", 180, "approximate", "stat.other")], 3)
    pts = points_of(db, latest_evidence(db, a)["id"])
    approx = pts[-1]
    c.expect("approximate_not_compared", approx["comparison"] == "approximate" and approx["rate_per_hour"] is None
             and approx["comment_count"]["value"] == 180, approx)
    out.append(c)

    # 加一个更晚的 exact（使 A 的最新观测为精确值，供 overview 排名与 Top2 掉榜使用）。
    storage.p1_import(db, run, [rec(a, "2026-10-10T07:00:00+08:00", 170, "exact", "stat.other")], 3)

    # --- 日期窗口跨前点：窗口首点使用窗口外真实前值，保留区间信息 ---
    c = Check("history_window_uses_out_of_window_previous")
    a_id = latest_evidence(db, a)["id"]
    windowed = points_of(db, a_id, from_raw="2026-10-10T00:30:00+08:00", to_raw="2026-10-10T01:30:00+08:00")
    c.expect("window_point_count", len(windowed) == 1, [p["observed_at"] for p in windowed])
    w = windowed[0]
    c.expect("window_first_comparable_with_previous_outside",
             w["comparison"] == "comparable" and w["previous_observed_at"] == utc_iso("2026-10-10T00:00:00+08:00")
             and w["interval_seconds"] == 1800, {"comparison": w["comparison"],
                                                "previous": w["previous_observed_at"],
                                                "seconds": w["interval_seconds"]})
    out.append(c)

    # --- 分页：稳定且 has_more 真实 ---
    c = Check("history_stable_pagination")
    page1 = storage.p1_history(db, a_id, None, None, 2, 0)
    page2 = storage.p1_history(db, a_id, None, None, 2, 2)
    full = storage.p1_history(db, a_id, None, None, 500, 0)
    c.expect("page_sizes", len(page1["points"]) == 2 and len(page2["points"]) == 2 and len(full["points"]) >= 8,
             {"p1": len(page1["points"]), "p2": len(page2["points"]), "full": len(full["points"])})
    c.expect("has_more_true", page1["has_more"] is True, page1["has_more"])
    c.expect("pages_contiguous",
             [p["observed_at"] for p in page1["points"] + page2["points"]] ==
             [p["observed_at"] for p in full["points"][:4]],
             [p["observed_at"] for p in page1["points"] + page2["points"]])
    c.expect("no_more_at_end", full["has_more"] is False, full["has_more"])
    out.append(c)

    # --- B：同刻冲突阻断后续借用；C：乱序导入按观测时间排序 ---
    b = "fixture-B"
    for record in (rec(b, "2026-10-09T00:00:00+08:00", 200), rec(b, "2026-10-09T01:00:00+08:00", 210),
                   rec(b, "2026-10-09T02:00:00+08:00", 220)):
        storage.p1_import(db, run, [record], 3)
    storage.p1_import(db, run, [rec(b, "2026-10-09T02:00:00+08:00", 999)], 3)
    storage.p1_import(db, run, [rec(b, "2026-10-09T03:00:00+08:00", 240)], 3)
    c = Check("history_same_time_conflict_blocks_chain")
    pts = points_of(db, latest_evidence(db, b)["id"])
    conflict = pts[2]
    after = pts[3]
    c.expect("conflict_bucket", conflict["comparison"] == "same_time_conflict" and len(conflict["evidence_ids"]) == 2
             and conflict["rate_per_hour"] is None and conflict["delta"] is None, conflict)
    c.expect("after_conflict_is_single_point", after["comparison"] == "single_point" and after["previous_observed_at"] is None,
             {"comparison": after["comparison"], "previous": after["previous_observed_at"], "value": after["comment_count"]["value"]})
    out.append(c)

    c = Check("history_out_of_order_sorted_by_observed_at")
    cid = "fixture-C"
    storage.p1_import(db, run, [rec(cid, "2026-10-08T01:00:00+08:00", 320)], 3)   # 后到的更早时间先写入
    storage.p1_import(db, run, [rec(cid, "2026-10-08T00:00:00+08:00", 300)], 3)
    pts = points_of(db, latest_evidence(db, cid)["id"])
    c.expect("sorted_ascending", [p["observed_at"] for p in pts] ==
             [utc_iso("2026-10-08T00:00:00+08:00"), utc_iso("2026-10-08T01:00:00+08:00")],
             [p["observed_at"] for p in pts])
    c.expect("order_rate", pts[1]["comparison"] == "comparable" and abs(pts[1]["rate_per_hour"] - 20.0) < 1e-9, pts[1])
    out.append(c)
    return out


def group_watch(db, port, request):
    """观察周期生命周期 + 原子上限 + 全库身份上限（真实 Handler/回环 HTTP）。"""
    out = []
    a, b, c = "fixture-A", "fixture-B", "fixture-C"
    ids = {pid: latest_evidence(db, pid)["id"] for pid in (a, b, c)}

    # 同源拒写：缺 Origin → 403；多余字段 → 400；空原因 → 422；evidence 不存在 → 404。
    chk = Check("watch_write_status_codes")
    status, body = request("POST", "/api/p1/watchlist", {"evidence_id": ids[a], "reason": "夹具加入"}, origin=False)
    chk.expect("missing_origin_403", status == 403, {"status": status, "body": body[:200]})
    status, _ = request("POST", "/api/p1/watchlist", {"evidence_id": ids[a], "reason": "夹具加入", "extra": 1})
    chk.expect("extra_field_400", status == 400, status)
    status, _ = request("POST", "/api/p1/watchlist", {"evidence_id": ids[a], "reason": "   "})
    chk.expect("empty_reason_422", status == 422, status)
    status, _ = request("POST", "/api/p1/watchlist", {"evidence_id": "p1ev-missing", "reason": "夹具加入"})
    chk.expect("evidence_missing_404", status == 404, status)
    status, _ = request("POST", "/api/p1/watchlist", {"watch_id": ids[a], "reason": "字段不符"})
    chk.expect("wrong_fields_400", status == 400, status)
    out.append(chk)

    # 真实加入 A/B/C → 201；重复加入 A → 409 且不延时；活跃 ≤3。
    chk = Check("watch_add_and_duplicate_conflict")
    created = {}
    for pid in (a, b, c):
        status, body = request("POST", "/api/p1/watchlist", {"evidence_id": ids[pid], "reason": f"夹具加入 {pid}（非真实来源）"})
        payload = json.loads(body) if body else {}
        created[pid] = payload.get("watch")
        chk.expect(f"add_{pid}_201", status == 201 and payload.get("watch", {}).get("status") == "active",
                   {"status": status, "watch": created[pid]})
    status, body = request("POST", "/api/p1/watchlist", {"evidence_id": ids[a], "reason": "重复加入"})
    chk.expect("duplicate_409", status == 409, {"status": status, "body": body[:200]})
    status, body = request("GET", "/api/p1/watchlist")
    data = json.loads(body)
    chk.expect("active_limit_3", data["active_limit"] == 3, data["active_limit"])
    chk.expect("active_not_over_3", len(data["active"]) == 3 and len(data["history"]) == 3,
               {"active": len(data["active"]), "history": len(data["history"])})
    observed_list = [x["latest_observed_at"] for x in data["active"]]
    chk.expect("recent_observed_desc", observed_list == sorted(observed_list, reverse=True), observed_list)
    out.append(chk)

    # 退出：200 → 已退出；再次退出 409；不存在 404；历史保留；显式重加产生新周期且保留旧周期。
    chk = Check("watch_exit_and_readd")
    b_watch = created[b]["watch_id"]
    status, body = request("POST", "/api/p1/watchlist/exit", {"watch_id": b_watch, "reason": "夹具人工退出"})
    chk.expect("exit_200_exited", status == 200 and json.loads(body)["watch"]["status"] == "exited", {"status": status, "body": body[:200]})
    status, _ = request("POST", "/api/p1/watchlist/exit", {"watch_id": b_watch, "reason": "再次退出"})
    chk.expect("exit_again_409", status == 409, status)
    status, _ = request("POST", "/api/p1/watchlist/exit", {"watch_id": "p1w-missing", "reason": "不存在"})
    chk.expect("exit_missing_404", status == 404, status)
    status, body = request("POST", "/api/p1/watchlist", {"evidence_id": ids[b], "reason": "夹具重加"})
    readded = json.loads(body).get("watch") if body else {}
    chk.expect("readd_201_new_period", status == 201 and readded.get("watch_id") not in (None, b_watch), {"status": status, "watch_id": readded.get("watch_id")})
    status, body = request("GET", "/api/p1/watchlist")
    data = json.loads(body)
    old = [x for x in data["history"] if x["watch_id"] == b_watch]
    chk.expect("old_period_kept", len(old) == 1 and old[0]["status"] == "exited" and old[0]["exit_reason"],
               old)
    chk.expect("history_len_4", len(data["history"]) == 4, len(data["history"]))
    out.append(chk)

    # 到期（隔离夹具把 expires_at 定向设为过去）+ 真实 POST 显式重加新周期 + 旧过期周期保留。
    # 不再伪造第 4 个身份或重复活跃周期：合法状态下身份总数≤3，独立的“第 4 个活跃观察” cap 分支
    # 无法由纯 API 状态自然到达，故不为覆盖该分支而注入虚构周期行。
    chk = Check("watch_expiry_and_readd")
    with db:
        db.execute("UPDATE p1_watch_periods SET expires_at=? WHERE watch_id=?",
                   ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), created[c]["watch_id"]))
    status, body = request("GET", "/api/p1/watchlist")
    data = json.loads(body)
    expired = [x for x in data["history"] if x["watch_id"] == created[c]["watch_id"]][0]
    chk.expect("expired_derived_from_clock", expired["status"] == "expired", expired["status"])
    chk.expect("expired_not_in_active", all(x["watch_id"] != created[c]["watch_id"] for x in data["active"]),
               [x["watch_id"] for x in data["active"]])
    chk.expect("old_c_period_kept_expired", expired["status"] == "expired", expired["status"])
    # 到期释放了活跃槽位：经真实 Handler 显式重加 C，生成新周期；旧周期仍以 expired 保留。
    status, body = request("POST", "/api/p1/watchlist", {"evidence_id": ids[c], "reason": "夹具到期后显式重加 C（非真实来源）"})
    readded_c = json.loads(body).get("watch") if body else {}
    chk.expect("c_readd_201_new_period", status == 201 and readded_c.get("watch_id") not in (None, created[c]["watch_id"]),
               {"status": status, "watch_id": readded_c.get("watch_id")})
    status, body = request("GET", "/api/p1/watchlist")
    data = json.loads(body)
    old_c = [x for x in data["history"] if x["watch_id"] == created[c]["watch_id"]]
    chk.expect("old_c_still_expired", len(old_c) == 1 and old_c[0]["status"] == "expired", old_c)
    chk.expect("c_active_again", any(x["watch_id"] == readded_c.get("watch_id") and x["status"] == "active" for x in data["active"]),
               [x["watch_id"] for x in data["active"]])
    # 恰 A/B/C 三条真实身份各保有一个有效活跃周期；活跃数自然不超过上限。
    chk.expect("active_never_over_3", len(data["active"]) <= 3, len(data["active"]))
    chk.expect("three_active_periods", len(data["active"]) == 3, len(data["active"]))
    out.append(chk)

    # Top2 掉榜仍观察 + 可继续追加真实观测；第 4 身份真实导入被全库上限拒绝。
    chk = Check("watch_outlives_topk_and_identity_cap")
    overview = storage.p1_overview(db, 2)
    group_labels = sorted(g["group_label"] for g in overview["groups"])
    top2_ids = [e["evidence_id"] for g in overview["groups"] for e in g["top"]]
    status, body = request("GET", "/api/p1/watchlist")
    active_ids = [x["evidence_id"] for x in json.loads(body)["active"]]
    # watch_item() 返回身份“当前最新真实证据”的 evidence_id，会随新观测更新；这里读取 A 的当前最新 id
    # 并按身份对照，避免拿旧 evidence_id 误判观察身份消失。
    a_now = latest_evidence(db, a)["id"]
    chk.expect("A_off_top2_but_active", a_now not in top2_ids and a_now in active_ids,
               {"a_now": a_now, "top2": top2_ids, "active": active_ids, "groups": group_labels})
    before = len(points_of(db, ids[a]))
    storage.p1_import(db, ROOT_FOR_FIXTURE, [rec(a, "2026-10-10T08:00:00+08:00", 175, "exact", "stat.other")], 3)
    after = points_of(db, ids[a])
    chk.expect("new_observation_appended_while_watched", len(after) == before + 1 and after[-1]["delta"] == 5,
               {"before": before, "after": len(after), "delta": after[-1]["delta"]})
    a_after = latest_evidence(db, a)["id"]
    chk.expect("a_evidence_id_updated", a_after != a_now, {"before": a_now, "after": a_after})
    status, body = request("GET", "/api/p1/watchlist")
    data = json.loads(body)
    chk.expect("still_active_after_new_point", any(x["evidence_id"] == a_after and x["status"] == "active" for x in data["active"]),
               [x["evidence_id"] for x in data["active"]])
    fourth = rec("fixture-D", "2026-10-10T09:00:00+08:00", 10)
    status, body = request("POST", "/api/p1/import", {"records": [fourth]})
    chk.expect("fourth_identity_rejected_409", status == 409, {"status": status, "body": body[:200]})
    total = db.execute("SELECT count(DISTINCT identity_key) FROM p1_evidence").fetchone()[0]
    chk.expect("identity_total_still_3", total == 3, total)
    out.append(chk)
    return out


def group_http_edges(db, port, request):
    """strict query / body、错误状态、vendor 静态白名单、路径遍历拒绝（真实 Handler）。"""
    out = []
    a_id = latest_evidence(db, "fixture-A")["id"]
    chk = Check("history_strict_query")
    cases = {
        "missing_evidence": ("/api/p1/history", 400),
        "unknown_key": (f"/api/p1/history?evidence_id={a_id}&top_k=1", 400),
        "duplicate_key": (f"/api/p1/history?evidence_id={a_id}&evidence_id={a_id}", 400),
        "blank_from": (f"/api/p1/history?evidence_id={a_id}&from=", 400),
        "no_timezone_from": (f"/api/p1/history?evidence_id={a_id}&from=2026-10-10T00:00:00", 422),
        "bad_limit": (f"/api/p1/history?evidence_id={a_id}&limit=0", 422),
        "bad_offset": (f"/api/p1/history?evidence_id={a_id}&offset=-1", 400),
        "from_after_to": (f"/api/p1/history?evidence_id={a_id}&from=2026-10-11T00:00:00%2B08:00&to=2026-10-10T00:00:00%2B08:00", 422),
        "notfound": ("/api/p1/history?evidence_id=p1ev-missing", 404),
    }
    observed = {}
    for name, (path, expected) in cases.items():
        status, _ = request("GET", path)
        observed[name] = {"expected": expected, "actual": status}
        if status != expected:
            chk.ok = False
    chk.detail["statuses"] = observed
    status, body = request("GET", f"/api/p1/history?evidence_id={a_id}&limit=500&offset=0")
    ok = json.loads(body)["filter"]["time_basis"] == "observed_at"
    chk.expect("200_time_basis_observed_at", status == 200 and ok, status)
    out.append(chk)

    chk = Check("static_vendor_whitelist_and_traversal")
    status, body = request("GET", "/vendor/echarts/echarts.min.js")
    chk.expect("vendor_js_200", status == 200 and len(body) > 1000, {"status": status, "len": len(body)})
    status, _ = request("GET", "/vendor/echarts/provenance.json")
    chk.expect("provenance_200", status == 200, status)
    status, _ = request("GET", "/vendor/echarts/../../storage.py")
    chk.expect("traversal_404", status == 404, status)
    status, _ = request("GET", "/vendor/echarts/echarts.min.js.map")
    chk.expect("unknown_vendor_404", status == 404, status)
    out.append(chk)

    chk = Check("memory_compile_and_route_ast")
    for name in ("server.py", "storage.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        try:
            compile(source, name, "exec")
            chk.expect(f"compile_{name}", True, "compile() ok")
        except SyntaxError as exc:
            chk.expect(f"compile_{name}", False, str(exc))
    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    storage_src = (ROOT / "storage.py").read_text(encoding="utf-8")
    index_src = (ROOT / "index.html").read_text(encoding="utf-8")
    chk.expect("watchlist_route", "/api/p1/watchlist" in server_src and "/api/p1/watchlist/exit" in server_src, True)
    chk.expect("history_route", "/api/p1/history" in server_src, True)
    chk.expect("vendor_whitelist_only", "VENDOR_STATIC" in server_src and "/vendor/echarts/echarts.min.js" in server_src, True)
    chk.expect("watch_ddl_idempotent", "CREATE TABLE IF NOT EXISTS p1_watch_periods" in storage_src, True)
    chk.expect("watch_cap_atomic_immediate", "BEGIN IMMEDIATE" in storage_src and "def immediate_write" in storage_src, True)
    # 回归锁：本脚本不得再直接向 p1_watch_periods 注入周期行（此前用于绕 cap 的 ghost 身份）。
    self_src = Path(__file__).read_text(encoding="utf-8")
    chk.expect("no_direct_watch_period_insert", ("INSERT INTO " + "p1_watch_periods") not in self_src, True)
    chk.expect("index_local_vendor_script", 'src="/vendor/echarts/echarts.min.js"' in index_src, True)
    chk.expect("no_cdn_reference", "cdn." not in index_src.lower() and "cdn." not in server_src.lower(), True)
    tree = ast.parse(server_src)
    listed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    listed.add(comp.value)
    chk.detail["routes"] = sorted(listed)
    chk.expect("ast_routes_present", {"/api/p1/watchlist", "/api/p1/history", "/api/p1/overview"} <= listed, sorted(listed))
    out.append(chk)
    return out


ROOT_FOR_FIXTURE = None


def make_request(port):
    def request(method, path, payload=None, origin=True, timeout=10):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if origin:
            req.add_header("Origin", f"http://127.0.0.1:{port}")
        req.add_header("Host", f"127.0.0.1:{port}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
    return request


def main():
    global ROOT_FOR_FIXTURE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, required=True, help="必填；只接受 1..3，首次使用 3")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="必填；必须是不存在的新目录")
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 只接受 1..3；首次使用 --limit 3")
    if args.evidence_dir.exists():
        parser.error("--evidence-dir 必须是不存在的新目录（拒绝覆盖已有内容）")

    started = datetime.now(timezone.utc)
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=False)
    isolation_root = evidence_dir / "root"
    isolation_root.mkdir(parents=True, exist_ok=False)
    ROOT_FOR_FIXTURE = isolation_root
    # 静态路由只服务 ROOT 下固定白名单文件；隔离检查按原字节复制 vendor，用于验证真实 200 与拒绝路径遍历。
    for rel in ("vendor/echarts/echarts.min.js", "vendor/echarts/provenance.json"):
        source = ROOT / rel
        if not source.is_file():
            raise RuntimeError(f"当前 repo 缺少 {rel}，无法建立隔离静态检查")
        target = isolation_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    db_path = evidence_dir / "fixture.db"
    report = {"task_id": "gender-ui-v7-b-check-20261016", "status": "PASS",
              "scope": "isolated_local_fixture_only", "product_acceptance": "BLOCKED", "gui_acceptance": False,
              "real_db_touched": False, "network_requests": 0, "tests_limit": args.limit,
              "started_at": started.isoformat(), "evidence_dir": str(evidence_dir), "groups": {}}

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            raise AssertionError(f"隔离检查禁止对外网络访问：{host}")
        return real_connect(self, address)

    socket.socket.connect = guarded
    try:
        import server  # noqa: E402  真实模块：只为定义 Handler，不启动 __main__
        server.ROOT = isolation_root
        server.DB = db_path

        db = storage.connect(db_path)
        db.close()
        db = storage.connect(db_path)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        server.PORT = port
        server.HOST = "127.0.0.1"
        server.LIMIT = 3
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        request = make_request(port)

        groups = [("group1_history_rules", lambda: group_history(db, isolation_root)),
                  ("group2_watch_lifecycle", lambda: group_watch(db, port, request)),
                  ("group3_http_edges_static", lambda: group_http_edges(db, port, request))]
        for index, (name, fn) in enumerate(groups):
            if index >= args.limit:
                report["groups"][name] = [{"check": name, "status": "SKIPPED", "detail": "本 --limit 未执行"}]
                continue
            try:
                report["groups"][name] = [c.as_dict() for c in fn()]
            except Exception:
                report["groups"][name] = [{"check": f"{name}_unexpected_error", "status": "FAIL",
                                           "detail": traceback.format_exc()}]
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        rows = db.execute("SELECT count(*) FROM p1_evidence").fetchone()[0]
        watch_rows = db.execute("SELECT count(*) FROM p1_watch_periods").fetchone()[0]
        report["isolation_row_count"] = rows
        report["isolation_watch_rows"] = watch_rows
        report["port"] = port
        db.close()
    finally:
        socket.socket.connect = real_connect

    failures = [c for group in report["groups"].values() for c in group if c["status"] == "FAIL"]
    if failures:
        report["status"] = "FAIL"
        report["failures"] = [{"check": c["check"], "detail": c.get("detail")} for c in failures]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (evidence_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"status": report["status"], "evidence_dir": str(evidence_dir),
               "summary": {k: {"total": len(v), "failed": sum(c["status"] == "FAIL" for c in v)}
                           for k, v in report["groups"].items()}}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
