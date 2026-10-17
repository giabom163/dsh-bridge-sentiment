const $ = (id) => document.getElementById(id);
const state = { tasks: [], selected: null, plans: new Map(), action: null, editTask: null, connected: true, hasLoadedTasks: false, lastSuccessAt: null, graph: null, filter: null };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
function prettyTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
}
const FALLBACK_STATUS = {ready:"待规划",prepared:"空会话待确认",running:"执行中",awaiting_review:"待审查",awaiting_user:"待用户验收",accepted:"成功",rework:"返修待规划",failed:"失败",cancelled:"作废",stop_unconfirmed:"停止未确认",cancel_requested:"取消中"};
function labelStatus(value) {
  if (window.BridgeTaskGraph) return window.BridgeTaskGraph.statusLabel(value);
  if (value === null || value === undefined || value === "") return "未知";
  return FALLBACK_STATUS[value] || `未知（${value}）`;
}
function labelAcceptance(value) {
  if (window.BridgeTaskGraph) return window.BridgeTaskGraph.acceptanceLabel(value);
  if (value === null || value === undefined || value === "") return "未知（未记录）";
  return ({PASS:"通过",FAIL:"失败",BLOCKED:"阻塞"})[value] || `未知（${value}）`;
}
function labelModel(value) {
  if (window.BridgeTaskGraph) return window.BridgeTaskGraph.modelLabel(value);
  return value === null || value === undefined || value === "" ? "未记录" : String(value);
}
function toast(message, error = false) {
  const item = $("toast"); item.textContent = message; item.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => item.className = "toast", 3400);
}
async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options, headers:{"Content-Type":"application/json", ...(options.headers || {})}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function refresh(keep = true) {
  try {
    const data = await api("/api/tasks"); state.tasks = data.tasks || [];
    // 已经确认成功（accepted）和已经作废（cancelled）的任务沉到最下面：它们不再需要动作。
    const sunk = row => ["accepted", "cancelled"].includes(row.state && row.state.status) ? 1 : 0;
    const stamp = row => Date.parse((row.state && (row.state.finished_at || row.state.started_at || row.state.created_at)) || "") || 0;
    state.tasks.sort((a, b) => (sunk(a) - sunk(b)) || (stamp(b) - stamp(a)));
    state.connected = true; state.hasLoadedTasks = true; state.lastSuccessAt = new Date().toISOString();
    $("taskCount").textContent = state.tasks.length;
    renderMetrics(); renderRows();
    if (state.selected && keep && state.tasks.some(x => x.task_id === state.selected)) await selectTask(state.selected);
    else if (!state.selected) $("detailPanel").innerHTML = placeholder();
    renderGraph();
  } catch (error) {
    // 保留已有数据，但区分“从未成功加载”与“已有数据后断线”：不把未知当成空。
    state.connected = false;
    $("taskCount").textContent = state.hasLoadedTasks ? state.tasks.length : "—";
    renderMetrics(); renderRows();
    renderGraph();
    toast(error.message, true);
  }
}
function renderGraph() {
  updateConnection();
  if (!state.graph) return;
  state.graph.update(state.tasks, state.hasLoadedTasks);
  state.graph.setFilter($("searchInput").value);
  if (state.selected) state.graph.select(state.selected);
  state.graph.setConnection(state.connected, state.lastSuccessAt);
}
function updateConnection() {
  const el = $("connectionState"); if (!el) return;
  const time = state.lastSuccessAt ? new Date(state.lastSuccessAt).toLocaleTimeString("zh-CN", {hour12:false}) : "尚未成功";
  el.classList.toggle("dropped", !state.connected);
  el.innerHTML = `<i></i> ${state.connected ? "本地连接" : `已断连 · 最后成功 ${esc(time)}`}`;
  el.title = `最后成功刷新 ${time}`;
}
function initGraph() {
  const errorEl = $("graphError");
  if (!window.BridgeTaskGraph) {
    errorEl.hidden = false;
    errorEl.textContent = "协同图组件 /task-graph.js 未加载；任务表与详情仍可使用，不做假图降级。";
    return null;
  }
  try {
    const graph = window.BridgeTaskGraph.mount({graphId:"taskGraph", listId:"graphList", statusId:"graphStatus",
      noteId:"graphNote", emptyId:"graphEmpty", errorId:"graphError", onSelect:(id) => selectTask(id)});
    if (!graph.available && graph.error) toast(graph.error, true);
    return graph.available ? graph : null;
  } catch (error) {
    errorEl.hidden = false;
    errorEl.textContent = `协同图初始化失败：${error.message}`;
    return null;
  }
}
function metricCard(key, title, value, sub, icon) {
  return `<article class="metric ${state.filter === key ? "active" : ""}" data-filter="${key}" role="button" tabindex="0" title="点击筛选"><div class="metric-top"><span>${title}</span><span class="metric-icon">${icon}</span></div><div class="metric-value">${value}</div><div class="metric-sub">${sub}</div></article>`;
}
function renderMetrics() {
  if (!state.hasLoadedTasks) {
    // 从未成功读取任务：计数未知，显示"—"而不是 0，避免把未知当成空。
    const unknown = [["all","任务总数","—","尚未成功读取","◫"],["review","待我审查","—","尚未成功读取","◇"],["reviewable","可审产物","—","尚未成功读取","✓"],["failed","执行失败","—","尚未成功读取","! "]];
    $("metrics").innerHTML = unknown.map(([k,t,v,s,i]) => metricCard(k,t,v,s,i)).join("");
    return;
  }
  const counts = { all: state.tasks.length, review: 0, reviewable: 0, failed: 0 };
  for (const row of state.tasks) {
    const st = row.state.status, facts = row.state.execution_facts || {};
    if (["awaiting_review", "awaiting_user"].includes(st)) counts.review++;
    if (row.state.reviewable === true) counts.reviewable++;
    if (["failed", "stop_unconfirmed", "cancelled"].includes(st) || facts.exit_code > 0
        || (facts.violations || []).length || (facts.missing_artifacts || []).length
        || (facts.native_error_count || 0) > 0) counts.failed++;
  }
  const metrics = [["all","任务总数",counts.all,"本地任务记录","◫"],["review","待我审查",counts.review,"待审查 + 待接收","◇"],
                   ["reviewable","可审产物",counts.reviewable,"review_gate 通过","✓"],["failed","执行失败",counts.failed,"越界 / 缺产物 / 非零退出","! "]];
  $("metrics").innerHTML = metrics.map(([k,t,v,s,i]) => metricCard(k,t,v,s,i)).join("");
  document.querySelectorAll(".metric[data-filter]").forEach(el => el.addEventListener("click", () => {
    state.filter = (state.filter === el.dataset.filter && el.dataset.filter !== "all") ? null : el.dataset.filter;
    renderMetrics(); renderRows();
  }));
}
function renderRows() {
  const query = $("searchInput").value.trim().toLowerCase();
  const rows = state.tasks.filter(x => {
    const st = x.state.status;
    if (state.filter === "review") return ["awaiting_review", "awaiting_user"].includes(st);
    if (state.filter === "reviewable") return x.state.reviewable === true;
    if (state.filter === "failed") return ["failed", "stop_unconfirmed", "cancelled"].includes(st);
    return true;
  }).filter(x => `${x.task_id} ${x.task.goal}`.toLowerCase().includes(query));
  // 表格空态与图、文本列表遵循同一判别：从未成功加载 ≠ 成功加载且确为空。
  const unknown = !state.hasLoadedTasks;
  $("emptyState").classList.toggle("visible", state.tasks.length === 0);
  $("emptyTitle").textContent = unknown ? "无法读取任务" : "还没有任务";
  $("emptyHint").textContent = unknown ? "尚未成功加载任务列表；无法确认任务是否为空。请等待重试或检查本机连接。" : "创建任务契约后，可在这里规划、执行和审查。";
  $("emptyNewButton").hidden = unknown;
  $("taskRows").innerHTML = rows.map(row => `<tr class="task-row ${["accepted", "cancelled"].includes(row.state.status) ? "sunk" : ""} ${state.selected === row.task_id ? "selected" : ""}" data-id="${esc(row.task_id)}"><td><div class="task-title">${esc(row.task.title || row.task.goal)}</div><div class="task-id">${esc(row.task_id)} · r${esc(row.state.revision)}</div></td><td><span class="executor">${esc(row.task.executor)}</span></td><td><span class="badge ${esc(row.state.status)}">${esc(labelStatus(row.state.status))}</span></td><td class="update-time">${esc(prettyTime(row.state.finished_at || row.state.started_at || row.state.created_at))}</td><td class="row-open">›</td></tr>`).join("");
  document.querySelectorAll(".task-row").forEach(el => el.addEventListener("click", () => selectTask(el.dataset.id)));
}
function placeholder() { return `<div class="detail-placeholder"><div class="placeholder-art"><i></i><i></i><i></i></div><strong>选择一个任务</strong><span>计划、执行结果和审查记录会显示在这里。</span></div>`; }
function actionButtons(data) {
  const s = data.state.status, id = data.task.task_id;
  const planButton = ["ready","rework","prepared"].includes(s) ? `<button class="primary-button small-button" data-action="plan">生成计划</button>` : "";
  const shared = data.task.opencode_mode === "shared";
  const prepareButton = shared && ["ready","rework"].includes(s) && state.plans.has(id) ? `<button class="primary-button small-button" data-action="prepare">创建可见空会话</button>` : "";
  const runButton = ["ready","rework","prepared"].includes(s) && (!shared || s === "prepared") && state.plans.has(id) ? `<button class="primary-button small-button" data-action="run">发送提示词执行</button>` : "";
  const cancelButton = s === "running" && data.state.current_attempt ? `<button class="danger-button small-button" data-action="cancel">请求取消</button>` : "";
  const auditButton = s === "awaiting_review" ? `<button class="primary-button small-button" data-action="audit">只读审查（通过 / 不通过）</button>` : "";
  const acceptButton = s === "awaiting_user" ? `<button class="primary-button small-button" data-action="accept">附证据并接收</button>` : "";
  const rejectButton = ["awaiting_review", "awaiting_user"].includes(s) ? `<button class="danger-button small-button" data-action="reject">打回返修</button>` : "";
  const dismissButton = (data.state.human_accept && data.state.human_accept.decision === "reject")
    ? `<button class="secondary-button small-button" data-action="dismiss">撤销打回</button>` : "";
  const reviseButton = !["running","prepared","stop_unconfirmed","accepted"].includes(s) ? `<button class="secondary-button small-button" data-action="revise">修订契约</button>` : "";
  const voidButton = !["running", "prepared", "cancel_requested", "cancelled"].includes(s)
    ? `<button class="secondary-button small-button" data-action="void">作废</button>` : "";
  const nativeButton = data.native ? `<button class="secondary-button small-button" data-action="native-status">回读原生状态</button>` : "";
  return planButton + prepareButton + runButton + nativeButton + cancelButton + auditButton + acceptButton + rejectButton + dismissButton + reviseButton + voidButton;
}
async function selectTask(id) {
  state.selected = id; renderRows();
  if (state.graph) state.graph.select(id);
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(id)}`); renderDetail(data);
  } catch (error) { toast(error.message, true); }
}
// 芯片与折叠块：flowHtml / timelineHtml / renderDetail 都要用，必须放在模块作用域（曾因放在
// renderDetail 内部导致 flowHtml 抛 "chip is not defined"，整个详情面板渲染失败——实机测试抓到）。
function chip(label, value, tone) {
  return `<span class="chip ${tone || ""}"><i>${esc(label)}</i>${esc(value)}</span>`;
}
function fold(title, body, count) {
  return `<details class="fold"><summary>${esc(title)}${count ? `<b>${esc(String(count))}</b>` : ""}</summary><div class="fold-body">${body}</div></details>`;
}
function durationLabel(ms) {
  if (!isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return Math.round(ms) + " ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + " s";
  return Math.floor(ms / 60000) + " 分 " + Math.round((ms % 60000) / 1000) + " s";
}
// Timeline 水位视图（Temporal/Jaeger 那类）：每次 attempt 一条，按时间定位、按结果配色。
function timelineHtml(s, data) {
  const rows = (data && data.attempts) || [];
  if (!rows.length) return `<p class="detail-subtle">还没有 attempt，暂无可画的时间线。</p>`;
  const now = Date.now();
  const marks = [];
  rows.forEach((row, index) => {
    const start = Date.parse(row.started_at || "");
    if (!start) return;
    const end = Date.parse(row.finished_at || "") || now;
    const facts = row.execution_facts || {};
    const failed = (row.exit_code !== undefined && row.exit_code !== 0)
      || (facts.violations || row.violations || []).length
      || (facts.missing_artifacts || row.missing_artifacts || []).length
      || (row.native_errors || []).length;
    marks.push({index: index + 1, row, start, end, failed,
                tone: row.finished_at ? (failed ? "bad" : "ok") : "warn"});
  });
  if (!marks.length) return `<p class="detail-subtle">attempt 记录里没有开始时间，画不出水位条。</p>`;
  const min = Math.min(...marks.map(m => m.start));
  const max = Math.max(...marks.map(m => m.end), min + 1000);
  const total = Math.max(max - min, 1000);
  const bars = marks.map(m => {
    const left = ((m.start - min) / total) * 100;
    const width = Math.max(((m.end - m.start) / total) * 100, 1.2);
    const label = [m.row.executor, m.row.model].filter(Boolean).join(" · ");
    const facts = m.row.execution_facts || {};
    // 位置不用内联 style（CSP style-src 'self' 会拦掉），改成 data 属性，渲染后用 CSSOM 赋值。
    return `<div class="tl-row"><div class="tl-label">#${m.index} <span>${esc(m.row.attempt_id.slice(-8))}</span><i>${esc(label)}</i></div>
      <div class="tl-track"><div class="tl-bar ${m.tone}" data-left="${left.toFixed(2)}" data-width="${width.toFixed(2)}" title="${esc(prettyTime(m.row.started_at))} → ${esc(m.row.finished_at ? prettyTime(m.row.finished_at) : "进行中")}"></div></div>
      <div class="tl-meta">${esc(durationLabel(m.end - m.start))}${m.row.exit_code !== undefined ? " · 退出码 " + esc(String(m.row.exit_code)) : ""}${facts.stop_reason ? " · " + esc(String(facts.stop_reason)) : ""}${m.failed ? " · 有机器错误" : ""}</div></div>`;
  }).join("");
  const repaired = marks.length > 1 || (s.human_accept && s.human_accept.decision === "reject") || s.revision > 1;
  const back = repaired ? `<div class="tl-back">↺ <b>review → repair</b>：这一轮里有 ${marks.length} 次尝试，后一次是在前一次产物基础上返修/重跑（旧 attempt 与产物不可变）。</div>` : "";
  return `<h3 class="pane-title">尝试时间线</h3><div class="timeline">${bars}</div>${back}
    <div class="tl-axis"><span>${esc(prettyTime(new Date(min).toISOString()))}</span><span>${esc(prettyTime(new Date(max).toISOString()))}</span></div>`;
}
// 渲染后把时间线几何写进 DOM（CSSOM 赋值不受 CSP style-src 限制）。
function applyTimelineGeometry() {
  document.querySelectorAll(".tl-bar[data-left]").forEach(bar => {
    bar.style.left = bar.dataset.left + "%";
    bar.style.width = bar.dataset.width + "%";
  });
}
// 生命周期流程：把状态机画成一条链，当前节点高亮；分支状态（失败/停止未确认/返修/取消）挂在旁边。
function flowHtml(s, data) {
  const chain = [["ready", "待规划"], ["prepared", "空会话待确认"], ["running", "执行中"],
                 ["awaiting_review", "待审查"], ["awaiting_user", "待接收"], ["accepted", "已接收"]];
  const branchLabels = {failed: "执行失败", stop_unconfirmed: "停止未确认", rework: "返修待规划", cancelled: "已取消", cancel_requested: "取消中"};
  const branch = branchLabels[s.status] || null;
  const index = chain.findIndex(([key]) => key === s.status);
  // 分支状态（失败/取消/停止未确认/返修）在链上没有 current：按证据把走过的那几步标成 done，别整条都灰。
  const reached = index >= 0 ? index : (s.current_attempt ? 2 : 0);
  const nodes = chain.map(([, label], i) => {
    const cls = i === index ? "node current" : (i <= reached ? "node done" : "node");
    return `<div class="${cls}"><span>${esc(label)}</span></div>`;
  }).join('<i class="arrow">→</i>');
  const hang = branch ? `<div class="flow-hang"><i class="arrow">↳</i><div class="node bad branch"><span>${esc(branch)}</span></div></div>` : "";
  const chips = [
    chip("尝试", s.current_attempt ? s.current_attempt.slice(-8) : "无"),
    chip("修订", "r" + String(s.revision)),
    chip("开始", s.started_at ? prettyTime(s.started_at) : "未开始"),
    chip("结束", s.finished_at ? prettyTime(s.finished_at) : "未结束"),
    chip("验收", labelAcceptance(s.acceptance), s.acceptance === "PASS" ? "ok" : ""),
    s.review_gate ? chip("可审", (s.review_gate.reviewable ? "可审" : (s.review_gate.reason || "不可审")) + (s.review_gate_source === "live" ? "（实时）" : "（存量）"), s.review_gate.reviewable ? "ok" : "bad") : "",
  ].filter(Boolean).join("");
  return `<div class="flow">${nodes}</div>${hang}<div class="chips">${chips}</div>${timelineHtml(s, data)}
    <p class="detail-subtle">流程来自桥本地记录：规划 → 准备（shared 需客户端确认可见）→ 执行 → 回收 → 可审准入 → 只读审查（产品意见）→ 人工接收（接收 / 打回）。分支节点表示当前尝试停在哪一步外侧。</p>`;
}
function renderDetail(data) {
  const task = data.task, s = data.state, plan = state.plans.get(task.task_id);
  const result = data.result;
  const n = data.native;
  // 每个块都做成「一行结论 + 需要时才展开」，避免详情面板被长文本撑爆。
  // chip / fold 已在模块作用域定义（flowHtml 也要用）。
  const nativeSection = n ? fold("OpenCode 原生会话", `<div class="detail-text">标题：${esc(n.title)}
