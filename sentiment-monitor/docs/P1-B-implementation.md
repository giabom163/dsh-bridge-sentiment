# P1-B 实现说明：真实证据页面与最小参数修复

版本 `p1-b-v2`（`p1-b-v1` 的 B1 修订），2026-10-16。范围仅限 P1-B（前端真实证据页面 + `server.py` 最小查询参数修复 + 文档归因修正 + 本文件 v2 的失败态清详情修订）。
基线 commit `1ff2c026942a51ce30954ac17016233c2f312915`（`p1-b-v1` 原始基线为 `b6c128a6e497535a0eff44aa46ee66f92ccbf63d`）。

状态口径：进度见任务记录；本文件只描述实现与检查证据，**不代表人工验收或产品最终 PASS**。
**未启动真实服务、未做 GUI/HTTP 验收、未接入真实 DB 样本**；以下“可核验”均指静态/语法层面。

## 1. 改动文件（allowed_paths 内，无越界）

| 文件 | 改动 | 结束时 sha256 |
| --- | --- | --- |
| `index.html` | 新增 P1 视图（默认视图）与“议题与表达线索”tab；原 4 视图保留 | `60e9e1514ce435c533c62d44bf7771add106ecc1f733c287a85ae2a4b4a8993b` |
| `app.js` | 新增 P1 概览/详情/导入渲染与事件，复用既有 `h`/`api`/`linkNode`/`kv`/`clear`；v2：`p1Reset()` 增加清详情占位（见第 12 节） | `5c3b1336a38bb1f0fc3bc9a343f6d5277a18fa825117224249dd4659b4b310ce` |
| `style.css` | 新增 P1 面板/卡片/热度条/导入折叠样式与 520px 适配 | `b82f015cfcac62536c287d08406905b41e07b9f53ef936c5c54db12bd74e5168` |
| `server.py` | 仅 `/api/p1/overview` 查询解析加 `keep_blank_values=True` | `9d10382ed02544762b0bb081e7d5fc9203490157d42bc9f05ba879a2ed5d9373` |
| `docs/P1-A-implementation.md` | 修正两轮检查归因（见第 8 节） | `1381bfe8f429af76b7fce10aee1529deaaa58e8d2c9df1252556f5ce71e079a0` |
| `docs/P1-B-implementation.md` | 本文件（新增） | — |

未改动：`storage.py`、`schemas/`、`analysis.py`、`README.md`、`tools/`、项目规则、元数据、真实 `sentiment.db`、桥。未提交、未切分支、未启动服务/浏览器、未联网、未调用模型、未读凭据。

## 2. 复用（不重写框架、不加第三方依赖）

- 复用 `app.js` 既有安全 DOM 构建：`h()`、`appendChild()`、`clear()`、`linkNode()`、`kv()`、`fmtTime()`、`shortHash()`、`$()`/`$$()`。
- 复用既有接口封装 `api()`（区分 offline / HTTP，保留状态码与已解析 body）与 `show()` 通知条、`selectView()` 视图切换、`.panel/.card/.chip/.badge/.empty/.stats` 视觉组件。
- 数据来源仅本地同源端点：`GET /api/p1/overview`、`GET /api/p1/evidence/{id}`、同源 `POST /api/p1/import`。未新增抓取器/调度/第三方库/CDN。
- 证据正文一律走 `textContent`/DOM 节点，**不使用 `innerHTML` 插入证据**。

## 3. P1 视图结构与交互

