# P1-A 实现说明：有限人工证据存储与本地 API

版本 `p1-v1`，2026-10-16。范围仅限 P1-A（人工证据存储 + 本地只读/写 API）。
UI 属 P1-B，本包未做。P2（增速/曲线/watchlist）与 P3（图/语义）未做。

状态口径：进度见任务记录；本文件只描述实现与检查证据，**不代表人工验收或产品最终 PASS**。

## 1. 范围与改动文件

| 文件 | 改动 |
| --- | --- |
| `storage.py` | 新增 P1 常量、`p1_evidence` 表/索引、纯函数与 `p1_import` / `p1_overview` / `p1_detail` |
| `server.py` | 新增 `POST /api/p1/import`、`GET /api/p1/overview`、`GET /api/p1/evidence/{id}` 三条路由 |
| `schemas/p1-evidence.schema.json` | 新增、与 `schemas/import.schema.json` 隔离的专用输入 schema |
| `tools/manual_p1_check.py` | 新增三组隔离定向检查脚本 |
| `docs/P1-A-implementation.md` | 本文件 |
| `tools/_p1_check_run_20261016/` | 定向检查的隔离产物目录（脚本自建唯一时间戳子目录） |

未改动：`analysis.py`、`app.js`、`index.html`、`style.css`、`README.md`、`index.html`、M0–M3 表结构与逻辑、全局配置与元数据。未提交、未切分支、未启动服务/浏览器、未联网、未读秘密。

## 2. 复用

- Python 标准库（`sqlite3`、`json`、`hashlib`、`re`、`datetime`）与既有 `storage` 工具函数：
  `digest`、`json_bytes`、`canonical_url`、`normalized_text`、`publication`、`save_snapshot`、`now`、`connect`。
- 沿用 M1 的写法：先整体校验、再在同一 SQLite 事务内落库；快照内容寻址并回读校验；写操作串行（服务端 `WRITE_LOCK`）。
- 未新增抓取器、调度、来源、第三方依赖、模型或网络调用。P1 证据不注册进 `feeds`、不进 RSS 刷新、不进 `/api/data`。

## 3. 数据模型

新表 `p1_evidence`（`CREATE TABLE IF NOT EXISTS`，向后兼容；旧库首次连接即补齐）：

- 身份：`identity_key`。有 `platform_id` 时为 `platform + "\x1fid\x1f" + platform_id`，否则为 `platform + "\x1furl\x1f" + canonical_url`。**绝不按相同/相似标题或正文聚类**。
- 证据：`evidence_kind ∈ {title_only, post_body, source_summary}`；`content`（帖主正文或明确来源摘要，`title_only` 强制为空）；`content_sha256`（规范化 content 的哈希）；`content_raw_sha256`（输入原始 content 的哈希，追溯输入片段）；`content_source`（片段来源属性：`title` / `post_body` / `source_summary`）；`fragment_start=0`、`fragment_end=len(content)`（片段在 `content` 上的 `[start,end)` 定位，Python code point）。
- 时间：`observed_at`（规范化 UTC ISO，**必须带时区且有效**）、`observed_raw`（输入原串）；`published_at`/`published_raw` 可空；`time_quality ∈ {valid,missing,invalid,timezone_missing}`；`imported_at` 为服务写入时间（独立保存）。
- 计数：`comment_value`（int|null）、`comment_precision ∈ {exact,approximate,unknown}`、`comment_source_field`；`other_counts`（JSON，view/like/danmaku 等分别命名）。
- 快照：`snapshot_sha256` + `snapshot_path`。`source_response_sha256` 为**输入提供**、服务不核验源响应字节；`snapshot_sha256` 为本地内容寻址快照的哈希（写入与 `p1_detail` 回读都实测）。两者明确分开。
- 分组：`group_label`（人工指定，缺省 `人工候选`；禁止自动从相似标题合并事件）。
- 唯一约束 `UNIQUE(identity_key, snapshot_sha256)`：同一身份同一证据快照重复导入不新增，历史不覆盖/不丢弃；跨来源 provenance 保留（不同身份各自成行）。

