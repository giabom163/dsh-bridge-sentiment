# UI v7-B 实现说明（受限修订轮）

## 0. 本轮定位与状态

- 任务：`gender-ui-v7-b-flash-20261016`；本文件在同一 attempt worktree 内累积记录各窄轮（均为「与方案/审查/实机不符的最小修复」）及本轮文档事实最小更正。
- 历史前两轮：受限修订轮真实基线 `479e30a691754b6aec726608189af1d2d770cdbf`；B 窄修轮（图表实例 key / resize / 历史区间）真实基线 `c53eef629bd02b0febbf88c18d51195e6c4bba0b`，其只改 `storage.py`、`app.js`、本文件（见 §2.2、§2.8–§2.10）。
- **历史轮：B 实机打回窄修（300 秒），真实基线 `b4b5a533061a765ee1634d2cf9da241908d69f46`**，只改 `app.js`、`style.css`、`index.html`、本文件四文件（该轮修复条目见 §2.11，差异见 §1.1）；不改 `storage.py`/`server.py`/`analysis.py`、vendor、AGENTS、其他文档、配置、元数据、真实 DB；不联网、不调用模型、不启动产品 8790/GUI 助手、不提交、不切分支、不清理。
- **历史轮：B 实现说明事实最小更正（文档轮），真实基线 `dbfcc98d77080e6e0f0f83f8dad70bf7c43021ee`（当时 HEAD；其后以 `a44a3e5892d16b988522ca9b28d0f54ae9f69ede` 保存为待审候选）**：仅按 `docs/UI-v7-B-review.md` 第 3 节更正本文件的修改范围表、修复章节索引与验收事实，不测试、不联网、不提交、不改产品代码，且不清除任何历史失败记录。该轮**未完全消除**仍存在的不存在章节索引（`§3.1`）与旧 `NOT_STARTED` 表述，本轮据实更正（见 §1.0、§3、§7）。
- **本轮：B 实机 405 残留历史最小修复 + 说明事实更正，真实基线 `a44a3e5892d16b988522ca9b28d0f54ae9f69ede`（当前 HEAD）**：只改 `app.js`、本文件两文件；修复 `p1Reset()` 在读取/导入失败时未清同帖历史 DOM 与两图 caption、未失效 `watchToken`/`historyToken` 的旧路径（见 §1.0、§2.12），并据实补充本轮真实 8791 GUI 复现 **FAIL** 与**待复验**（见 §3）；不改 `index.html`/`style.css`/`storage.py`/`server.py`/`analysis.py`、`tools/`、vendor、AGENTS、其他文档、配置、元数据、真实 DB；不联网、不调用模型、不启动产品服务/GUI、不提交、不切分支、不清理。
- **本轮未执行任何测试、语法检查、脚本、HTTP、服务或 GUI**：只读源码与 `git status`/`git diff` 并编辑（见 §7）。
- 进度与验收是两套独立字段：
  - 受限修订轮实现（本工作区）：**DONE**；其唯一一次定向检查：**PASS**（退出码 0，三组 18 项全 PASS，详见 §6，其余各轮**未**重跑）。
  - B 实机打回窄修执行轮实现：**DONE**（该轮修复条目见 §2.11，均已落地）；该轮修复后主线程真实回归：**在本轮 8791 GUI 复现中为 FAIL**（残留旧同帖历史行与图 caption，见 §1.0、§2.12），**本轮修复后待复验，不预判 PASS**。该轮含的旧时区标签与窄屏历史表两项已由主线程真实 GUI **局部复验**，但**完整回归尚未 PASS**。
  - 本轮实机 405 残留历史最小修复：**DONE**（见 §2.12）；修复后真实 GUI 复验：**未执行（待复验），不预判 PASS**。
  - 真实 GUI / 真实 8790/8791 / 真实 DB 人工验收：**首轮真实 GUI 整体 FAIL；窄修后主线程真实回归 FAIL（本轮 8791 复现）；本轮修复后待复验**；提交 `b4b5a53` 的「B 静态批准」仅作**有限 GUI 验证的准入（前置门）**，**不等于**产品 PASS；执行者**不预判 PASS**；人工 accept：**null**（由主线程判定，执行者不自动 accept，**不虚构 GUI 通过**）。