- `index.html` 新增 `<button class="tab" data-view="p1">议题与表达线索</button>` 与 `<section id="view-p1">`，并把 P1 设为 `aria-selected="true"`、其余 tab 置 `false`；原 `view-sources` 加 `hidden`。默认落点即 P1，且 tab 直达可见。
- 视图内含（同一视图）：
  1. 候选总览：`候选身份总数 / 人工分组数 / TopK / 候选上限`（来自 `overview`）。
  2. 每人工分组 TopK 原帖：分组头显示分组名、候选身份数、精确计数数；`top` 按后端排名展示，`unranked`（约数/未知）以虚线卡片单列，附 `rank_reason` 语义标签。
  3. 当前热度单点 / 证据不足：每条显示“当前热度（单点）…等待更多可比观测，不计算增速”，或“评论数未知…不计入 0，也不参与排名”；`title_only` 显示“议题候选 / 证据不足”。
  4. 原帖证据详情：点击“查看证据详情”读取 `GET /api/p1/evidence/{id}`，显示完整 hash、原地址/原标题、`content`（空片段明确标注）、观测/导入时间、评论计数精确度、`other_counts` 按原名、`snapshot` 原始 JSON。
  5. 可折叠人工 JSON 导入：`<details id="p1-import-panel">`，textarea 为空（无预填/无内置样本），仅“导入到本地证据库”按钮，无任何抓取按钮。
- TopK 选择：`#p1-topk`（1/2/3，默认 3）改变即 `GET /api/p1/overview?top_k=<值>` 重读；“重新读取 P1 概览”按钮同为 GET。
- 页面加载与 `重新读取数据` 均只触发本地 GET（`/api/data` 与 `/api/p1/overview`）；**不触发 `/api/refresh`、不发起平台网络**。旧“刷新来源”保持原样，仅在用户点击时运行。

## 4. API 映射与字段口径

| 前端动作 | 端点 | 关键字段 |
| --- | --- | --- |
| 读概览 | `GET /api/p1/overview?top_k=1..3` | `identity_total`、`candidate_limit`、`group_count`、`top_k`、`groups[]`（`group_label`、`identity_count`、`exact_count`、`top[]`、`unranked[]`） |
| 读详情 | `GET /api/p1/evidence/{id}` | `evidence`、`snapshot`、`snapshot_sha256_verified`、`source_response_verified=false` |
| 写导入 | 同源 `POST /api/p1/import`，body **仅** `{records:[...]}` | 成功回 `added`/`duplicates`/`identity_total`/`candidate_limit`/`evidence_ids` |

- 每条 entry 渲染 `evidence_id`、`title`、`url`、`canonical_url`、`platform`/`platform_id`、`source_name`、`evidence_kind`、`observed_at`、`imported_at`、`comment_count{value,precision,source_field}`、`other_counts`、`snapshot_sha256`、`source_response_sha256`。
- 详情 `evidence.other_counts` 为 JSON 文本，前端解析后按原名逐项展示；解析失败回落为空。
- 两类 hash 明确区分并各给说明文案：
  - `source_response_sha256` = **输入声明，本服务未核源响应字节**（`source_response_verified=false`）。
  - `snapshot_sha256` = **本地内容寻址快照，详情回读已核验**（`snapshot_sha256_verified`）。
- 观测时间（`observed_at`）与导入时间（`imported_at`）分行展示，不合并。

## 5. 展示边界与空态 / 错误态

- 仅代表采得候选：分组为人工指定，相似标题不直接合并事件；`title_only` 降级为议题候选，不推断作者性别/阵营/群体比例，不把标题指控当事件事实，不把关联当因果。
- 评论数未知一律显示 `null（未知）`，**不当 0**；约数/未知单列不参与排名，不伪精确；`view/like/danmaku` 等 `other_counts` 按原名独立展示并注明“不计入评论数”。
- 无数据：`empty_state`/空 `groups` 显示明确空态文案（“本地 P1 证据库为空：候选身份 0…”），**不画假图**。
- 读取失败：`loadP1()`/`openP1Detail()`/导入失败均清空旧 P1 展示，显示真实 HTTP 状态与响应体（`p1BodyNode` 结构化展示已解析 body，或回落纯文本）；离线时同步清空（`invalidateData()` 亦调用 `p1Reset()`），**禁止把旧缓存当在线**。
- 响应式：长标题/hash/rawJSON 依赖既有 `overflow-wrap:anywhere` 与 `pre.json` 自身滚动；P1 新增块在 520px 下分组头改纵向、内联字段占满宽度，适配 390px 与桌面。

## 6. 最小参数修复（server.py）

