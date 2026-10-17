"use strict";

/* 舆情观察台 M2 工作台
 * 仅使用标准 HTML/CSS/JS。所有动态内容通过 DOM API 或 textContent 写入，
 * 不使用 innerHTML 插入来自接口/用户的正文，避免 HTML 注入。
 */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const QUALITY_LABELS = {
  valid: "时间格式有效",
  missing: "时间缺失",
  invalid: "时间非法",
  timezone_missing: "缺时区",
  legacy_unverified: "旧数据未核验"
};
const RUN_STATUS_LABELS = {
  running: "进行中",
  succeeded: "成功",
  failed: "失败",
  interrupted: "中断（服务退出）"
};

const state = {
  data: null,
  manualLimit: null,
  feedsById: {},
  runsById: {},
  online: false,
  loadedAt: null,
  p1: null,                       // GET /api/p1/overview 真实返回
  p1Entries: [],                  // 已载入材料（top + unranked 展平，每条含 group_label/bucket）
  p1Content: {},                  // evidence_id -> {status, content}：用于本地关键词过滤的内容缓存
  filters: { source: "all", kind: "all", keyword: "" },
  selectedTopic: null,
  drawerTrigger: null,
  topK: "3",
  overviewToken: 0,               // 概览请求令牌：过期响应不得写状态
  drawerToken: 0,                 // 抽屉请求令牌：过期响应不得写状态
  watch: null,                    // GET /api/p1/watchlist 真实返回
  watchToken: 0,                  // 观察清单请求令牌
  historyToken: 0,                // 同帖历史请求令牌
  history: null,                  // 当前选中周期的真实历史点
  selectedWatchId: null,
  windowCandidates: null,         // 观测窗口内：evidence_id -> 窗口内最新真实观测快照
  windowTruncated: 0,             // 因分页未取完被明确排除、不计入完成候选的身份数
  windowFrom: null,               // 已应用窗口起点（显式 +08:00 字符串）或 null
  windowTo: null,                 // 已应用窗口终点（不含观测时刻）或 null
  windowInput: { from: "", to: "" },
  groupMeta: {},
  charts: { "chart-count": null, "chart-rate": null }   // key 必须与 DOM id / renderChart 存储 key 完全一致
};

// 概览始终按候选上限（3）载入全部候选，展示 K 由前端在选择范围内重算，避免后端 top_k 截断。
const P1_LOAD_TOP_K = 3;

/* ---------- 安全 DOM 构建 ---------- */

function appendChild(node, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    for (const item of child) appendChild(node, item);
    return;
  }
  node.append(child instanceof Node ? child : document.createTextNode(String(child)));
}

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "onclick") node.addEventListener("click", value);
      else node.setAttribute(key, String(value));
    }
  }
  for (const child of children) appendChild(node, child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function isSafeUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

function linkNode(value, label) {
  const text = label ?? value;
  if (isSafeUrl(value)) {
    return h("a", { href: value, target: "_blank", rel: "noopener noreferrer", text: text || value });
  }
  return h("span", { class: "muted", text: text || "（无有效 http(s) 链接）" });
}

/* ---------- 接口封装：区分离线 / 后端错误，原样保留状态码与消息 ---------- */

class ApiError extends Error {
  constructor(message, status, kind, rawText) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.rawText = typeof rawText === "string" ? rawText : "";  // 原始响应体文本（可能非 JSON）
  }
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store"
    });
  } catch (err) {
    throw new ApiError("无法连接本地服务（离线或服务未启动）", 0, "offline", "");
  }
  const raw = await response.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch (err) { data = null; }
  }
  if (!response.ok) {
    const message = data && typeof data.error === "string" && data.error ? data.error : `HTTP ${response.status}`;
    const error = new ApiError(message, response.status, "http", raw);
    error.payload = data;
    throw error;
  }
  return data;
}

/* ---------- 通用展示 ---------- */

function show(message, error = false) {
  const el = $("#notice");
  el.textContent = message || "";
  el.classList.toggle("error", !!error);
  el.classList.toggle("ok", !error && !!message);
}

function setConnection(kind, text) {
  const el = $("#connection");
  el.classList.remove("online", "offline", "error", "loading");
  el.classList.add(kind);
  el.textContent = text;
}

function fmtTime(value) {
  if (value === null || value === undefined || value === "") return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间无法解析";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
}

function fmtRatio(value) {
  if (value === null || value === undefined) return "—（空表）";
  return (value * 100).toFixed(1) + "%";
}

// 观测窗口控件值（datetime-local，按 Asia/Shanghai UTC+08:00 解释）→ 显式带时区字符串。
// 返回 undefined 表示格式非法，null 表示留空（不按该边界筛选）。
function toWindowIso(value) {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) return undefined;
  const seconds = match[6] || "00";
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${seconds}${CN_OFFSET}`;
}

function fmtNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function formatInterval(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
  if (seconds <= 0) return `${seconds} 秒（时间异常）`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes * 10) / 10} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes - hours * 60);
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

function chartLib() {
  return typeof window !== "undefined" && window.echarts ? window.echarts : null;
}

function qualityLabel(quality) {
  return QUALITY_LABELS[quality] || quality || "未知";
}

function statusLabel(status) {
  return RUN_STATUS_LABELS[status] || status || "未知";
}

function shortHash(value) {
  return typeof value === "string" && value ? value.slice(0, 12) + "…" : "—";
}

function feedLabel(feedId) {
  const feed = state.feedsById[feedId];
  return feed ? `#${feedId} ${feed.name}` : `#${feedId ?? "?"}`;
}

function kv(label, valueNode) {
  return [h("dt", { text: label }), h("dd", null, valueNode)];
}

/* ---------- 视图切换 ---------- */

function selectView(name) {
  for (const tab of $$(".tab")) {
    const on = tab.dataset.view === name;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
  for (const view of $$(".view")) {
    view.hidden = view.id !== "view-" + name;
  }
}

// 数据管理内部视图切换（与主视图独立，避免与旧管理结构互相干扰）
function selectAdminView(name) {
  for (const tab of $$(".admin-tab")) {
    const on = tab.dataset.admin === name;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
  for (const view of $$(".admin-view")) {
    view.hidden = view.id !== "admin-" + name;
  }
}

/* ---------- 渲染：统计与来源 ---------- */

function renderStats() {
  const data = state.data;
  if (!data) return;
  const stats = data.stats || {};
  $("#stat-records").textContent = String(stats.record_count ?? 0);
  $("#stat-canonical").textContent = String(stats.canonical_count ?? 0);
  $("#stat-unknown").textContent = String(stats.unknown_time_count ?? 0);
  $("#stat-unknown-ratio").textContent = fmtRatio(stats.unknown_time_ratio);
  $("#stat-feeds").textContent = String((data.feeds || []).length);
  $("#stat-shown").textContent = `${(data.items || []).length} / ${data.items_limit ?? 500}`;

  $("#meta-schema").textContent = data.schema_version || "—";
  $("#meta-coverage").textContent = data.coverage === "sample_only"
    ? "sample_only（仅样本，不代表总体）"
    : (data.coverage || "—");
  $("#meta-analysis").textContent = data.analysis_status === "unreviewed_dictionary"
    ? "unreviewed_dictionary（本地词典粗筛，未复核）"
    : (data.analysis_status || "—");

  const counts = stats.time_quality_counts;
  const dist = $("#quality-dist");
  clear(dist);
  if (counts && Object.keys(counts).length) {
    const parts = Object.keys(counts).sort().map((key) => `${qualityLabel(key)}(${key})=${counts[key]}`);
    dist.textContent = parts.join("；");
  } else {
    dist.textContent = "无记录";
  }

  const legacy = data.legacy_items_pending ?? 0;
  $("#legacy-note").textContent = legacy > 0
    ? `${legacy} 条旧 items 原样保留，未自动迁移；旧时间不得当作真实发布时间。`
    : "0 条（无旧 items 待处理）。";
}

function renderRefreshLabel() {
  const button = $("#refresh");
  button.textContent = state.manualLimit == null
    ? "刷新来源"
    : `刷新来源（前 ${state.manualLimit} 个来源，每源最多 ${state.manualLimit} 条）`;
}

function renderFeeds() {
  const body = $("#feeds-body");
  clear(body);
  const feeds = state.data ? state.data.feeds || [] : [];
  if (!feeds.length) {
    body.append(h("tr", null, h("td", { colspan: "6", class: "muted" }, "还没有来源。请在左侧添加公开 RSS 地址。")));
    return;
  }
  for (const feed of feeds) {
    const lastRun = feed.last_run_id ? state.runsById[feed.last_run_id] : null;
    const runCell = feed.last_run_id
      ? h("span", null,
          h("button", { class: "link", type: "button", onclick: () => openRunById(feed.last_run_id) }, feed.last_run_id),
          lastRun ? h("small", { class: "muted" }, ` ${statusLabel(lastRun.status)}`) : null)
      : h("span", { class: "muted", text: "无批次" });
    body.append(h("tr", null,
      h("td", null, h("b", { text: feed.name })),
      h("td", null, linkNode(feed.url)),
      h("td", { class: "num" }, String(feed.item_count ?? 0)),
      h("td", null, fmtTime(feed.last_success_at)),
      h("td", null, runCell),
      h("td", null, h("button", { class: "secondary", type: "button", onclick: () => useFeedForImport(feed.id) }, "用此来源导入"))
    ));
  }
}

function fillSelect(select, feeds, allLabel) {
  const current = select.value;
  clear(select);
  if (allLabel !== undefined) select.append(h("option", { value: "all", text: allLabel }));
  for (const feed of feeds) {
    select.append(h("option", { value: String(feed.id), text: `#${feed.id} ${feed.name}` }));
  }
  if (current && Array.from(select.options).some((option) => option.value === current)) {
    select.value = current;
  }
}

function renderSourceOptions() {
  const feeds = state.data ? state.data.feeds || [] : [];
  fillSelect($("#filter-source"), feeds, "全部来源");
  fillSelect($("#import-feed"), feeds);
}

function renderKeywords() {
  const data = state.data;
  if (!data) return;
  $("#keyword-form [name=keywords]").value = (data.keywords || []).join("，");
}

function useFeedForImport(feedId) {
  const select = $("#import-feed");
  select.value = String(feedId);
  const textarea = $("#import-form [name=records]");
  textarea.focus();
  show(`已选择来源 ${feedLabel(feedId)}，粘贴 JSON 记录后点击导入。`);
}

/* ---------- 渲染：原文与证据 ---------- */

function currentFilters() {
  return {
    source: $("#filter-source").value,
    quality: $("#filter-quality").value,
    sentiment: $("#filter-sentiment").value,
    keyword: $("#filter-keyword").value.trim().toLocaleLowerCase("zh-CN")
  };
}

function filteredItems() {
  const data = state.data;
  if (!data) return [];
  const filters = currentFilters();
  return (data.items || []).filter((item) => {
    if (filters.source !== "all" && String(item.feed_id) !== filters.source) return false;
    if (filters.quality !== "all" && item.time_quality !== filters.quality) return false;
    if (filters.sentiment !== "all" && item.sentiment !== filters.sentiment) return false;
    if (filters.keyword) {
      const haystack = `${item.title || ""} ${item.summary || ""}`.toLocaleLowerCase("zh-CN");
      if (!haystack.includes(filters.keyword)) return false;
    }
    return true;
  });
}

function recordCard(item) {
  return h("article", { class: "card" },
    h("div", { class: "meta" },
      h("span", { class: `badge ${item.sentiment === "偏正面" ? "pos" : item.sentiment === "偏负面" ? "neg" : "neu"}` }, item.sentiment || "未知"),
      h("span", { class: "chip" }, `来源 ${item.feed_name ?? item.feed_id ?? "?"}`),
      h("span", { class: "chip" }, `时间质量 ${qualityLabel(item.time_quality)}`),
      h("time", { class: "muted" }, fmtTime(item.published))
    ),
    h("h3", null, linkNode(item.link, item.title || "（无标题）")),
    h("p", { class: "excerpt" }, (item.summary || "").slice(0, 400) || "（无摘要）"),
    h("dl", { class: "kv" },
      kv("记录ID", h("code", { text: item.id })),
      kv("规范记录ID", h("code", { text: item.canonical_id })),
      kv("快照哈希 sha256", h("code", { title: item.raw_sha256 || "", text: shortHash(item.raw_sha256) })),
      kv("内容哈希 sha256", h("code", { title: item.content_sha256 || "", text: shortHash(item.content_sha256) })),
      kv("去重", item.dedup_reason === "exact_text" ? "exact_text（正文精确重复）" : "independent（独立正文）"),
      kv("抓取时间", fmtTime(item.fetched_at)),
      kv("原始时间字段", item.published_raw || "（空）"),
      kv("批次", item.run_id
        ? h("button", { class: "link", type: "button", onclick: () => openRunById(item.run_id) }, item.run_id)
        : "—")
    ),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: () => openRecord(item.id) }, "查看证据详情"),
      isSafeUrl(item.canonical_url)
        ? h("a", { class: "link", href: item.canonical_url, target: "_blank", rel: "noopener noreferrer" }, "规范化 URL")
        : null
    )
  );
}