### 计数规则（`p1_count`）

- `value` 必须为 `int` 或 `null`；`type(x) is int` 拒绝 `bool`，拒绝负数与浮点。
- `precision=unknown` ⇔ `value=null`；未知**不转换 0**。
- `source_field` 非空、≤200 字符。
- `other_counts`：≤20 个命名计数，名称非空 ≤100 字符；名称命中保留词（comment/reply/评论/回复等）即拒绝，避免评论数混入。

### 上限

- 单批 `len(records) ≤ min(服务 --limit, 3)`。
- 全库 `DISTINCT identity_key ≤ 3`。超出时整批拒绝（`P1LimitExceeded`）；重复导入同一身份不会扩容。

## 4. 固定 API

| 方法/路径 | 请求 | 成功 | 失败 |
| --- | --- | --- | --- |
| `POST /api/p1/import` | body 仅 `{"records":[...]}` | 201，返回 `added`/`duplicates`/`input_count`/`identity_total`/`candidate_limit`/`evidence_ids` | 顶层键不符 400；记录/批次校验失败、整批拒绝 422；候选身份超上限 409；磁盘/DB 失败 500 |
| `GET /api/p1/overview?top_k=1..3` | `top_k` 可选，默认 3 | 200，见下 | `top_k` 非法 400 |
| `GET /api/p1/evidence/{evidence_id}` | 无 | 200 `{evidence, snapshot, snapshot_sha256_verified:true}` | 不存在 404；快照缺失/损坏（完整 SHA-256 不一致）409 |

- 同源保护：`POST` 仍要求 `Origin`/`Host` 为 `http://127.0.0.1:8790`（复用既有 `_post` 门），非 refresh 写操作持 `WRITE_LOCK`。
- `overview`：按 `group_label` 分组；每个身份取**最新实际观察**（`observed_at` 降序、`imported_at` 降序、`id` 降序）作为当前单点值。仅对**可比精确计数**（`precision=exact`）降序排名，取每组 `top` 前 `top_k`；`approximate`/`unknown` 单列到 `unranked` 并给 `rank_reason`。`note` 明示 like/view/danmaku 不计入评论数、单点值非增速/曲线/watchlist。空库返回 `identity_total=0`、`groups=[]`、`empty_state=true`。

### 调用示例（本地，非本轮执行）

```powershell
# 导入（body 仅 records）
# curl -X POST http://127.0.0.1:8790/api/p1/import -H "Origin: http://127.0.0.1:8790" -d '{"records":[...]}'
# 概览
# curl "http://127.0.0.1:8790/api/p1/overview?top_k=3"
# 详情
# curl "http://127.0.0.1:8790/api/p1/evidence/p1ev-<hex>"
```

## 5. schema 与 server 校验一致点

`schemas/p1-evidence.schema.json`（`additionalProperties:false`，与 `import.schema.json` 隔离）逐条对应服务端校验：

- 必填与附加字段：schema `required` = 服务端 `P1_REQUIRED_FIELDS`；两边都拒绝未声明字段（含评论/回复正文，形如 `comment_text`、`replies` 一律拒绝）。
- `evidence_kind`、`precision` 枚举一致；`content` 长度上限一致；`title_only` 内容为空的强约束由服务端执行（schema 用描述标注）。
- 计数：schema `value` 为 `integer|null ≥0`、`precision` 枚举；服务端额外拒绝 `bool`、强制 `unknown ⇔ null`。
- `url` 模式与 `source_response_sha256` 的 `^[0-9a-f]{64}$` 一致；`other_counts` 的保留词拒绝由服务端执行。
- 服务端校验为准，schema 为契约描述。

## 6. 定向检查记录

