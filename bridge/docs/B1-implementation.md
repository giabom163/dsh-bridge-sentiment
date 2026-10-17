# B1 桥只读任务协同图 · 实现说明

本文件只记录本次实际改动与真实运行结果。**这不是最终验收 PASS**：未做真实浏览器/实机核对，主线程实机验收仍未执行。当前状态语义上相当于 `awaiting_review` + 机器验收 `BLOCKED`。

## 0. 本次修订（针对 docs/B1-audit.md 的 F1/F2/F3）

本轮只修 F1/F2/F3，保留其他实现；未改 `bridge.py`、`adapters.py`、schema、执行/接收逻辑、真实 store、CSP 或依赖。

- **F1（未知 ≠ 空）**：`web/app.js` 新增 `state.hasLoadedTasks`，仅在 `GET /api/tasks` 成功后才置真并传给图组件；`web/task-graph.js` 新增 `state.hasLoaded` 并接收该标志。从未成功加载时，图覆盖层显示“尚未成功读取任务列表；无法确认任务是否为空…”，文本列表显示“无法读取任务（尚未成功加载）；不能判定为空。”，表格空态显示“无法读取任务”并隐藏“创建第一个任务”，指标计数显示“—”而非 0；只有成功加载且确为空才显示“暂无任务/还没有任务”（三者共用同一判别）。已有数据后断线仍保留旧数据并显示旧的最后成功时间。
- **F2（执行前截断）**：`tests/graph_checks.py` 改为在检查循环上按 `--limit` 截断（`plan[:args.limit]`），只执行前 N 组；记录新增 `limit` 与 `executed_checks` 字段，便于机器核对实际执行的组。
- **F3（逐项 UI 哈希）**：`check_static_routes` 现在对 `UI_ROUTES`（`/`、`/app.js`、`/style.css`、`/task-graph.js`、`/vendor/cytoscape.min.js`）逐项比较 **runtime 记录哈希 == HTTP 响应体 SHA-256 == 本地磁盘 SHA-256**；输出字段更名并收窄范围为 `ui_response_hashes_match` + 明细 `ui_route_hashes`。
- **主线程浏览器核 F1 的夹具**：`tests/graph_checks.py` 新增显式 `--serve-ui-fixture api-fail|empty` 与 `--port`，只在该参数被主动使用时才绑定 `127.0.0.1:<port>` 持续提供真实 UI；使用独立临时 store（`data/ui-fixture-<hex>`），顶部注入醒目红色“样本夹具”横幅（经 `/style.css` 追加规则 + 页面注入元素，CSP 未放宽），`api-fail` 仅使 `/api/tasks` 固定返回 503（正文含 `fixture`），`empty` 返回成功空列表。普通检查模式不持久驻留服务，也不自行启动该夹具。

真实运行记录见 §6.1。

## 1. 实际修改的文件