- 历史事实（首次 900 秒 timeout、`provider.transport` 报 HTTP 200 后 `Decode error`、缺指定说明、原契约只准一次却实际两次定向脚本均 exit 1、一次编辑工具 warning）全部保留在 §3，且不以本轮结果覆盖。
- 历史触发原因（B 实机打回窄修执行轮）：主线程在**已通过静态准入**后做真实 8790 实机验收，发现 `docs/UI-v7-B-review.md` 第 2 节**打回项**所列明确缺陷（**实机 FAIL**）；该轮据此做四文件窄修，**实机证据与修复过程原样记录，不删不改、不虚构通过**。
- 本轮触发原因：窄修后主线程真实 **8791** GUI 复现发现残留缺陷——数据管理「人工证据」提交 `[]` 触发真实 Handler **405**、业务提示旧材料已清空后，观察清单仍残留**旧同帖历史行与图 caption**。本轮据此做两文件最小修复，**实机证据与修复过程原样记录，不删不改、不虚构通过**。

## 1. 实际修改文件

### 1.0 本轮修改（B 实机 405 残留历史最小修复 + 说明事实更正，基线 `a44a3e5892d16b988522ca9b28d0f54ae9f69ede`）

本轮只改以下两文件：

| 文件 | 变更摘要 |
| --- | --- |
| `app.js` | `p1Reset()` 增补同帖历史旧路径清理：`state.watchToken++`/`state.historyToken++` 失效在途响应，`#watch-detail-title`/`#watch-detail-status`/`#watch-history-points` 清空并放中性占位，两图 caption `#chart-count-status`/`#chart-rate-status` 复位为初始文案；业务不再残留旧 count/rate。观察资格列表 `#watch-active`/`#watch-history-body` 保持不动（见 §2.12）。 |
| `docs/UI-v7-B-implementation.md` | 本文件（新增本轮实机 FAIL 与待复验，更正仍存在的不存在章节索引 `§3.1` 与旧 `NOT_STARTED`，历史文件表明确标注所属旧轮；见 §0、§2.12、§3、§7）。 |

本轮未修改：`index.html`、`style.css`、`storage.py`、`server.py`、`analysis.py`、`tools/`、`vendor/`、`AGENTS.md`、`docs/P1-B-implementation.md`、其他文档、配置与元数据、真实 DB。

### 1.1 历史轮：B 实机打回窄修执行轮（基线 `b4b5a533061a765ee1634d2cf9da241908d69f46`）修改的四文件（保留，不清除）

该轮只改以下四文件（与 `git diff b4b5a53..a44a3e5` 的产品代码差异一致；本文件为文档）：

| 文件 | 变更摘要 |
| --- | --- |
| `app.js` | 活跃卡时区标签改为 `Asia/Shanghai UTC+08:00`；倒计时按总分钟取整后再拆分（消除「23 小时 60 分」）；导入成功与观察清单刷新后顺序重读并重渲染；新增 `refreshSelectedWatchHistory()` 使退出后详情同步；窗口生效时立即 pending 清空；单点速率 caption 据真实 `rate_per_hour` 如实说明（见 §2.11）。 |
| `index.html` | 总览热度标题「当前热度（单点观测）」→「当前热度（最新观测）」；观察历史表加 `watch-history-table` 类。 |
| `style.css` | 仅新增 `.watch-history-table` 的局部 `min-width` 与列宽，维持 `.table-wrap` 局部横向滚动；不改全局表格与页面横向溢出规则。 |
| `docs/UI-v7-B-implementation.md` | 本文件（记录该轮修复条目，见 §2.11）。 |

该轮未修改：`storage.py`、`server.py`、`analysis.py`、`tools/manual_p1_b_check.py`、`tools/serve_b_fixture.py`、`vendor/`、`AGENTS.md`、`docs/P1-B-implementation.md`、其他文档、配置与元数据。

### 1.2 历史轮次（受限修订轮 `479e30a…` / B 窄修轮 `c53eef6…`）实际修改文件（保留，不清除）

以下为**历史轮次**（受限修订轮基线 `479e30a691754b6aec726608189af1d2d770cdbf`、B 窄修轮基线 `c53eef629bd02b0febbf88c18d51195e6c4bba0b`）的实际修改文件，**不属于本轮**，仅为保留历史事实而留存（表内文字为历史原样）：