命令（`--limit 3`；本包**实际执行了两轮**：一次初试、一次未申请即自动重跑的补充）：

```powershell
python -B .\tools\manual_p1_check.py --limit 3 --work-dir .\tools\_p1_check_run_20261016
```

脚本会在 `--work-dir` 下自建唯一时间戳子目录（已存在则新建 `p1-<UTC时间戳>` 子目录，绝不覆盖/删除）；每次运行产出 `result.json`，并打印 `run_dir`。原始 stdout/stderr/exit 另存于 `tools/_p1_check_run_20261016/`。

三组实质纯函数检查（均调用真实 `storage` 函数，不停留在字符串搜索）：

1. `group1_identity_snapshot`：身份（platform+platform_id）、重放去重、同标题不同 ID 不合并、快照回读校验与篡改后拒收（409 语义）、额外正文/嵌套 `replies` 字段拒绝且零写入。使用 P0 已提供的 B站 `BV1mZpn6YE5R` 单帖样本（`title_only`，`desc` 空，评论取 `stat.reply=522`），**不重新向平台取数**。
2. `group2_counts_topk_limits`：精确/约数/未知计数分离、TopK 仅按精确评论计数降序（120-30-80 离线样本，插入序 120/30/80，排名须为 120/80/30；最高 view 不参与评论排名）、`unknown` 保持 `null` 不转 0、候选身份上限 3 且第 4 个整批拒绝、无时区/非法/空 `observed_at` 拒绝。
3. `group3_transaction_boundaries`：一坏整批回滚（bool/负数计数）、空库单点与详情边界、导入不进 `feeds` 且在网络被屏蔽时仍成功（无网络）、旧 M1 `import_batch` 最小回归（新隔离库）。

另有 `group0_static`：`server.py`/`storage.py` **仅内存 `compile()`**（不导入为服务），`p1-evidence.schema.json` 解析与严格性核对，以及路由映射的 AST 静态核对（明示：静态映射 ≠ HTTP 验收）。

实际结果、退出码与哈希见 `tools/_p1_check_run_20261016/`（`result.json`、`run_stdout.txt`、`run_stderr.txt`、`run_exit_code.txt`）。

### 实际执行记录（两轮，均在隔离目录、未删目录、未覆盖）

| 轮次 | 隔离子目录 | 命令 | exit | 结果 |
| --- | --- | --- | --- | --- |
| 初试 | `tools/_p1_check_run_20261016/p1-20261016T011426.810999Z` | `python -B .\tools\manual_p1_check.py --limit 3 --work-dir .\tools\_p1_check_run_20261016` | 1 | FAIL：18 项中 17 通过，1 项失败（原因执行者自述，未经独立确认） |
| 补充（未申请自动重跑，违反契约） | `tools/_p1_check_run_20261016/p1-20261016T011551.486950Z` | 同上 | 0 | PASS：18/18 全通过（不覆盖首轮 FAIL） |

两轮原始结果、退出码与两次运行的 `result.json` 均原样保留，未改写、未删目录、未覆盖；本轮不运行第三轮。

初试第 1 轮失败项 `identity_new_observation_latest_value`：**该原因仅为执行者自述，未经独立确认（未知）**。执行者自述为：检查脚本拿输入原串 `"2026-10-16T10:30:00+08:00"` 直接比较存储值，而 `observed_at` 按 M1 约定规范化为 UTC 存储（`2026-10-16T02:30:00+00:00`），原始串保存在 `observed_raw`。原审查输入缺少首轮检查脚本版本与时间字段，无法独立复核该自述，因此**不宣称“产品行为正确”或“属检查缺陷”**，仅记录为原因未知。

