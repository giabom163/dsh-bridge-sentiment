/* B1 桥只读任务协同图。
 *
 * 数据来源只有 web/app.js 已获取的 /api/tasks 列表（每条为当前尝试，不是完整历史）。
 * 本组件自身不发起任何网络请求，不请求 native-status，也不触发模型请求。
 * 节点是真实任务；边只来自 task.dependencies。有依赖边时用 Cytoscape 内置 breadthfirst，
 * 无依赖边时用内置 grid 多列铺开；不加载任何布局扩展。图内节点标签只取短 title（无 title 用
 * task_id 的尾部并加省略标识，避免公共前缀难区分）；文本列表对无 title 的任务直接显示完整
 * task_id，点击详情也完整显示，绝不用整段 goal。Cytoscape 缺失时明确报错，不做假图降级。
 */
(function (global) {
  "use strict";

  // 契约 10 个任务状态的中文完整文字（docs/B1-contract.md 第 3 行）。
  var STATUS_LABELS = {
    ready: "待规划",
    prepared: "空会话待确认",
    running: "执行中",
    awaiting_review: "待审查",
    awaiting_user: "待用户验收",
    accepted: "成功",
    rework: "返修待规划",
    failed: "失败",
    cancelled: "作废",
    stop_unconfirmed: "停止未确认"
  };
  // 桥取消 API 的临时过程标签，不属于契约 10 状态，单独保留。
  var PROCESS_LABELS = { cancel_requested: "取消中（过程标签）" };
  // 3 个验收值，独立于任务状态。
  var ACCEPTANCE_LABELS = { PASS: "通过", FAIL: "失败", BLOCKED: "阻塞", PENDING: "待审" };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
  function blank(value) { return value === null || value === undefined || value === ""; }
  function safeClass(value) { return String(value == null ? "unknown" : value).replace(/[^a-z0-9_-]/gi, "_"); }

  function statusLabel(value) {
    if (blank(value)) { return "未知（状态未记录）"; }
    if (STATUS_LABELS[value]) { return STATUS_LABELS[value]; }
    if (PROCESS_LABELS[value]) { return PROCESS_LABELS[value]; }
    return "未知（" + value + "）"; // 保留未知原值，不折叠成已知状态
  }
  function acceptanceLabel(value) {
    if (blank(value)) { return "未知（未记录）"; }
    if (ACCEPTANCE_LABELS[value]) { return ACCEPTANCE_LABELS[value]; }
    return "未知（" + value + "）"; // 不默认 PASS
  }
  // 产品意见（C 轴）与人工接收（D 轴）现在是各自独立的字段；旧数据没有时显示"未记录"，不倒推结论。
  var REVIEW_LABELS = { pass: "通过", fail: "不通过", blocked: "阻塞" };
  function reviewLabel(review) {
    if (!review || typeof review !== "object") { return "未记录"; }
    return REVIEW_LABELS[review.verdict] || "未知（" + String(review.verdict) + "）";
  }
  function acceptLabel(record) {
    if (!record || typeof record !== "object") { return "未记录"; }
    return record.decision === "accept" ? "已接收" : record.decision === "reject" ? "已驳回" : "未记录";
  }
  function modelLabel(value) { return blank(value) ? "未记录" : String(value); }
  // 图内节点短标签：优先简短 title（限长），无 title 用 task_id 尾部并加省略标识，避免公共
  // 前缀把不同任务显示成同一标签；完整 id 在文本列表直接显示、点击详情完整显示。绝不把整段 goal 当标签。
  var LABEL_MAX = 28;
  function taskLabel(task, id) {
    var title = blank(task && task.title) ? "" : String(task.title).trim();
    if (title) { return title.length > LABEL_MAX ? title.slice(0, LABEL_MAX - 1) + "…" : title; }
    var raw = String(id == null ? "" : id);
    return raw.length > LABEL_MAX ? "…" + raw.slice(-(LABEL_MAX - 1)) : raw;
  }
  // 文本列表标签：有 title 用 title；无 title 直接给出完整 task_id（不做尾部截断），使公共前缀
  // 相近的任务在列表里也能分辨；图内节点仍用 taskLabel 的限长尾部。
  function listLabel(task, id) {
    var title = blank(task && task.title) ? "" : String(task.title).trim();
    return title ? title : String(id == null ? "" : id);
  }
  function badgeClass(value) { return "badge " + safeClass(value); }
  function prettyTime(value) {
    if (!value) { return "尚未成功"; }
    var date = new Date(value);
    return isNaN(date.valueOf()) ? "尚未成功" : date.toLocaleString("zh-CN", { hour12: false });
  }

  function mount(options) {
    options = options || {};
    var el = document.getElementById(options.graphId || "taskGraph");
    var listEl = document.getElementById(options.listId || "graphList");
    var statusEl = document.getElementById(options.statusId || "graphStatus");
    var noteEl = document.getElementById(options.noteId || "graphNote");
    var emptyEl = document.getElementById(options.emptyId || "graphEmpty");
    var errorEl = document.getElementById(options.errorId || "graphError");
    var onSelect = typeof options.onSelect === "function" ? options.onSelect : null;

    var state = { tasks: [], filter: "", selected: null, missing: [], connected: true, hasLoaded: false, lastSuccessAt: null };

    var controller = {
      available: false,
      error: null,
      update: function () {},
      setFilter: function () {},
      select: function () {},
      setConnection: function () {},
      destroy: function () {}
    };

    function showError(message) {
      controller.error = message;
      if (errorEl) { errorEl.hidden = false; errorEl.textContent = message; }
    }
    function statusLine(text, disconnected) {
      if (!statusEl) { return; }
      statusEl.textContent = text;
      statusEl.classList.toggle("disconnected", !!disconnected);
    }

    if (!el) {
      showError("缺少图容器，无法渲染协同图。");
      return controller;
    }
    if (!global.cytoscape || typeof global.cytoscape !== "function") {
      // 组件缺失明确报错；任务表仍可用。
      showError("Cytoscape core 未加载（/vendor/cytoscape.min.js）。协同图不可用；任务表与详情仍可使用，不做假图降级。");
      controller.setConnection = function (connected, lastSuccessAt) {
        statusLine("协同图不可用 · " + (connected ? "连接正常" : "已断连 · 最后成功刷新 " + prettyTime(lastSuccessAt)), true);
      };
      return controller;
    }
    if (errorEl) { errorEl.hidden = true; }

    var cy = global.cytoscape({
      container: el,
      elements: [],
      wheelSensitivity: 0.2,
      style: [
        { selector: "node", style: {
          "background-color": "#eef2f0", "border-width": 2, "border-color": "#c7d3cd",
          "label": "data(label)", "font-size": 10, "color": "#1e2a27",
          "text-valign": "bottom", "text-margin-y": 5, "width": 32, "height": 32,
          "text-wrap": "wrap", "text-max-width": 92 } },
        { selector: "node.status-ready, node.status-prepared, node.status-awaiting_review, node.status-awaiting_user",
          style: { "background-color": "#edf5ef", "border-color": "#4c9a69" } },
        { selector: "node.status-running, node.status-cancel_requested",
          style: { "background-color": "#fff5e7", "border-color": "#d89b36" } },
        { selector: "node.status-accepted", style: { "background-color": "#226b53", "border-color": "#164b3a", "color": "#164b3a" } },
        { selector: "node.status-failed, node.status-cancelled, node.status-stop_unconfirmed",
          style: { "background-color": "#fff0ee", "border-color": "#ce625a" } },
        { selector: "node.status-rework, node.status-unknown",
          style: { "background-color": "#f5f3ed", "border-color": "#a0aaa5" } },
        { selector: "edge", style: {
          "width": 2, "line-color": "#b9c6c0", "target-arrow-color": "#b9c6c0",
          "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.9 } },
        { selector: "node.cluster", style: {
          "background-color": "#f4f7f5", "background-opacity": 0.55, "border-width": 1,
          "border-style": "dashed", "border-color": "#c7d3cd", "label": "data(label)",
          "font-size": 9, "color": "#8b968f", "text-valign": "top", "text-margin-y": -4, "padding": 16 } },
        { selector: "node.selected", style: { "border-width": 4, "border-color": "#226b53" } },
        { selector: ".filtered-out", style: { "display": "none" } }
      ],
      layout: { name: "preset" }
    });

    function matches(row) {
      if (!state.filter) { return true; }
      var task = row.task || {};
      // 与表格同一过滤字段，保证图与搜索同步。
      return (row.task_id + " " + (task.goal || "")).toLowerCase().indexOf(state.filter) >= 0;
    }

    function clusterKey(row) {
      // 没有依赖边时按「仓库」聚类（同仓库的任务天然成组），仓库缺失归入"未记录仓库"。
      var repo = String((row.task || {}).repo || "").replace(/[\\/]+$/, "");
      var name = repo ? repo.split(/[\\/]/).pop() : "";
      return name || "未记录仓库";
    }

    function buildElements(tasks, clustered) {
      var nodes = [], edges = [], seen = Object.create(null), missing = [];
      tasks.forEach(function (row) {
        var task = row.task || {}, s = row.state || {};
        var id = row.task_id || task.task_id;
        if (!id) { return; }
        seen[id] = true;
        var statusValue = blank(s.status) ? "" : String(s.status);
        var data = {
          id: id,
          label: taskLabel(task, id),
          status: statusValue
        };
        if (clustered) { data.parent = "cluster:" + clusterKey(row); }
        nodes.push({ data: data, classes: "status-" + safeClass(blank(statusValue) ? "unknown" : statusValue) });
      });
      if (clustered) {
        var counts = Object.create(null), parents = [];
        nodes.forEach(function (node) {
          var parent = node.data.parent;
          counts[parent] = (counts[parent] || 0) + 1;
        });
        Object.keys(counts).forEach(function (parent) {
          parents.push({ data: { id: parent, label: parent.replace("cluster:", "") + "（" + counts[parent] + "）" },
                         classes: "cluster" });
        });
        // 只有一个聚类时包一层没有意义：撤掉父节点，保持原来的平铺。
        if (parents.length > 1) { nodes = parents.concat(nodes); }
        else { nodes.forEach(function (node) { delete node.data.parent; }); }
      }
      tasks.forEach(function (row) {
        var task = row.task || {};
        var owner = row.task_id || task.task_id;
        var deps = Array.isArray(task.dependencies) ? task.dependencies : [];
        deps.forEach(function (dep) {
          if (!seen[dep]) { missing.push({ dep: dep, owner: owner }); return; }
          edges.push({ data: { id: dep + "->" + owner, source: dep, target: owner } });
        });
      });
      return { nodes: nodes, edges: edges, missing: missing };
    }

    function renderList() {
      if (!listEl) { return; }
      var rows = state.tasks.filter(matches);
      if (!rows.length) {
        // 从未成功加载 ≠ 成功加载且确为空：文本列表也不把未知当成空。
        var message;
        if (!state.hasLoaded) {
          message = "无法读取任务（尚未成功加载）；不能判定为空。";
        } else if (state.tasks.length) {
          message = "没有匹配搜索的任务。";
        } else {
          message = "暂无任务。";
        }
        listEl.innerHTML = '<li class="graph-list-empty">' + message + "</li>";
        return;
      }
      listEl.innerHTML = rows.map(function (row) {
        var task = row.task || {}, s = row.state || {};
        var deps = Array.isArray(task.dependencies) ? task.dependencies : [];
        return '<li class="' + (state.selected === row.task_id ? "selected" : "") + '">' +
          '<button type="button" class="graph-list-item" data-id="' + esc(row.task_id) + '" title="' + esc(row.task_id) + '">' +
          esc(listLabel(task, row.task_id)) + "</button>" +
          '<span class="' + badgeClass(s.status) + '">' + esc(statusLabel(s.status)) + "</span>" +
          '<span class="graph-list-accept">验收：' + esc(acceptanceLabel(s.acceptance)) + "</span>" +
          '<span class="graph-list-review">意见：' + esc(reviewLabel(s.product_review)) + "</span>" +
          '<span class="graph-list-review">接收：' + esc(acceptLabel(s.human_accept)) + "</span>" +
          '<span class="graph-list-model">模型：' + esc(modelLabel(task.model)) + "</span>" +
          (deps.length ? '<span class="graph-list-deps">依赖：' + esc(deps.join("、")) + "</span>" : "") +
          "</li>";
      }).join("");
      listEl.querySelectorAll("button[data-id]").forEach(function (button) {
        button.addEventListener("click", function () { choose(button.dataset.id); });
      });
    }

    function renderNote() {
      if (!noteEl) { return; }
      var parts = ['<span>来源：GET /api/tasks（每条为当前尝试，非完整历史）</span>'];
      if (state.missing.length) {
        parts.push("<span>缺失依赖：" + state.missing.map(function (m) {
          return esc(m.dep) + "（被 " + esc(m.owner) + " 引用，但不在返回列表中；不伪造其节点或状态）";
        }).join("；") + "</span>");
      } else if (cy.edges().length) {
        parts.push("<span>依赖边均来自 task.dependencies。</span>");
      } else if (state.tasks.length) {
        // 无任何解析边时明确说明，不暗示边已存在或已实机覆盖。
        parts.push("<span>暂无依赖记录（依赖边仅来自 task.dependencies）——已按仓库聚类成组显示；有依赖的任务会自动切回层级图。</span>");
      }
      noteEl.innerHTML = parts.join("");
    }

    function updateEmpty() {
      // 只有“成功加载且确为空”才显示空态；从未成功加载时单独作为未知/不可读显示。
      var empty = state.hasLoaded && state.tasks.length === 0;
      if (emptyEl) { emptyEl.hidden = !empty; }
      if (el) { el.classList.toggle("graph-canvas-empty", empty); }
      if (errorEl) {
        if (!state.hasLoaded && !controller.error) {
          errorEl.hidden = false;
          errorEl.textContent = "尚未成功读取任务列表；无法确认任务是否为空。请等待重试或检查本机连接。";
        } else if (state.hasLoaded && !controller.error) {
          errorEl.hidden = true;
        }
      }
    }

    function applyFilter() {
      cy.nodes().forEach(function (node) {
        var task = null;
        for (var i = 0; i < state.tasks.length; i++) {
          if (state.tasks[i].task_id === node.id()) { task = state.tasks[i]; break; }
        }
        node.toggleClass("filtered-out", !(task && matches(task)));
      });
      cy.edges().forEach(function (edge) {
        edge.toggleClass("filtered-out",
          edge.source().hasClass("filtered-out") || edge.target().hasClass("filtered-out"));
      });
    }

    function choose(taskId) {
      controller.select(taskId);
      if (onSelect) { onSelect(taskId); }
    }

    // 无依赖边时不画单排长列，也不留白：按仓库聚类成组（compound node）多列铺开；
    // 有真实依赖边时仍用 breadthfirst 保留真实层级关系。
    function layoutOptions(nodeCount) {
      if (cy.edges().length) {
        return { name: "breadthfirst", directed: true, spacingFactor: 1.15, padding: 26,
                 fit: true, animate: false, nodeDimensionsIncludeLabels: true };
      }
      if (cy.nodes("node.cluster").length) {
        return { name: "cose", padding: 30, animate: false, fit: true,
                 nodeDimensionsIncludeLabels: true, nodeRepulsion: 9000, idealEdgeLength: 60 };
      }
      var cols = Math.max(1, Math.ceil(Math.sqrt(Math.max(1, nodeCount))));
      return { name: "grid", cols: cols, avoidOverlap: true, avoidOverlapPadding: 12,
               padding: 26, fit: true, animate: false, nodeDimensionsIncludeLabels: true };
    }

    // 过滤后只按可见节点取景，避免仍被 filtered-out 的隐藏标签撑大边界而缩得极小。
    function fitVisible() {
      var visible = cy.elements().filter(function (ele) { return !ele.hasClass("filtered-out"); });
      if (visible.length) { cy.fit(visible, 26); }
    }

    controller.available = true;
    controller.update = function (tasks, hasLoaded) {
      state.tasks = Array.isArray(tasks) ? tasks : [];
      state.hasLoaded = hasLoaded === true;
      var hasEdges = state.tasks.some(function (row) {
        var deps = (row.task || {}).dependencies;
        return Array.isArray(deps) && deps.length > 0;
      });
      var built = buildElements(state.tasks, !hasEdges);
      state.missing = built.missing;
      cy.elements().remove();
      cy.add(built.nodes.concat(built.edges));
      cy.layout(layoutOptions(cy.nodes().length)).run();
      applyFilter();
      fitVisible();
      renderList();
      renderNote();
      updateEmpty();
    };
    controller.setFilter = function (query) {
      state.filter = String(query == null ? "" : query).trim().toLowerCase();
      applyFilter();
      fitVisible();
      renderList();
    };
    controller.select = function (taskId) {
      state.selected = taskId;
      cy.nodes().removeClass("selected");
      var node = cy.getElementById(taskId);
      if (node && node.length) { node.addClass("selected"); }
      renderList();
    };
    controller.setConnection = function (connected, lastSuccessAt) {
      state.connected = connected !== false;
      state.lastSuccessAt = lastSuccessAt || null;
      if (!state.connected) { cy.stop(); } // 断线不继续运行动画
      if (el) { el.classList.toggle("graph-stale", !state.connected); }
      statusLine(
        (state.connected ? "已连接 · " : "已断连 · ") + "最后成功刷新 " + prettyTime(state.lastSuccessAt),
        !state.connected);
    };
    controller.destroy = function () { cy.destroy(); };

    cy.on("tap", "node", function (event) { choose(event.target.id()); });

    return controller;
  }

  global.BridgeTaskGraph = {
    mount: mount,
    statusLabel: statusLabel,
    acceptanceLabel: acceptanceLabel,
    reviewLabel: reviewLabel,
    acceptLabel: acceptLabel,
    modelLabel: modelLabel,
    badgeClass: badgeClass,
    prettyTime: prettyTime,
    STATUS_LABELS: STATUS_LABELS,
    ACCEPTANCE_LABELS: ACCEPTANCE_LABELS
  };
})(typeof window !== "undefined" ? window : this);
