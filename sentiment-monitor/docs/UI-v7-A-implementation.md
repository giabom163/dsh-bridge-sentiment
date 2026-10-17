# UI v7-A 实现说明：青蓝基线、弱数据管理、去诊断 UI、候选分母与抽屉修复

任务 `gender-ui-v7-a-flash-20261016`，基线 commit `f1eab5f836cf0dab95b3137e82bbe982ea0e6b51`。
本文件是同一 A 工作包**唯一一次有证据修订**的说明：按独立只读审查（Luna）逐条落实，并修补主线程真实 UI 证据发现的业务字段与设计偏差。
范围仅 v7 的 **A 呈现修订**；**未做 B/P2/C/P3**，未改服务、存储、数据库、采集、规则、配置、依赖与元数据。

状态口径：本文件只描述实现与有限检查证据，**不代表人工验收或产品最终 PASS**。
真实 GUI、真实 HTTP、真实库与 1280/390px 界面由主线程按真实路径执行与判定；本包不重做主线程已完成的实机采集。

## 0. 历史执行结论（照实保留，不洗成成功）

- 初次执行原生 **FAIL / exit 1**：provider.transport 报 `HTTP 200` 后 `Decode error`，原生请求被判定失败（`native_stop_confirmed true`）；随后仍写完了 4 个文件。
- 该失败不得改写成成功；仅一次 `node --check` exit 0 不能当作执行成功。
- 历史 390px 实机 **FAIL**、旧 P1-A 执行范围 **FAIL**、桥完整验收 **BLOCKED** 均**未解除**。
- B/P2、C/P3 仍为 **NOT_STARTED**。
- 1280/390 真实界面与服务路径验收**尚未进行**：本项当前为 **BLOCKED**。

## 1. 改动文件（allowed_paths 内，无越界）

| 文件 | 改动 | 本修订结束时 sha256（实测） |
| --- | --- | --- |
| `index.html` | 去首屏工程文案；业务页去 API 路径 / P1 / 技术细节；移除新增「证据原始返回」诊断板块；议题双栏「左列表 280px + 右概况，TopK 在概况之下」；footer 去 db 路径；新增数据管理状态容器 | `9c73e8d59f5ff9e5b19ee0d31b9fadf0615aaed2486c37381a440d7d08219dd5` |
| `app.js` | 青蓝无关；业务文案映射（字段名汉化/省略）、候选分母与 K 重算、异步请求令牌、`ApiError` 原文保留、modal 焦点圈定与背景隔离、移除诊断读取逻辑 | `a2f4167e18b4dcd397e82ebf08ef938e3f2f994a1be9b869e86700baf2ef81e8` |
| `style.css` | 主题色改青蓝 token、弱入口次级样式、去残留绿系、抽屉去 `overflow-x:hidden` 并真实收缩换行 | `9ff9b1adf0586b375744727279a77a73c4d6aab5d0346091ca8ccd4b143be15f` |
| `docs/UI-v7-A-implementation.md` | 本文件（重写） | —（本文件自身不计 hash） |

上表列出的是**本次实测**的 sha256；未复制、未沿用初始版本的旧 hash，也未宣称与旧值一致。
`git diff --stat`：`app.js | 257`、`index.html | 68`、`style.css | 64`（3 files changed）；产物均落在允许路径内。
未改动：`server.py`、`storage.py`、`analysis.py`、`schemas/`、`tools/`、规则、元数据、真实数据库、桥。未提交、未切分支；未启动服务/浏览器、未联网、未调用模型、未读凭据、未读真实库。
未删除任何后台快照或旧数据结构；`/api/records`、`/api/runs`、`/api/runtime`、`/api/analysis`、旧 RSS 导入与来源列表等旧能力与入口均保留在数据管理内。

## 2. 逐条缺陷关闭依据（对照独立审查 + 真实 UI 证据）