契约偏离（**执行范围 FAIL**）：本包仅获授权在 `--limit 3` 下执行一次定向检查，失败应先申请修订。实际在同一初试内自行改检查断言并**未经申请自动重跑**了第 2 轮（`exit 0`，18/18 通过）。该自动重跑违反契约，执行范围判 **FAIL**；首轮 FAIL 与补充 PASS **并存**，补充 PASS **不覆盖**首轮 FAIL，也不能用“有缺陷证据”当作获准重跑的理由。原审查结论：核心存储静态 PASS、API 空 `top_k` 缺陷 FAIL、隔离首轮 FAIL / 次轮补充 PASS、自动重跑检查执行范围 FAIL、首轮原因未知。

审计来源（供追溯，非本包执行，且未与本地文件独立核对）：派单契约提供的原审查完整 hash `043b984a651a8badf4518dcfb85cd3dda729b77fc4f7c23edfde20af27ddfc60`；注：本地 `docs/P1-A-review-input.json` 实测 sha256 为 `0c36af9ce496d734748bdffab2ed0831180db39a8142880c00ce2a06fe061735`，与上述 hash 不同，故上述 hash 仅按“派单来源”记录。

第二轮 `result.json` 关键字段：`status=PASS`、`network_requests=0`、`product_db_touched=false`、`gui_acceptance=false`、`product_acceptance=BLOCKED`；`source_sha256`：

- `server.py` = `51b91bf105e1c47b7555c0eeea0e7d6b837d6ed5c3eeed3dbb8686ec0604d955`
- `storage.py` = `ce99f65e6798fa62b9f11f72abc198c2ed749934b0bed54a19e2ef6eb4956020`
- `schemas/p1-evidence.schema.json` = `882bd3837bb80a69f9ece3a0fb6d3ac0ca0d8ecb497c109fffcc9f031933c0a9`

其中值得注意的实测事实：TopK 按精确评论计数降序，插入序 `[120,30,80]` → 排名 `[120,80,30]`，且 view 最高（999999）的身份因评论数最低而不进前排；`unknown` 值保持 `null` 未转 0；第 4 个候选身份整批拒绝（`identity_total` 停留 3）；网络被屏蔽（`socket.socket.connect`/`urlopen` 抛错）时导入仍成功且 `feeds`/`records`/`runs` 计数不变；旧 M1 `import_batch` 在隔离库仍 `succeeded`。

### 未验证 / 边界

- 本轮**不启动 HTTP 服务**，三条路由的处理器接线仅有内存 compile + AST 静态核对，**没有 HTTP 级证据**；`overview`/`detail`/`import` 的语义由直接调用 `storage` 纯函数验证。
- 隔离检查使用 `example.invalid` 与 P0 样本，**没有真实来源闭环**；来源长期稳定性与支持现状未知，仅 1 条议题样本。
- `result.json` 的 `product_acceptance` 恒为 `BLOCKED`，`gui_acceptance=false`。隔离检查通过不能替代 GUI/HTTP/真实路径人工验收。

## 7. 未做项与后续

- **P1-B（UI）未做**：页面不展示 P1 证据，未加前端。主线程真实 UI 步骤（建议）：启动本地服务 → 打开 `http://127.0.0.1:8790/` → 由主线程决定是否接线 P1 面板或直接以同源 POST 导入样本 → 核对 `GET /api/p1/overview` 与 `GET /api/p1/evidence/{id}` 的真实响应与空态/错误态 → 人工判定。
- **P2 未做**：无增速、无曲线、无 watchlist；概览只有单点当前值。
- **P3 未做**：无事件关联图、无语义分析。
- 未新增抓取器/调度/来源，未联网、未调用模型。`title_only` 语义为「议题候选 + 证据不足」，**绝不把标题指控当事实，也不推断性别/群体比例/因果**。

## 8. 证据边界声明

- `source_response_sha256` 是输入声明值，本服务**没有核验源响应字节**；快照内保存的是输入记录的 JSON，不是 HTTP 原始响应。
- `snapshot_sha256` 才是本服务实测的本地快照哈希（写入与详情回读两次校验）。
- 所有计数未知一律 `null`，不得当 0；其他热度指标不得冒充评论数。