function renderRecords() {
  const container = $("#records");
  clear(container);
  const data = state.data;
  const summary = $("#records-summary");
  if (!data) {
    summary.textContent = "尚未读取数据。";
    container.append(h("p", { class: "empty" }, "尚未读取数据。"));
    return;
  }
  const total = (data.stats && data.stats.record_count) || 0;
  const items = data.items || [];
  const cap = data.items_limit ?? 500;
  const rows = filteredItems();
  summary.textContent =
    `全表 ${total} 条（分母）；接口列表上限 ${cap} 条，本次载入 ${items.length} 条；当前筛选后 ${rows.length} 条。` +
    `（展示条数不等于总量）`;

  if (total === 0) {
    container.append(h("p", { class: "empty" }, "空库：数据库中还没有任何记录。请先在“来源与导入”添加来源，然后导入 JSON 或刷新来源。"));
    return;
  }
  if (rows.length === 0) {
    container.append(h("p", { class: "empty" }, "当前筛选无匹配：全表有记录，但没有符合来源 / 时间质量 / 关键词 / 情感条件的条目。"));
    return;
  }
  for (const item of rows) container.append(recordCard(item));
}

function renderRecordDetail(data) {
  const panel = $("#record-detail");
  const body = $("#record-detail-body");
  clear(body);
  const record = data.record || {};
  const snapshot = data.raw_snapshot;

  body.append(
    h("p", { class: "muted" }, "服务端在回读原文快照时已重新校验 raw_sha256（storage.digest）。以下为真实返回值，未做替代或缓存。"),
    h("dl", { class: "kv wide" },
      kv("记录ID", h("code", { text: record.id })),
      kv("规范记录ID", h("code", { text: record.canonical_id })),
      kv("来源", feedLabel(record.feed_id)),
      kv("来源内ID source_id", record.source_id || "（空）"),
      kv("标题", record.title || "（空）"),
      kv("摘要", record.summary || "（空）"),
      kv("URL", linkNode(record.url)),
      kv("规范化 URL", linkNode(record.canonical_url)),
      kv("时间质量", `${qualityLabel(record.time_quality)}（${record.time_quality}）`),
      kv("发布时间 published_at", fmtTime(record.published_at)),
      kv("原始时间字段 published_raw", record.published_raw || "（空）"),
      kv("抓取时间 fetched_at", fmtTime(record.fetched_at)),
      kv("快照哈希 raw_sha256", h("code", { text: record.raw_sha256 || "—" })),
      kv("内容哈希 content_sha256", h("code", { text: record.content_sha256 || "—" })),
      kv("快照路径 raw_path", h("code", { text: record.raw_path || "—" })),
      kv("去重规则", record.dedup_reason || "—"),
      kv("批次", record.run_id
        ? h("button", { class: "link", type: "button", onclick: () => openRunById(record.run_id) }, record.run_id)
        : "—"),
      kv("情感（未复核词典）", record.sentiment || "—")
    ),
    h("h3", { class: "detail-sub" }, "raw_snapshot（内容寻址 JSON）"),
    h("pre", { class: "json" }, JSON.stringify(snapshot, null, 2))
  );
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function openRecord(id) {
  const panel = $("#record-detail");
  const body = $("#record-detail-body");
  clear(body);
  panel.hidden = false;
  body.append(h("p", { class: "muted" }, `正在读取 /api/records/${id} …`));
  try {
    const data = await api(`/api/records/${encodeURIComponent(id)}`);
    renderRecordDetail(data);
  } catch (err) {
    clear(body);
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    body.append(
      h("h3", { class: "detail-sub" }, "证据详情读取失败"),
      h("p", { class: "error-text" }, `${status}：${err.message}`),
      err.status === 409
        ? h("p", { class: "muted" }, "服务端在回读原文快照时发现哈希不一致或文件缺失（409），此处原样显示，不做替代或降级。")
        : null
    );
  }
}

/* ---------- 渲染：报告模板（未复核样本） ---------- */

function buildReport() {
  const data = state.data;
  if (!data) return "尚未读取数据，无法生成模板。";
  const stats = data.stats || {};
  const items = data.items || [];
  const cap = data.items_limit ?? 500;
  const lines = [];

  lines.push("# 舆情观察台 · 未复核样本报告模板");
  lines.push("");
  lines.push(`生成时间（本地展示）：${fmtTime(new Date().toISOString())}`);
  lines.push("");
  lines.push("> 状态：**未复核（unreviewed）**。本模板由本地工具按键值直接拼装，未经人工复核，");
  lines.push("> 不构成结论、事件认定、预警、通知或准确率声明。自动事件聚类、可靠预警与生产模型当前均未接入。");
  lines.push("");

  lines.push("## 覆盖与分母");
  lines.push(`- coverage：${data.coverage || "—"}${data.coverage === "sample_only" ? "（仅样本，不代表总体）" : ""}`);
  lines.push(`- analysis_status：${data.analysis_status || "—"}${data.analysis_status === "unreviewed_dictionary" ? "（本地词典粗筛，未复核）" : ""}`);
  lines.push(`- schema_version：${data.schema_version || "—"}`);
  lines.push(`- 全表记录数（分母）：${stats.record_count ?? 0}`);
  lines.push(`- 规范记录数：${stats.canonical_count ?? 0}`);
  lines.push(`- 未知时间记录数：${stats.unknown_time_count ?? 0}，占比 ${fmtRatio(stats.unknown_time_ratio)}`);
  lines.push(`- 列表上限：${cap} 条；本次载入 ${items.length} 条（展示条数不是总量）`);
  lines.push("");

  lines.push("## 时间质量分布（全表）");
  const counts = stats.time_quality_counts || {};
  if (Object.keys(counts).length) {
    for (const key of Object.keys(counts).sort()) {
      lines.push(`- ${qualityLabel(key)}（${key}）：${counts[key]}`);
    }
  } else {
    lines.push("- 无记录");
  }
  lines.push("");

  lines.push("## 来源清单");
  const feeds = data.feeds || [];
  if (!feeds.length) {
    lines.push("- 无来源");
  } else {
    for (const feed of feeds) {
      const run = feed.last_run_id ? state.runsById[feed.last_run_id] : null;
      lines.push(
        `- #${feed.id} ${feed.name}｜条目数 ${feed.item_count ?? 0}｜最后成功 ${fmtTime(feed.last_success_at)}｜` +
        `最近批次 ${feed.last_run_id || "无"}${run ? "（" + statusLabel(run.status) + "）" : ""}`
      );
      lines.push(`  - URL：${feed.url}`);
    }
  }
  lines.push("");

  lines.push("## 样本清单（来源 | 时间质量 | 记录ID | 规范记录ID | 标题）");
  if (!items.length) {
    lines.push("- 无样本");
  } else {
    for (const item of items) {
      lines.push(
        `- [${item.feed_name ?? item.feed_id ?? "?"}] 时间质量=${qualityLabel(item.time_quality)}（${item.time_quality}）` +
        ` | 记录ID=${item.id} | 规范ID=${item.canonical_id} | ${item.title || "（无标题）"}`
      );
    }
    if ((stats.record_count ?? 0) > items.length) {
      lines.push(`- …全表另有约 ${(stats.record_count ?? 0) - items.length} 条未在列表上限内展示`);
    }
  }
  lines.push("");

  lines.push("## 未接入能力（不编造）");
  lines.push("- 事件聚类 / 事件 API：未接入");
  lines.push("- 预警 / 通知 / 推送：未接入");
  lines.push("- 模型调用与用量：未接入（标签为本地词典粗筛）");
  lines.push("- 批次取消：未接入");
  lines.push(`- 旧 items 待处理：${data.legacy_items_pending ?? 0} 条（原样保留，未迁移）`);
  lines.push("");
  lines.push("## 复核提示");
  lines.push("- 本模板未经过人工复核，接收前必须由人工逐条核对来源、时间与正文快照。");
  lines.push("- 情感标签与未知时间判定均为规则粗筛，不得作为定性结论或对外口径。");

  return lines.join("\n");
}

function generateReport() {
  if (!state.online) { show("当前服务状态未知，不能生成实时样本模板。", true); return; }
  $("#report-out").value = buildReport();
  show("已生成未复核样本模板（纯文本，未提交、未对外发送）。");
}

/* ---------- 渲染：运行状态 ---------- */

function renderRuns() {
  const body = $("#runs-body");
  clear(body);
  const runs = state.data ? state.data.runs || [] : [];
  if (!runs.length) {
    body.append(h("tr", null, h("td", { colspan: "10", class: "muted" }, "还没有采集批次。")));
    return;
  }
  for (const run of runs) {
    body.append(h("tr", null,
      h("td", null, h("button", { class: "link", type: "button", onclick: () => openRunById(run.id) }, run.id)),
      h("td", null, feedLabel(run.feed_id)),
      h("td", null, run.kind || "—"),
      h("td", null, h("span", { class: `status ${run.status}` }, statusLabel(run.status))),
      h("td", null, fmtTime(run.started_at)),
      h("td", null, fmtTime(run.finished_at)),
      h("td", { class: "num" }, String(run.input_count ?? 0)),
      h("td", { class: "num" }, String(run.added ?? 0)),
      h("td", { class: "num" }, String(run.duplicates ?? 0)),
      h("td", null, run.error
        ? h("span", { class: "error-text", title: run.error }, run.error.slice(0, 60))
        : "—")
    ));
  }
}

function renderRunDetail(run) {
  const panel = $("#run-detail");
  const body = $("#run-detail-body");
  clear(body);
  if (!run || !run.id) {
    body.append(h("p", { class: "error-text" }, "批次数据缺失。"));
    panel.hidden = false;
    return;
  }
  body.append(h("dl", { class: "kv wide" },
    kv("批次ID", h("code", { text: run.id })),
    kv("来源", feedLabel(run.feed_id)),
    kv("类型 kind", run.kind || "—"),
    kv("状态", h("span", { class: `status ${run.status}` }, `${statusLabel(run.status)}（${run.status}）`)),
    kv("开始", fmtTime(run.started_at)),
    kv("结束", fmtTime(run.finished_at)),
    kv("输入条数 input_count", String(run.input_count ?? 0)),
    kv("新增 added", String(run.added ?? 0)),
    kv("重复 duplicates", String(run.duplicates ?? 0)),
    kv("批次限额 limit_n", String(run.limit_n ?? "—")),
    kv("输入哈希 input_sha256", h("code", { text: run.input_sha256 || "—" })),
    kv("错误 error", run.error ? h("span", { class: "error-text" }, run.error) : "（无）")
  ));
  panel.hidden = false;
}

async function openRunById(id) {
  const panel = $("#run-detail");
  const body = $("#run-detail-body");
  selectView("admin");
  selectAdminView("runs");
  clear(body);
  panel.hidden = false;
  body.append(h("p", { class: "muted" }, `正在读取 /api/runs/${id} …`));
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    renderRunDetail(run);
  } catch (err) {
    clear(body);
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    body.append(h("p", { class: "error-text" }, `${status}：${err.message}`));
  }
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRuntime(info) {
  const body = $("#runtime-body");
  clear(body);
  appendChild(body, [
    kv("状态", "已读取（GET /api/runtime）"),
    kv("PID", String(info.pid ?? "—")),
    kv("启动时间 started_at", fmtTime(info.started_at)),
    kv("入口 entry", h("code", { text: info.entry || "—" })),
    kv("storage 模块", h("code", { text: info.storage_module || "—" })),
    kv("数据库", h("code", { text: info.database || "—" })),
    kv("快照目录", h("code", { text: info.snapshot_root || "—" })),
    kv("manual_limit", String(info.manual_limit ?? "—")),
    kv("server.py sha256", h("code", { text: (info.loaded_sha256 && info.loaded_sha256["server.py"]) || "—" })),
    kv("storage.py sha256", h("code", { text: (info.loaded_sha256 && info.loaded_sha256["storage.py"]) || "—" }))
  ]);
}

/* ---------- 业务页：统一筛选、业务状态与侧抽屉（不渲染技术证据） ---------- */

const P1_KIND_LABELS = {
  title_only: "仅标题",
  post_body: "帖主正文",
  source_summary: "来源摘要"
};
const P1_PRECISION_LABELS = { exact: "精确", approximate: "约数", unknown: "未知" };
// 观察周期状态与历史可比性：只做数据质量解释，不含情绪、因果、群体或事件判断。
const WATCH_STATUS_LABELS = { active: "观察中", expired: "已到期（观察资格结束）", exited: "已人工退出" };
const COMPARISON_LABELS = {
  single_point: "单点（等待第二个观测点）",
  comparable: "可比（精确计数、口径一致）",
  retraction: "回撤（计数减少，显示负变化）",
  same_time_conflict: "同刻冲突（同刻不同计数，不选赢家、不比较）",
  unknown: "计数未知（不补 0，不参与比较）",
  approximate: "约数（不补 0，不参与数值比较）",
  source_changed: "口径变化（来源字段变化，不比较）",
  invalid_interval: "时间异常（间隔非正，不计算速率）"
};
// 观测窗口控件按界面明示的 Asia/Shanghai UTC+08:00 解释，并转成显式带时区字符串再请求。
const CN_OFFSET = "+08:00";
const FILTER_HOSTS = ["overview-filters", "topics-filters", "search-filters", "watch-filters"];
// 其他计数的业务词映射：只保留中文口径，未知字段在业务中省略，不回显原始字段名。
const P1_OTHER_LABELS = {
  view: "播放",
  play: "播放",
  like: "点赞",
  danmaku: "弹幕",
  coin: "投币",
  favorite: "收藏",
  share: "分享"
};
// 评论数来源统一映射为业务词，不输出平台原始字段名 source_field。
const P1_COMMENT_SOURCE_LABEL = "平台公开评论总数";

function p1KindLabel(kind) { return P1_KIND_LABELS[kind] || kind || "未知类型"; }
function p1PrecisionLabel(precision) { return P1_PRECISION_LABELS[precision] || precision || "未知"; }
function p1CommentSourceLabel(sourceField) { return sourceField ? P1_COMMENT_SOURCE_LABEL : "（未标注）"; }

function p1StatusText(err) {
  if (err instanceof ApiError && err.kind === "offline") return "连接失败（无法连接本地服务）";
  return err && err.status ? `HTTP ${err.status}` : "读取失败";
}

function p1BodyNode(err) {
  // 管理区查看用：优先显示原始响应文本（JSON 原文或非 JSON 原文），都保留真实 HTTP 状态单独显示。
  if (!err) return h("p", { class: "error-text" }, "（无响应体）");
  if (typeof err.rawText === "string" && err.rawText) {
    return h("pre", { class: "json" }, err.rawText);
  }
  if (err.payload && typeof err.payload === "object") {
    return h("pre", { class: "json" }, JSON.stringify(err.payload, null, 2));
  }
  // 空 rawText 必须区分：网络失败（根本没收到响应）与已连通但响应体为空，两者不可混同，也不补造 body/状态码。
  if (err.kind === "offline" || err.status === 0) {
    return h("p", { class: "error-text" }, "（网络失败：未收到任何响应体）");
  }
  if (err.status) {
    return h("p", { class: "error-text" }, `（HTTP ${err.status}：已连通，响应体为空）`);
  }
  return h("p", { class: "error-text" }, "（无响应体原文）");
}

function p1OtherCountsNode(other) {
  if (!other || !Object.keys(other).length) return h("span", { class: "muted" }, "（无）");
  const rows = [];
  for (const name of Object.keys(other)) {
    const label = P1_OTHER_LABELS[name];
    if (!label) continue;           // 未知字段：业务中省略，不原样回显
    const count = other[name] || {};
    const counted = count.value !== null && count.value !== undefined;
    rows.push(h("div", { class: "p1-other" },
      `${label}${counted ? " " + count.value : " 未知"}（${p1PrecisionLabel(count.precision)}；不计入评论数）`));
  }
  return rows.length ? h("div", { class: "p1-others" }, rows) : h("span", { class: "muted" }, "（无）");
}

function setBizStatus(kind, text) {
  // 业务页状态一致：导航切换不会掩盖真实读取状态（含观察清单）。
  for (const id of ["overview-status", "topics-status", "search-status", "watch-status"]) {
    const el = $("#" + id);
    if (!el) continue;
    el.classList.remove("error", "ok");
    if (kind === "error") el.classList.add("error");
    else if (kind === "ok") el.classList.add("ok");
    el.textContent = text;
  }
}

function p1NewestObservedText(entries) {
  let best = null;
  for (const entry of entries) {
    if (!entry.observed_at) continue;
    const time = new Date(entry.observed_at).getTime();
    if (!Number.isNaN(time) && (best === null || time > best)) best = time;
  }
  return best === null ? "时间未知" : fmtTime(new Date(best).toISOString());
}

function p1ContentText(evidenceId) {
  const cached = state.p1Content[evidenceId];
  return cached && cached.status === "ok" ? (cached.content || "") : "";
}

// 精确评论数（视图内重算，不依赖后端 top_k 截断后的 rank）
function p1IsExact(entry) {
  const count = entry && entry.comment_count;
  return !!count && count.precision === "exact" && count.value !== null && count.value !== undefined;
}

function p1ExactSorted(entries) {
  return entries.filter(p1IsExact).slice()
    .sort((a, b) => (b.comment_count.value - a.comment_count.value)
      || String(a.evidence_id).localeCompare(String(b.evidence_id)));
}

function p1Unranked(entries) {
  return entries.filter((entry) => !p1IsExact(entry));
}

function p1WindowActive() {
  return !!(state.windowFrom || state.windowTo);
}

// 日期窗口内每个身份的“窗口内最新真实观测”快照集合；窗口生效但尚未载入时返回空，
// 绝不用窗口外最新值冒充窗口内结果。
function p1ActiveEntries() {
  if (p1WindowActive()) {
    return state.windowCandidates ? Array.from(state.windowCandidates.values()) : [];
  }
  return state.p1Entries;
}

function p1FilteredEntries() {
  const filters = state.filters;
  return p1ActiveEntries().filter((entry) => {
    if (filters.source !== "all" && (entry.source_name || "") !== filters.source) return false;
    if (filters.kind !== "all" && (entry.evidence_kind || "") !== filters.kind) return false;
    if (filters.keyword) {
      const haystack = `${entry.title || ""} ${entry.source_name || ""} ${p1ContentText(entry.evidence_id)}`
        .toLocaleLowerCase("zh-CN");
      if (!haystack.includes(filters.keyword)) return false;
    }
    return true;
  });
}

function p1FilterSummary(filtered) {
  const filters = state.filters;
  const active = p1ActiveEntries();
  const windowText = p1WindowActive()
    ? `观测窗口 ${fmtTime(state.windowFrom)} 起（含）— ${state.windowTo ? fmtTime(state.windowTo) + " 止（不含）" : "至最新观测"}`
    : "观测窗口=全部观测时间（未按日期筛选）";
  const parts = [
    `来源=${filters.source === "all" ? "全部" : filters.source}`,
    `证据类型=${filters.kind === "all" ? "全部" : p1KindLabel(filters.kind)}`,
    `关键词=${filters.keyword ? filters.keyword : "（无）"}`,
    windowText
  ];
  const truncatedNote = p1WindowActive() && state.windowTruncated
    ? `；另有 ${state.windowTruncated} 条候选因历史分页未取完被明确排除，不计入完成候选与分母`
    : "";
  return `统一筛选：${parts.join(" · ")}；本次符合观测窗口的原帖候选（同一分母）${active.length} 条，其他筛选后 ${filtered.length} 条${truncatedNote}。`;
}

function p1ContentNote() {
  const ids = p1ActiveEntries().map((entry) => entry.evidence_id);
  const missing = ids.filter((id) => !(state.p1Content[id] && state.p1Content[id].status === "ok")).length;
  return missing
    ? `（${missing} 条材料的正文/来源摘要未载入，关键词只匹配标题与来源。）`
    : "（关键词覆盖标题、来源与已载入的正文/来源摘要。）";
}

function buildFilterBars() {
  const sources = [...new Set(state.p1Entries.map((entry) => entry.source_name).filter(Boolean))].sort();
  const kinds = [...new Set(state.p1Entries.map((entry) => entry.evidence_kind).filter(Boolean))];
  for (const id of FILTER_HOSTS) {
    const host = $("#" + id);
    if (!host) continue;
    clear(host);
    const sourceSelect = h("select", { class: "flt-source", "aria-label": "来源筛选" },
      h("option", { value: "all", text: "全部来源" }),
      sources.map((name) => h("option", { value: name, text: name })));
    const kindSelect = h("select", { class: "flt-kind", "aria-label": "证据类型筛选" },
      h("option", { value: "all", text: "全部证据类型" }),
      kinds.map((kind) => h("option", { value: kind, text: p1KindLabel(kind) })));
    const keyword = h("input", { type: "search", class: "flt-keyword", "aria-label": "关键词筛选",
      placeholder: "标题 / 内容 / 来源关键词", autocomplete: "off" });
    // 观测窗口（后端 API 已支持才放控件）：时间按 Asia/Shanghai UTC+08:00 解释，起点含、终点不含。
    const from = h("input", { type: "datetime-local", class: "flt-from", step: "60",
      "aria-label": "观测窗口起点（Asia/Shanghai UTC+08:00，含）" });
    const to = h("input", { type: "datetime-local", class: "flt-to", step: "60",
      "aria-label": "观测窗口终点（Asia/Shanghai UTC+08:00，不含）" });
    const applyWindow = () => setWindowInputs(from.value, to.value);
    from.addEventListener("change", applyWindow);
    to.addEventListener("change", applyWindow);
    const clearWindow = h("button", { class: "link flt-clear", type: "button",
      onclick: () => setWindowInputs("", "") }, "清除观测窗口");
    sourceSelect.value = state.filters.source;
    kindSelect.value = state.filters.kind;
    keyword.value = state.filters.keyword;
    from.value = state.windowInput.from;
    to.value = state.windowInput.to;
    sourceSelect.addEventListener("change", () => { state.filters.source = sourceSelect.value; renderBusinessViews(); });
    kindSelect.addEventListener("change", () => { state.filters.kind = kindSelect.value; renderBusinessViews(); });
    keyword.addEventListener("input", () => {
      state.filters.keyword = keyword.value.trim().toLocaleLowerCase("zh-CN");
      renderBusinessViews();
    });
    host.append(
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "来源"), sourceSelect),
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "证据类型"), kindSelect),
      h("label", { class: "flt flt-wide" }, h("span", { class: "flt-label" }, "关键词"), keyword),
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "观测窗口起点（含）"), from),
      h("label", { class: "flt" }, h("span", { class: "flt-label" }, "观测窗口终点（不含）"), to),
      h("div", { class: "flt flt-wide flt-note" },
        h("span", { class: "hint" }, "窗口按观测时间（Asia/Shanghai UTC+08:00）筛选，起点含、终点不含，终点须晚于起点；总览 / 议题 / 检索 / 观察清单共用同一窗口与分母。"),
        clearWindow)
    );
  }
}