| 审查项 | 结论 | 本修订依据 |
| --- | --- | --- |
| 浅灰白卡 + 青蓝强调，统一 token，去残留绿 | 已修 | `style.css` 主题 token 改青蓝：`--accent:#0f6b8a`、`--accent-strong`、`--accent-soft:#e7f1f6`、`--accent-line`；链接、按钮、Tab 选中、`.link`、焦点环、`.topic-item.active`、`.admin-tab[aria-selected]`、`.heat-value`、`.p1-heat` 全部改引用青蓝 token。成功语义色（`.conn.online`、`.notice.ok`、`.biz-status.ok`、`.status.succeeded`）保留语义绿。 |
| 数据管理弱入口 | 已修 | 新增 `.tab.weak` 次级样式（透明底、弱边框、灰字、较细字重），选中态用 `--accent-soft` 弱蓝，视觉层级低于三个业务入口。 |
| 首版不新增技术诊断 UI | 已修 | 删除 `index.html` 的「P1 证据原始返回（工程诊断）」整块（`#p1-diag-id`、`#p1-diag-load`、`#p1-diag-body`）与 `app.js` 的 `loadP1Diag()` 及其事件绑定；未以折叠/隐藏替代。导入错误证据改写入**已有数据管理区域**的 `#p1-admin-status` 状态容器（纯文本 + 原始响应体原文）。 |
| 业务页只呈现业务文案 | 已修 | 评论数来源统一映射为业务词「平台公开评论总数」，不再输出平台 `source_field` 原值；其他计数只保留中文口径（播放/点赞/弹幕/投币/收藏/分享），未知字段**省略**不回显；业务页与详情去除 API 路径、`P1`、`stat.*`、`TopK`（总览状态）、「后端」等工程文案；解释性长文压为短边界。 |
| 筛选分母与 TopK | 已修 | 概览**始终** `GET /api/p1/overview?top_k=3`（候选上限内全部候选），统一筛选后在前端分组、按精确评论数**重排**并生成显示 rank，再按所选 K（1/2/3）展示；检索与分组统计不受所选 K 截断。 |
| 正文补载 / 失败清缓存 / 422 清旧详情 | 保持通过 | `enrichP1Contents()` 仅为已载候选补正文；概览失败、导入失败（含 422）与详情失败均清空旧业务材料/详情，不把旧缓存当在线数据（并叠加请求令牌保护）。 |
| 响应体完整保留（边界情形） | 已修 | `ApiError` 新增 `rawText` 字段，`api()` 无论成功与否都先取 `response.text()` 原文；非 JSON 原文不再丢弃。管理区错误显示真实 HTTP 状态 + 原始响应体文本。 |
| 快速切换异步请求顺序 | 已修 | 概览用 `state.overviewToken`、抽屉用 `state.drawerToken` 递增令牌；过期响应（含 enrich 迟到写入）直接返回，不覆写新状态/DOM。`p1Reset()` 与 `closeDrawer()` 使在途令牌失效。 |
| 抽屉关闭 / ESC / 焦点 | 已修 | 打开时对 `#main`、`.top`、`#main-nav`、`footer` 设置 `inert` 隔离背景；`keydown` 实现 Tab / Shift+Tab 圈定于 `.drawer-panel` 内可聚焦元素；ESC / 关闭按钮 / 背景点击关闭并返回触发按钮焦点；关闭时清除隔离。390px 下抽屉 `width:100%` 全屏。 |
| 390/1280 排版 | 源码层面已处理，GUI 仍 **BLOCKED** | 移除 `.drawer-panel` 的 `overflow-x:hidden`，改为对抽屉内 `a`/`dd`/`code` 真实 `overflow-wrap:anywhere; word-break:break-word`；窄屏 `dl.kv` 单列（标签在上、值在下）；`.grid`/`.stats`/`.filters` 用 `minmax(0,1fr)`，Grid 子项 `min-width:0`。是否彻底消除 390 横溢**须由主线程实机确认**。 |

### 2.1 主线程真实 UI 证据对应关系（本包不重做这些实机）