| 文件 | 变更摘要（历史轮次） |
| --- | --- |
| `storage.py` | `p1_watch_add` 在重复检查/活跃计数/插入前显式取得 SQLite 写锁（`BEGIN IMMEDIATE`），异常 rollback、成功 commit；调用方已有事务时**明确拒绝**该调用（见 §2.2）。 |
| `app.js` | 分页每页前后核对令牌、截断身份不计入窗口候选；pending 清 `#overview-shortcuts`；历史先渲染可读表、两图各自 try/catch；watchlist GET 失败与 watch POST 失败清旧缓存/重读 GET 并保留原错证据。 |
| `tools/manual_p1_b_check.py` | 移除 ghost 身份与所有绕 cap 的虚构身份/重复活跃周期；改为 C 到期后真实 `POST` 显式重加；`A_off_top2_but_active` 按身份（当前最新 evidence_id）核对；新增 cap 原子性与“无直接注入周期行”静态回归锁。 |
| `tools/serve_b_fixture.py` | 创建目录前只接受 `--limit 3`；账本响应体改为按内容哈希完整写入独立 artifact（不截断、不打截断标记），账本引用其 sha256/长度/路径。 |
| `docs/UI-v7-B-implementation.md` | 本文件（补齐此前缺失的指定实现说明）。 |

历史轮次未修改：`server.py`、`index.html`、`style.css`（当时审查必修项不涉及，保持原样即为最小改动），以及 `vendor/`、`AGENTS.md`、`analysis.py`、`docs/P1-B-implementation.md`、其他文档、配置与元数据。

## 2. 必修项与对应修复

- 小节归属：§2.1、§2.3–§2.7 属受限修订轮（基线 `479e30a…`）；§2.2、§2.8–§2.10 属 B 窄修轮（基线 `c53eef6…`，其小节标题写作「本轮窄修」指该轮）；§2.11 为 B 实机打回窄修执行轮（基线 `b4b5a53…`）的修复条目。

### 2.1 窗口分页不再用截断点冒充窗口最新候选（app.js）

- `fetchHistoryPoints(evidenceId, isStale)`：改为每页**请求前**与**响应后**都核对令牌，过期立即返回 `null`，旧窗口/旧选择不再继续发满 20 页。
- `loadWindowCandidates(token)`：接口顺序为升序，`has_more` 仍为真时截断页末点不是窗口内真实最新点。现在对 `history.truncated` 为真的身份**明确标记为加载不完整并排除**，不计入完成候选、也不取 `points[-1]` 当窗口最新值；被排除数量记入 `state.windowTruncated`，在统一筛选说明中以“另有 N 条候选因历史分页未取完被明确排除，不计入完成候选与分母”呈现。
- 总览 / 议题 / 检索三视图仍共用 `p1ActiveEntries()` 的同一窗口候选集合，**保持三个业务同分母**。
- `renderWindowPending()` 现在同步清空 `#overview-shortcuts` 并放入“正在读取…”占位，窗口切换期间不再残留上一窗口原帖卡片。失败路径经 `loadP1` 的 catch → `p1Reset()`，`p1Reset` 已清 `overview-shortcuts`。

### 2.2 storage 观察 cap 跨连接原子（storage.py）

- 新增 `immediate_write(db)` 上下文管理器。`p1_watch_add` 的“重复检查 + 活跃计数 + 插入”整体放入其中：
  - 连接未处于事务时先 `BEGIN IMMEDIATE` 取得写锁，成功 `commit()`、异常 `rollback()`；
  - **调用方已开启事务（`db.in_transaction`）时明确拒绝**：既有 SAVEPOINT 既不能重新取得写锁，也不能证明该事务已是 writer transaction，故不再退回 SAVEPOINT，而是抛出 `P1TransactionRequired`（`immediate_write` 不再进入检查、不插入），**不提交也不回滚调用方事务**，由调用方自行处理其事务。本函数因此**只对无事务连接提供跨连接原子门**；不再把 SAVEPOINT 描述为等价原子的保证。