function syncFilterBars() {
  for (const id of FILTER_HOSTS) {
    const host = $("#" + id);
    if (!host) continue;
    const sourceSelect = $(".flt-source", host);
    const kindSelect = $(".flt-kind", host);
    const keyword = $(".flt-keyword", host);
    const from = $(".flt-from", host);
    const to = $(".flt-to", host);
    if (sourceSelect) sourceSelect.value = state.filters.source;
    if (kindSelect) kindSelect.value = state.filters.kind;
    if (keyword) keyword.value = state.filters.keyword;
    if (from) from.value = state.windowInput.from;
    if (to) to.value = state.windowInput.to;
  }
}

function groupFilteredEntries(filtered) {
  const groups = new Map();
  for (const entry of filtered) {
    const label = entry.group_label || "（未命名分组）";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(entry);
  }
  return groups;
}

function p1HeatNode(label, entries) {
  const exact = p1ExactSorted(entries);
  const lead = exact[0] || entries[0];
  if (!lead) return h("p", { class: "muted" }, `「${label}」在当前筛选下没有可展示条目。`);
  const count = lead.comment_count || {};
  const counted = count.value !== null && count.value !== undefined;
  return h("div", { class: "heat-inner" },
    h("p", { class: "heat-value" }, counted ? String(count.value) : "未知"),
    h("p", { class: "heat-cap" }, counted
      ? `评论数（${p1PrecisionLabel(count.precision)}）· 该原帖历史累计评论数的最新真实观测；逐帖计数与速率曲线见「观察清单」。`
      : `评论数未知（${p1PrecisionLabel(count.precision)}）· 不计入 0，也不参与排名。`),
    h("dl", { class: "kv" },
      kv("所属议题", label),
      kv("来源", lead.source_name || "—"),
      kv("观测时间", fmtTime(lead.observed_at)),
      kv("评论数来源", p1CommentSourceLabel(count.source_field)))
  );
}