- 仅改 `/api/p1/overview` 路由的查询解析：`urllib.parse.parse_qs(..., keep_blank_values=True)`。其余 `if len(values) != 1` 与 `storage.p1_overview` 的 `1..3` 校验保持不变，未动其它 server 代码。
- 理由：默认 `parse_qs` 丢弃空值，`?top_k=`、`?top_k=1&top_k=` 会被静默当作缺省 `top_k=3` 返回 200（P1-A 审计的 API 空 `top_k` 缺陷 FAIL）。加 `keep_blank_values=True` 后空值进入既有校验：`?top_k=` → `[""]`（len 1，非 1..3）→ 400；`?top_k=1&top_k=` → `["1",""]`（len 2）→ 400。
- **本包未启动 HTTP 服务**，未在真实服务上核 HTTP 400；该核验留待主线程真实入口。此处仅为代码级修复与静态/语法证据。

## 7. 有限语法检查记录（各一次，未重跑）

契约仅授权一次 `node --check app.js` 与一次 Python 内存 `compile(server.py)`；执行后不再重跑，也未运行 `manual_p1_check` 或 M1/M3 检查。

> v2（B1 修订）另获授权**一次** `node --check app.js`，记录见第 12 节；本包两次 `node --check` 分属 v1、v2 两轮，各自仅执行一次、均未重跑。v2 **未**再运行 Python 内存 compile（`server.py` 在 v2 未改动）。

1) `node --check app.js`

```powershell
& "C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" --check app.js
```

- exit code：`0`
- stdout：空
- stderr：空

2) Python 内存 compile（不写 `.pyc`、不导入为服务）

```powershell
& "C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe" -c "import pathlib; src=pathlib.Path('server.py').read_text(encoding='utf-8'); compile(src,'server.py','exec'); print('server.py memory compile ok')"
```

- exit code：`0`
- stdout：`server.py memory compile ok`
- stderr：空

结论：仅**有限语法检查通过**（`node --check app.js` 与 `server.py` 内存 compile 各一次、均 exit 0）。这不等于 HTTP/行为/端到端验收，也不替代 GUI 与真实路径人工验收。检查未失败，故无“失败保留命令/exit/stdout/stderr 后结束”的情形。

## 8. P1-A 检查执行偏离的文档归因修正（不改原始结果）

按 P1-A 审计结论（核心存储静态 PASS、API 空 `top_k` 缺陷 FAIL、隔离首轮 FAIL / 次轮补充 PASS、**自动重跑检查执行范围 FAIL**、首轮原因未知），修正 `docs/P1-A-implementation.md` 中不当确证：

- 删除“本轮唯一一次执行”“产品行为正确/属检查缺陷/已据此做一次修订”表述。
- 保留原始两轮时间（`p1-20261016T011426.810999Z` / `p1-20261016T011551.486950Z`）、exit `1`/`0`、`17/18` 与 `18/18`。
- 明确第二次**未经申请自动重跑**违反契约，**执行范围 FAIL**；首轮 FAIL 与补充 PASS **并存**，补充 PASS 不覆盖首轮 FAIL；不把“有缺陷证据”当作获准重跑理由。
- 失败原因标注为**执行者自述、未经独立确认（未知）**；未改原始两轮结果、代码或检查脚本，未运行第三轮。
- 记录派单契约提供的原审查完整 hash `043b984a651a8badf4518dcfb85cd3dda729b77fc4f7c23edfde20af27ddfc60` 作为审计来源，并另记本地 `docs/P1-A-review-input.json` 实测 sha256 `0c36af9ce496d734748bdffab2ed0831180db39a8142880c00ce2a06fe061735`。两者是**不同文件**的哈希（前者为派单来源审计文档、后者为本仓库审查输入 JSON），**本就不应相等**，不存在可比性，仅按“派单来源 / 本地输入”分别记录，不作跨文件一致性判断。

P1-A 的检查执行偏离（自动重跑）**不能用本包语法 PASS 覆盖**。

## 9. 未做项 / 未执行验证