- 仓库内唯一调用方 `server.py` 的 `/api/p1/watchlist` 处理每个请求用新连接（`db.in_transaction` 为假），因此正常路径始终走 `BEGIN IMMEDIATE`；拒绝分支只在调用方自持事务的误用场景触发。
- `server.py` 既有进程内 `WRITE_LOCK` 保留（Handler 仍串行化单进程写），存储层 `BEGIN IMMEDIATE` 作为跨连接原子门。
- 既有 DDL `CREATE TABLE IF NOT EXISTS p1_watch_periods` 保持幂等。

### 2.3 图表失败仍保留可读表与另一张图（app.js）

- `renderWatchHistory(record, history)` 顺序调整为：**先** `renderWatchPoints(points, truncated)` 渲染真实可读历史表，**再**逐图渲染。
- 两张图各自独立 `try/catch` 包裹 `echarts.init()` / `setOption()` / `resize()`：任一图抛错时销毁并清空该图、给短占位“该图表渲染失败；下方可读历史表为同源真实值，另一张图不受影响”，并更新该图 caption；另一张图与可读表保留。
- 图表异常不再冒泡到 `loadWatchHistory` 的读取失败分支，**不会**把已成功取得的真实点渲染成空窗口。
- 业务短提示走 `setWatchAdminStatus` 的固定文案，图表真实错误 message 只落数据管理“人工证据”状态容器留证。

### 2.4 watchlist GET 失败清旧历史（app.js）

- `loadWatchlist()` 的 catch 现在同时：在丢弃已失效的旧响应之外，令 `state.historyToken++`（失效在途/旧历史）、`state.history = null`、`state.selectedWatchId = null`；清空并占位 `#watch-active`、`#watch-history-body`、`#watch-history-points`、两张图与 `#watch-detail-status`。旧历史点与详情图不再残留在页面。

### 2.5 watch POST 失败清缓存并真实重读 GET（app.js）

- 新增 `refreshWatchAfterWriteFailure(err, actionLabel)`：先 `state.watch = null` 并 `renderWatchlist()` 清空旧观察展示，再 `await loadWatchlist()` 真实重读 GET；GET 失败时 `loadWatchlist` 自身保持空展示（不复用旧值）。最后再落写请求的原始错误证据（`setWatchAdminStatus`），**避免被 GET 的失败状态覆盖**。
- `submitWatchAdd` / `submitWatchExit` 的 catch 统一改走该函数。

### 2.6 定向检查脚本去虚构身份并补到期重加（tools/manual_p1_b_check.py）

- 删除 `p1w-ghost-cap` ghost 身份及其“占满上限触发第 4 活跃 cap 分支”的注入；不再伪造任何第 4 身份或重复活跃周期。全库身份上限仍由真实 `POST /api/p1/import`（`fixture-D`）→ 409 与 `count(DISTINCT identity_key)==3` 覆盖；活跃数只保留自然不变量 `active <= 3`。
- `watch_expiry_and_atomic_cap` 重写为 `watch_expiry_and_readd`：把 C 的 `expires_at` 定向设为过去 → 断言 `expired` 且不在 active、旧周期仍在 history 且为 `expired`；再用真实 `POST /api/p1/watchlist` **显式重加 C** → 断言 201、新 `watch_id`、旧周期仍 `expired`、C 重新 active；结束时 A/B/C 各保有一个有效活跃周期且 `active <= 3`。
- `A_off_top2_but_active` 断言保留（不删）。改为按**身份**核对：`a_now = latest_evidence(db, "fixture-A")["id"]`，断言 `a_now not in top2` 且 `a_now in active`。
- 仅使用 A/B/C 三条真实 evidence 身份，全部经真实 `storage.p1_import` / 真实 `POST` 写入隔离库。

### 2.7 隔离 GUI 助手 limit 预检与账本完整（tools/serve_b_fixture.py）

- `--limit` 改为只接受 `3`，且在 `--evidence-dir` 创建前校验失败即 `parser.error`（不会半初始化三身份）。
- `Ledger` 响应体不再 `[:4000]` 截断：按内容哈希完整写入 `<evidence-dir>/http-bodies/<sha>.body`（独立 artifact，不截断、不打截断标记），账本行引用 `body_sha256` / `body_length` / `body_path` / `body_complete: true`。

### 2.8 图表实例 key 统一（app.js，本轮窄修）