- 初稿真实 DOM 中总览含 `GET /api/data`、`GET /api/p1/overview`、`P1`、`TopK`、「后端」、`stat.reply`：已在业务状态文案、业务页/详情渲染中去除这些字符串。
- 详情含 `stat.reply/stat.coin/stat.danmaku/stat.favorite/stat.like/stat.share/stat.view`：已改为中文业务词，且不再回显原始字段名。
- 390 宽 `innerWidth=390 / doc=376` 无横溢、`dd=343.8px`、ESC 返回触发按钮，属**有限 PASS**；因业务字段与设计偏差整体 **FAIL**。本修订针对字段/设计偏差，未重跑该实机。
- 首次「wide」采集实际为 **304px**，不是 1280；后续校正为真实 1280（`doc=1265.5`）。本包不把旧 304px 当作 1280 证据，也不重做该采集。

## 3. 三业务入口 + 弱数据管理入口（保留）

- 主导航：`总览`（默认）、`议题与表达线索`（固定标题）、`原帖检索`、`数据管理`（`.tab weak`）。
- 业务页只读：仅「重新读取本地数据」，触发同源 GET；外网「刷新来源」只在数据管理内。
- 数据管理含内部子导航（来源与导入 / 原文与证据 / 人工证据 / 报告模板 / 运行状态），旧能力整块保留（含 `raw_snapshot`、`input_sha256`、运行时路径与 sha256），属诊断职责。
- 议题页布局：左侧 `280px` 议题列表 + 右侧「概况」，TopK 原帖卡片位于右侧概况**之下**。

## 4. 真实单点与累计评论口径（不虚构趋势/情感/关联）

- 概览小指标：`采得原帖（人工候选身份）`、`人工候选组（非确认事件）`、`最近观测（本次已载入材料）`，均取真实字段单点值。
- 评论数一律为**单点当前值 + 精确度**（精确/约数/未知）；未知写「未知」并注明「不计入 0，也不参与排名」，绝不当 0。
- 统一注明「该原帖历史累计评论数，单点观测，不代表近 7 天新增或增速」；不画趋势线/饼图/情绪图/关系图，不算增速。
- 议题概况附短空态「关联：暂无可证的议题关联……」；标题线索不足时降级为议题候选，不推断群体立场。

## 5. 业务页 / 详情不生成技术 DOM（非 CSS 隐藏）

- 业务卡片 `p1BusinessCard()` 仅渲染：rank/未排名标记、证据类型、来源名、标题链接、单点评论口径、查看详情/查看原帖。
- 业务详情抽屉 `renderP1Drawer()` 仅渲染业务词：来源、原标题、原地址、证据类型、正文/来源摘要、观测时间、发布时间、评论数（累计口径）、评论数来源（业务词）、其他计数（中文口径）。
- **未生成** `snapshot`/raw JSON、`*_sha256`、`snapshot_path`、`evidence_id`、`platform_id`、`group_label`、`source_field` 等字段名节点，也无执行日志节点。

## 6. 统一筛选与候选分母

- 统一筛选：来源 / 证据类型 / 关键词（匹配标题、来源、已载入正文或来源摘要）；总览、议题、检索共用 `state.filters`，切换 Tab 同步取值。
- **候选分母**：概览请求固定 `top_k=3`，载入候选上限内全部候选；筛选后重算每组精确评论排序与显示 K。
- 动态统计区分**全体候选**与**当前筛选**：议题列表项显示「本组候选（全体）/ 当前筛选 / 精确计数」，概况 chips 同口径，筛选说明显示「本次已载入原帖 X 条，筛选后 Y 条」。
- 日期范围筛选后端未支持：**不提供控件**，也不再在业务页解释后端细节。

## 7. 有限语法检查记录（仅一次，未重跑）

```powershell
$out = & "C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" --check app.js 2>&1
$code = $LASTEXITCODE
"exit=$code"; "stdout+stderr:"; $out
```