function p1BusinessCard(entry, rank) {
  const count = entry.comment_count || {};
  const counted = count.value !== null && count.value !== undefined;
  const heat = counted
    ? `评论数 ${count.value}（${p1PrecisionLabel(count.precision)}）· 该原帖历史累计的最新真实观测；逐帖曲线见「观察清单」。`
    : `评论数未知（${p1PrecisionLabel(count.precision)}）· 不计入 0，也不参与排名。`;
  const showRank = typeof rank === "number";
  return h("article", { class: "card p1-entry" },
    h("div", { class: "meta" },
      showRank
        ? h("span", { class: "badge pos" }, `#${rank}`)
        : (counted ? null : h("span", { class: "badge neu" }, "未排名")),
      h("span", { class: "chip" }, p1KindLabel(entry.evidence_kind)),
      h("span", { class: "chip" }, `来源 ${entry.source_name || "?"}`)
    ),
    h("h3", null, linkNode(entry.url, entry.title || "（无标题）")),
    entry.evidence_kind === "title_only"
      ? h("p", { class: "excerpt" }, "仅标题线索，尚无正文或来源摘要：暂不生成梗概或表达判断，不推断性别、阵营或群体比例。")
      : null,
    h("p", { class: "p1-heat" }, heat),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: (event) => openP1Drawer(entry.evidence_id, event.currentTarget) }, "查看详情"),
      isSafeUrl(entry.url)
        ? h("a", { class: "link", href: entry.url, target: "_blank", rel: "noopener noreferrer" }, "查看原帖")
        : null
    ),
    // 明确的人工加入观察入口（小表单可键盘操作，不使用 prompt）。
    watchEntryControl(entry)
  );
}

function p1TopicSummary(label, entries) {
  const meta = (state.groupMeta || {})[label] || {};
  const exactCount = p1ExactSorted(entries).length;
  return h("div", null,
    h("h3", null, label),
    h("div", { class: "meta" },
      h("span", { class: "chip" }, `本组候选（全体）${meta.identity_count ?? entries.length}`),
      h("span", { class: "chip" }, `当前筛选 ${entries.length}`),
      h("span", { class: "chip" }, `精确计数 ${exactCount}`)),
    h("p", { class: "hint" }, "人工指定分组，相似标题不直接合并事件；排名仅按精确评论数降序，约数/未知计数单列。"),
    p1HeatNode(label, entries),
    h("p", { class: "boundary" }, "关联：暂无可证的议题关联——当前缺少带证据引用的关系查询，证据不足不生成关系图，也不推断群体立场。")
  );
}

function p1Reset() {
  // 读取失败 / 离线 / 导入失败时清空旧 P1 展示与旧详情，绝不把旧缓存当作在线数据。
  state.overviewToken++;          // 使在途概览/onrich 响应失效
  state.drawerToken++;            // 使在途抽屉响应失效
  state.p1 = null;
  state.p1Entries = [];
  state.p1Content = {};
  state.selectedTopic = null;
  state.windowCandidates = null;
  state.windowTruncated = 0;
  state.history = null;
  state.selectedWatchId = null;
  state.groupMeta = {};
  clearWatchCharts();
  const stats = $("#overview-stats");
  if (stats) stats.hidden = true;
  for (const id of ["ov-identity", "ov-groups", "ov-observed"]) {
    const el = $("#" + id);
    if (el) el.textContent = "—";
  }
  for (const id of ["overview-topics", "overview-heat", "overview-shortcuts", "topic-list",
                    "topic-summary", "topic-topk", "search-results",
                    "overview-filters", "topics-filters", "search-filters"]) {
    const el = $("#" + id);
    if (el) clear(el);
  }
  for (const id of ["overview-filter-note", "topics-filter-note", "search-filter-note"]) {
    const el = $("#" + id);
    if (el) el.textContent = "";
  }
  closeDrawer(true);
}

function renderBusinessEmpty() {
  const stats = $("#overview-stats");
  if (stats) stats.hidden = true;
  for (const id of ["ov-identity", "ov-groups", "ov-observed"]) {
    const el = $("#" + id);
    if (el) el.textContent = "—";
  }
  for (const id of ["overview-topics", "overview-heat", "overview-shortcuts", "topic-list",
                    "topic-summary", "topic-topk", "search-results"]) {
    const el = $("#" + id);
    if (el) clear(el);
  }
  const emptyText = "暂无可展示的原帖材料：请到「数据管理 → 人工证据」由人工粘贴已取证记录后提交（本页不预填、不自动导入、不抓取）。";
  for (const id of ["overview-topics", "topic-list", "search-results", "topic-topk"]) {
    const el = $("#" + id);
    if (el) el.append(h("p", { class: "empty" }, emptyText));
  }
  const summary = $("#topic-summary");
  if (summary) summary.append(h("p", { class: "muted" }, "无可展示议题概况。"));
  for (const id of ["overview-filter-note", "topics-filter-note", "search-filter-note"]) {
    const el = $("#" + id);
    if (el) el.textContent = "";
  }
}

function renderOverview(overview, filtered) {
  const stats = $("#overview-stats");
  if (stats) stats.hidden = false;
  $("#ov-identity").textContent = String(overview.identity_total ?? 0);
  $("#ov-groups").textContent = String(overview.group_count ?? 0);
  $("#ov-observed").textContent = p1NewestObservedText(p1ActiveEntries());

  const groups = groupFilteredEntries(filtered);
  const list = $("#overview-topics");
  clear(list);
  if (!groups.size) {
    list.append(h("p", { class: "empty" }, "当前筛选无匹配议题候选。"));
  } else {
    for (const [label, entries] of groups) {
      list.append(h("button", {
        class: "topic-row", type: "button",
        onclick: () => { state.selectedTopic = label; selectView("topics"); syncFilterBars(); renderBusinessViews(); }
      },
        h("span", { class: "topic-row-name" }, label),
        h("span", { class: "topic-row-meta" }, `当前筛选条目 ${entries.length} · 最近观测 ${p1NewestObservedText(entries)}`)));
    }
  }

  const heat = $("#overview-heat");
  clear(heat);
  const selectedLabel = state.selectedTopic && groups.has(state.selectedTopic)
    ? state.selectedTopic
    : (groups.size ? groups.keys().next().value : null);
  heat.append(selectedLabel
    ? p1HeatNode(selectedLabel, groups.get(selectedLabel))
    : h("p", { class: "muted" }, "当前筛选下无可展示热度。"));

  const shortcuts = $("#overview-shortcuts");
  clear(shortcuts);
  const exactAll = p1ExactSorted(filtered);
  const ranked = exactAll.slice(0, 3);
  const pool = ranked.length ? ranked.map((entry, index) => ({ entry, rank: index + 1 }))
    : filtered.slice(0, 3).map((entry) => ({ entry, rank: undefined }));
  if (!pool.length) shortcuts.append(h("p", { class: "empty" }, "当前筛选下没有原帖捷径。"));
  for (const item of pool) shortcuts.append(p1BusinessCard(item.entry, item.rank));

  $("#overview-filter-note").textContent = p1FilterSummary(filtered);
}

function renderTopics(filtered) {
  const groups = groupFilteredEntries(filtered);
  const list = $("#topic-list");
  clear(list);
  const topkHost = $("#topic-topk");
  const summary = $("#topic-summary");
  if (!groups.size) {
    list.append(h("p", { class: "empty" }, "当前筛选无匹配议题。"));
    clear(summary);
    summary.append(h("p", { class: "muted" }, "无可展示议题概况。"));
    clear(topkHost);
    topkHost.append(h("p", { class: "empty" }, "暂无可展示原帖。"));
    $("#topics-filter-note").textContent = p1FilterSummary(filtered);
    return;
  }
  const selected = groups.has(state.selectedTopic) ? state.selectedTopic : groups.keys().next().value;
  state.selectedTopic = selected;
  const backend = state.groupMeta || {};
  for (const [label, entries] of groups) {
    const meta = backend[label] || {};
    const exactCount = p1ExactSorted(entries).length;
    list.append(h("button", {
      class: `topic-item ${label === selected ? "active" : ""}`, type: "button",
      "aria-pressed": label === selected ? "true" : "false",
      onclick: () => { state.selectedTopic = label; renderBusinessViews(); }
    },
      h("span", { class: "topic-item-name" }, label),
      h("span", { class: "topic-item-meta" }, `本组候选 ${meta.identity_count ?? entries.length} · 当前筛选 ${entries.length} · 精确计数 ${exactCount}`),
      h("span", { class: "topic-item-meta" }, `最近观测 ${p1NewestObservedText(entries)}`)));
  }

  clear(summary);
  summary.append(p1TopicSummary(selected, groups.get(selected) || []));

  clear(topkHost);
  const members = groups.get(selected) || [];
  const exactAll = p1ExactSorted(members);
  const k = Number(state.topK) || 3;
  const topEntries = exactAll.slice(0, k);
  const unranked = p1Unranked(members);
  if (!topEntries.length) {
    topkHost.append(h("p", { class: "empty" }, "该议题没有可比精确评论数：证据不足，无可比排名（不伪精确、不把未知当 0）。"));
  }
  topEntries.forEach((entry, index) => topkHost.append(p1BusinessCard(entry, index + 1)));
  if (unranked.length) {
    topkHost.append(h("h4", { class: "detail-sub" }, "未参与排名（评论数为约数或未知）"));
    for (const entry of unranked) topkHost.append(p1BusinessCard(entry));
  }
  $("#topics-filter-note").textContent = `${p1FilterSummary(filtered)}${p1ContentNote()}`;
}

function renderSearch(filtered) {
  const host = $("#search-results");
  clear(host);
  if (!filtered.length) {
    host.append(h("p", { class: "empty" }, p1ActiveEntries().length
      ? "当前筛选无匹配原帖（范围仅为本次符合观测窗口的原帖候选）。"
      : "暂无可检索的原帖材料（本次符合观测窗口的候选 0 条）。"));
  } else {
    for (const entry of filtered) host.append(p1BusinessCard(entry));
  }
  $("#search-filter-note").textContent = `${p1FilterSummary(filtered)}${p1ContentNote()}`;
}

function applyP1(data) {
  state.p1 = data;
  const entries = [];
  for (const group of data.groups || []) {
    for (const entry of group.top || []) entries.push({ ...entry, group_label: group.group_label, bucket: "top" });
    for (const entry of group.unranked || []) entries.push({ ...entry, group_label: group.group_label, bucket: "unranked" });
  }
  state.p1Entries = entries;
  const labels = new Set((data.groups || []).map((group) => group.group_label));
  if (!state.selectedTopic || !labels.has(state.selectedTopic)) {
    state.selectedTopic = (data.groups && data.groups[0] && data.groups[0].group_label) || null;
  }
}

function computeGroupMeta() {
  const meta = {};
  for (const entry of p1ActiveEntries()) {
    const label = entry.group_label || "（未命名分组）";
    if (!meta[label]) meta[label] = { identity_count: 0 };
    meta[label].identity_count += 1;
  }
  state.groupMeta = meta;
}

function renderBusinessViews() {
  const overview = state.p1;
  if (!overview || overview.empty_state || !(overview.groups || []).length) {
    state.groupMeta = {};
    renderBusinessEmpty();
    return;
  }
  if (p1WindowActive() && state.windowCandidates === null) {
    renderWindowPending();
    return;
  }
  computeGroupMeta();
  const filtered = p1FilteredEntries();
  renderOverview(overview, filtered);
  renderTopics(filtered);
  renderSearch(filtered);
}