- 缺陷证据：`renderChart` 以 DOM id（`chart-count`/`chart-rate`）写入 `state.charts`，而 `clearWatchCharts` 与 `window.resize` 却遍历语义 key（`count`/`rate`），两者不一致，导致切换历史/错误清空时 dispose 不到真实实例、resize 也遍历不到已画图。
- 修复：`state.charts` 初始化、`renderChart` 存储、`renderWatchHistory` 单图异常清理、`clearWatchCharts`、`window.resize` 现在统一使用 `chart-count`/`chart-rate`；清空时能 dispose 真实实例，1280→390 已画图可正确 resize。

### 2.9 resize 单图异常独立捕获（app.js，本轮窄修）

- 新增 `resizeWatchChart(key, label)`：逐图独立 `try/catch` 包裹 `chart.resize()`。单图抛错时只销毁并清空该图、给固定业务短提示、更新该图 caption，并在数据管理「人工证据」状态容器记录真实 `error.message`；另一张图与可读历史表保留。沿用 §2.3 的图表 try/catch 逻辑，不引入新框架。

### 2.10 历史表区间显示与 caption 纠正（app.js，本轮窄修）

- 可读历史表保持 7 列与布局：`观测时刻` 列仍显示该点终点时间；`实际间隔` 列改为显示真实比较区间“比较起点 → 终点（格式化时区时间）+ 格式化时长”，**仅当该点确有 `previous_observed_at`** 时显示，缺前值显示 `—`，不推断。（更正：该列实际用 `formatInterval(interval_seconds)` 输出“X 分钟 / X 小时 Y 分”的格式化时长，并非原始秒数；上一轮文档误写为“间隔秒数”，本轮据源码改正。）
- “参考前值在窗口外”说明**只在该点比较起点确早于当前 `state.windowFrom`** 时追加；窗口内比较不再一律误标为窗口外。
- 计数图与速率图 caption 分开：计数图单点仍提示“等待第二个观测点”；速率图**单个已计算速率区间**也如实显示该真实速率（“仅 1 个已计算速率区间”），不再用“等待第二个观测点 / 不声称增速”否认已算速率；速率图**无有效区间**时才提示等待。未知/约数仍显示“未知”，不补 0。
- 图表 tooltip 只显示观测时刻与数值，不暴露 API 字段名。

### 2.11 本轮（B 实机打回窄修执行轮）修复条目

本轮为在 `b4b5a53` 静态准入后、真实 8790 GUI 首轮**整体 FAIL** 所触发的四文件窄修（基线 `b4b5a53…`，差异见 §1.1）；逐条对应 `docs/UI-v7-B-review.md` 第 2 节静态结论：

1. 时区标签（`app.js`）：活跃卡「加入时刻（服务器 UTC）」改为「加入时刻（Asia/Shanghai UTC+08:00）」，与 `fmtTime()` 固定按 `Asia/Shanghai` 格式化一致，不再误标服务器 UTC。
2. 倒计时取整（`app.js`）：`watchStatusText` 先把剩余毫秒向下取整为总分钟，再拆小时/分钟，分钟落在 0–59，消除「23 小时 60 分」。
3. 导入后刷新（`app.js`）：`submitP1Import` 成功后顺序 `await loadP1()`、`await loadWatchlist()` 并 `renderBusinessViews()`，不再沿用旧 evidence_id / 旧图。
4. 退出后详情（`app.js`）：新增 `refreshSelectedWatchHistory()`，`loadWatchlist()` 成功后按最新周期记录重读所选同帖历史；所选周期已不在清单则清空详情与图。窗口切换与 `#watch-reload` 不再并发另起旧 record 的 `loadWatchHistory`，避免旧值覆写新值。
5. 窄屏历史表（`index.html` + `style.css`）：历史表加 `watch-history-table` 类；`style.css` 仅新增该表局部 `min-width` / 列宽，维持 `.table-wrap` 局部横向滚动，不改全局表格与页面横向溢出规则。
6. 日期 pending（`app.js`）：`loadP1()` 在窗口生效时立即清空 `windowCandidates` / `windowTruncated` 并 `renderWindowPending()`，pending 期间不残留上一窗口旧卡与旧 note。
7. 单点速率说明（`app.js`）：`renderChart` 在窗口内仅 1 个计数观测点时，若该点已有真实可比的已计算 `rate_per_hour`，caption 改为「速率以真实可比区间为准」，不再与速率图矛盾地称「等待第二个观测点 / 不声称增速」。
8. 总览标题（`index.html`）：总览热度标题「当前热度（单点观测）」改为「当前热度（最新观测）」。

