# UI v7-A 真实错误路径验收夹具（仅隔离库与真实服务）

任务 `gender-ui-v7-a-error-fixture-20261016`，基线 commit `00d6c625b24b97871c14fef85b26f8b8570ae03b`。
本文件说明本工作包新增的**标准库夹具** `tools/serve_a_error_fixture.py` 及其用法与验收边界。

范围仅**补足 A 两条 GET 失败路径的真实 HTTP 状态与原始响应体证据**；**不扩功能**，不改生产代码，不做业务/布局/框架改动。

状态口径：本文件只描述实现与静态审查，**不代表人工验收或产品最终 PASS**，也不宣布上一轮 A 复审 FAIL 已解除。
脚本**尚未运行**；真实 HTTP / GUI 证据由主线程按真实路径执行一次 `--limit 3` 后判定。

## 1. 为什么需要它

独立只读复审（Luna）此前判定 FAIL：两处 GET 失败路径未把**真实 HTTP 状态与原始响应体**送入数据管理状态容器（`openP1Drawer()` 详情 GET、`loadP1()` 概览 GET）。
上一轮窄修复（`docs/UI-v7-A-implementation.md` 第 11 节，基线 `b0dc62c`）已把失败证据改为只进 `#p1-admin-status` 并保留 `ApiError.rawText`，但**真实 409 / 500 端到端证据尚未取得**。
本夹具用**真实 repo 服务 + 真实前端 + 隔离库**触发真实失败状态，为人工实机复验提供可核对入口。

## 2. 改动文件（allowed_paths 内，无越界）

| 文件 | 改动 |
| --- | --- |
| `tools/serve_a_error_fixture.py` | 新增标准库夹具（本工作包产物） |
| `docs/UI-v7-A-error-fixture.md` | 本说明文件 |

未改动：`server.py`、`storage.py`、`analysis.py`、`app.js`、`index.html`、`style.css`、`schemas/`、规则、元数据、真实数据库、桥。
未提交、未切分支；未启动服务/浏览器、未联网、未调用模型、未读凭据、未读真实 `sentiment.db`、未删除任何文件。

## 3. 夹具做什么（真实路径，不伪造）

`tools/serve_a_error_fixture.py` 只使用 Python 标准库：

1. 必填 `--evidence-dir` 且**必须是不存在的新目录**，否则拒绝（`exist_ok=False`，无覆盖）。
2. 在新目录内建隔离 `root/`（静态资源 + `snapshots/`）与隔离 `fixture.db`（SQLite）。
3. 从当前 repo **复制** `index.html` / `app.js` / `style.css` 的**原始字节**到隔离 root，并记录源与副本 sha256，二者必须一致。
4. 从当前 repo **真实 import** `storage` / `analysis` / `server`（`server` 只定义，不启动 `__main__`）。
5. 运行时只改内存中的模块全局：`server.ROOT=隔离 root`、`server.DB=隔离库`、`server.HOST=127.0.0.1`、`server.PORT=--port`、`server.LIMIT=--limit`。**不修改任何仓库文件**。
6. 用 `storage.connect` + `analysis.init` 建全 schema，用 `storage.p1_import` 真实写入 **1 条**隔离候选（产生快照，**保留不删**）。
7. 按 `--mode` 只做**一处**隔离库变异（见第 4 节），随后启动真实 `ThreadingHTTPServer` + 真实 `Handler` 子类。

夹具内的隔离样本明确标名 **“隔离验收样本，不是真实来源”**：

- `platform=fixture.invalid`、`url=https://example.invalid/a-error-fixture`；
- `evidence_kind=title_only` 且 `content=""`；
- 评论 `{value:1, precision:"exact", source_field:"fixtureCount"}`；
- `observed_at` 取**本次运行的真实带时区时间**（`datetime.now(timezone.utc)`），不补造历史；
- `source_response_sha256` = 对**真实 fixture 元数据字节**（JSON，含 `synthetic:true` 与标记）的 SHA-256，并在产物中标注为**合成**。

## 4. 两种模式与预期真实错误

| 模式 | 隔离库内的唯一变异 | 预期真实结果 |
| --- | --- | --- |
| `detail` | 把唯一一行 `p1_evidence.snapshot_path` 指向**从未创建过**的 `snapshots/missing-fixture-<hash>.json`（旧快照保留在盘上） | `GET /api/p1/overview?top_k=3` 仍真实 **200**；`GET /api/p1/evidence/<id>` 走真实 `storage.p1_detail`，`read_bytes()` 抛 `OSError`（文件不存在）→ 真实 **409**（`server.py:218-225`） |
| `overview` | 只执行一次 `ALTER TABLE p1_evidence RENAME COLUMN imported_at TO fixture_missing_imported_at`（不动其他表/索引） | `storage.connect` 幂等建表仍成功、`GET /api/data` 仍真实 **200**；`GET /api/p1/overview?top_k=3` 的 `ORDER BY ... imported_at ...` 触发真实 `sqlite3.OperationalError` → 真实 **500** |

