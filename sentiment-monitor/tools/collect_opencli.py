#!/usr/bin/env python
"""S1 只读采集器：用本机已装的 OpenCLI 取公开热榜/推荐流，按声明式映射表投影为 records 字段。

当前端点（全部 access=read 的公开榜单/推荐流，单次一次调用、无分页、无重试）：
  - toutiao hot / toutiao recommend
  - weibo hot / tieba hot / 36kr hot
  - zhihu hot / bilibili hot / xiaohongshu feed（本轮新增；xiaohongshu feed 是**推荐流**，不是热榜）
  - douyin（hashtag hot / search）当前不可用，**未入表**，原文见 docs/S1-collector.md

限流（本轮新增，做进采集器本体，不再靠人手工 sleep）：
  - `--min-interval`：同一平台两次采集的最小间隔（默认 1800 秒）；
  - `--gap-seconds`：一轮内平台之间的错峰间隔（默认 25 秒）；
  - `--max-per-run`：一轮总量上限（默认 80 条）；
  - 平台级冷却账：`<平台> -> 上次采集时间/上次结果` 持久化到 JSON
    （默认 `<仓库根>/.collector-throttle.json`，`--throttle-file` 可覆盖）；
    启动时读账，未到冷却时间的平台**直接跳过并如实记 reason=throttled**；
    只有真正发起采集（非 `--dry-run`）才更新该账；
  - 风控信号熔断：非 0 退出码 / `BROWSER_CONNECT` / `AUTH_REQUIRED` / `COMMAND_EXEC` /
    空数组 / HTTP 429·5xx 出现即停止该平台且不重试，保留真实错误码与原文；
  - `--dry-run` **不打开/创建数据库**、**不写节流账**（只读账）。

复用优先（见 docs/S1-collector.md 的“复用清单与不新写的理由”）：
  - 取数：OpenCLI 站点适配器 `opencli <site> <cmd>`，只调用公开热榜/推荐流读取命令，
    无写动作、无登录、不复制 cookie。
  - 写库与快照：只调用 storage.py 既有函数 connect/start_run/import_batch/save_snapshot。
    不新建数据库、不新建表、不改表结构。
  - 情感粗筛：复用 server.py 既有 classify 词典函数（未复核初筛，不代表准确情感分析）。
本文件自己写的只有：CLI 定位、声明式字段映射表（ENDPOINTS）、参数解析、节流账与错误呈现。

只读硬约束：argv 由 ENDPOINTS 常量拼装，不接受任意子命令，因此不存在发布/评论/关注/删除路径。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402  复用已复核的粗略情感词典 classify
import storage  # noqa: E402  复用项目证据库既有函数

COLLECTOR = "s1-opencli"
DEFAULT_LIMIT = 3
MAX_LIMIT = 50  # 契约：单次 ≤3 页或 ≤50 条；OpenCLI 自身 limit 上限也是 50
DEFAULT_TIMEOUT = 60
DEFAULT_DB = ROOT / "sentiment.db"

# 限流默认值（本轮新增；均为程序机制，不依赖人工 sleep）
DEFAULT_MIN_INTERVAL = 1800   # 秒：同一平台两次采集的最小间隔
DEFAULT_GAP_SECONDS = 25      # 秒：一轮内平台之间的错峰间隔
DEFAULT_MAX_PER_RUN = 80      # 条：一轮总量上限
DEFAULT_THROTTLE_FILE = ROOT / ".collector-throttle.json"
THROTTLE_VERSION = 1

# 唯一允许的读端点：声明式映射表。argv 只由这里拼装，调用方无法传入其它子命令。
#
# 每行字段含义（逐源声明，映射逻辑里没有任何 if/else 分支）：
#   argv                              opencli 子命令及固定参数；access=read，只读公开列表
#   platform                          平台级冷却键（同一平台的多个端点共用一条冷却记录）
#   feed_url                          来源分组标识，落 feeds.url（见 docs/S1-collector.md 的未验证项）
#   default_group                     默认分组名，落 feeds.name
#   fields                            本地 records 字段 -> 上游原始字段名：
#                                     None = 该端点没有这个字段，一律留 null/空串，
#                                     不从 URL、标题或其它字段反推（不猜）
#   summary_empty_when_same_as_title  summary 与 title 完全相同则置空（头条 hot 的 query 就是标题）
#
# 上游原始字段名：头条沿用首轮实测；其余端点为 2026-10-16 实测
# （npm @jackwener/opencli 1.8.8，`-f json`）：
#   头条 hot:        rank/title/query/group_id/hot_value/label/image_url
#   头条 recommend:  rank/title/abstract/group_id/published_at/comments/source/tag/image_url
#   微博 hot:        rank/word/category/hot_value/label/url（**没有 title 字段，标题在 word**）
#   贴吧 hot:        rank/title/description/discussions/url（**没有发布时间**）
#   36氪 hot:        rank/title/url（**实测只有这三个字段**）
#   知乎 hot:        rank/title/heat/answers/url（**无摘要字段**）
#   B站 hot:         rank/title/author/play/danmaku/bvid/url（**无摘要、无发布时间**）
#   小红书 feed:      id/title/type/author/likes/url（**推荐流，无摘要、无发布时间**）
ENDPOINTS = {
    "hot": {
        "argv": ["toutiao", "hot"],
        "platform": "toutiao",
        "feed_url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "default_group": "toutiao-hot",
        "fields": {"title": "title", "url": "url", "summary": "query",
                   "source_id": "group_id", "published_at": None},
        "summary_empty_when_same_as_title": True,
    },
    "recommend": {
        "argv": ["toutiao", "recommend"],
        "platform": "toutiao",
        "feed_url": "https://www.toutiao.com/api/pc/feed/",
        "default_group": "toutiao-recommend",
        "fields": {"title": "title", "url": "url", "summary": "abstract",
                   "source_id": "group_id", "published_at": "published_at"},
        "summary_empty_when_same_as_title": False,
    },
    "weibo-hot": {
        "argv": ["weibo", "hot"],
        "platform": "weibo",
        "feed_url": "https://s.weibo.com/top/summary",
        "default_group": "weibo-hot",
        "fields": {"title": "word", "url": "url", "summary": "category",
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "tieba-hot": {
        "argv": ["tieba", "hot"],
        "platform": "tieba",
        "feed_url": "https://tieba.baidu.com/hottopic/browse/topicList",
        "default_group": "tieba-hot",
        "fields": {"title": "title", "url": "url", "summary": "description",
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "36kr-hot": {
        "argv": ["36kr", "hot"],
        "platform": "36kr",
        "feed_url": "https://36kr.com/hot-list/catalog",
        "default_group": "36kr-hot",
        "fields": {"title": "title", "url": "url", "summary": None,
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "zhihu-hot": {
        "argv": ["zhihu", "hot"],
        "platform": "zhihu",
        "feed_url": "https://www.zhihu.com/hot",
        "default_group": "zhihu-hot",
        # 上游只有 rank/title/heat/answers/url：没有摘要、没有独立 id 字段（问题号在 url 里，
        # 按“不从 URL 反推”的既有约定不取），没有发布时间。
        "fields": {"title": "title", "url": "url", "summary": None,
                   "source_id": None, "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "bilibili-hot": {
        "argv": ["bilibili", "hot"],
        "platform": "bilibili",
        "feed_url": "https://www.bilibili.com/v/popular/all",
        "default_group": "bilibili-hot",
        # bvid 是上游明确的稿件 ID 字段，直接取；author/play/danmaku 不是摘要也不进计数列。
        "fields": {"title": "title", "url": "url", "summary": None,
                   "source_id": "bvid", "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
    "xiaohongshu-feed": {
        "argv": ["xiaohongshu", "feed"],
        "platform": "xiaohongshu",
        "feed_url": "https://www.xiaohongshu.com/explore",
        "default_group": "xiaohongshu-feed",
        # 注意：这是**推荐流**（首页 Feed），不是热榜；名字与文档都如实标注。
        # id 是上游笔记 ID，直接取；type/author/likes 无本地对应列（likes 是点赞展示值，
        # 不是评论数，不得冒充评论总数）。
        "fields": {"title": "title", "url": "url", "summary": None,
                   "source_id": "id", "published_at": None},
        "summary_empty_when_same_as_title": False,
    },
}

# 本地 schema 里必须有值、缺失即报错的字段（不静默丢弃，不用其它字段顶替）。
REQUIRED_FIELDS = ("title", "url")

# 风控/熔断信号：出现任一即停止该平台且不重试，保留真实错误码与原文。
RISK_CODES = {"BROWSER_CONNECT", "AUTH_REQUIRED", "COMMAND_EXEC", "EMPTY_RESULT", "LOGIN_WALL"}
RISK_TEXT_PATTERNS = (
    ("BROWSER_CONNECT", re.compile(r"BROWSER_CONNECT")),
    ("AUTH_REQUIRED", re.compile(r"AUTH_REQUIRED")),
    ("COMMAND_EXEC", re.compile(r"COMMAND_EXEC")),
    ("EMPTY_RESULT", re.compile(r"EMPTY_RESULT")),
    ("HTTP_429", re.compile(r"\b429\b")),
    ("HTTP_5XX", re.compile(r"\b(?:HTTP|status|statusCode)[ _:\"-]*5\d\d\b", re.IGNORECASE)),
)


def _validate_endpoints():
    """声明式映射表的完整性自检：少键/多键都在导入时立刻报错，而不是运行到一半才 KeyError。"""
    expected = {"argv", "platform", "feed_url", "default_group", "fields",
                "summary_empty_when_same_as_title"}
    for name, spec in ENDPOINTS.items():
        if set(spec) != expected:
            raise RuntimeError(f"端点 {name} 的声明键不完整：{sorted(spec)}")
        if set(spec["fields"]) != {"title", "url", "summary", "source_id", "published_at"}:
            raise RuntimeError(f"端点 {name} 的 fields 键不完整：{sorted(spec['fields'])}")
        if not all(spec["fields"].get(key) for key in REQUIRED_FIELDS):
            raise RuntimeError(f"端点 {name} 必须声明 {'/'.join(REQUIRED_FIELDS)} 的上游字段")
        if not isinstance(spec["platform"], str) or not spec["platform"]:
            raise RuntimeError(f"端点 {name} 必须声明非空 platform（冷却键）")


_validate_endpoints()

# 运行模式说明随实际模式切换（不写死“本轮只跑了 dry-run”，避免在真实写库的那一轮里说反话）。
MODE_NOTE_DRY = ("本轮为 --dry-run：不打开/创建数据库、不写节流账（只读账）；"
                 "真实写库路径未在本轮执行，属未实证项。")
MODE_NOTE_REAL = ("本轮为非 dry-run：节流账按平台更新；有采集结果的端点按 storage.import_batch "
                  "写入证据库与快照（逐端点结果见 results[].run）。")

NOTES = [
    "feed 行沿用既有 feeds 表；records/snapshot 全部经 storage.py 既有函数写入。",
    "上游每次轮询 hot_value / comments 变化会产生新的 raw_sha256，按 UNIQUE(feed_id,raw_sha256) 视为一次新观测。",
    "sentiment 仍是未复核词典粗筛，时间质量由 storage.publication 判定。",
    "多来源字段名互不相同，逐源映射见 ENDPOINTS；上游未提供的字段（发布时间、source_id、摘要）"
    "一律留 null/空串，不从 URL 或标题反推。",
    "冷却账按“平台”记：同一平台的多个端点（如头条 hot / recommend）共用一条记录；"
    "被冷却跳过的平台不发起上游请求，reason=throttled。",
    "风控信号（非 0 退出码 / BROWSER_CONNECT / AUTH_REQUIRED / COMMAND_EXEC / 空数组 / HTTP 429·5xx）"
    "触发即停止本轮，不重试，保留真实错误码与原文。",
]


class CollectError(RuntimeError):
    """带阶段与原始错误文本的失败，便于原文呈现，不静默降级。"""

    def __init__(self, stage, message, exit_code, extra=None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.exit_code = exit_code
        self.extra = extra or {}


def utc():
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value):
    """解析冷却账里的 ISO 时间；无法解析时返回 None（由调用方决定如何处理）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def _force_utf8_stdout():
    """Windows 控制台默认 GBK；上游正文含 emoji 时直接 print 会 UnicodeEncodeError。

    实机缺陷（首轮）:recommend 某条含 U+1F602，未修复前 exit 1 并抛 Traceback。
    这里把标准输出/错误切到 UTF-8（无法编码的字符用 replace 兜底），保证 JSON 一定打得出来。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _first_file(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def node_candidates(explicit):
    out = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("OPENCLI_NODE")
    if env:
        out.append(Path(env))
    found = shutil.which("node")
    if found:
        out.append(Path(found))
    versions = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if versions.is_dir():
        out.extend(sorted((p / "node.exe" for p in versions.iterdir()), reverse=True))
    out.append(Path("C:/Program Files/nodejs/node.exe"))
    return out


def opencli_candidates(explicit):
    rel = Path("node_modules/@jackwener/opencli/dist/src/main.js")
    out = []
    if explicit:
        out.append(Path(explicit))
    env = os.environ.get("OPENCLI_MAIN")
    if env:
        out.append(Path(env))
    profiles = Path.home() / ".dsh" / "profiles"
    if profiles.is_dir():
        out.extend(sorted((p / rel for p in profiles.iterdir()), reverse=True))
    out.append(Path.home() / rel)
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "npm" / rel)
    return out


def risk_signal(exc):
    """把一次失败归一成风控/停止信号标签（无已知信号时返回 None）。

    契约列出的信号：非 0 退出码、BROWSER_CONNECT、AUTH_REQUIRED、COMMAND_EXEC、空数组、HTTP 429/5xx。
    本实现从不重试，因此出现任一信号即“停止该平台”；标签只用于如实记录看到的是哪一类。
    """
    texts = [exc.message or ""]
    code = exc.extra.get("error_code")
    if code:
        texts.append(str(code))
    for key in ("stderr", "stdout_head", "stdout"):
        value = exc.extra.get(key)
        if value:
            texts.append(str(value))
    blob = "\n".join(texts)
    if exc.stage == "empty":
        return "EMPTY_RESULT"
    if exc.stage == "opencli" and "超时" in (exc.message or ""):
        return "TIMEOUT"
    if code in RISK_CODES:
        return str(code)
    for name, pattern in RISK_TEXT_PATTERNS:
        if pattern.search(blob):
            return name
    call = exc.extra.get("call") or {}
    return_code = call.get("return_code")
    if isinstance(return_code, int) and return_code != 0:
        return f"EXIT_CODE_{return_code}"
    return None


def load_throttle(path):
    """读冷却账。文件不存在=空账；存在但不可读/结构不对则报错停止（不静默放行限流）。"""
    if not path.is_file():
        return {"version": THROTTLE_VERSION, "updated_at": None, "platforms": {}}, "absent"
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectError("throttle", f"冷却账不可读，为避免放开限流已停止：{exc}", 7,
                           {"throttle_file": str(path)}) from exc
    if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
        raise CollectError("throttle", "冷却账结构不符合预期（缺少 platforms 对象），为避免放开限流已停止",
                           7, {"throttle_file": str(path)})
    for platform, entry in data["platforms"].items():
        # 条目级同样 fail-closed：平台条目不是对象、或没有可解析的 last_attempt_at，都算坏账，
        # 一律拒绝运行（不能当成“无冷却”而放开限流）。
        if not isinstance(entry, dict) or parse_utc(entry.get("last_attempt_at")) is None:
            raise CollectError("throttle",
                               "冷却账结构不符合预期（平台条目缺少可解析的 last_attempt_at），"
                               "为避免放开限流已停止",
                               7, {"throttle_file": str(path), "platform": str(platform)})
    return data, "loaded"


def save_throttle(path, data):
    """写冷却账：同目录临时文件 + os.replace，避免半截文件被下一次运行当成有效账。"""
    payload = dict(data)
    payload["version"] = THROTTLE_VERSION
    payload["updated_at"] = utc()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def record_attempt(ledger, platform, endpoint, status, signal=None, error=None, returned=None):
    """把一次**真实**采集（含失败）写进账：上次采集时间 + 上次结果。"""
    moment = utc()
    entry = ledger["platforms"].get(platform)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({
        "last_attempt_at": moment,
        "last_status": status,
        "last_endpoint": endpoint,
        "last_signal": signal,
        "last_error": str(error)[:2000] if error else None,
    })
    if returned is not None:
        entry["last_returned_count"] = returned
    if status == "ok":
        entry["last_success_at"] = moment
    ledger["platforms"][platform] = entry
    return entry


def cooldown_of(entry, moment, min_interval):
    """判断平台是否在冷却窗口内：返回 (是否冷却, 上次时间原文, 已过秒数, 剩余秒数)。

    “无记录”（entry 为 None，首次采集）不构成冷却；而“条目存在但不可用”（不是对象、
    或没有可解析的 last_attempt_at）一律按“冷却中”处理，绝不返回“无冷却”。
    后者读账阶段的 `load_throttle` 已直接拒绝运行（exit 7），这里只是不可达的第二道防线。
    """
    if min_interval <= 0:
        return False, None, None, None
    if entry is None:
        # 该平台还没有任何记录（首次采集）：不构成冷却。
        # 注意“条目不存在”与“条目存在但坏”是两回事，后者已由 load_throttle 在读账阶段拒绝运行。
        return False, None, None, None
    if not isinstance(entry, dict):
        # 不可达的第二道防线：条目存在但不是对象，一律按冷却处理，绝不放开限流。
        return True, None, None, min_interval
    last_raw = entry.get("last_attempt_at")
    last = parse_utc(last_raw)
    if last is None:
        # 不可达的第二道防线：读账阶段已拒绝这种账；这里也按冷却处理，不放开限流。
        return True, last_raw, None, min_interval
    elapsed = (moment - last).total_seconds()
    if elapsed < 0:
        # 账里时间是未来时间：保守按冷却中处理，并如实给出负数 elapsed。
        return True, last_raw, elapsed, min_interval
    if elapsed < min_interval:
        return True, last_raw, elapsed, min_interval - elapsed
    return False, last_raw, elapsed, None


def run_opencli(node, main, endpoint, limit, timeout):
    """唯一一次上游调用：read-only 公开端点，无分页、无重试、无并发。

    失败时带出该站点的 endpoint 名、真实退出码与 stderr 原文，不用别的站点数据顶替。
    """
    argv = [str(node), str(main), *ENDPOINTS[endpoint]["argv"], "--limit", str(limit), "-f", "json"]
    started = utc()
    clock = time.monotonic()
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        raise CollectError("opencli", f"opencli 超时（{timeout}s），已停止，不自动重试",
                           3, {"endpoint": endpoint, "argv": argv,
                               "stdout_head": (exc.stdout or "")[:2000]}) from exc
    except OSError as exc:
        raise CollectError("opencli", f"无法启动 opencli：{exc}", 2,
                           {"endpoint": endpoint, "argv": argv}) from exc
    finished = utc()
    call = {"endpoint": endpoint, "argv": argv, "cwd": str(ROOT), "timeout_seconds": timeout,
            "started_at": started, "finished_at": finished,
            "duration_seconds": round(time.monotonic() - clock, 3), "return_code": done.returncode}
    if done.returncode != 0:
        raise CollectError("opencli", f"opencli 退出码非 0：{done.returncode}",
                           3, {"call": call, "stderr": done.stderr.strip()[:4000],
                               "stdout_head": done.stdout.strip()[:4000]})
    text = done.stdout.strip()
    if not text:
        raise CollectError("opencli", "opencli 退出码为 0 但没有输出", 4,
                           {"call": call, "stderr": done.stderr.strip()[:4000]})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectError("parse", f"opencli 输出不是合法 JSON：{exc}", 4,
                           {"call": call, "stdout_head": text[:2000]}) from exc
    if isinstance(payload, dict):
        code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None
        raise CollectError("opencli", f"opencli 返回错误信封：{json.dumps(payload, ensure_ascii=False)[:2000]}",
                           3, {"call": call, "error_code": code})
    if not isinstance(payload, list):
        raise CollectError("parse", "opencli JSON 顶层不是数组", 4,
                           {"call": call, "stdout_head": text[:2000]})
    if not payload:
        # OpenCLI 自身对空列表抛 EmptyResultError（退出码非 0），这里只是第二道防线。
        raise CollectError("empty", "上游返回空列表", 5, {"call": call})
    return payload[:limit], call


def take(item, field_name):
    """按映射表取上游字段原文：字段未声明（None）或上游缺失/非文本时返回 None。

    不填默认值、不从其它字段反推（不猜）；返回的字符串保持既有行为做首尾去空白。
    """
    if field_name is None:
        return None
    value = item.get(field_name)
    return value.strip() if isinstance(value, str) else None


def map_rows(endpoint, items):
    """按声明式映射表把 OpenCLI 条目投影为 records 行 + 供 dry-run 展示的字段预览。

    原始字段名只从 ENDPOINTS[endpoint]["fields"] 读取；缺 title/url 直接报错并附原始条目，
    不静默丢弃；未声明的字段留 null/空串。
    """
    spec = ENDPOINTS[endpoint]
    fields = spec["fields"]
    rows, previews = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CollectError("map", f"第 {index + 1} 条不是对象：{json.dumps(item, ensure_ascii=False)[:500]}", 6)
        title = take(item, fields["title"]) or ""
        url = take(item, fields["url"]) or ""
        if not title or not url:
            raise CollectError("map", f"第 {index + 1} 条缺少 title/url：{json.dumps(item, ensure_ascii=False)[:500]}", 6)
        source_id = take(item, fields["source_id"]) or None
        summary = take(item, fields["summary"]) or ""
        if spec["summary_empty_when_same_as_title"] and summary == title:
            summary = ""
        published_at = take(item, fields["published_at"])  # 上游没有该字段时保持 None，不猜发布时间
        raw_xml = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = {"title": title, "summary": summary, "url": url, "source_id": source_id,
               "published_at": published_at, "raw_xml": raw_xml}
        normalized, quality = storage.publication(published_at)
        previews.append({
            "id": "(写库时由 storage.import_batch 生成 record-<uuid4>)",
            "feed_id": "(写库时按来源分组解析/创建)",
            "canonical_id": "(写库时按 exact-text-v1 精确正文规则分配)",
            "raw_sha256": storage.digest(storage.json_bytes(row)),
            "raw_path": "snapshots/<raw_sha256>.json",
            "title": storage.normalized_text(title),
            "summary": storage.normalized_text(summary),
            "url": item.get(fields["url"]),
            "canonical_url": storage.canonical_url(url),
            "source_id": source_id,
            "published_at": normalized,
            "published_raw": published_at,
            "time_quality": quality,
            "rank": item.get("rank"),
            "raw_sha256_method": "sha256(storage.json_bytes(row))，与 save_snapshot 落盘字节一致",
        })
        rows.append(row)
    return rows, previews


def ensure_feed(db, group_id, feed_url):
    """按 url 复用既有 feeds 行；缺失时用与 server.py 相同的 INSERT 语句创建（不改表结构）。"""
    existing = db.execute("SELECT id FROM feeds WHERE url=?", (feed_url,)).fetchone()
    if existing:
        return existing["id"], "reused"
    with db:
        cursor = db.execute("INSERT INTO feeds(name,url,added_at) VALUES(?,?,?)",
                            (group_id[:100], feed_url, storage.now()))
    return cursor.lastrowid, "created"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", action="append", choices=sorted(ENDPOINTS), default=None,
                        help="读端点（白名单，可重复传多次以在一轮内采多个平台）："
                             + " / ".join(f"{name}" for name in sorted(ENDPOINTS))
                             + "（默认 hot：头条热榜）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"每个端点条数 1-{MAX_LIMIT}（默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只采集并打印映射结果：不打开/创建数据库，也不写节流账（只读账）")
    parser.add_argument("--group-id", default=None,
                        help="来源分组标识（落 feeds.name），仅单端点运行可传；默认按端点取 "
                             + " / ".join(f"{name}={ENDPOINTS[name]['default_group']}" for name in sorted(ENDPOINTS)))
    parser.add_argument("--min-interval", type=int, default=DEFAULT_MIN_INTERVAL,
                        help=f"同一平台两次采集的最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}；0=关闭冷却）")
    parser.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS,
                        help=f"一轮内平台之间的错峰间隔秒数（默认 {DEFAULT_GAP_SECONDS}）")
    parser.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN,
                        help=f"一轮总量上限条数（默认 {DEFAULT_MAX_PER_RUN}）")
    parser.add_argument("--throttle-file", type=Path, default=None,
                        help=f"平台冷却账 JSON 路径（默认 {DEFAULT_THROTTLE_FILE}）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="既有证据库路径，默认 <仓库>/sentiment.db")
    parser.add_argument("--node", default=None, help="node 可执行文件；默认按环境变量与已知路径查找")
    parser.add_argument("--opencli", default=None, help="OpenCLI 的 dist/src/main.js 路径；默认按已知 profile 路径查找")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"单次调用超时秒数（默认 {DEFAULT_TIMEOUT}）")
    return parser


def validate_args(args):
    """参数校验在调用上游之前完成，非法值直接报错，不发起任何请求。"""
    if not 1 <= args.limit <= MAX_LIMIT:
        raise CollectError("args", f"--limit 必须在 1-{MAX_LIMIT} 之间，收到 {args.limit}", 2)
    if args.min_interval < 0:
        raise CollectError("args", f"--min-interval 必须 >= 0，收到 {args.min_interval}", 2)
    if args.gap_seconds < 0:
        raise CollectError("args", f"--gap-seconds 必须 >= 0，收到 {args.gap_seconds}", 2)
    if args.max_per_run < 1:
        raise CollectError("args", f"--max-per-run 必须 >= 1，收到 {args.max_per_run}", 2)
    if args.timeout < 1:
        raise CollectError("args", f"--timeout 必须 >= 1，收到 {args.timeout}", 2)


def import_collected(db, endpoint, group_id, rows):
    """把某个端点已采到的行写进既有证据库；复用 storage.py 既有函数，不改表结构。"""
    spec = ENDPOINTS[endpoint]
    feed_id, feed_state = ensure_feed(db, group_id, spec["feed_url"])
    run_id = storage.start_run(db, feed_id, "opencli-" + endpoint, len(rows))
    try:
        outcome = storage.import_batch(db, ROOT, run_id, rows, server.classify)
    except Exception as exc:  # 校验/磁盘/SQLite
        return {"run_id": run_id, "db_error": {"type": type(exc).__name__, "message": str(exc)}}
    return {"feed": {"id": feed_id, "state": feed_state, "name": group_id, "url": spec["feed_url"]},
            "run": {key: outcome[key] for key in
                    ("id", "status", "input_count", "added", "duplicates", "input_sha256",
                     "started_at", "finished_at")}}


def main(argv=None):
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    endpoints = []
    for value in (args.endpoint or ["hot"]):
        if value not in endpoints:
            endpoints.append(value)
    throttle_path = Path(args.throttle_file) if args.throttle_file else DEFAULT_THROTTLE_FILE
    report = {
        "collector": COLLECTOR,
        "status": "running",
        "dry_run": args.dry_run,
        "endpoints": endpoints,
        "limit": args.limit,
        "min_interval": args.min_interval,
        "gap_seconds": args.gap_seconds,
        "max_per_run": args.max_per_run,
        "throttle": {"file": str(throttle_path), "state": None, "read_only": bool(args.dry_run),
                     "written": False},
        "access": "read",
        "request_count": 0,
        "returned_total": 0,
        "node": None,
        "opencli": None,
        "results": [],
        "db": {"path": str(args.db), "present": args.db.is_file(), "touched": False},
        "notes": [(MODE_NOTE_DRY if args.dry_run else MODE_NOTE_REAL), *NOTES],
    }
    node = main_js = None
    try:
        validate_args(args)
        if args.group_id and len(endpoints) != 1:
            raise CollectError("args", "--group-id 只适用于单端点运行；多端点请分别运行或去掉该参数", 2)
        node = _first_file(node_candidates(args.node))
        if node is None:
            raise CollectError("env", "未找到 node 可执行文件，请用 --node 指定",
                               2, {"searched": [str(p) for p in node_candidates(args.node)]})
        main_js = _first_file(opencli_candidates(args.opencli))
        if main_js is None:
            raise CollectError("env", "未找到 OpenCLI 的 dist/src/main.js，请用 --opencli 指定",
                               2, {"searched": [str(p) for p in opencli_candidates(args.opencli)]})
        report["node"], report["opencli"] = str(node), str(main_js)
        ledger, state = load_throttle(throttle_path)
        report["throttle"]["state"] = state
    except CollectError as exc:
        if node is not None:
            report["node"] = str(node)
        if main_js is not None:
            report["opencli"] = str(main_js)
        return emit_error(exc, report)

    remaining = args.max_per_run
    traffic = False      # 本轮是否已经真的发出过上游调用（决定是否需要错峰 sleep）
    stop_reason = None   # 本轮中止后，剩余端点的跳过原因
    collected = {}       # endpoint -> rows（只在非 dry-run 时写库）
    failure_exit = []

    for endpoint in endpoints:
        spec = ENDPOINTS[endpoint]
        platform = spec["platform"]
        group_id = args.group_id or spec["default_group"]
        result = {"endpoint": endpoint, "platform": platform, "group_id": group_id,
                  "feed_url": spec["feed_url"], "field_map": dict(spec["fields"]),
                  "limit": args.limit, "status": None, "reason": None,
                  "gap_waited_seconds": 0.0, "returned_count": 0, "call": None, "records": []}
        report["results"].append(result)

        if stop_reason:
            result["status"], result["reason"] = "skipped", stop_reason
            continue

        moment = datetime.now(timezone.utc)
        entry = ledger["platforms"].get(platform)
        cooled, last_raw, elapsed, wait = cooldown_of(entry, moment, args.min_interval)
        if cooled:
            info = entry if isinstance(entry, dict) else {}
            result["status"], result["reason"] = "skipped", "throttled"
            result["throttle"] = {
                "last_attempt_at": last_raw,
                "last_status": info.get("last_status"),
                "last_endpoint": info.get("last_endpoint"),
                "seconds_since_last": None if elapsed is None else round(elapsed, 3),
                "seconds_remaining": None if wait is None else round(wait, 3),
                "min_interval": args.min_interval,
            }
            continue

        if remaining <= 0:
            result["status"], result["reason"] = "skipped", "max_per_run"
            continue

        effective = min(args.limit, remaining)
        result["effective_limit"] = effective
        if traffic and args.gap_seconds > 0:
            clock = time.monotonic()
            time.sleep(args.gap_seconds)
            result["gap_waited_seconds"] = round(time.monotonic() - clock, 3)

        try:
            items, call = run_opencli(node, main_js, endpoint, effective, args.timeout)
            rows, previews = map_rows(endpoint, items)
        except CollectError as exc:
            signal = risk_signal(exc)
            result["status"] = "failed"
            result["reason"] = "risk_control" if signal else "error"
            result["signal"] = signal
            result["error"] = {"stage": exc.stage, "exit_code": exc.exit_code,
                               "message": exc.message, **exc.extra}
            result["call"] = exc.extra.get("call")
            record_attempt(ledger, platform, endpoint, "failed", signal, exc.message)
            if not args.dry_run:
                save_throttle(throttle_path, ledger)
                report["throttle"]["written"] = True
            failure_exit.append(exc.exit_code)
            # 风控信号出现即停止（不重试）；其它失败同样中止本轮，避免在未知状态下继续打上游。
            stop_reason = ("round_aborted_after_" + signal) if signal else "round_aborted_after_error"
            continue

        traffic = True
        report["request_count"] += 1
        report["returned_total"] += len(items)
        remaining -= len(items)
        result.update({"status": "ok", "reason": "collected", "returned_count": len(items),
                       "call": call, "records": previews, "row_count": len(rows)})
        if rows:
            collected[endpoint] = rows
        record_attempt(ledger, platform, endpoint, "ok", None, None, len(items))
        if not args.dry_run:
            save_throttle(throttle_path, ledger)
            report["throttle"]["written"] = True

    if not args.dry_run and collected:
        try:
            db = storage.connect(args.db)
        except Exception as exc:  # sqlite3.Error / OSError
            return emit_error(CollectError("db", f"打开证据库失败：{exc}", 7, {"db": str(args.db)}), report)
        try:
            report["db"] = {"path": str(args.db), "present": True, "touched": True}
            for endpoint, rows in collected.items():
                result = next(r for r in report["results"] if r["endpoint"] == endpoint)
                outcome = import_collected(db, endpoint, result["group_id"], rows)
                if "db_error" in outcome:
                    result["status"], result["reason"] = "failed", "db_import"
                    result["db_error"] = outcome["db_error"]
                    failure_exit.append(7)
                else:
                    result["feed"] = outcome["feed"]
                    result["run"] = outcome["run"]
                outcome.pop("db_error", None)
        finally:
            db.close()

    collected_count = sum(1 for r in report["results"] if r["status"] == "ok")
    throttled_count = sum(1 for r in report["results"] if r["reason"] == "throttled")
    if failure_exit:
        report["status"] = "failed"
    elif collected_count:
        report["status"] = "ok"
    elif throttled_count:
        report["status"] = "throttled"
    else:
        report["status"] = "skipped"
    report["collected_count"] = collected_count
    report["throttled_count"] = throttled_count
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return failure_exit[0] if failure_exit else 0


def emit_error(exc, context=None):
    """失败报告：status/error 由异常决定，context 只补充不覆盖（避免把 failed 覆盖回 ok）。"""
    payload = {"collector": COLLECTOR, "status": "blocked" if exc.exit_code == 2 else "failed",
               "error": {"stage": exc.stage, "exit_code": exc.exit_code, "message": exc.message,
                         **exc.extra}}
    if context:
        for key, value in context.items():
            if key not in payload:
                payload[key] = value
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