## 3. 历史失败事实（保留，不得删除或覆盖）

- Flash 首次执行 **900 秒 timeout**，已停稳；`provider.transport` 报告 HTTP 200 后出现 `Decode error`，已停稳。
- 原契约只允许**一次**定向检查，实际执行了**两次**定向脚本，两次均 **exit 1** → 执行范围 **FAIL**。
- 第一次运行的期望值存在**时区换算错误**，且失败后**未停止**。
- 第二次运行 `watch_outlives...` 断言 **FAIL**；失败 detail（原始值）：
  - `top2 = [p1ev-ad96b9246f41f8b2bf3a86dd9438df1619efb57a28d3e3e98b9c0c1f47270080, p1ev-b5df278caf3fb8880c9f978416ebc1881366f2d93951913c5d7545ac4a71c509]`
  - `active = [p1ev-6b7db0c522f06279c409a46a79c668e9ae4d35d865010673535f9947ce02932c, p1ev-b5df278caf3fb8880c9f978416ebc1881366f2d93951913c5d7545ac4a71c509, p1ev-ghost]`
- 此前缺失指定说明 `docs/UI-v7-B-implementation.md`；上一轮补齐本文件。
- 一次**编辑工具 warning**：上一轮编辑过程中出现一次 `oldString` 未找到的 warning（桥审查已记录须保留）；本工作区未提供其原始字节，故不补造具体文案，仅作为历史事实保留。
- 上一轮唯一一次定向检查 **exit 0 × 三组 18 项全 PASS**（见 §6）；本轮**未重跑**任何检查，且不以此覆盖上述 FAIL 或前述执行范围 FAIL。
- 上述原始 stdout/stderr/result 由主线程保存在**外部证据目录**；本工作区未提供其原始字节，故本轮**不补造**退出码或输出，也不删除/覆盖主线程的外部保存。

## 4. `A_off_top2_but_active` 失败归因更正（不臆断，本轮窄修）

- 静态核对（用真实 `storage` 纯函数 `p1_prepare` / `json_bytes` / `digest` 逐条重算 evidence_id，不建库、不启服务）确认夹具映射与历史 detail 完全一致：
  - 组：三条夹具记录 `group_label = "隔离合成分组"`，同一组。
  - Top2（`p1_overview(db, 2)`）：C=320 → `p1ev-ad96b92…`，B=240 → `p1ev-b5df278…`。
  - A 的最新真实观测为 2026-10-10T07:00（值 170，`stat.other`）→ `p1ev-6b7db0c…`，确实落在 Top2 之外。
  - ghost 行无对应 `p1_evidence`，`watch_item()` 为其退化为 `evidence_id="p1ev-ghost"`、`latest_observed_at=null`。
- **归因更正（据独立复审）**：旧的**整组** `watch_outlives...` FAIL **没有逐项真假明细**，不能据此断言 `watch_outlives...` 组内各项分别是真或假；给定原始值里 **A 的 Top2 条件成立**（A 的最新证据确实落在 Top2 之外），且 A 的旧 evidence 仍在 active 列表中。因此**不能把 A 从 active 消失确定性归因于 ghost**——没有证据支持该因果。
- ghost 行本身无对应 `p1_evidence`，属**独立的夹具缺陷/违约**（不应存在的注入身份）；它与 A 的断言失败不能并列为同一确定性根因。
- 仅能静态确认的脆弱点：`watch_item()` 返回身份**当前最新** `evidence_id`（随新观测更新），若继续拿旧 evidence_id 对照实时 active 列表，**追加新观测后**就可能误报。
- 处理：保留断言，改为按当前最新 evidence_id（等价于按身份）对照，并新增 `a_evidence_id_updated` 明确“evidence_id 随新观测更新属正常”，不把合法的 evidence_id 更新误判为观察身份消失；**未删除断言刷 PASS**。

## 5. 已知限制（本轮不重构）