| 路径 | 改动 |
| --- | --- |
| `web/task-graph.js`（新增） | 只读协同图组件：真实任务节点、仅按 `task.dependencies` 连边、内置 `breadthfirst` 布局、点击节点打开真实详情、空图/缺失依赖/未知状态可见、可访问文本列表；自身不发网络请求。修订：接收 `hasLoaded` 标志，区分“从未成功加载（未知）”与“成功加载且确为空”（F1）。 |
| `web/vendor/cytoscape.min.js`（新增） | 固定版本 Cytoscape core（3.34.3，MIT）。 |
| `web/vendor/cytoscape-LICENSE`（新增） | 随 core 保存的上游 MIT 许可证原文。 |
| `web/vendor/NOTICE.md`（新增） | 记录来源、版本、URL、校验值与许可证信息。 |
| `web/index.html` | 按 CSP 以本地脚本加载 `vendor/cytoscape.min.js`、`task-graph.js`、`app.js`；新增图容器、文本列表、连接/断线显示 id。修订：表格空态文字加 `emptyTitle`/`emptyHint` id，供区分未知与空（F1）。 |
| `web/app.js` | 复用唯一 5 秒刷新器并接入图更新；搜索与图同步过滤；节点/表/图选中联动；断线保留数据并显示旧时间、停止图动画；10 状态与 3 验收值映射共用、未知原值保留、null 模型显示“未记录”。修订：新增 `hasLoadedTasks`，首次失败不把未知当空，指标/表格/图/列表一致区分（F1）。 |
| `web/style.css` | 仅新增图、状态、断连与窄屏样式。 |
| `webui.py` | 为新增静态资源增加**逐项显式白名单**路由；把实际加载的 `web/task-graph.js`、`web/vendor/cytoscape.min.js` 纳入 `loaded_sha256`；更新 build 标识为 `shared-session-graph-20261015`。CSP 与 API 未改。 |
| `tests/graph_checks.py`（新增） | 三组定向离线检查，支持 `--limit 1–3`。修订：`--limit` 改为执行前截断（只运行前 N 组，F2）；静态检查逐项核对 UI 路由响应体哈希（F3）；新增显式 `--serve-ui-fixture api-fail\|empty` + `--port` 浏览器夹具（见 §0）。 |
| `docs/B1-implementation.md`（本文件，新增） | 实现与检查记录。 |

未修改：`bridge.py`、`adapters.py`、`schemas/task.schema.json`、认证、原 store、执行/接收逻辑。`git status` 的改动集仅为上表内路径。

`git diff --stat`（首轮，已提交基线）：
```
 web/app.js     | 68 ++++++++++++++++++++++++++++++++++++++++++++++++++++------
 web/index.html | 16 +++++++++++++-
 web/style.css  |  2 ++
 webui.py       | 18 ++++++++++++++--
 4 files changed, 94 insertions(+), 10 deletions(-)
```

本次修订 `git diff --stat`（工作区未提交，仅限 allowed_paths）：

```
 docs/B1-implementation.md |  50 ++++++++++++--
 tests/graph_checks.py     | 161 +++++++++++++++++++++++++++++++++++++++-------
 web/app.js                |  21 ++++--
 web/index.html            |   2 +-
 web/task-graph.js         |  31 +++++++--
 5 files changed, 223 insertions(+), 42 deletions(-)
```

（`data/` 下的检查夹具目录已由 `.gitignore` 的 `/data/` 忽略，不出现在改动集。）

## 2. 状态与验收（中文完整文字，独立显示）

图节点、表格、详情共用同一映射（`web/task-graph.js` 导出，`web/app.js` 复用）。10 个任务状态按契约完整文字：

`ready 待规划` / `prepared 空会话待确认` / `running 执行中` / `awaiting_review 待审查` / `awaiting_user 待用户验收` / `accepted 已接收` / `rework 返修待规划` / `failed 失败` / `cancelled 已取消` / `stop_unconfirmed 停止未确认`。

- 未知状态保留原值，显示为 `未知（原值）`，不折叠成已知状态。
- `cancel_requested` 保留为独立过程标签 `取消中（过程标签）`，不挤占 10 状态。
- 3 个验收值 `PASS 通过` / `FAIL 失败` / `BLOCKED 阻塞` **独立于任务状态**显示（详情“验收（独立）”）；缺失/未知显示 `未知（未记录）` / `未知（原值）`，不默认 PASS。
- 模型为 null/缺省时显示“未记录”，非空值照实显示。

## 3. 图与刷新、断线