async function enrichP1Contents(token) {
  // 关键词过滤需要正文/来源摘要；overview 不含 content，这里按已载入材料（全库 ≤3）真实 GET 详情补齐。
  const ids = [...new Set(state.p1Entries.map((entry) => entry.evidence_id).filter(Boolean))];
  await Promise.all(ids.map(async (id) => {
    if (token !== state.overviewToken) return;   // 过期请求：不写状态
    if (state.p1Content[id] && state.p1Content[id].status === "ok") return;
    try {
      const data = await api(`/api/p1/evidence/${encodeURIComponent(id)}`);
      if (token !== state.overviewToken) return;
      const ev = data.evidence || {};
      state.p1Content[id] = { status: "ok", content: ev.content || "" };
    } catch (err) {
      if (token !== state.overviewToken) return;
      state.p1Content[id] = { status: "error", content: "", message: (err && err.message) || "" };
    }
  }));
}

/* ---------- 观测窗口 / 同帖历史 / 观察清单（真实 API，不抓取、不补 0） ---------- */

// 稳定分页遍历：has_more 为真时继续请求；达到分页上限则明确标为截断，绝不伪称完整历史。
// 每页请求前后核对令牌：旧窗口/旧选择的在途分页立即停止，不再继续消耗请求。
async function fetchHistoryPoints(evidenceId, isStale) {
  const from = state.windowFrom;
  const to = state.windowTo;
  let offset = 0;
  const all = [];
  let truncated = false;
  for (let page = 0; page < 20; page += 1) {
    if (isStale()) return null;            // 请求前：过期即停
    const query = new URLSearchParams({ evidence_id: evidenceId, limit: "500", offset: String(offset) });
    if (from) query.set("from", from);
    if (to) query.set("to", to);
    const data = await api(`/api/p1/history?${query.toString()}`);
    if (isStale()) return null;            // 响应后：过期响应不写状态、不再发下一页
    const points = data.points || [];
    all.push(...points);
    if (!data.has_more) return { points: all, truncated };
    if (!points.length) { truncated = true; return { points: all, truncated }; }
    offset += points.length;
  }
  return { points: all, truncated: true };
}

async function loadWindowCandidates(token) {
  if (!p1WindowActive()) return null;
  const candidates = new Map();
  state.windowTruncated = 0;
  for (const entry of state.p1Entries) {
    const history = await fetchHistoryPoints(entry.evidence_id, () => token !== state.overviewToken);
    if (history === null || token !== state.overviewToken) return null;
    if (!history.points.length) continue;   // 窗口内无该帖观测：不进入分母
    if (history.truncated) {
      // 分页仍 has_more：接口升序，截断页末点不是窗口内真实最新点。
      // 明确标记为“加载不完整”并排除，不计入完成候选，也不冒充窗口内最新值。
      state.windowTruncated += 1;
      continue;
    }
    const last = history.points[history.points.length - 1];
    candidates.set(entry.evidence_id, { ...entry, observed_at: last.observed_at,
      comment_count: last.comment_count, window_truncated: false });
  }
  return candidates;
}

function setWindowInputs(fromValue, toValue) {
  state.windowInput = { from: fromValue || "", to: toValue || "" };
  const isoFrom = toWindowIso(state.windowInput.from);
  const isoTo = toWindowIso(state.windowInput.to);
  if (isoFrom === undefined || isoTo === undefined) {
    show("观测窗口时间格式非法，未应用；请使用日期时间控件（Asia/Shanghai UTC+08:00）。", true);
    return;
  }
  if (isoFrom && isoTo && new Date(isoTo) <= new Date(isoFrom)) {
    show("观测窗口终点必须晚于起点（起点含、终点不含），未应用。", true);
    return;
  }
  state.windowFrom = isoFrom;
  state.windowTo = isoTo;
  syncFilterBars();
  loadP1();                                  // 与总览 / 议题 / 检索共用同一窗口与分母
  // 观察清单成功刷新后统一重读所选周期历史（同一新窗口）；不再并发另起旧 record 的 loadWatchHistory，避免旧值覆写。
  loadWatchlist();
}

function renderWindowPending() {
  state.groupMeta = {};
  for (const id of ["overview-topics", "topic-list", "search-results", "topic-topk", "overview-shortcuts"]) {
    const el = $("#" + id);
    if (!el) continue;
    clear(el);
    el.append(h("p", { class: "muted" }, "正在按观测窗口读取各原帖窗口内最新真实观测…（窗口外最新值不并入）"));
  }
  for (const id of ["overview-heat", "topic-summary"]) {
    const el = $("#" + id);
    if (el) { clear(el); }
  }
  for (const id of ["overview-filter-note", "topics-filter-note", "search-filter-note"]) {
    const el = $("#" + id);
    if (el) el.textContent = p1FilterSummary([]);
  }
}

/* ---------- 观察清单：人工加入 / 退出 + 同帖双图与可读历史表 ---------- */

function watchRecordFor(evidenceId) {
  const items = (state.watch && state.watch.history) || [];
  return items.find((item) => item.evidence_id === evidenceId) || null;
}

function setWatchAdminStatus(kind, text, bodyNode) {
  const host = $("#watch-admin-status");
  if (!host) return;
  clear(host);
  host.classList.remove("error", "ok");
  if (kind === "error") host.classList.add("error");
  else if (kind === "ok") host.classList.add("ok");
  host.append(h("p", { class: kind === "error" ? "error-text" : "muted" }, text));
  if (bodyNode) host.append(bodyNode);
}

// 写请求（加入 / 退出）失败：先清旧观察业务缓存，再真实重读 GET；GET 失败仍保持空，绝不复用旧值。
// 重读（成功或失败）不得覆盖写请求的原始错误证据，故最后再落管理证据。
async function refreshWatchAfterWriteFailure(err, actionLabel) {
  const adminText = `${actionLabel}失败（${p1StatusText(err)}）：${(err && err.message) || ""}`;
  const adminBody = p1BodyNode(err);
  state.watch = null;
  renderWatchlist();                 // data 为空 → 清空旧活跃/历史展示，避免继续把已过期或已改变的周期显示成活跃
  await loadWatchlist();             // 真实重读 GET；失败时 loadWatchlist 保持空并给出失败占位
  setWatchAdminStatus("error", adminText, adminBody);
  show(`${actionLabel}失败（${p1StatusText(err)}）：旧观察展示已清空并已重读；原帖、证据与历史均未改变。`, true);
}

async function submitWatchAdd(evidenceId, reason, form) {
  const text = (reason || "").trim();
  if (!text || text.length > 200) {
    show("请填写 1–200 字符的人工加入原因（小表单，不使用浏览器弹窗）。", true);
    return;
  }
  try {
    const result = await api("/api/p1/watchlist", { method: "POST", body: { evidence_id: evidenceId, reason: text } });
    show(`已加入观察（${WATCH_STATUS_LABELS[result.watch.status] || result.watch.status}）：24 小时后观察资格自然结束，不自动续期，不触发采集。`);
    if (form) form.hidden = true;
    await loadWatchlist();
    renderBusinessViews();
  } catch (err) {
    await refreshWatchAfterWriteFailure(err, "加入观察");
  }
}

async function submitWatchExit(watchId, reason, form) {
  const text = (reason || "").trim();
  if (!text || text.length > 200) {
    show("请填写 1–200 字符的人工退出原因（小表单，不使用浏览器弹窗）。", true);
    return;
  }
  try {
    const result = await api("/api/p1/watchlist/exit", { method: "POST", body: { watch_id: watchId, reason: text } });
    show(`已退出观察（${WATCH_STATUS_LABELS[result.watch.status] || result.watch.status}）：历史与证据保留，未删除。`);
    if (form) form.hidden = true;
    await loadWatchlist();
    renderBusinessViews();
  } catch (err) {
    await refreshWatchAfterWriteFailure(err, "退出观察");
  }
}

function watchReasonForm(submitLabel, onSubmit) {
  const input = h("input", { type: "text", maxlength: "200", class: "watch-reason",
    placeholder: "人工填写原因（1–200 字符）", "aria-label": "观察原因" });
  const form = h("form", { class: "watch-form" },
    h("label", { class: "watch-label" }, submitLabel.replace("确认", "") + "原因", input),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "submit" }, submitLabel),
      h("button", { class: "link", type: "button", onclick: () => { form.hidden = true; } }, "取消")));
  form.hidden = true;
  form.addEventListener("submit", (event) => { event.preventDefault(); onSubmit(input.value, form); });
  return { form, input };
}

function watchEntryControl(entry) {
  const record = watchRecordFor(entry.evidence_id);
  if (record && record.status === "active") {
    return h("span", { class: "chip watch-chip" }, "已加入观察（观察中）");
  }
  const { form, input } = watchReasonForm("确认加入观察", (value, node) => submitWatchAdd(entry.evidence_id, value, node));
  const toggle = h("button", { class: "secondary", type: "button", onclick: () => {
    form.hidden = !form.hidden;
    if (!form.hidden) input.focus();
  } }, "加入观察");
  return h("div", { class: "watch-entry" }, toggle, form);
}

function watchStatusText(item) {
  if (item.status === "active") {
    const expires = new Date(item.expires_at).getTime();
    const left = expires - Date.now();
    if (Number.isFinite(left) && left > 0) {
      // 按总剩余分钟一次取整后再拆 hours/minutes，确保 minutes 落在 0..59（避免出现 23 小时 60 分）。
      const totalMinutes = Math.floor(left / 60000);
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      return `观察中 · 距到期 ${hours} 小时 ${minutes} 分`;
    }
    return "观察中";
  }
  return WATCH_STATUS_LABELS[item.status] || item.status || "未知";
}

function watchActiveCard(item) {
  const { form, input } = watchReasonForm("确认退出观察", (value, node) => submitWatchExit(item.watch_id, value, node));
  const toggle = h("button", { class: "secondary", type: "button", onclick: () => {
    form.hidden = !form.hidden;
    if (!form.hidden) input.focus();
  } }, "退出观察");
  return h("article", { class: "card p1-entry watch-card" },
    h("div", { class: "meta" },
      h("span", { class: "badge pos" }, "观察中"),
      h("span", { class: "chip" }, `来源 ${item.source_name || "?"}`)),
    h("h3", null, linkNode(item.url, item.title || "（无标题）")),
    h("dl", { class: "kv" },
      kv("加入原因", item.entry_reason || "—"),
      kv("加入时刻（Asia/Shanghai UTC+08:00）", fmtTime(item.entered_at)),
      kv("24h 到期", `${fmtTime(item.expires_at)}（${watchStatusText(item)}）`),
      kv("首次真实观测", fmtTime(item.first_observed_at)),
      kv("最近真实观测", fmtTime(item.latest_observed_at))),
    h("div", { class: "card-actions" },
      h("button", { class: "secondary", type: "button", onclick: () => loadWatchHistory(item.watch_id) }, "查看同帖历史与曲线"),
      toggle),
    form);
}

function watchHistoryRow(item) {
  return h("tr", null,
    h("td", null, linkNode(item.url, item.title || "（无标题）")),
    h("td", null, item.source_name || "—"),
    h("td", null, WATCH_STATUS_LABELS[item.status] || item.status || "—"),
    h("td", null, fmtTime(item.entered_at)),
    h("td", null, fmtTime(item.expires_at)),
    h("td", null, item.entry_reason || "—"),
    h("td", null, item.exited_at ? `${fmtTime(item.exited_at)} · ${item.exit_reason || "—"}` : "—"),
    h("td", null, fmtTime(item.first_observed_at)),
    h("td", null, fmtTime(item.latest_observed_at)),
    h("td", null, h("button", { class: "link", type: "button", onclick: () => loadWatchHistory(item.watch_id) }, "查看")));
}

