# M2 工作台：界面 ↔ 真实接口映射

版本 m2-ui-1，2026-10-15。状态：**未完成实机验收（未运行测试）**。本文件描述前端实际调用的接口、展示字段与未实现能力，用于人工审查；不声称已通过实机或用户验收。

所有接口请求都指向同源服务 `http://127.0.0.1:8790`。服务端强制校验 `Origin`/`Host`，因此页面**必须**通过该地址打开（`http://127.0.0.1:8790/`），以 `file://` 或其它端口打开时 POST 会被 403 拒绝。前端不缓存数据：每次读取都是实时请求，失败时显示离线/错误，不用旧数据冒充在线。

## 视图与接口映射

### 视图一：来源与导入

| 界面元素 | 接口 | 方法 | 说明 |
| --- | --- | --- | --- |
| 页面加载、统计卡、来源列表 | `/api/data` | GET | 读取 `feeds / items / keywords / stats / runs / manual_limit / legacy_items_pending / schema_version / items_limit` |
| 添加来源表单 | `/api/feeds` | POST | 输入 `name`、`url`；成功 201，重复 409 |
| 保存关键词表单 | `/api/keywords` | POST | 输入 `keywords` 数组，成功 200 |
| 导入 JSON 记录 | `/api/import` | POST | 输入 `{feed_id, records}`；成功 201，校验失败 422，来源不存在 400，磁盘/库失败 500 |
| 顶部“刷新来源” | `/api/refresh` | POST | 依次处理前 `manual_limit` 个来源、每源最多 `manual_limit` 条，每源独立批次 |
| “用此来源导入”按钮 | 仅前端 | — | 把该来源填入导入表单的来源下拉，不发请求 |

展示字段：来源 `name/url/item_count/last_success_at/last_run_id`（最后成功时间与最近批次来自 `/api/data` 的 feeds 子查询）；`last_run_id` 可点击进入批次详情。

### 视图二：原文与证据

| 界面元素 | 接口 | 方法 | 说明 |
| --- | --- | --- | --- |
| 记录列表与全表分母 | `/api/data` | GET | `items` 为真实列表（服务端 ORDER BY 后 LIMIT 500），`stats.record_count` 为全表分母 |
| 查看证据详情 | `/api/records/{id}` | GET | 返回 `{record, raw_snapshot}`；服务端回读快照并重新校验 `raw_sha256`；未知 ID 404，证据缺失/损坏 409 |

展示字段（列表与详情）：`id`、`canonical_id`、`feed_name`、`raw_sha256`、`content_sha256`、`raw_path`、`time_quality`、`published_at`、`published_raw`、`fetched_at`、`dedup_reason`、`run_id`、`sentiment`、`url`、`canonical_url`。

筛选项（均在已载入的 `items` 内做前端过滤，不改变后端查询）：

- 来源：按 `feed_id` 过滤。
- 时间质量：`valid / missing / invalid / timezone_missing / legacy_unverified`。
- 情感：`偏正面 / 偏负面 / 中性/待判`。
- 关键词：对 `title + summary` 做本地小写包含匹配（临时筛选，不写回 `/api/keywords` 配置）。

三种状态明确区分：

- 空库：`stats.record_count === 0` → 提示“空库”，引导先添加来源并导入/刷新。
- 筛选无匹配：全表有记录但筛选结果为 0 → 提示“当前筛选无匹配”。
- 后端错误 / 离线：请求失败时按类型提示（见下）。

### 视图三：报告模板

- 只从**当前已读取的** `/api/data` 键值拼装纯文本模板，不发起写操作、不发送、不提交。
- 明确标注 `coverage`、`analysis_status`、全表分母、列表上限、未知时间数量与占比、时间质量分布（全表）、来源清单（条目数 / 最后成功 / 最近批次）、样本清单（来源 + 时间质量 + 记录ID + 规范ID + 标题）。
- 顶部与结尾双重标注：**未复核（unreviewed）**，不构成结论/事件认定/预警/准确率声明；并列出未接入能力。
- “复制”按钮使用 `navigator.clipboard`，失败时回退为选中文本框供手动复制。

### 视图四：运行状态

| 界面元素 | 接口 | 方法 | 说明 |
| --- | --- | --- | --- |
| 批次列表 | `/api/data` | GET | 使用返回的 `runs`（最多 100 条） |
| 批次详情 | `/api/runs/{id}` | GET | 批次全字段；未知 ID 404 |
| “读取运行时信息” | `/api/runtime` | GET | 本地诊断信息，仅回环，不代表身份认证 |

展示字段：`id/feed_id/kind/status/started_at/finished_at/input_count/added/duplicates/limit_n/input_sha256/error`；运行时含 `pid/started_at/entry/storage_module/database/snapshot_root/manual_limit/loaded_sha256`。

## 错误与状态处理

| 情况 | 前端表现 |
| --- | --- |
| 无法连接（fetch 抛错 / 服务未启动） | 顶部连接条显示“离线”，并说明“未显示缓存数据” |
| HTTP 4xx/5xx | 顶部显示“后端错误（HTTP <码>）”，并**原样**显示服务端 `error` 文本 |
| 401 / 403 / 409 / 422 等 | 不吞掉、不重写状态码；422 导入失败时同时显示 `run_id` 与 `status=failed` |
| 证据详情 409 | 显示“哈希不一致或文件缺失”，不做替代或降级 |

## 未接入能力（后端无对应接口，前端明确标注、不编造）

- 事件聚类 / 事件 API。
- 预警 / 通知 / 推送。
- 模型调用与用量（情感标签为本地词典粗筛）。
- 批次取消 / 中止。
- 导出、删除、用户权限、登录态。

## 未验证项与证据缺口（不得据本文件宣布通过）

- 未运行任何自动化测试、未访问网络、未启动服务进行实机点击验证。
- 页面在后端的真实渲染、`/api/import` 的 201/422/400/500 各路径、`/api/records/{id}` 的 409 路径均**未经本轮实机验证**。
- 视觉与无障碍（窄屏、键盘、对比度）仅按代码实现，未做真机/辅助技术验证。
- 需求中的“实机验收通过”“真实客户端接收”等仍未完成，状态保持 BLOCKED。

## 修改范围

本轮仅改动 `index.html`、`app.js`、`style.css`、`README.md`，并新增本文件；未改 `server.py`、`storage.py`、`schemas/`、`AGENTS.md`、认证或全局配置；未引入任何依赖；未提交。