- 两条失败都走 `server.Handler.do_GET` 的真实异常捕获（`ValueError→400`、`OSError/sqlite3.Error→500`、`p1_detail` 分支 `409`），**不替换 API 函数、不 route mock、不伪造错误**。
- 前端为 repo 真实字节；`app.js` 载入时会真实 `GET /api/p1/overview` 并对已载候选真实 `GET /api/p1/evidence/<id>`，失败时按既有逻辑写入 `#p1-admin-status`（本轮未改动该逻辑）。

## 5. 允许的两处运行时覆盖（仅此两处）

1. `do_POST` 一律返回 **405**：禁用写入口与「刷新来源」，避免触发联网与写库。仅保留真实 GET 读取路径。
2. `send` 以与真实 `send` **完全相同的序列化**（`json.dumps(value, ensure_ascii=False).encode()` 或原 `bytes`）tee 实际 `method/path/status/content_type/body` 到本次**账本**。

除上述外，`Handler._get`、`do_GET`、所有业务函数与前端均**未改动**。

## 6. 参数与用法

```
python tools/serve_a_error_fixture.py \
  --limit 3 \
  --evidence-dir <必须不存在的新目录> \
  --mode detail|overview \
  --port 8791|8792 \
  [--seconds 1..180，默认 180]
```

- `--limit` 必填，**只接受 1..3**，首次使用 **3**。
- `--evidence-dir` 必填，**必须是不存在的新目录**，否则拒绝。
- `--mode` 必填，`detail` 或 `overview`。
- `--port` 必填，**只允许 8791 或 8792**（真实产品 8790 不变）。
- 服务**单次最多 180 秒**后由标准库 `threading.Timer` 自动 `shutdown`；无父子浏览器、无其他进程控制。
- 退出后不删除任何文件。

## 7. 产物（全部落在 `--evidence-dir` 内）

| 文件 | 内容 |
| --- | --- |
| `fixture.json` | ready 信息：URL、PID、真实读取入口、源码 sha256（`server/storage/analysis` 与三份前端源/副本）、隔离库路径、fixture 标记、期限 deadline、变异记录、预期状态 |
| `http-ledger.jsonl` | 逐条真实响应的 `method/path/status/content_type/body_sha256/body_length/body`（原始响应体） |
| `errors.json` | 非 2xx 真实响应（含预期 409/500 的原始 body）与预期状态 |
| `summary.json` | 停止时间、响应/错误计数、观察到的状态码集合、`expected_status_observed`、`files_kept` |
| `root/`、`fixture.db` | 隔离静态资源与隔离库（含 `snapshots/` 与 `-wal/-shm`），**保留不删** |

`readyJSON`（stdout 一行）含：URL / PID / 源码 hash / 数据库路径 / fixture 标记 / 期限。

## 8. 未执行的验证（如实列出）

- **本工作包未运行该脚本、未启动服务/浏览器、未发 HTTP、未跑测试、未做 `node --check` 或 Python 编译检查。**
- 真实 409 / 500 端到端状态、`#p1-admin-status` 的真实显示、`ApiError.rawText` 原文留证、`.skip` 背景隔离在真实抽屉中的表现，**全部待主线程实机复验**。
- 静态审查通过（若由 Luna 判定 PASS）**不等于**产品 PASS，也不替代人工实机验收。

## 9. 主线程执行与验收建议（本包不执行）

1. 选一个**不存在**的新目录，例如 `<outputs>/a-error-fixture-detail-<ts>`。
2. `detail` 模式执行一次：
   `python tools/serve_a_error_fixture.py --limit 3 --evidence-dir <新目录> --mode detail --port 8791`
   打开 `http://127.0.0.1:8791/`：确认概览真实 200，且详情（页面自动 enrich 或点「查看详情」）真实 **409**，数据管理区显示真实 HTTP 状态与原始响应体。
3. `overview` 模式另取新目录、另用 8792：
   `python tools/serve_a_error_fixture.py --limit 3 --evidence-dir <另一新目录> --mode overview --port 8792`
   打开 `http://127.0.0.1:8792/`：确认概览真实 **500**，数据管理区保留真实错误与原始响应体，旧材料被清空（不显示缓存）。
4. 核对 `fixture.json` 中的源码 hash 与 repo 当前文件一致；核对 `http-ledger.jsonl` / `errors.json` 里的 `status`、`path`、`body` 为真实响应。
5. 服务最多 180 秒自动停止；停止后核对 `summary.json.expected_status_observed` 与 `files_kept`，确认无文件被删、未接触真实 `sentiment.db`。
6. 以上为真实路径人工验收，由主线程执行与判定；本包不代替、不宣称产品 PASS。

## 10. 边界声明

- 本文件所述 hash/路径均为夹具将要输出/复制的口径；**本包未运行，故无本次实测运行值**。
- 历史结论保留：上一轮 A 复审 **FAIL** 未被本包解除；初始执行 FAIL/exit 1、390px 实机 FAIL、旧 P1-A 执行范围 FAIL、桥完整验收 BLOCKED 均未改变；P2/P3 为 NOT_STARTED。
- 本轮仅新增两份允许文件，**生产代码零改动**；隔离通过不能替代真实库/人工路径验收。