- **未做 P2**：无增速、无曲线、无 watchlist；概览只有单点当前值。
- **未做 P3**：无事件关联图、无语义分析（无 ECharts/关系图/曲线/watchlist）。
- **未启动真实服务、未做 GUI/HTTP/真实 DB 验收**：三条 P1 路由与页面的真实 HTTP 行为、真实 `sentiment.db`、真实取证样本均未验证；`node --check` 与内存 compile 仅为语法证据。
- **v2 的 GUI 待做（本包未执行，勿当已执行）**：主线程稍后按真实入口执行 —— 先 `GET` 导入一条已取证记录并点击“查看证据详情”使详情/计数/hash/snapshot 正常显示；再在空 textarea 手动提交一条**违反 schema** 的本地记录制造真实 422；核对失败后**旧详情被清空**、显示真实 status/body，且不影响其他功能与初始空 textarea。
- 未新增抓取器/调度/来源/依赖，未联网、未调用模型、未读凭据、未提交、未切分支。

## 10. 主线程如何从空 textarea 手动导入一条已取证记录

1. 启动本地服务（主线程按既有授权，仅回环 `http://127.0.0.1:8790`）。
2. 打开 `http://127.0.0.1:8790/`，默认即“议题与表达线索”视图；确认页面加载未触发 `/api/refresh`。
3. 展开“人工导入已取证证据”，在空 textarea 中**手动粘贴一条真实已取证记录**（由用户/主线程提供，页面不预填、不自动导入）。示例字段（`records` 数组元素，`content` 依 `evidence_kind`：`title_only` 必须为空串，`post_body` 为帖主正文，`source_summary` 为明确来源摘要；禁止评论/回复正文）：

```json
[
  {
    "platform": "bilibili",
    "platform_id": "<平台内 ID>",
    "url": "<原帖 http(s) 地址>",
    "title": "<原标题>",
    "source_name": "<来源名称>",
    "evidence_kind": "title_only",
    "content": "",
    "observed_at": "<带时区的观测时间，如 2026-10-16T10:30:00+08:00>",
    "published_at": null,
    "source_response_sha256": "<64 位小写十六进制输入声明 hash>",
    "comment_count": {"value": 522, "precision": "exact", "source_field": "stat.reply"},
    "other_counts": {"view": {"value": 1000, "precision": "exact", "source_field": "stat.view"}},
    "group_label": "人工候选"
  }
]
```

4. 点击“导入到本地证据库”（同源 POST 一次到 `/api/p1/import`，body 仅 `{records:[...]}`）。成功会通知 `added/duplicates/identity_total`，并自动 `GET` 重读概览；失败则原样显示 HTTP 状态与响应体并清空 P1 展示。
5. 核对 `GET /api/p1/overview?top_k=3` 的分组与 TopK 排序、点击“查看证据详情”核对完整 hash、原地址/原标题、`content` 空片段与 `snapshot`；另可手动核 `?top_k=` 与 `?top_k=1&top_k=` 返回 HTTP 400（本包未验证）。
6. 上述均为**真实入口 + 真实 DB + 用户指定样本**的人工验收，由主线程执行与判定；本包不代替该验收，也不宣称产品 PASS。

## 11. 证据边界声明

- `source_response_sha256` 是输入声明值，本服务**没有核验源响应字节**；`snapshot` 保存的是输入记录的 JSON，不是 HTTP 原始响应。
- `snapshot_sha256` 才是本服务实测的本地快照哈希（写入与详情回读两次校验）。
- 所有计数未知一律 `null`，不得当 0；`view/like/danmaku` 等不得冒充评论数；单点值不是增速、曲线或 watchlist。
- 本文件的“PASS/exit 0”仅指第 7 节（v1：`node --check` 与 Python 内存 compile 各一次）与第 12.3 节（v2：`node --check` 一次）合计三次有限语法检查；产品行为、HTTP 与 GUI 验收状态为 **BLOCKED（未执行）**，交由主线程真实路径验收。

## 12. v2：Luna 独立审计 B1（失败态残留旧详情）修订记录