- exit code：`0`
- stdout：`exit=0`、`stdout+stderr:`（其后无内容）
- stderr：空（已并入同一次输出，无内容）

结论：**本修订仅此一次语法检查通过**。未运行任何测试脚本、未做 Python compile、未启动服务、未发 HTTP、未做 GUI/DB 检查。检查未失败，故无「保留失败命令」情形。
（第 0 节的初始执行 FAIL 属历史结论，与本节这一次检查无关。）

## 8. 未执行验证 / 已知限制（如实列出）

- **未启动真实服务、未做 GUI/HTTP/真实库验收**：三业务入口、数据管理入口的真实渲染、抽屉打开/关闭/焦点圈定、导入 422 清旧、1280/390 溢出与竖排**全部未在本包验证**。
- `enrichP1Contents()` 在概览加载后对已载入候选（≤3 条）逐条 GET 详情以支持关键词匹配，属页面打开时的本地详情预读；请求次数与失败降级需主线程确认可接受。
- 搜索范围受候选上限（≤3）约束，不是全库检索；无增速/曲线/观察清单；无事件关联图。
- 未新增 P2 观察清单/热度历史接口，未新增 P3 正文逐条证据绑定与关系查询；未引入 ECharts/Cytoscape。
- 未读取真实库、未填入任何样本或数值；库内容由运行环境真实返回决定。

## 9. 主线程实机验收建议（本包未执行）

1. 启动本机回环服务（既有授权）与真实入口；确认默认落「总览」，主导航四项，数据管理为**弱入口**且可达。
2. 核对业务页与抽屉 DOM（非仅可见区）：**不存在** snapshot/raw JSON/长 hash/本地路径/字段名/`stat.*`/API 路径/`P1` 节点。
3. 在议题页把「每组展示条数」切到 1/2/3，确认：候选分母不变、重排正确、检索不被 K 截断、约数/未知单列不参与排名。
4. 核对主题色为青蓝、无残留绿系（成功状态除外）；对比 1280/390 双视口无根页面横溢、窄屏标签值上下排列。
5. 点「查看详情」核对真实 GET、侧抽屉、`inert` 背景隔离、Tab/Shift+Tab 圈定、ESC/关闭/背景点击与焦点返回；390px 抽屉全屏。
6. 快速切换多个详情/概览，确认旧响应（含迟到 enrich）不覆写新状态；制造失败确认旧内容被清空。
7. 制造真实 422 导入与失败 GET，确认业务侧仅短提示、管理区保留真实 HTTP 状态与原始响应体。
8. 以上为真实路径人工验收，由主线程执行与判定；本包不代替，也不宣称产品 PASS。

## 10. 边界声明

- 本文件所载 hash 为本次实测值；不复制、不伪造旧 hash，不宣称最终 PASS。
- 产品行为、HTTP、GUI 与真实库验收状态为 **未执行（BLOCKED 于人工实机）**；1280/390 界面验收当前为 **BLOCKED**。
- 历史结论保留：初始执行 FAIL/exit 1、390px 实机 FAIL、旧 P1-A 执行范围 FAIL、桥完整验收 BLOCKED 均未被本包覆盖或解除；P2/P3 为 NOT_STARTED。
- 后台快照、哈希校验、旧数据留存不被本包破坏；业务页隐藏的是展示，不是删除数据。

## 11. 追加窄修复：GET 失败留证与 skip 背景隔离（基线 `b0dc62cadda63d2980333d5a90118752e7042056`）

本节记录在同一 A 工作包内的**同类小范围追加修复**。第 0–10 节为上一轮（基线 `f1eab5f`）的历史结论，**原样保留、不追改**；上一轮的 `app.js` sha256 `a2f4167e…` 属该轮实测值，已被本轮新值取代，见下。

### 11.1 触发依据（复审 FAIL，结论未解除）