function renderWatchlist() {
  const data = state.watch;
  const activeHost = $("#watch-active");
  const historyBody = $("#watch-history-body");
  if (activeHost) clear(activeHost);
  if (historyBody) clear(historyBody);
  if (!data) return;
  const active = data.active || [];
  const history = data.history || [];
  if (activeHost) {
    if (!active.length) {
      activeHost.append(h("p", { class: "empty" },
        "当前没有活跃观察周期。请到「议题与表达线索 / 原帖检索」在原帖卡片上使用「加入观察」小表单人工加入（不抓取、不自动续期）。"));
    }
    for (const item of active) activeHost.append(watchActiveCard(item));
  }
  if (historyBody) {
    if (!history.length) {
      historyBody.append(h("tr", null, h("td", { colspan: "10", class: "muted" }, "还没有任何观察周期。")));
    }
    for (const item of history) historyBody.append(watchHistoryRow(item));
  }
  setBizStatus("ok", `观察清单：活跃 ${active.length} / ${data.active_limit ?? 3}，历史周期 ${history.length}（含已退出 / 已到期）。观察资格不随关键词或日期筛选变化。`);
}

function clearWatchCharts() {
  for (const key of ["chart-count", "chart-rate"]) {
    const chart = state.charts[key];
    if (chart && typeof chart.dispose === "function") { try { chart.dispose(); } catch (err) { /* 忽略 dispose 失败 */ } }
    state.charts[key] = null;
  }
  for (const id of ["chart-count", "chart-rate"]) {
    const el = $("#" + id);
    if (el) clear(el);
  }
}

function renderWatchPoints(points, truncated) {
  const body = $("#watch-history-points");
  if (!body) return;
  clear(body);
  if (!points || !points.length) {
    body.append(h("tr", null, h("td", { colspan: "7", class: "muted" },
      "该观测窗口内没有真实观测点；不拿窗口外的最新值冒充窗口内结果。")));
    return;
  }
  for (const point of points) {
    const count = point.comment_count || {};
    const known = count.precision === "exact" && count.value !== null && count.value !== undefined;
    const prevAt = point.previous_observed_at;
    // 实际间隔列：仅当确有真实前值时才显示“比较起点→终点（格式化时区时间）+ 秒数”，不推断缺前值。
    const intervalText = prevAt
      ? `${fmtTime(prevAt)} → ${fmtTime(point.observed_at)}（${formatInterval(point.interval_seconds)}）`
      : "—";
    let label = COMPARISON_LABELS[point.comparison] || point.comparison || "—";
    // 仅当比较起点确早于当前观测窗口起点时才说明“窗口外”，窗口内不得误标。
    if (prevAt && state.windowFrom && new Date(prevAt).getTime() < new Date(state.windowFrom).getTime()) {
      label += "；比较起点在观测窗口之外（沿用窗口外真实前值，已按真实间隔计算）";
    }
    body.append(h("tr", null,
      h("td", null, fmtTime(point.observed_at)),
      h("td", { class: "num" }, known ? String(count.value) : "未知"),
      h("td", null, p1PrecisionLabel(count.precision)),
      h("td", { class: "num" }, intervalText),
      h("td", { class: "num" }, point.delta === null || point.delta === undefined ? "—" : String(point.delta)),
      h("td", { class: "num" }, point.rate_per_hour === null || point.rate_per_hour === undefined
        ? "—" : `${fmtNumber(point.rate_per_hour)} 条/小时`),
      h("td", null, label)));
  }
  if (truncated) {
    body.append(h("tr", null, h("td", { colspan: "7", class: "error-text" },
      "历史分页达到取数上限，本表仅为已取到的部分，不声称完整历史。")));
  }
}

function renderChart(key, label, points, valueOf, captionId, kind) {
  const el = $("#" + key);
  const caption = $("#" + captionId);
  const series = points.map((point) => ({ time: point.observed_at, value: valueOf(point) }));
  const known = series.filter((item) => item.value !== null && item.value !== undefined);
  if (!points.length) {
    if (caption) caption.textContent = `${label}：窗口内无观测点（不绘图、不补 0）`;
    if (el) clear(el);
    return;
  }
  if (caption) {
    if (kind === "rate") {
      // 速率图：单个已计算速率区间也是真实速率，必须如实显示，不得说“等待第二个观测点”从而否认已算速率。
      caption.textContent = known.length === 0
        ? `${label}：暂无已计算速率区间（相邻可比精确观测点不足），不补 0、不插值`
        : known.length === 1
          ? `${label}：仅 1 个已计算速率区间，如实显示该真实速率（不补 0、不插值）`
          : `${label}：${known.length} 个已计算速率区间（缺测/未计算保留断点，不补 0、不插值）`;
    } else {
      // 窗口内仅 1 个计数观测点时：若该点已有真实可比的已计算速率（真实前值在窗口外/窗口内均可），
      // caption 不得再说“等待第二个观测点 / 不声称增速”而与速率图矛盾；改为如实说明速率以真实可比区间为准。
      const rateAvailable = points.some((point) =>
        point.rate_per_hour !== null && point.rate_per_hour !== undefined);
      caption.textContent = known.length === 1
        ? (rateAvailable
            ? `${label}：窗口内仅 1 个真实观测点；速率以真实可比区间为准（不补 0、不插值、不编点）`
            : `${label}：仅 1 个真实观测点，等待第二个观测点（不补 0、不插值、不声称增速）`)
        : `${label}：${known.length} 个可比真实观测点（缺测/未知保留断点，不补 0、不插值）`;
    }
  }
  const lib = chartLib();
  if (!lib) {
    if (el) {
      clear(el);
      el.append(h("p", { class: "muted" }, "本地图表库不可用；未使用 CDN 或占位曲线，下方可读历史表为同源真实值。"));
    }
    return;
  }
  if (!el) return;
  clear(el);
  const chart = lib.init(el);
  state.charts[key] = chart;
  chart.setOption({
    animation: false,
    grid: { left: 58, right: 18, top: 16, bottom: 44 },
    tooltip: {
      trigger: "axis",
      formatter: (params) => {
        const list = Array.isArray(params) ? params : [params];
        const lines = [];
        for (const item of list) {
          const point = series[item.dataIndex] || {};
          lines.push(`观测时刻 ${fmtTime(point.time)}`);
          lines.push(`${label}：${point.value === null || point.value === undefined ? "未知（不补 0）" : fmtNumber(point.value)}`);
        }
        return lines.join("<br/>");
      }
    },
    xAxis: { type: "category", data: series.map((item) => fmtTime(item.time)), axisLabel: { hideOverlap: true } },
    yAxis: { type: "value", scale: true },
    series: [{
      name: label, type: "line", smooth: false, connectNulls: false, symbol: "circle", symbolSize: 7,
      data: series.map((item) => (item.value === null || item.value === undefined ? null : item.value))
    }]
  });
  if (typeof chart.resize === "function") chart.resize();
}

function renderWatchHistory(record, history) {
  const points = (history && history.points) || [];
  const truncated = !!(history && history.truncated);
  const status = $("#watch-detail-status");
  if (status) {
    const windowText = p1WindowActive()
      ? `观测窗口 ${fmtTime(state.windowFrom)} 起（含）— ${state.windowTo ? fmtTime(state.windowTo) + " 止（不含）" : "至最新观测"}`
      : "观测窗口=全部观测时间";
    status.textContent = `已选周期：${WATCH_STATUS_LABELS[record.status] || record.status}；加入原因「${record.entry_reason || "—"}」；${windowText}。`
      + (truncated ? "历史分页已达上限，未声称完整。" : "");
  }
  clearWatchCharts();
  // 先渲染真实可读历史表：即使图表初始化/绘制失败，表与另一张图仍保留，绝不把已取得的真实点伪装成空窗口。
  renderWatchPoints(points, truncated);
  const chartErrors = [];
  for (const spec of [
    { key: "chart-count", label: "累计评论计数", captionId: "chart-count-status", kind: "count",
      valueOf: (point) => (point.comment_count && point.comment_count.precision === "exact" && point.comment_count.value !== null
        ? point.comment_count.value : null) },
    { key: "chart-rate", label: "每小时变化率", captionId: "chart-rate-status", kind: "rate",
      valueOf: (point) => (point.rate_per_hour === null || point.rate_per_hour === undefined ? null : point.rate_per_hour) },
  ]) {
    try {
      renderChart(spec.key, spec.label, points, spec.valueOf, spec.captionId, spec.kind);
    } catch (err) {
      // 单图异常：清空该图并给短占位，保留可读表与另一张图；真实错误只落管理证据容器。
      const chart = state.charts[spec.key];
      if (chart && typeof chart.dispose === "function") { try { chart.dispose(); } catch (ignore) { /* 忽略 dispose 失败 */ } }
      state.charts[spec.key] = null;
      const el = $("#" + spec.key);
      if (el) {
        clear(el);
        el.append(h("p", { class: "muted" }, "该图表渲染失败；下方可读历史表为同源真实值，另一张图不受影响。"));
      }
      const caption = $("#" + spec.captionId);
      if (caption) caption.textContent = `${spec.label}：图表渲染失败（保留可读历史表）`;
      chartErrors.push(`${spec.label}：${(err && err.message) || err}`);
    }
  }
  if (chartErrors.length) {
    setWatchAdminStatus("error", `图表渲染失败（已保留可读历史表与另一张图）：${chartErrors.join("；")}`);
  }
}

async function loadWatchHistory(watchId) {
  const record = ((state.watch && state.watch.history) || []).find((item) => item.watch_id === watchId);
  if (!record) { show("观察周期不存在，无法读取历史。", true); return; }
  const token = ++state.historyToken;
  state.selectedWatchId = watchId;
  const title = $("#watch-detail-title");
  if (title) title.textContent = `同帖历史：${record.title || "（无标题）"}`;
  const status = $("#watch-detail-status");
  if (status) status.textContent = "正在读取该原帖的真实观测历史…";
  clearWatchCharts();
  clear($("#watch-history-points"));
  $("#watch-history-points").append(h("tr", null, h("td", { colspan: "7", class: "muted" }, "正在读取真实观测历史…")));
  try {
    const history = await fetchHistoryPoints(record.evidence_id, () => token !== state.historyToken);
    if (history === null || token !== state.historyToken) return;
    state.history = history;
    renderWatchHistory(record, history);
  } catch (err) {
    if (token !== state.historyToken) return;
    state.history = null;
    clearWatchCharts();
    renderWatchPoints([], false);
    if (status) status.textContent = "历史读取失败：旧图与旧表已清空，未显示缓存，也未改判筛选。";
    setBizStatus("error", `历史读取失败（${p1StatusText(err)}）；旧图与旧表已清空。`);
    setWatchAdminStatus("error", `历史读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
  }
}

// 观察清单成功刷新后：以最新周期记录（status / evidence_id 均为最新）重读所选同帖历史，
// 避免退出/新观测后详情与曲线仍停留在旧值（例如列表已“已退出”而详情还写“观察中”）。
// 所选 watch_id 已不在清单中则清空详情；旧周期仍可从上方列表重新选择查看。
// 统一由 loadWatchlist 触发，不在 watch-reload 等入口并发另起旧 record 的 loadWatchHistory，防止旧值覆写新值。
async function refreshSelectedWatchHistory() {
  const watchId = state.selectedWatchId;
  if (!watchId) return;
  const record = ((state.watch && state.watch.history) || []).find((item) => item.watch_id === watchId);
  if (!record) {
    state.historyToken++;                 // 失效在途旧历史响应
    state.history = null;
    state.selectedWatchId = null;
    clearWatchCharts();
    const title = $("#watch-detail-title");
    if (title) title.textContent = "同帖历史";
    const status = $("#watch-detail-status");
    if (status) status.textContent = "所选观察周期已不在当前观察清单中；旧图与旧表已清空，可从上方重新选择周期查看。";
    const points = $("#watch-history-points");
    if (points) {
      clear(points);
      points.append(h("tr", null, h("td", { colspan: "7", class: "muted" }, "尚未选择观察周期。")));
    }
    return;
  }
  await loadWatchHistory(watchId);
}

async function loadWatchlist() {
  const token = ++state.watchToken;
  try {
    const data = await api("/api/p1/watchlist");
    if (token !== state.watchToken) return;
    state.watch = data;
    renderWatchlist();
    await refreshSelectedWatchHistory();   // 成功刷新后按最新周期记录更新所选详情/曲线（不存在则清空）
  } catch (err) {
    if (token !== state.watchToken) return;
    state.watch = null;
    state.historyToken++;                 // 失效在途 / 旧历史响应
    state.history = null;
    state.selectedWatchId = null;
    clearWatchCharts();
    clear($("#watch-active"));
    clear($("#watch-history-body"));
    $("#watch-history-body").append(h("tr", null, h("td", { colspan: "10", class: "muted" }, "观察清单读取失败，旧展示已清空。")));
    const points = $("#watch-history-points");
    if (points) {
      clear(points);
      points.append(h("tr", null, h("td", { colspan: "7", class: "muted" }, "观察清单读取失败，旧同帖历史点已清空。")));
    }
    const detail = $("#watch-detail-status");
    if (detail) detail.textContent = "观察清单读取失败：旧观察展示、旧同帖历史点与旧图均已清空，未显示缓存。";
    setBizStatus("error", `观察清单读取失败（${p1StatusText(err)}）；旧观察展示与旧历史已清空。`);
    setWatchAdminStatus("error", `观察清单读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
  }
}


function setBackgroundInert(inert) {
  // .skip（跳过导航到主内容）也是可聚焦背景控件，必须一起隔离/恢复，否则抽屉打开时仍可 Tab 到背景。
  for (const selector of ["#main", ".top", "#main-nav", "footer", ".skip"]) {
    const el = $(selector);
    if (!el) continue;
    if (inert) el.setAttribute("inert", "");
    else el.removeAttribute("inert");
  }
}

function showDrawer(open) {
  const drawer = $("#drawer");
  if (!drawer) return;
  drawer.hidden = !open;
  drawer.setAttribute("aria-hidden", open ? "false" : "true");
  setBackgroundInert(open);          // 隔离背景：打开时背景不可聚焦/不可交互
}

function closeDrawer(clearBody = false) {
  state.drawerToken++;               // 关闭使在途抽屉请求失效
  if (clearBody) {
    const body = $("#drawer-body");
    if (body) clear(body);
  }
  showDrawer(false);
  const trigger = state.drawerTrigger;
  state.drawerTrigger = null;
  if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) trigger.focus();
}