会话 ID：${esc(n.session_id)}
服务：${esc(n.server)}
工作区：${esc(n.directory)}
模型：${esc(n.model)}
可见性：${esc(n.visibility)}
提示词：${n.prompt_sent ? "已交给 CLI，原生接纳以事件为准" : "尚未发送"}
最近事件：${esc(n.last_event || "空会话")} ${esc(prettyTime(n.last_event_at))}
原生停稳：${n.stop_confirmed === true ? "已确认" : n.stop_confirmed === false ? "未确认" : "尚未回收"}</div><p class="detail-subtle">在连接同一服务的 OpenCode 中按标题或会话 ID 查找该工作区。</p>`) : "";
  const warnings = result?.native_warnings || n?.warnings || [];
  const warningSection = warnings.length ? fold("原生工具警告", `<div class="detail-text">${esc(warnings.map(x=>`${x.tool || x.stage}: ${x.message}`).join("\n"))}</div>`, warnings.length) : "";
  const activitySection = data.activity?.length ? fold("最近执行输出", `<div class="detail-text">${esc(data.activity.map(x=>[x.type,x.tool,x.status,x.text].filter(Boolean).join(" · ")).join("\n\n"))}</div>`, data.activity.length) : "";
  const effects = plan ? fold(`执行计划 · ${plan.plan_sha256.slice(0,12)}…`, `<div class="detail-text">${esc(plan.effects.join("\n"))}</div><div class="detail-subtle">${esc(plan.limitations.join("\n"))}</div>`) : "";
  // 执行事实（A 轴）与可审准入（B 轴）：用芯片行表达，不再铺长段落。
  const facts = (result && result.execution_facts) || s.execution_facts || null;
  const gate = (result && result.review_gate) || s.review_gate || null;
  const factsChips = facts ? [
    chip("退出码", String(facts.exit_code), facts.exit_code === 0 ? "ok" : "bad"),
    chip("停稳", facts.stop_confirmed === true ? "已确认" : facts.stop_confirmed === false ? "未确认" : "未记录",
         facts.stop_confirmed === true ? "ok" : facts.stop_confirmed === false ? "bad" : ""),
    chip("原生错误", String(facts.native_error_count), facts.native_error_count ? "bad" : "ok"),
    chip("超时", facts.timed_out ? "是" : "否", facts.timed_out ? "warn" : ""),
    chip("停止原因", facts.stop_reason || "无", facts.stop_reason ? "warn" : ""),
  ].join("") : "";
  const gateChip = gate ? chip(gate.reviewable ? "可审准入" : "不可审", gate.reviewable ? "可只读审查" : (gate.reason || "未记录"),
                               gate.reviewable ? "ok" : "bad") : "";
  const changed = ((result && result.changes) || []).map(x => x.path);
  const changeLine = `<div class="detail-text">变更文件：${esc(changed.length ? changed.join("、") : "无")}</div>`;
  const errorsDetail = result && result.native_errors && result.native_errors.length
    ? fold("原生错误明细", `<div class="detail-text">${esc(result.native_errors.map(x=>x.message || JSON.stringify(x)).join("\n"))}</div>`, result.native_errors.length) : "";
  const execPane = (result || facts) ? `<div class="chips">${factsChips}${gateChip}</div>${changed.length > 4 ? fold("变更文件", changeLine, changed.length) : changeLine}${errorsDetail}` : `<p class="detail-subtle">还没有执行结果。</p>`;
  const softReview = s.product_review, softAccept = s.human_accept;
  const reviewChips = [
    chip("产品意见", softReview ? (softReview.verdict || "未记录") : "未记录", softReview && softReview.verdict === "pass" ? "ok" : softReview && softReview.verdict === "fail" ? "bad" : ""),
    chip("人工接收", softAccept ? (softAccept.decision || "未记录") : "未记录", softAccept && softAccept.decision === "accept" ? "ok" : ""),
    softAccept ? chip("接收时间", prettyTime(softAccept.at)) : "",
  ].join("");
  const evidenceKeys = Object.keys(s.review_evidence || {});
  const reviewPane = `<div class="chips">${reviewChips}</div>${gate && !gate.reviewable ? `<p class="detail-subtle">不可审原因：${esc(gate.reason || "未记录")}；按当前规则不得重派或接收。</p>` : ""}${gate && gate.reviewable ? `<p class="detail-subtle">可进入只读审查；这不等于已通过，更不等于可接收。</p>` : ""}${evidenceKeys.length ? fold("审查证据", `<div class="detail-text">${esc(evidenceKeys.map(k=>`${k}: ${String((s.review_evidence[k] || {}).sha256 || "").slice(0, 64)}`).join("\n"))}</div>`, evidenceKeys.length) : ""}`;
  // 快捷操作与命令：目录按钮 + 命令下拉（不再一次摊开 5 条完整命令）。
  const paths = data.paths || {};
  const openButtons = [["worktree", "工作区"], ["attempt", "尝试目录"], ["task", "任务目录"],
                       ["evidence", "证据目录"], ["store", "store"], ["reports", "报告目录"]]
    .filter(([scope]) => paths[scope])
    .map(([scope, label]) => `<button class="secondary-button small-button" data-open="${scope}">${esc(label)}</button>`).join("");
  const commands = data.commands || [];
  const cmdPicker = commands.length ? `<div class="cmd-picker"><select id="cmdSelect">${commands.map((c, i) => `<option value="${i}">${esc(c.label)}</option>`).join("")}</select><code id="cmdPreview">${esc(commands[0].command)}</code><button class="secondary-button small-button" id="cmdCopy">复制</button></div><p class="detail-subtle">命令已按本控制台的实际 store 生成；「打开目录」只允许 store 内目录，或启动时用 --reports 配置的报告目录。</p>` : "";
  const cmdPane = `${openButtons ? `<div class="detail-actions">${openButtons}</div>` : ""}${cmdPicker || "<p class=\"detail-subtle\">没有可下发的命令。</p>"}`;
  const observation = data.native_observation ? fold("最近原生回读", `<div class="detail-text">${esc(prettyTime(data.native_observation.time))}