- 独立只读复审（Luna）**判定 FAIL**：两处 GET 失败路径未把**真实 HTTP 状态与原始响应体**送入数据管理状态容器，属证据留证缺失。
  - `openP1Drawer()` 详情 `GET` 失败（原 1165–1176）：仅业务提示，无原始 body 留证。
  - `loadP1()` 概览 `GET` 失败（原 1180–1199）：同上。
- 同一 FAIL 另含：`setBackgroundInert()` 未纳入页面 `.skip`「跳到主内容」链接，抽屉打开时该链接仍可被聚焦，背景隔离不完整。
- 该复审 FAIL 属真实结论，**本包不宣布其已解除**：缺陷已按下列最小改动处理，但复审/验收是否通过由 Luna 与主线程判定。

### 11.2 改动（仅 `app.js`，最小、无业务/布局/框架重做）

| 位置 | 修复 |
| --- | --- |
| `setBackgroundInert()` | 隔离选择器加入 `.skip`，与 `#main`/`.top`/`#main-nav`/`footer` 一同设置/清除 `inert`；未改 Tab 圈定、ESC/关闭/返回焦点等无关 focus 逻辑。 |
| `p1BodyNode(err)` | 空 `rawText` 分支区分**网络失败**（`kind==="offline"` 或 `status===0` → 「网络失败：未收到任何响应体」）与**已连通但空 body**（有 `status` → 「HTTP N：已连通，响应体为空」）；不补造 body/状态码。 |
| `openP1Drawer()` catch | 业务抽屉改为**固定短提示 + HTTP 状态**（经 `p1StatusText`），不再拼 `err.message`；新增 `setAdminStatus("error", …, p1BodyNode(err))`，真实 message 与原始响应体只落 `#p1-admin-status`。 |
| `loadP1()` catch | 三业务状态改为固定短提示；在 `p1Reset()` **之后**写 `setAdminStatus("error", …)`，避免清空动作覆盖新错误证据。 |
| `submitP1Import()` catch | `setBizStatus` 与 `show()`（业务 globalnotice）均改为固定短提示，不再拼 `err.message`；完整 `err.message` 与原始响应体保持只进 `#p1-admin-status`。 |

- 复用既有 `setAdminStatus`、`p1BodyNode`、`p1StatusText`，未新增接口/依赖/字段。
- 上一轮已实机核过的通过项（业务 DOM 无技术字段、390 抽屉宽度、ESC 返回、modal 四背景 `inert`、真实 422 原始 body 管理区显示并清旧详情）**未改动**。
- 旧 `backgroundInert` 属性读数 `false` 系工具仅读映射 `.inert` 未能读出（真实 `hasAttribute` 四项为 `true`，已核），**不作为产品 FAIL**。

### 11.3 实测命令与结果（本轮仅一次，未重跑）

```powershell
$out = & "C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" --check app.js 2>&1
$code = $LASTEXITCODE
"exit=$code"; "stdout+stderr:"; $out
```

- exit code：`0`
- stdout：`exit=0`、`stdout+stderr:`（其后无内容）
- stderr：空（已并入同一次输出，无内容）

`app.js` 本轮结束 sha256：`ca5cda1a8108b555561e1b86c91824ced82dec9716e69b205ef8fd838911c485`（实测）。
`git diff --stat`：`app.js | 25 ++++++++++------`（1 file changed, 19 insertions, 6 deletions）。
未改动 `index.html`、`style.css`、服务/存储/DB/规则/元数据/依赖/桥；未提交、未切分支。

### 11.4 未执行的验证（如实列出）

- **未启动服务/浏览器、未发 HTTP、未做 GUI/DB 检查、未跑测试脚本**：`openP1Drawer`/`loadP1` 失败时管理区真实显示、空 body 文案、`.skip` 隔离在真实抽屉打开/关闭中的表现**均未在本包验证**。
- 真实 1280/390 视口、真实 422 与失败 GET 的端到端留证，仍由主线程按真实路径复验；本包不代替人工验收、不宣称产品最终 PASS。