- 边**只**来自 `task.dependencies`；被引用但不在 `GET /api/tasks` 返回列表中的依赖，在图注显示“缺失依赖”，且不伪造其节点或状态。
- 布局为 Cytoscape 内置 `breadthfirst`，不加载任何布局扩展，不使用 CDN。
- 复用 `web/app.js` 中原有唯一的 `setInterval(()=>refresh(),5000)`；图组件内无任何定时器（`task-graph.js` 不含 `setInterval`）。
- 刷新失败时：状态置为断连，顶部连接标签与图区显示“已断连 · 最后成功刷新 <旧时间>”，图容器加 `graph-stale` 并以 `cy.stop()` 停止动画；保留已有数据，但不把旧数据当新鲜。
- **F1 三态区分（图、文本列表、表格一致）**：①从未成功加载 → 显示“无法读取任务/无法确认是否为空”，不显示“暂无任务”，表格隐藏“创建第一个任务”，指标显示“—”；②成功加载且确为空 → 显示“暂无任务/还没有任务”；③已有数据后断线 → 保留旧数据、旧时间并标断连。判据为 `state.hasLoadedTasks`（app）/`state.hasLoaded`（图）。
- 不触发 `native-status`，不发送模型请求。

## 4. 固定依赖下载（两次，间隔 ≥20 秒）

- 第 1 次：`GET https://registry.npmjs.org/cytoscape`（HTTP 200，2026-10-15T12:52:06Z–12:52:07Z UTC），`dist-tags.latest = 3.34.3`。
- 第 2 次：`GET https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.3.tgz`（HTTP 200，2026-10-15T12:52:31Z–12:52:32Z UTC），与第 1 次间隔 24.4 秒。
- 仅此两次外部请求，无其他外部探测，未走 CDN。

本地实算校验值（与 `web/vendor/NOTICE.md` 一致，已回读核对）：