function renderP1Drawer(data) {
  const body = $("#drawer-body");
  clear(body);
  const ev = data.evidence || {};
  let other = {};
  try { other = ev.other_counts ? JSON.parse(ev.other_counts) : {}; } catch (err) { other = {}; }
  const count = { value: ev.comment_value, precision: ev.comment_precision, source_field: ev.comment_source_field };
  const counted = count.value !== null && count.value !== undefined;
  const contentText = ev.content || "";
  const contentNode = contentText
    ? h("p", { class: "drawer-content" }, contentText)
    : h("p", { class: "muted" }, ev.evidence_kind === "title_only"
        ? "仅标题线索：尚无帖主正文或来源摘要，暂不生成梗概或表达判断。"
        : "（无正文或来源摘要内容）");
  body.append(
    h("p", { class: "drawer-title-text" }, ev.title || "（无标题）"),
    h("dl", { class: "kv wide" },
      kv("来源", ev.source_name || "—"),
      kv("原标题", ev.title || "（空）"),
      kv("原地址", linkNode(ev.url)),
      kv("证据类型", p1KindLabel(ev.evidence_kind)),
      kv("正文 / 来源摘要", contentNode),
      kv("观测时间", fmtTime(ev.observed_at)),
      kv("发布时间", ev.published_at ? `${fmtTime(ev.published_at)}（${qualityLabel(ev.time_quality)}）` : "（未提供）"),
      kv("评论数（累计口径）", counted
        ? `${count.value}（${p1PrecisionLabel(count.precision)}）；该原帖历史累计评论数的最新真实观测，逐帖曲线见「观察清单」。`
        : `未知（${p1PrecisionLabel(count.precision)}）：未采得可核验评论数，不计入 0。`),
      kv("评论数来源", p1CommentSourceLabel(count.source_field)),
      kv("其他计数（不计入评论数）", p1OtherCountsNode(other))
    ),
    ev.evidence_kind === "title_only"
      ? h("p", { class: "hint" }, "证据不足：仅有标题线索，属议题候选，不作事件承认或表达判断。")
      : null
  );
}

async function openP1Drawer(evidenceId, trigger) {
  const token = ++state.drawerToken;
  const body = $("#drawer-body");
  const title = $("#drawer-title");
  state.drawerTrigger = trigger || document.activeElement;
  if (title) title.textContent = "原帖详情";
  clear(body);
  body.append(h("p", { class: "muted" }, "正在读取原帖详情…"));
  showDrawer(true);
  const closeButton = $("#drawer-close");
  if (closeButton) closeButton.focus();
  try {
    const data = await api(`/api/p1/evidence/${encodeURIComponent(evidenceId)}`);
    if (token !== state.drawerToken) return;   // 过期响应：不覆写新状态
    renderP1Drawer(data);
  } catch (err) {
    if (token !== state.drawerToken) return;
    clear(body);
    body.append(
      h("h3", { class: "detail-sub" }, "原帖详情读取失败"),
      h("p", { class: "error-text" }, `${p1StatusText(err)}：原帖详情未能读取。`),
      h("p", { class: "muted" }, "旧详情已清空，未把旧数据当作成功结果。完整错误响应体可在「数据管理 → 人工证据」查看。")
    );
    // 业务抽屉只给固定短提示 + HTTP 状态；完整 err.message 与原始响应体只写数据管理状态容器。
    setAdminStatus("error", `原帖详情读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
  }
}

async function loadP1() {
  const token = ++state.overviewToken;
  setBizStatus("loading", "正在读取本地数据…");
  // 窗口生效时立即进入 pending：同步清空上一观测窗口的三业务旧候选、旧卡与筛选 note，
  // 表明新窗口正在读取；读取期间绝不继续展示上一窗口结果，也不用窗口外最新值冒充窗口内候选。
  if (p1WindowActive()) {
    state.windowCandidates = null;
    state.windowTruncated = 0;
    renderWindowPending();
  }
  try {
    // 始终按候选上限载入全部候选（top_k=3），展示用 K 在前端过滤后重算，避免后端截断候选。
    const data = await api(`/api/p1/overview?top_k=${P1_LOAD_TOP_K}`);
    if (token !== state.overviewToken) return;
    applyP1(data);
    buildFilterBars();
    state.windowCandidates = null;
    state.windowTruncated = 0;
    renderBusinessViews();
    await enrichP1Contents(token);
    if (token !== state.overviewToken) return;
    if (p1WindowActive()) {
      const candidates = await loadWindowCandidates(token);
      if (candidates === null || token !== state.overviewToken) return;
      state.windowCandidates = candidates;
    }
    renderBusinessViews();
    setBizStatus("ok", `已读取本地数据：全库候选 ${data.identity_total ?? 0} 条，人工分组 ${data.group_count ?? 0} 组；`
      + (p1WindowActive()
        ? `观测窗口内 ${p1ActiveEntries().length} 条进入同一分母（窗口外最新值不并入）。`
        : "未按观测日期筛选；逐帖计数与速率曲线见「观察清单」。"));
  } catch (err) {
    if (token !== state.overviewToken) return;
    p1Reset();
    setBizStatus("error", `数据读取失败（${p1StatusText(err)}）；旧材料已清空，未显示缓存。`);
    // p1Reset 之后再写管理状态，避免清空动作覆盖新的错误证据；完整 message 与原始响应体只落管理区。
    setAdminStatus("error", `概览读取失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
  }
}

function setAdminStatus(kind, text, bodyNode) {
  const host = $("#p1-admin-status");
  if (!host) return;
  clear(host);
  host.classList.remove("error", "ok");
  if (kind === "error") host.classList.add("error");
  else if (kind === "ok") host.classList.add("ok");
  host.append(h("p", { class: kind === "error" ? "error-text" : "muted" }, text));
  if (bodyNode) host.append(bodyNode);
}

async function submitP1Import() {
  const button = $("#p1-import-submit");
  const textarea = $("#p1-import-records");
  const raw = textarea.value.trim();
  if (!raw) {
    show("请先在文本框中粘贴人工已取证的 records 数组（本页面不预填、不自动导入）。", true);
    return;
  }
  let records;
  try {
    records = JSON.parse(raw);
  } catch (err) {
    show(`JSON 解析失败：${err.message}`, true);
    return;
  }
  if (!Array.isArray(records)) {
    show("内容必须是 records 数组（形如 [{...}]）。", true);
    return;
  }
  if (records.length > 3) {
    show(`候选记录一次最多 3 条，且全库候选总数 ≤3；当前 ${records.length} 条，服务端将返回 422/409。`, true);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/p1/import", { method: "POST", body: { records } });
    show(`导入完成：新增 ${result.added}，重复 ${result.duplicates}，候选总数 ${result.identity_total} / ${result.candidate_limit}。`);
    textarea.value = "";
    setAdminStatus("ok", `导入完成：新增 ${result.added}，重复 ${result.duplicates}，候选总数 ${result.identity_total} / ${result.candidate_limit}。`, null);
    await loadP1();
    // 导入新观测后：等待最新观察清单（最新观测/到期状态），并按最新所选周期重读同帖历史，
    // 避免旧 evidence_id、旧图；此处沿用 loadWatchlist 的失败清空语义（GET 失败仍为空，原导入状态留证）。
    await loadWatchlist();
    renderBusinessViews();
  } catch (err) {
    // 导入失败（含 422）：清空旧材料与旧详情，绝不把旧数据当成功。
    p1Reset();
    setBizStatus("error", `导入失败（${p1StatusText(err)}）；旧材料与详情已清空，未显示缓存。`);
    // 业务 globalnotice 只给固定短提示；不拼 err.message（可能含 API 路径/hash）。
    show(`导入失败（${p1StatusText(err)}）；旧材料与详情已清空，未显示缓存。`, true);
    // 工程错误证据（真实 HTTP 状态 + 原始响应体）只写数据管理「人工证据」状态容器。
    setAdminStatus("error", `导入失败（${p1StatusText(err)}）：${(err && err.message) || ""}`, p1BodyNode(err));
  } finally {
    button.disabled = !state.online;
  }
}

/* ---------- 数据加载 ---------- */

function applyData(data) {
  state.data = data;
  state.manualLimit = data.manual_limit ?? null;
  state.feedsById = {};
  for (const feed of data.feeds || []) state.feedsById[feed.id] = feed;
  state.runsById = {};
  for (const run of data.runs || []) state.runsById[run.id] = run;
  renderRefreshLabel();
  renderStats();
  renderFeeds();
  renderSourceOptions();
  renderKeywords();
  renderRecords();
  renderRuns();
}

function setWriteAvailability(enabled) {
  for (const selector of ["#refresh", "#report-generate", "#report-copy", "#feed-form button[type=submit]",
                          "#keyword-form button[type=submit]", "#import-form button[type=submit]",
                          "#analysis-form button[type=submit]", "#p1-import-submit"]) $(selector).disabled = !enabled;
}

function invalidateData() {
  state.data = null;
  state.manualLimit = null;
  state.feedsById = {};
  state.runsById = {};
  state.online = false;
  renderRefreshLabel();
  setWriteAvailability(false);
  for (const id of ["stat-records", "stat-canonical", "stat-unknown", "stat-unknown-ratio", "stat-feeds", "stat-shown",
                    "meta-schema", "meta-coverage", "meta-analysis", "quality-dist", "legacy-note"]) $("#" + id).textContent = "未知（读取失败）";
  clear($("#feeds-body"));
  $("#feeds-body").append(h("tr", null, h("td", {colspan:"6"}, "服务离线或读取失败，来源状态未知。")));
  clear($("#runs-body"));
  $("#runs-body").append(h("tr", null, h("td", {colspan:"10"}, "服务离线或读取失败，批次状态未知。")));
  clear($("#records"));
  $("#records").append(h("p", {class:"empty"}, "服务离线或读取失败，当前无可展示的实时数据。"));
  $("#records-summary").textContent = "当前数据不可用，旧数据已隐藏。";
  $("#record-detail").hidden = true;
  $("#run-detail").hidden = true;
  clear($("#runtime-body"));
  $("#runtime-body").append(...kv("状态", "读取失败，旧运行时信息已隐藏。"));
  $("#report-out").value = "";
  clear($("#import-feed"));
  $("#analysis-body").textContent = "读取失败，旧分析队列已隐藏。";
  p1Reset();
  state.watch = null;
  state.watchToken++;
  state.historyToken++;
  const watchActive = $("#watch-active");
  if (watchActive) {
    clear(watchActive);
    watchActive.append(h("p", { class: "empty" }, "服务离线或读取失败，观察清单旧展示已清空，未显示缓存。"));
  }
  const watchBody = $("#watch-history-body");
  if (watchBody) {
    clear(watchBody);
    watchBody.append(h("tr", null, h("td", { colspan: "10", class: "muted" }, "服务离线或读取失败，旧观察历史已清空。")));
  }
  const watchPoints = $("#watch-history-points");
  if (watchPoints) {
    clear(watchPoints);
    watchPoints.append(h("tr", null, h("td", { colspan: "7", class: "muted" }, "服务离线或读取失败，旧历史点已清空。")));
  }
  const watchDetail = $("#watch-detail-status");
  if (watchDetail) watchDetail.textContent = "服务离线或读取失败，旧图与旧表已清空，未显示缓存。";
  setBizStatus("error", "服务离线或读取失败，旧材料与详情已清空，未显示缓存。");
}

async function load({keepNotice = false} = {}) {
  setConnection("loading", "正在读取本地数据…");
  try {
    const data = await api("/api/data");
    applyData(data);
    state.online = true;
    setWriteAvailability(true);
    state.loadedAt = new Date();
    setConnection("online", `在线 · 已读取本地数据（${fmtTime(state.loadedAt.toISOString())}）`);
    if (!keepNotice) show("已从本地服务读取最新数据。");
  } catch (err) {
    invalidateData();
    if (err instanceof ApiError && err.kind === "offline") {
      setConnection("offline", "离线：无法连接本地服务，未显示缓存数据。");
    } else {
      const status = err.status ? `HTTP ${err.status}` : "错误";
      setConnection("error", `读取失败（${status}）：${err.message}`);
    }
    show(err.message, true);
  }
}

/* ---------- 事件绑定 ---------- */

for (const tab of $$(".tab")) {
  tab.addEventListener("click", () => { selectView(tab.dataset.view); syncFilterBars(); });
  tab.addEventListener("keydown", (event) => {
    const tabs = $$(".tab");
    const index = tabs.indexOf(tab);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      selectView(next.dataset.view);
      syncFilterBars();
      next.focus();
    }
  });
}