原生 outcome：${esc(data.native_observation.session?.outcome || "尚无结果")}
原生 idle 时间：${esc(data.native_observation.session?.time?.idle || "尚无记录")}
${esc(data.native_observation.note)}</div>`) : "";
  const job = data.job?.running ? `<div class="chips">${chip("桥任务", "执行器正在运行，刷新读取最新状态", "warn")}</div>` : data.job?.error ? `<div class="chips">${chip("操作失败", data.job.error, "bad")}</div>` : "";
  const tabs = [["overview", "概览"], ["flow", "流程"], ["exec", "执行"], ["review", "审查"], ["cmd", "命令"]];
  const tabBar = `<div class="tabs">${tabs.map(([key, label], index) => `<button class="tab ${index === 0 ? "active" : ""}" data-tab="${key}">${label}</button>`).join("")}</div>`;
  $("detailPanel").innerHTML = `<div class="detail-head"><div><h2>${esc(task.title || task.goal)}</h2><p>${esc(task.task_id)} · revision ${esc(s.revision)}</p></div><button class="icon-button detail-close" id="detailClose" title="关闭">×</button></div>
    <div class="detail-meta"><div class="meta-box"><span>状态</span><b><span class="badge ${esc(s.status)}">${esc(labelStatus(s.status))}</span></b></div><div class="meta-box"><span>验收（独立）</span><b>${esc(labelAcceptance(s.acceptance))}</b></div><div class="meta-box"><span>执行器 · 模型</span><b>${esc(task.executor)} · ${esc(labelModel(task.model))}</b></div><div class="meta-box"><span>最后更新</span><b>${esc(prettyTime(s.finished_at || s.started_at || s.created_at))}</b></div></div>
    <div class="detail-actions">${actionButtons(data)}</div>${job}${tabBar}
    <div class="pane active" data-pane="overview"><div class="detail-section"><h3>任务目标</h3><div class="detail-text">${esc(task.goal)}</div></div>${fold("允许修改", `<div class="detail-text">${esc((task.allowed_paths || []).join("\n"))}</div>`) }${effects}<p class="detail-subtle">详情为 state.current_attempt 的当前尝试，不是完整历史。</p></div>
    <div class="pane" data-pane="flow">${flowHtml(s, data)}</div>
    <div class="pane" data-pane="exec">${execPane}${activitySection}${nativeSection}${observation}${warningSection}</div>
    <div class="pane" data-pane="review">${reviewPane}</div>
    <div class="pane" data-pane="cmd">${cmdPane}</div>`;
  applyTimelineGeometry();
  $("detailClose").addEventListener("click", () => {state.selected=null; renderRows(); $("detailPanel").innerHTML=placeholder();});
  document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach(item => item.classList.toggle("active", item === button));
    document.querySelectorAll("[data-pane]").forEach(pane => pane.classList.toggle("active", pane.dataset.pane === button.dataset.tab));
  }));
  document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => handleAction(button.dataset.action, data)));
  document.querySelectorAll("[data-open]").forEach(button => button.addEventListener("click", () => openFolder(data.task.task_id, button.dataset.open)));
  const selector = $("cmdSelect"), preview = $("cmdPreview");
  if (selector && preview) {
    selector.addEventListener("change", () => { preview.textContent = commands[Number(selector.value)].command; });
    $("cmdCopy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(preview.textContent); toast("命令已复制。"); }
      catch (error) { toast("复制失败，请手动选中命令文本。", true); }
    });
  }
}
async function openFolder(taskId, scope) {
  try {
    const data = await api("/api/open", {method: "POST", body: JSON.stringify({task_id: taskId, scope})});
    toast(`已在本机文件管理器打开：${data.opened}`);
  } catch (error) { toast(error.message, true); }
}
async function handleAction(action, data) {
  if (action === "native-status") {
    try { await api(`/api/tasks/${data.task.task_id}/native-status`, {method:"POST",body:"{}"}); await selectTask(data.task.task_id); toast("原生状态已回读；没有发送模型提示词。"); }
    catch (error) { toast(error.message,true); }
    return;
  }
  if (action === "plan") {
    try { const plan = await api(`/api/tasks/${data.task.task_id}/plan`, {method:"POST",body:JSON.stringify({})}); state.plans.set(data.task.task_id, plan); await selectTask(data.task.task_id); toast("计划已生成；请先审阅影响范围。"); }
    catch (error) { toast(error.message,true); }
    return;
  }
  if (action === "revise") { openTaskDialog(data.task); return; }
  if (action === "run" || action === "prepare") openRunDialog(data, action);
  if (action === "cancel") openCancelDialog(data);
  if (action === "audit") openReviewDialog(data, "audit");
  if (action === "accept") openReviewDialog(data, "accept");
  if (action === "reject") openReviewDialog(data, "reject");
  if (action === "dismiss") openReviewDialog(data, "dismiss");
  if (action === "void") openReviewDialog(data, "void");
}
function openTaskDialog(task = null) {
  state.editTask = task;
  $("taskDialogTitle").textContent = task ? "修订任务契约" : "新建任务";
  $("saveTaskButton").textContent = task ? "保存修订" : "保存任务契约";
  $("taskDialogError").textContent = "";
  $("contractInput").value = task ? JSON.stringify(task,null,2) : JSON.stringify({task_id:"example-task",title:"新的桥任务",opencode_mode:"shared",goal:"描述你要完成的具体任务",repo:"C:/path/to/clean/git/repository",executor:"opencode",model:"opencode-go/deepseek-v4.1-flash",allowed_paths:["src/"],artifacts:["src/result.txt"],input_refs:[],completion_criteria:["说明可观察的完成条件"],timeout_seconds:300},null,2);
  $("taskDialog").showModal();
}
$("saveTaskButton").addEventListener("click", async () => {
  try {
    const contract = JSON.parse($("contractInput").value);
    const task = state.editTask;
    const path = task ? `/api/tasks/${encodeURIComponent(task.task_id)}/revise` : "/api/tasks";
    const data = await api(path,{method:"POST",body:JSON.stringify(task ? {contract} : {contract})});
    $("taskDialog").close(); state.plans.delete(data.task_id || task?.task_id); await refresh(false);
    const id = data.task_id || task?.task_id; if (id) await selectTask(id); toast(task ? "修订已保存；旧尝试保留。" : "任务契约已保存。 ");
  } catch(error) { $("taskDialogError").textContent = error.message; }
});
function openAction(title, eyebrow, html, confirmText, context) {
  state.action = context; $("actionTitle").textContent=title; $("actionEyebrow").textContent=eyebrow;
  $("actionContent").innerHTML=html; $("actionError").textContent=""; $("confirmActionButton").textContent=confirmText; $("confirmActionButton").disabled=true;
  $("actionDialog").showModal();
  $("actionContent").querySelectorAll("input").forEach(input=>input.addEventListener("input",updateActionReady));
  $("actionContent").querySelectorAll("input").forEach(input=>input.addEventListener("change",updateActionReady));
  updateActionReady();
}
function updateActionReady() {
  const c=state.action; if(!c)return; let ready=false;
  if(c.type==="run" || c.type==="prepare") ready=$("approveRun").checked && $("ackUnmetered").checked && (!c.visibleRequired || $("confirmVisible").checked);
  else if(c.type==="cancel") ready=$("approveCancel").checked && $("confirmPhrase").value.trim()===`CANCEL ${c.id}`;
  else if(c.type==="audit") ready=$("auditFile").files.length===1;
  else if(c.type==="accept") ready=$("auditFile").files.length===1 && $("userFile").files.length===1 && $("confirmAccept").checked;
  else if(c.type==="reject") ready=$("auditFile").files.length===1 && $("confirmReject").checked && $("rejectReason").value.trim().length>0;
  else if(c.type==="dismiss") ready=$("dismissReason").value.trim().length>0;
  else if(c.type==="void") ready=$("voidReason").value.trim().length>0;
  $("confirmActionButton").disabled=!ready;
}
function openRunDialog(data, action = "run") {
  const p=state.plans.get(data.task.task_id); if(!p){toast("请先生成并审阅计划。",true);return;}
  const effects=(p.effects||[]).map(x=>`<li>${esc(x)}</li>`).join("");
  const limits=(p.limitations||[]).map(x=>`<li>${esc(x)}</li>`).join("");
  const prepare = action === "prepare", visibleRequired = !prepare && data.task.opencode_mode === "shared";
  openAction(prepare ? "创建 OpenCode 空会话" : "发送提示词并执行","任务授权",`<div class="info-box"><strong>计划哈希</strong><br>${esc(p.plan_sha256)}<br><br><strong>将发生</strong><ul>${effects}</ul></div><div class="warning-box"><strong>预算限制尚未实证。</strong>执行器可能调用模型和消耗额度；桥无法保证原生请求次数、重试或费用上限。<ul>${limits}</ul></div><label><input type="checkbox" id="approveRun">我已审阅该计划，并批准启动这一个任务</label><label><input type="checkbox" id="ackUnmetered">我知悉请求预算尚未被硬限制</label>${visibleRequired ? `<label><input type="checkbox" id="confirmVisible">我已在真实 OpenCode 客户端找到此会话：${esc(data.native?.session_id)}</label>` : ""}${prepare ? "<p>此步骤创建工作区和空会话，不发送模型提示词。</p>" : ""}`,prepare ? "创建空会话" : "发送提示词",{type:action,id:data.task.task_id,plan:p,visibleRequired});
}
function openCancelDialog(data) {
  const id=data.task.task_id, attempt=data.state.current_attempt;
  openAction("中断此任务","取消批准",`<div class="warning-box">共享模式会向此会话发送原生 interrupt，并结束桥的观察进程；不会关闭 OpenCode 服务。私有模式结束该任务进程树。必须回读原生状态确认停稳；现场保留。</div><div class="info-box"><strong>任务：</strong>${esc(id)}<br><strong>当前 attempt：</strong>${esc(attempt)}</div><label><input type="checkbox" id="approveCancel">我批准中断此 attempt</label><label>输入 <strong>CANCEL ${esc(id)}</strong> 确认<input type="text" id="confirmPhrase" autocomplete="off"></label>`,"请求取消",{type:"cancel",id,attempt});
}
function openReviewDialog(data, decision) {
  const accept = decision === "accept", reject = decision === "reject", dismiss = decision === "dismiss", isVoid = decision === "void";
  const fileInput = (id, label) => `<label>${label}<input type="file" id="${id}" accept=".md,.txt,.json,.log,.png,.jpg,.jpeg"></label>`;
  const reasonBox = (id, label) => `<label>${label}<textarea id="${id}" rows="3" autocomplete="off"></textarea></label>`;
  const verdicts = `<fieldset class="verdict"><legend>产品意见</legend>
    <label><input type="radio" name="verdict" value="pass" checked>通过</label>
    <label><input type="radio" name="verdict" value="fail">不通过</label>
    <label><input type="radio" name="verdict" value="blocked">阻塞</label></fieldset>`;
  const hint = accept ? "接收要求只读审查证据和用户真实路径证据同时提供；机器门（退出码 / 越界 / 缺产物 / 原生错误）一字不放宽。"
    : reject ? "打回会把任务退回 rework（可改契约后重跑），不改历史产物、不自动重跑、不删除现场。至少附只读审计证据。"
    : dismiss ? "撤销打回只对「已被人工打回」的任务生效；必须填理由（写进 revisions 账），任务回到「待接收」，可重新审查。"
    : isVoid ? "作废是把「已结束但没有价值」的任务冻结掉：状态转「作废」并沉到列表最下面，不改历史产物、不删证据、不自动重跑。必须填理由。"
    : "此处只记录只读审查与产品意见；验收仍是 BLOCKED，之后还需要你的真实路径证据。";
  const html = `<div class="info-box">证据文件会复制到任务目录 <code>web-evidence</code> 并保留。${hint}</div>`
    + (dismiss || isVoid ? "" : fileInput("auditFile", "只读审查证据"))
    + (accept ? verdicts + fileInput("userFile", "用户真实路径证据") + `<label><input type="checkbox" id="confirmAccept">我确认所选证据对应真实客户端路径及本任务</label>` : "")
    + (reject ? fileInput("userFile", "补充证据（可选）") + reasonBox("rejectReason", "打回理由（必填；会记进 human_accept.reason，供下一次修订使用）") + `<label><input type="checkbox" id="confirmReject">我确认打回此任务的当前产物</label>` : "")
    + (dismiss ? reasonBox("dismissReason", "撤销理由（必填；会记进 revisions 账）") : "")
    + (isVoid ? reasonBox("voidReason", "作废理由（必填；会记进 void 账，含作废前的状态）") : "");
  const title = accept ? "提交验收证据" : reject ? "打回任务" : dismiss ? "撤销打回" : isVoid ? "作废任务" : "记录只读审查";
  const eyebrow = accept ? "人工接收" : reject ? "人工打回" : dismiss ? "撤销判定" : isVoid ? "人工作废" : "只读审查";
  const confirmText = accept ? "审查并接收" : reject ? "确认打回" : dismiss ? "确认撤销" : isVoid ? "确认作废" : "提交审查";
  openAction(title, eyebrow, html, confirmText, {type: decision, id: data.task.task_id});
}
async function evidenceFile(inputId) {
  const file=$(inputId).files[0]; if(!file)throw new Error("请选择证据文件");
  if(file.size>256*1024)throw new Error(`${file.name} 超过 256 KiB 限制`);
  const bytes=new Uint8Array(await file.arrayBuffer()); let binary=""; for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
  return {name:file.name,content_base64:btoa(binary)};
}
$("confirmActionButton").addEventListener("click", async () => {
  const c=state.action; if(!c)return; $("confirmActionButton").disabled=true; $("actionError").textContent="正在提交…";
  try {
    let data;
    if(c.type==="run" || c.type==="prepare") data=await api(`/api/tasks/${c.id}/${c.type}`,{method:"POST",body:JSON.stringify({plan_sha256:c.plan.plan_sha256,approve_start:true,ack_unmetered:true,confirm_visible:c.visibleRequired === true})});
    else if(c.type==="cancel") data=await api(`/api/tasks/${c.id}/cancel`,{method:"POST",body:JSON.stringify({attempt_id:c.attempt,approve_cancel:true})});
    else {
      const body={decision:c.type};
      if(c.type!=="dismiss" && c.type!=="void")body.audit_evidence=await evidenceFile("auditFile");
      if(c.type==="accept")body.user_evidence=await evidenceFile("userFile");
      if(c.type==="reject" && $("userFile").files.length===1)body.user_evidence=await evidenceFile("userFile");
      if(c.type==="reject")body.reason=$("rejectReason").value.trim();
      if(c.type==="dismiss")body.reason=$("dismissReason").value.trim();
      if(c.type==="void")body.reason=$("voidReason").value.trim();
      if(c.type==="audit"){const picked=document.querySelector('input[name="verdict"]:checked');body.verdict=picked?picked.value:"blocked";}
      data=await api(`/api/tasks/${c.id}/review`,{method:"POST",body:JSON.stringify(body)});
    }
    $("actionDialog").close(); toast(c.type==="prepare"?"正在创建空会话；不会发送提示词。":c.type==="run"?"提示词执行已提交；请查看原生会话。":c.type==="cancel"?"取消请求已记录；请核对停止状态。":c.type==="reject"?"打回已记录：任务回到 rework，可改契约后重跑。":c.type==="dismiss"?"撤销打回已记录：任务回到待接收。":c.type==="void"?"作废已记录：任务沉底并冻结。":"审查记录已提交。 ");
    if(c.type==="run")await new Promise(resolve=>setTimeout(resolve,350)); await refresh();
  } catch(error) { $("actionError").textContent=error.message; $("confirmActionButton").disabled=false; }
});
$("newTaskButton").addEventListener("click",()=>openTaskDialog());
$("emptyNewButton").addEventListener("click",()=>openTaskDialog());
$("refreshButton").addEventListener("click",()=>refresh());
$("searchInput").addEventListener("input",()=>{ renderRows(); if (state.graph) state.graph.setFilter($("searchInput").value); });
setInterval(()=>refresh(),5000);
state.graph = initGraph();
api("/api/runtime").then(r=>{ $("runtimeInfo").textContent=`${r.build} · PID ${r.pid} · bridge ${r.loaded_sha256.bridge.slice(0,12)}`; $("runtimeInfo").title=JSON.stringify(r,null,2); }).catch(e=>toast(e.message,true));
refresh(false);