- 审查提到 `storage.p1_history` 每次分页会把该身份全历史读入并重算（长历史下重复全量扫描，属性能建议）。本轮按“不扩大任务、不重构内核”要求**保留为已知限制**：首版小数据、身份上限 ≤3、响应点上限 500；未改分页内核语义（比较仍先基于完整有序历史判定，再套窗口）。
- 合法状态下身份总数 ≤3 且同身份重复优先冲突，因此“第 4 个不同活跃观察”cap 分支难以由纯 API 状态自然到达；本轮**不为覆盖该分支注入虚构周期行**，只断言 `active <= 3` 与真实第 4 身份导入 409。

## 6. 上一轮一次定向检查（唯一一次；本轮未重跑）

- 执行目录：本工作区；解释器：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`（Python 3.13.12）。
- 命令（仅执行一次，未使用会吞 exit 1 的 shell 包装）：
  `python tools/manual_p1_b_check.py --limit 3 --evidence-dir <全新隔离目录>`
- 真实退出码：**0**（PASS）。
- stdout 原文（单行 JSON）：
  `{"status": "PASS", "evidence_dir": "...\\b-revise-20261016-160847\\evidence", "summary": {"group1_history_rules": {"total": 10, "failed": 0}, "group2_watch_lifecycle": {"total": 5, "failed": 0}, "group3_http_edges_static": {"total": 3, "failed": 0}}}`
- stderr：空。
- 产物（外部证据目录，未写入工作区、未删除、不覆盖主线程此前保存）：`C:\Users\Lenovo\AppData\Local\Temp\opencode\b-revise-20261016-160847\`，含 `stdout.log`、`stderr.log`，以及脚本自建的 `evidence\result.json`、`evidence\fixture.db`、`evidence\root\`。
- `result.json` 关键字段：`status=PASS`、`port=55996`（脚本内部真实 Handler 临时回环端口，非 8790/8791/8792）、`isolation_row_count=18`、`isolation_watch_rows=5`、`product_acceptance=BLOCKED`、`gui_acceptance=false`、`real_db_touched=false`、`network_requests=0`；三组共 18 个检查全部 PASS（10 + 5 + 3）。
- 说明：上一轮仅此一次；其后未再修、未重跑。此隔离夹具路径的 PASS **不等于**产品最终 PASS，也不替代真实 8790/GUI/真实 DB 的人工验收。检查前已完成全部代码修改；检查后仅回填本节与 §0 的文档文字，未改任何被检代码。本轮（B 窄修）**未重跑**任何检查。

## 7. 未执行 / 未覆盖

- 真实 GUI：**未开始**（未接管任何标签页、未启动 `tools/serve_b_fixture.py`）。
- 产品 8790 服务、真实 `sentiment.db`、网络、`/api/refresh`、外部模型：**均未执行/未触碰**。
- vendor 文件哈希未在本轮独立重算（沿用既有 provenance）。
- 定向检查只证明隔离夹具路径；**不等于**产品最终 PASS，也不替代主线程真实路径 / GUI 验收。
- 上一轮除唯一一次定向检查外，还额外执行了内存 `compile()` 与 `node --check app.js`；这**超出当时契约的允许执行范围，属执行范围 FAIL，不得称为“允许的受限语法检查”**（独立复审亦作同样判定）。该事实与其结果原样保留，不因文档更正而消除或美化。
- 本轮（B 窄修）**未执行任何检查**：无 `node --check`、无 `compile()`、无脚本、无 HTTP、无服务、无 GUI、无真实 DB；仅只读源码与 `git status`/`git diff` 并编辑。真实 GUI / 真实 8790 / 真实 DB 范围**未经验证**，故**不擅自验收 PASS**。

## 8. 审查口径澄清

- 审查对 A 的描述仅为**静态审计**，不覆盖主线程 A 的实机 PASS；本文件不据此改判 A。
- 审查中“`docs/P1-B-implementation.md` 存在”与本轮工作区结果无关：该文件属于既有内容，**本轮未改**；本轮只补齐指定 `docs/UI-v7-B-implementation.md`。

## 9. 交付边界

- 本轮（B 窄修）工作区仅修改 `storage.py`、`app.js`、`docs/UI-v7-B-implementation.md` 三个允许文件；未提交、未切分支、未修改 `.git`、外部配置、vendor、真实库或桥状态文件。上一轮修改文件见 §1。
- 本文件为 UTF-8 + LF 写入，避免 CRLF 导致的跨环境 hash 差异。