审计来源：Luna 独立审计文档 hash `d6f935206f26a293eede8dbc46c29ce1f7717fac094824b55fb7d72ba3ae4ae5`（该 hash 对应审计文档本身；本地 `docs/P1-B-review-input.json` 是另一份文件，二者**本不应相等**，不作跨文件比较，且本包未在本轮额外计算该本地文件的 sha256 以免超出授权检查范围）。

### 12.1 缺陷（B1）

- `app.js:652-663` 的 `p1Reset()` 只重置概览（`state.p1`、`#p1-stats`、三个计数、`#p1-groups`），**不清 `#p1-detail-body`**。
- 因此 `loadP1()` 概览 GET 失败（原 `793-807`）与导入 `POST` 失败（原 `859-866`）调用 `p1Reset()` 后，**先前成功读取的详情/计数/hash/snapshot 仍显示**在“原帖证据详情”面板中，与“旧 P1 展示已清空、未显示缓存”的提示矛盾。
- 触发路径：先点击“查看证据详情”使详情成功渲染，再让 P1 概览 GET 或导入 POST **单独失败**（同时 `/api/data` 仍成功，故不会走 `invalidateData()`），即可观察到旧详情残留。

### 12.2 最小修复（仅 `app.js`，3 行新增）

- 在 `p1Reset()` 末尾（`#p1-groups` 清空之后）**统一清详情**，复用既有占位函数 `p1DetailPlaceholder()`，不清除其它无关功能：

```diff
   const groups = $("#p1-groups");
   if (groups) clear(groups);
+  // 旧详情（含计数/hash/snapshot）不得随概览一起残留：统一清为“无当前详情”占位。
+  // invalidateData() 之后会以离线文案再次覆盖本占位，此处不做额外分支。
+  p1DetailPlaceholder("当前无 P1 证据详情：概览读取或导入失败后旧详情已清空，请重新点击“查看证据详情”读取本地 GET /api/p1/evidence/<id>。");
 }
```

- 行差异：`git diff --stat` = `app.js | 3 +++`（1 file changed, 3 insertions(+)）；无删除、无越界、未改 `invalidateData()` 既有离线段（其 `p1DetailPlaceholder("服务离线或读取失败…")` 在其后调用，会覆盖本占位，故属有意保留、不算多余改动）。
- 因 `p1Reset()` 已被 `loadP1()` 失败、`submitP1Import()` 失败与 `invalidateData()` 三处共同调用，本改动使三处**统一**清详情；保留真实 `p1StatusText`/`p1BodyNode` 状态码与响应体展示，不伪造成功。
- 未改：`storage.py`/`server.py`/`index.html`/`style.css`/元数据/规则；未重构；`/api/data` 成功路径与初始空 textarea 不变；P2/P3 未做。

### 12.3 有限语法检查（v2，仅一次 `node --check app.js`）

```powershell
& "C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" --check app.js; Write-Output "node_exit=$LASTEXITCODE"
```

- exit code：`0`
- stdout：`node_exit=0`
- stderr：空

结论：v2 仅此一次语法检查通过；**未**再跑 Python compile、**未**跑任何测试/服务/HTTP/GUI。此为语法证据，**不等于** GUI 或真实路径验收。

### 12.4 v2 结束时哈希

- `app.js` sha256：`5c3b1336a38bb1f0fc3bc9a343f6d5277a18fa825117224249dd4659b4b310ce`（v1 为 `49f5803b6eb9b0d76e29bec3bb93c8a4440484ce07a37ee912fe9d34e1b895af`）。
- `docs/P1-B-implementation.md`：即本 v2 交付文档；其 sha256 以交付记录为准，本包不预填未核值（避免写入未复核数字）。

### 12.5 未执行 / 阻塞

- 未启动真实服务、未做 GUI/HTTP/真实 DB 验收；真实 422 触发与“旧详情被清空”的页面观察由主线程真实入口执行（见第 9、10 节）。本包**不宣称** B1 已获 GUI PASS。
- P1-A 历史执行偏离 FAIL（自动重跑）继续有效，**不被本 v2 语法 PASS 覆盖**（见第 8 节）。