| 文件 | 字节 | SHA-256 |
| --- | --- | --- |
| `web/vendor/cytoscape.min.js` | 435503 | `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |
| `web/vendor/cytoscape-LICENSE` | 1082 | `eb319c6e6f233607f71e8e2f450391751883cfc0eeb3ca7ef574c13d1d9c2203` |
| 上游 tarball `cytoscape-3.34.3.tgz` | 1378263 | `5d9e479216ed58a5f1ac639e746fd7d9770c4393f8c355dd133abc90bcc9ff3f` |

限制：受“两次下载”约束，未在 metadata 响应中留存并回读 npm 发布的 `dist.integrity` 做逐字节比对。

## 5. 静态路由与 CSP

`webui.py` `do_GET` 中新增的路由均为逐项显式白名单，绝不按请求路径拼接文件：

`/task-graph.js`（text/javascript）、`/vendor/cytoscape.min.js`（text/javascript）、`/vendor/cytoscape-LICENSE`（text/plain）、`/vendor/NOTICE.md`（text/markdown）。

CSP 未削弱，仍为：`default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:`。`/api/*` 与写入逻辑未改。

`/api/runtime` 的 `loaded_sha256` 现包含 `web/ui` 四个原文件 + `web/task-graph.js` + `web/vendor/cytoscape.min.js`（实际加载字节 SHA-256），build 标识为 `shared-session-graph-20261015`。定向检查（F3）对其中每个 UI 路由的响应体字节与 `loaded_sha256` 逐项核对，字段名为 `ui_response_hashes_match`（范围即上列 5 个 UI 路由，不含 `webui.py`/bridge 模块等非 UI 条目）。

## 6. 定向检查（首轮真实执行记录，修订前现场，未覆盖）

> 说明：本节为 B1 首轮实现（修订前）的真实执行记录，原样保留。其 `loaded_hashes_match` 为旧字段名；修订后的真实记录见 §6.1。

命令与结果（真实执行；首次 `--limit 3` 后，因修正图节点状态样式类做了一次复核运行，共两次，未跑全量、未自动重试）：

```
& <python> -B tests\graph_checks.py --limit 3
EXIT_CODE=0        # 两次均 0
```

三组输出（原样）：

```json
{"check": "status_acceptance_unknown_null", "status": "PASS", "detail": {"status_labels_10": true, "unknown_kept": true, "null_model_kept": true, "raw_values_preserved": true, "explicit_model_kept": true}}
{"check": "dependency_selection_disconnect", "status": "PASS", "detail": {"dependency_edge_beta": true, "missing_dependency_seen": true, "edges_from_dependencies_only": true, "single_5s_poller": true, "graph_has_no_poller": true, "disconnect_stops_animation": true, "node_click_wired": true}}
{"check": "static_routes_and_ui_compat", "status": "PASS", "detail": {"runtime_ok": true, "loaded_hashes_match": true, "build_updated": true, "whitelist_routes": true, "csp_self_only": true, "original_ui_present": true, "no_cdn_reference": true, "traversal_rejected": true, "traversal_codes": [404, 404, 404]}}
```

保留夹具（未清理）：`data\graph-check-1ff376c1e18e`（首次）、`data\graph-check-d5e78cd7908a`（复核），两次均 `model_requests: 0`、`acceptance: BLOCKED`。检查在进程内起停服务，未持久启动、未发模型请求、未触发 `native-status`。（说明：上列首轮夹具目录属于首轮执行环境，在本修订工作副本中不存在，未重造、未覆盖。）

检查覆盖边界说明：

- 组 1 为**接口原值保真**（真实 GET 夹具）＋**前端映射文字静态断言**，未执行浏览器 JS。
- 组 2 为**接口依赖值**＋ `web/app.js`/`web/task-graph.js` 的静态断言（唯一轮询、缺失依赖、断线处理、点击联动）；断线视觉为静态确认，非浏览器实测。
- 组 3 为真实 HTTP 路由/哈希/CSP/穿越探测（原始请求行，避免客户端归一化），原界面元素存在性为页面静态断言。

### 6.1 修订后真实执行记录（F1/F2/F3）

修订后重新真实执行（`-B` 禁用 bytecode 缓存；未跑全量、未自动重试）：首次 `--limit 3`，随后 `--limit 1`。两次退出码均 0，`model_requests: 0`。

`--limit 3` 三组输出（原样）：

```json
{"check": "status_acceptance_unknown_null", "status": "PASS", "detail": {"status_labels_10": true, "unknown_kept": true, "null_model_kept": true, "raw_values_preserved": true, "explicit_model_kept": true}}
{"check": "dependency_selection_disconnect", "status": "PASS", "detail": {"dependency_edge_beta": true, "missing_dependency_seen": true, "edges_from_dependencies_only": true, "single_5s_poller": true, "graph_has_no_poller": true, "disconnect_stops_animation": true, "node_click_wired": true, "unknown_vs_empty_distinct": true}}
{"check": "static_routes_and_ui_compat", "status": "PASS", "detail": {"runtime_ok": true, "ui_response_hashes_match": true, "ui_route_hashes": {"/": {"runtime_key": "web/index.html", "runtime_sha256": "42dd74918382a118049f21e4ea5a3a27ca66f2a7e60c06c5f01380f9d83fb5c9", "http_sha256": "42dd74918382a118049f21e4ea5a3a27ca66f2a7e60c06c5f01380f9d83fb5c9", "disk_sha256": "42dd74918382a118049f21e4ea5a3a27ca66f2a7e60c06c5f01380f9d83fb5c9", "match": true}, "/app.js": {"runtime_key": "web/app.js", "runtime_sha256": "5c39b3ad3b020e09a9da0f7ac5940c8de1175106ccf78c21bd4adb0bca38a2c1", "http_sha256": "5c39b3ad3b020e09a9da0f7ac5940c8de1175106ccf78c21bd4adb0bca38a2c1", "disk_sha256": "5c39b3ad3b020e09a9da0f7ac5940c8de1175106ccf78c21bd4adb0bca38a2c1", "match": true}, "/style.css": {"runtime_key": "web/style.css", "runtime_sha256": "0fec8047cda2d621603afbfb8bedb170458056e13ec60717e79ceac1a894ad17", "http_sha256": "0fec8047cda2d621603afbfb8bedb170458056e13ec60717e79ceac1a894ad17", "disk_sha256": "0fec8047cda2d621603afbfb8bedb170458056e13ec60717e79ceac1a894ad17", "match": true}, "/task-graph.js": {"runtime_key": "web/task-graph.js", "runtime_sha256": "8a99f32c35cda13062b944848b187ab1286b66aafd7431f75133b232ba2da117", "http_sha256": "8a99f32c35cda13062b944848b187ab1286b66aafd7431f75133b232ba2da117", "disk_sha256": "8a99f32c35cda13062b944848b187ab1286b66aafd7431f75133b232ba2da117", "match": true}, "/vendor/cytoscape.min.js": {"runtime_key": "web/vendor/cytoscape.min.js", "runtime_sha256": "5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91", "http_sha256": "5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91", "disk_sha256": "5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91", "match": true}}, "build_updated": true, "whitelist_routes": true, "csp_self_only": true, "original_ui_present": true, "no_cdn_reference": true, "traversal_rejected": true, "traversal_codes": [404, 404, 404]}}
```

`--limit 1` 输出（证明执行前截断：只运行第 1 组，`executed_checks` 仅 1 项）：

```json
{"check": "status_acceptance_unknown_null", "status": "PASS", "detail": {"status_labels_10": true, "unknown_kept": true, "null_model_kept": true, "raw_values_preserved": true, "explicit_model_kept": true}}
{"limit": 1, "executed_checks": ["status_acceptance_unknown_null"], "model_requests": 0, "acceptance": "BLOCKED"}
```

保留夹具（未清理，均在 `data/` 下，`data/` 已 gitignore）：`graph-check-1afa62700fe9`（`--limit 3`）、`graph-check-208f4824fb65`（`--limit 1`），均 `model_requests: 0`、`acceptance: BLOCKED`。两次均在进程内起停服务，未持久驻留、未发模型请求、未触发 `native-status`。

夹具模式（仅供主线程浏览器人工核验 F1）另做了进程内端口 0 的隔离自检（临时 store，非仓库路径；未使用 `--serve-ui-fixture`、未持久驻留）：`api-fail` 的 `/api/tasks` 返回 503 且正文含 `fixture`；`empty` 的 `/api/tasks` 返回 200 `{"tasks": []}`；两种模式下 `/` 注入 `fixture-marker` 横幅元素、`/style.css` 追加 `.fixture-marker` 规则、`/app.js` 与 `/task-graph.js` 正常返回。真实浏览器观察仍为 BLOCKED，留主线程。

## 7. 未执行的验证与缺失项（如实列出）

- 未在真实浏览器/主线程环境实机核对：图渲染、节点点击打开详情、搜索与图同步的视觉效果、窄屏布局、断线时图动画停止的可视表现、Cytoscape 加载失败时的报错与表格可用性。
- 未在真实浏览器实测 F1 的三态（首次 GET 失败/成功空列表/已有数据后断线）。已提供显式夹具供主线程核验，命令形如：`python -B tests/graph_checks.py --limit 3 --serve-ui-fixture api-fail --port 8771` 与 `--serve-ui-fixture empty`；该夹具绑定 `127.0.0.1`、用独立临时 store、顶部有醒目“样本夹具”横幅、`api-fail` 仅使 `/api/tasks` 固定 503（正文含 `fixture`），不请求外部或模型、不碰真实 store；普通检查模式不会启动它。
- 未提供不含 Cytoscape 的降级演示；组件缺失时报错路径仅做了代码与静态断言，未在浏览器实测。
- 未提供真实、获准的任务数据来核对 10 状态/3 验收值；检查使用自建夹具，不等同实机验收证据。
- 组 1/组 2 含静态断言成分，不等同端到端行为验证。
- 未做 npm 发布 `dist.integrity` 逐字节比对（受下载次数约束）。
- 未修改/未删除任何桥 store、状态文件或人工验收证据；未提交、未推送。

## 8. 结论

代码与三组定向检查在本机离线范围内真实通过（退出码 0）。**运行成功 ≠ 最终验收 PASS**；主线程实机验收（尤其真实数据的状态/验收映射、断线可视表现、CSP 无控制台报错）尚未执行，最终验收结论仍为 `BLOCKED`。