for (const tab of $$(".admin-tab")) {
  tab.addEventListener("click", () => selectAdminView(tab.dataset.admin));
  tab.addEventListener("keydown", (event) => {
    const tabs = $$(".admin-tab");
    const index = tabs.indexOf(tab);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      selectAdminView(next.dataset.admin);
      next.focus();
    }
  });
}

$("#feed-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget; // 异步前捕获，避免 await 后 currentTarget 失效
  const form = new FormData(formElement);
  try {
    await api("/api/feeds", { method: "POST", body: { name: form.get("name"), url: form.get("url") } });
    formElement.reset();
    show("来源已添加。");
    await load({keepNotice:true});
  } catch (err) {
    show(err.message, true);
  }
});

$("#keyword-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const value = formElement.elements.keywords.value;
  const keywords = value.split(/[，,\n\r]+/).map((item) => item.trim()).filter(Boolean);
  try {
    await api("/api/keywords", { method: "POST", body: { keywords } });
    show("关键词已保存到本地配置。");
    await load({keepNotice:true});
  } catch (err) {
    show(err.message, true);
  }
});

$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const feedId = Number($("#import-feed").value);
  const text = formElement.elements.records.value.trim();
  if (!feedId) {
    show("请先选择导入目标来源。", true);
    return;
  }
  let records;
  try {
    records = JSON.parse(text);
  } catch (err) {
    show(`JSON 解析失败：${err.message}`, true);
    return;
  }
  if (!Array.isArray(records)) {
    show("记录必须是 JSON 数组。", true);
    return;
  }
  if (state.manualLimit != null && records.length > state.manualLimit) {
    show(`记录数 ${records.length} 超过 manual_limit ${state.manualLimit}，服务端将返回 422；请减少后再试。`, true);
    return;
  }
  const button = formElement.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const result = await api("/api/import", { method: "POST", body: { feed_id: feedId, records } });
    show(`导入完成：批次 ${result.id}，状态 ${statusLabel(result.status)}，输入 ${result.input_count}，新增 ${result.added}，重复 ${result.duplicates}。`);
    formElement.elements.records.value = "";
    await load({keepNotice:true});
  } catch (err) {
    if (err.status === 422 && err.payload && err.payload.run_id) {
      show(`导入被拒（HTTP 422）：${err.message}；批次 ${err.payload.run_id} 已记为 ${statusLabel(err.payload.status)}。`, true);
      await load({keepNotice:true});
    } else {
      const status = err.status ? `HTTP ${err.status}` : "连接失败";
      show(`导入失败（${status}）：${err.message}`, true);
    }
  } finally {
    button.disabled = !state.online;
  }
});

$("#refresh").addEventListener("click", async () => {
  const button = $("#refresh");
  const limit = state.manualLimit;
  button.disabled = true;
  show(limit == null ? "正在刷新来源…" : `按编号处理前 ${limit} 个来源，每源最多 ${limit} 条。`);
  try {
    const result = await api("/api/refresh", { method: "POST", body: {} });
    const results = result.results || [];
    if (!results.length) {
      show("没有配置来源，refresh 成功但无输入。");
    } else {
      const summary = results.map((item) => {
        if (item.status === "failed") return `${item.name}：失败（${item.error || "未知错误"}）`;
        return `${item.name}：${statusLabel(item.status)}，新增 ${item.added}，重复 ${item.duplicates}`;
      }).join("；");
      show(`刷新完成。${summary}`);
    }
    await load({keepNotice:true});
  } catch (err) {
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    show(`刷新失败（${status}）：${err.message}`, true);
  } finally {
    button.disabled = !state.online;
    renderRefreshLabel();
  }
});

for (const id of ["#filter-source", "#filter-quality", "#filter-sentiment"]) {
  $(id).addEventListener("change", () => renderRecords());
}
$("#filter-keyword").addEventListener("input", () => renderRecords());

$("#record-close").addEventListener("click", () => { $("#record-detail").hidden = true; });
$("#run-close").addEventListener("click", () => { $("#run-detail").hidden = true; });

$("#report-generate").addEventListener("click", generateReport);
$("#report-copy").addEventListener("click", async () => {
  const out = $("#report-out");
  if (!out.value) generateReport();
  try {
    await navigator.clipboard.writeText(out.value);
    show("报告模板已复制到剪贴板。");
  } catch (err) {
    out.focus();
    out.select();
    show("剪贴板不可用，已选中文本，请手动复制（Ctrl+C）。", true);
  }
});

$("#runtime-load").addEventListener("click", async () => {
  const button = $("#runtime-load");
  button.disabled = true;
  try {
    const info = await api("/api/runtime");
    renderRuntime(info);
    show("已读取运行时信息（仅回环诊断）。");
  } catch (err) {
    const status = err.status ? `HTTP ${err.status}` : "连接失败";
    show(`读取运行时失败（${status}）：${err.message}`, true);
  } finally {
    button.disabled = false;
  }
});

$("#reload-data").addEventListener("click", () => { load(); loadP1(); loadWatchlist(); });
for (const id of ["#overview-reload", "#topics-reload", "#search-reload"]) {
  $(id).addEventListener("click", () => { load(); loadP1(); });
}
$("#watch-reload").addEventListener("click", () => {
  load();
  loadP1();
  // loadWatchlist 成功后按最新周期记录重读所选历史；不在此并发旧 record 的 loadWatchHistory，避免旧值覆写新值。
  loadWatchlist();
});
// 单图 resize 异常独立捕获：只清理该图并给业务短提示，保留可读历史表与另一张图；
// 真实 error 只落数据管理「人工证据」状态容器留证。沿用已实现的图表 try/catch 逻辑，不新增框架。
function resizeWatchChart(key, label) {
  const chart = state.charts[key];
  if (!chart || typeof chart.resize !== "function") return;
  try {
    chart.resize();
  } catch (err) {
    try { if (typeof chart.dispose === "function") chart.dispose(); } catch (ignore) { /* 忽略 dispose 失败 */ }
    state.charts[key] = null;
    const el = $("#" + key);
    if (el) {
      clear(el);
      el.append(h("p", { class: "muted" }, "该图表调整尺寸失败；下方可读历史表为同源真实值，另一张图不受影响。"));
    }
    const caption = $("#" + key + "-status");
    if (caption) caption.textContent = `${label}：图表调整尺寸失败（保留可读历史表）`;
    setWatchAdminStatus("error", `图表调整尺寸失败（已保留可读历史表与另一张图）：${label}：${(err && err.message) || err}`);
  }
}
window.addEventListener("resize", () => {
  resizeWatchChart("chart-count", "累计评论计数");
  resizeWatchChart("chart-rate", "每小时变化率");
});

/* ---------- 业务导航与 P1 事件绑定 ---------- */

$("#topics-topk").addEventListener("change", () => {
  state.topK = $("#topics-topk").value || "3";
  // 展示用 K 只影响视图内重算与显示，不重新请求、不影响已载入候选全集。
  renderBusinessViews();
});
$("#p1-import-submit").addEventListener("click", submitP1Import);
$("#topics-goto-report").addEventListener("click", () => { selectView("admin"); selectAdminView("report"); });

// 详情侧抽屉：关闭按钮 / 背景点击 / ESC，关闭后焦点回到触发按钮；打开时圈定 Tab 焦点。
$("#drawer-close").addEventListener("click", () => closeDrawer(true));
$("#drawer-backdrop").addEventListener("click", () => closeDrawer(true));

const FOCUSABLE_SELECTOR = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

document.addEventListener("keydown", (event) => {
  const drawer = $("#drawer");
  if (!drawer || drawer.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDrawer(true);
    return;
  }
  if (event.key !== "Tab") return;
  const panel = $(".drawer-panel", drawer);
  if (!panel) return;
  const items = $$(FOCUSABLE_SELECTOR, panel).filter((el) => el.offsetParent !== null || el === document.activeElement);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (!panel.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

async function loadAnalysis() {
  const body = $("#analysis-body");
  clear(body);
  try {
    const data = await api("/api/analysis");
    body.append(h("p", null, `生产分析：${data.production_status}；适配器 ${data.adapter}；每批请求预算 ${data.request_budget}；费用未知。`));
    body.append(h("p", {class:"muted"}, data.warning));
    for (const item of data.jobs) {
      const card = h("article", {class:"card"}, h("h3", null, item.group_id), h("dl", {class:"kv"},
        kv("批次 ID", item.id), kv("输入哈希", item.input_hash), kv("状态", item.status),
        kv("错误", item.error || "无"), kv("已占用请求", `${item.budget.reserved} / ${item.budget.maximum}`),
        kv("证据统计", JSON.stringify(item.packet.statistics))),
        h("details", null, h("summary", null, "查看证据包与请求账本"), h("pre", {class:"json"}, JSON.stringify(item, null, 2))));
      if (item.status === "human_review") {
        const reason = h("input", {placeholder:"人工决定理由", "aria-label":"人工决定理由", maxlength:"2000"});
        card.append(reason);
        for (const [decision, label] of [["accept", "接收结论"], ["reject", "驳回结论"]]) {
          card.append(h("button", {type:"button", onclick: async () => {
            if (!reason.value.trim()) { show("请填写决定理由。", true); return; }
            try { await api("/api/analysis/decision", {method:"POST", body:{job_id:item.id,decision,reason:reason.value}}); await loadAnalysis(); }
            catch (err) { show(`人工决定失败（HTTP ${err.status}）：${err.message}`, true); }
          }}, label));
        }
      }
      body.append(card);
    }
    if (!data.jobs.length) body.append(h("p", null, "分析队列为空。"));
    body.append(h("h3", null, "质量与基线候选（未复核）"));
    for (const item of data.alerts) body.append(h("pre", {class:"json"}, JSON.stringify(item, null, 2)));
  } catch (err) {
    clear(body);
    body.append(h("p", {class:"error-text"}, `读取失败（HTTP ${err.status || 0}）：${err.message}；旧队列已隐藏。`));
  }
}
$("#analysis-load").addEventListener("click", loadAnalysis);
$("#analysis-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const record_ids = form.elements.record_ids.value.split(/\s+/).filter(Boolean);
  try {
    const result = await api("/api/analysis", {method:"POST", body:{group_id:form.elements.group_id.value,record_ids}});
    show(`证据包已保存，状态 ${result.status}；${result.error || "等待复核"}`);
    await loadAnalysis();
  } catch (err) { show(`创建失败（HTTP ${err.status || 0}）：${err.message}`, true); }
});
load();
loadP1();
loadWatchlist();
