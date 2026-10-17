# S4 事件聚类（规则优先 + 模型只在边界裁量）

实现：`tools/cluster_events.py`。状态：**离线路径（规则优先连通分量、`--dry-run`、`--no-model` 真实运行、`--similarity`、参数边界、只读打开、产物落盘与哈希回读）已在本机实跑验证通过（0 次真实模型调用）；边界对的模型代码路径（批量、节流、token 汇总、HTTP 失败保留与保守降级）已用本地夹具（`OPENCODE_GO_V1_FIXTURE=1`，**非真实模型**）实跑验证；`--limit` 的真实模型裁量结果与真实 token 用量本轮 BLOCKED（执行器进程没有 `OPENCODE_GO_V1_API_KEY`，未发送任何真实请求）**。详见第七节。

本文记录：复用清单、参数、聚类与相似度规则、模型裁量契约、产物格式、真实运行证据、失败路径、未实证项与边界。

工作区（attempt worktree）：`...\attempts\attempt-98e3e63cb93f41309d27863f66123980\workspace`，基线 commit `7335e89e2e4273deeda3ab13accfcad2ca8c1599`。

## 一、复用清单（先复用，后新写）

| 复用对象 | 真实路径 | 复用方式 | 本文件是否新写 |
| --- | --- | --- | --- |
| 只读打开 | `tools/annotate_relevance.py`（本任务 anchor） | 按 importlib 载入其模块，直接调用 `open_readonly()`：无未检查点数据时 `mode=ro&immutable=1`，存在非空 `-wal` 时退回 `mode=ro`；`mode=ro` / `immutable=1` 的取值**来自它**，不另写一套 | 否 |
| 失败类型 | 同上 | 直接复用 `AnnotationError`（`stage` / `exit_code` / `extra`），错误报告口径与 S3 一致 | 否 |
| 小工具 | 同上 | 复用 `utc_stamp()`（UTC 时间戳）、`clip()`（送模型输入截断）、`_utf8_stdout()`（Windows 控制台默认 GBK）、`usage_totals()`（usage 缺失保留 `null`） | 否 |
| 证据库约定 | `storage.py` | `now()`（UTC）、`digest()`（SHA-256）、`json_bytes()`（jsonl 行按项目既有规范序列化：键排序、紧凑、`ensure_ascii=False`） | 否 |
| 只读取数 | `storage.py` 的 `records`/`feeds` 表约定 | 只读 URI 连接 + `SELECT`（见第二节） | 是（仅 SQL 与字段映射） |
| 生产适配器 HTTP 细节 | `analysis.py`（已并入并独立审查） | **直接 import 其常量**：`PRODUCTION_ENDPOINT`、`PRODUCTION_MODEL`（**裸 id**）、`PRODUCTION_MODEL_ENV`、`PRODUCTION_KEY_ENV`、`PRODUCTION_USER_AGENT`、`PRODUCTION_SESSION`、`PRODUCTION_TIMEOUT`、`PRODUCTION_MAX_RESPONSE`、`PRODUCTION_ERROR_BODY`、`ProductionHTTPError`；请求体里 `"reasoning_effort": "none"` | 否，一个常量都不复制 |
| 工具做法 | `tools/annotate_relevance.py`、`tools/collect_opencli.py` | `--dry-run`、失败保留真实原文不静默降级、统一 JSON 报告与退出码、产物哈希回读 | 是（同一做法的本地实现） |

**必须新写的部分及理由**：

1. **聚类算法**：全仓没有标题相似度 / 连通分量的任何实现，规则阶段与边界带判定必须新写（第三节）。
2. **边界对判同提示词**：`analysis.production_messages(role, ...)` 把系统提示词硬绑在 A/B 证据角色上（A 只输出 `claims`、B 只输出 `reviews`），`analysis.py` **不在本任务允许改动的路径内**，无法把提示词参数化。故 `post_once()` 只重写「消息内容」，其**发送与解析逐行对齐 `analysis.production_adapter()` 的既有行为**（请求体字段、请求头、`build_opener` 不加重定向、超时、响应上限、`choices[0].message.content` 解析、单 JSON 对象要求），并显式保留 `analysis` 的 `OPENCODE_GO_V1_FIXTURE=1` 夹具分支：有凭据就带 `Authorization`；无凭据但显式设了夹具模式时不带（供主线程离线回放整条链路）。`tools/annotate_relevance.py:post_once()` 形状相同但**始终要求凭据**，故未复用它。
3. 参数解析、连通分量汇总、产物落盘与哈希回读。

未引入任何第三方依赖（仅 Python 标准库 + 本仓 `analysis.py`/`storage.py`）；未新增登录、未读取或复制任何 cookie/凭据文件；未使用 MediaCrawler；未碰任何平台。

## 二、命令与参数

```
python -B tools/cluster_events.py [--limit N] [--threshold F] [--similarity dice|jaccard]
                                  [--max-model-pairs N] [--model-batch N] [--no-model]
                                  [--min-interval SECONDS] [--dry-run] [--db PATH] [--out-dir DIR]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--limit` | `200` | 取最近 N 条 `records`（1–200）。排序 `fetched_at DESC`，同一时刻用 `id DESC` 兜底以保证确定性 |
| `--threshold` | `0.6` | 标题相似度阈值（∈`(0,1]`）。`>= 阈值` 直接判同事件（规则阶段） |
| `--similarity` | `dice` | 相似度口径：字符级 Dice（默认）或 token Jaccard |
| `--max-model-pairs` | `20` | 最多送模型的边界对数（0–50）。`0` 等于纯规则且**不需要凭据** |
| `--model-batch` | `10` | 单批模型对数，**不得超过 10**（契约硬约束，超限直接报错） |
| `--no-model` | 关 | 完全跳过模型，纯规则聚类（边界对一律按「不同事件」保守处理） |
| `--min-interval` | `8` | 模型批次之间的最小间隔秒数（首批发完才计） |
| `--dry-run` | 关 | 只打印将聚类多少条、会触发多少次模型调用；**0 次网络请求、不写输出文件** |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径（**只读**打开） |
| `--out-dir` | `<仓库>/events` | 产物目录（运行期产物，**不在** `allowed_paths` 内；本轮验证写入 Temp 以保持 worktree 干净） |

退出码：`0` 成功；`2` 参数/环境/数据库问题（含缺凭据）；`3` HTTP/连接失败；`4` 响应或模型输出解析失败；`7` 产物写入失败。

## 三、规则优先的聚类规则（零模型调用）

1. **canonical_url 完全相同** 的记录，无条件并入同一事件（不看标题）。
2. 否则计算**标题相似度**，`>= --threshold` 的对并入同一事件。
3. 以上的「并」是**连通分量**（并查集，路径压缩；以较小下标为根，保证结果确定性）。
4. 相似度口径（对标题先归一化：小写、去空白与标点，保留 CJK 与字母数字）：
   - `dice`（默认）：**字符级 Dice**，字符二元组集合 `2·|A∩B|/(|A|+|B|)`；长度 <2 时退化为单字符集。
   - `jaccard`：**token Jaccard**，`|A∩B|/|A∪B|`；分词口径为「字母数字连续串各算一个 token，每个 CJK 字算一个 token」。
5. 相似度落在 `[threshold-0.15, threshold)`（`BOUNDARY_WIDTH = 0.15`）的对是**边界对**，**只有它们**可能交给模型；`< threshold-0.15` 的一律按规则判为「不同事件」。
6. 已被规则连通（同一分量）的边界对**不再**送模型（省预算）。

## 四、模型裁量（只处理边界对）

- **只对边界对调用模型**；`--no-model` 完全跳过；`--max-model-pairs` 控制送模型的上限（默认 20，上限 50）。超出上限的边界对一律按「不同事件」保守处理，并在报告/ summary 里记为 `boundary_skipped_over_cap`。
- **提示词契约**：系统提示词要求模型**只输出一个 JSON 对象**，且仅含一个键 `pairs`，与输入一一对应，每条严格为三个字段：

```json
{"pairs":[{"pair_id":"p0001","same_event":true,"reason":"同一事件"}]}
```

| 字段 | 契约 |
| --- | --- |
| `pair_id` | 必须原样回填输入的 `pair_id`，不得新增、遗漏或重复 |
| `same_event` | 布尔值，表示两个标题是否指同一事件；**不确定时取 `false`** |
| `reason` | 不超过 30 字的中文短语（本工具**不**使用该字段做任何判断） |

送入模型的用户消息为 `{"pairs":[{"pair_id","a":{"feed","title"},"b":{"feed","title"}}]}`；`title` 超过 80 字时截断并加 `…` 标记（**只影响送入模型的输入，不改数据库原文**）。

- **保守解析**（`normalize_judgements` / `normalize_same`）：`same_event` 只接受布尔真值，字符串 `"true"/"1"/"yes"/"same"/"是"` 视为 True，**其余一律 False**；`pair_id` 越批则忽略、重复取首次、缺失判 False。即「不确定就归为**不同事件**」。
- **结构损坏**（模型输出缺 `pairs` 数组）：**整批**按「不同事件」处理，并在 `model.errors` 里留痕（不静默）。
- **批量与节流**：一次最多 10 对；批次之间按 `--min-interval`（默认 8 秒）间隔，首批发完才计；总调用次数与 token 用量写进报告与 summary。

## 五、产物（不写数据库）

成功一次运行写两个文件（默认落在 `<仓库>/events/`）：

1. `events/events-<UTC时间戳>.jsonl`：每条事件一行，字段 `event_id / size / feeds / titles / urls / first_seen / method`（按 `storage.json_bytes` 规范序列化）。
   - `event_id`：`evt-<sha256(sorted(成员 record_id))[:16]>`，同输入必同值。
   - `first_seen`：成员中最早的 `fetched_at`（UTC ISO）。
   - `urls`：成员记录的**原始 `url`**（热门原帖即证据入口）；`titles` 与 `urls` 同序（按 `fetched_at, id`）。
   - `method`：`rule`＝该事件只用规则边构成；`model`＝有模型判同边把**两个及以上规则分量**并了进来。
   - 事件按 `(size DESC, event_id)` 排序，确定性。
2. `events/summary-<UTC时间戳>.json`：事件数、单事件最大规模、单例事件数、规模分布、**各来源分布**（每源记录数与涉及事件数）、规则/模型各判了多少对（`pairs` 与 `model` 两个块）、逐批 usage 与 token 合计、`outputs`（两个产物的路径、字节数、SHA-256）。

写完后**回读哈希校验**：jsonl 的字节 SHA-256 必须与报告里的一致，否则报错（不把「写过了」当成「写对了」）。

`--dry-run` 与失败路径都**不写产物**。

## 六、节流与成本

- 规则阶段（相似度 + 连通分量）**零模型调用**。
- 模型阶段每批**最多 10 对**、每批**最多一个真实 HTTP 请求**，不重试、不并发、不翻页；批次之间按 `--min-interval` 节流。
- 单次真实运行最多发出 `ceil(selected_pairs / model_batch)` 次请求；总次数与 token 用量全部写进输出。
- 无定时任务、无后台轮询，只由人工显式执行。

## 七、真实验证（本轮在本机实跑）

### 7.1 环境与文件指纹

- 被测文件 `tools/cluster_events.py`：**SHA-256 `2ba5900204aee77e18fbab22f7f726cb08b536ae22caf06365bcde60bedb8e83`，34312 字节**（下方 7.2–7.10 的全部运行均在此字节版本上产生）
- Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`（3.13.14），全部以 `python -B` 运行（不落 `.pyc`）
- 只读输入：把仓库 `sentiment.db` 复制到 `C:\Users\Lenovo\AppData\Local\Temp\opencode\s4-readonly\sentiment.db`（512000 字节，SHA-256 `401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51`），**对复制件也只读打开**；真实库 `records` **148 条**、`feeds` 8 个
- 产物目录用 `--out-dir` 指向 Temp（`s4-out*`），**未在 worktree 内生成 `events/`**
- 运行后核对：worktree 内 `git status --short` 只有 `?? tools/cluster_events.py`；`events/` 不存在、`tools/__pycache__/` 不存在、`sentiment.db` 不存在

### 7.2 `--dry-run`（真实 stdout 摘要，退出码 `0`，`network_requests: 0`）

```json
{
  "tool": "s4-cluster", "mode": "dry-run",
  "db": { "path": "...\\s4-readonly\\sentiment.db", "present": true,
          "access": "mode=ro&immutable=1", "touched": false },
  "limit": 200, "threshold": 0.6, "similarity": "dice", "boundary_width": 0.15,
  "record_count": 148, "event_count_if_rule_only": 117,
  "pairs": { "total": 10878, "rule_same": 34, "rule_same_by_canonical_url": 13,
             "rule_same_by_similarity": 21, "rule_different": 10841, "boundary_total": 3 },
  "model": { "enabled": true, "max_model_pairs": 20, "model_batch": 10,
             "min_interval_seconds": 8.0, "boundary_selected": 3,
             "boundary_skipped_over_cap": 0, "boundary_skipped_no_model": 0,
             "model_call_count": 1 },
  "network_requests": 0,
  "would_write": ["events/events-<UTC时间戳>.jsonl", "events/summary-<UTC时间戳>.json"]
}
```

即：将聚类 **148** 条、纯规则得到 **117** 个事件、只触发 **1** 次模型调用（3 个边界对，单批），**0 次网络请求**。

### 7.3 `--no-model`（真实运行，纯规则，0 次模型调用，退出码 `0`）

```json
{
  "status": "ok", "degraded": false, "event_count": 117, "max_event_size": 4,
  "model": { "enabled": false, "boundary_selected": 0, "boundary_skipped_no_model": 3,
             "model_call_count": 0, "pairs_same": 0, "pairs_different": 0,
             "errors": [], "tokens": null },
  "feed_distribution": {
    "toutiao-hot": {"records": 40, "events": 24}, "zhihu-hot": {"records": 20, "events": 20},
    "weibo-hot": {"records": 40, "events": 27}, "36kr-hot": {"records": 17, "events": 17},
    "tieba-hot": {"records": 20, "events": 20}, "toutiao-recommend": {"records": 8, "events": 8},
    "验收样本来源-20261015": {"records": 3, "events": 2}
  },
  "outputs": {
    "jsonl": "...\\s4-out\\events-20261017T053341Z.jsonl", "jsonl_bytes": 71315,
    "jsonl_sha256": "4d0b6b16d24509d9f76e93f7995a01d74a6facb46f09396954aa3f88537069f3",
    "summary": "...\\s4-out\\summary-20261017T053341Z.json",
    "summary_sha256": "fdc2d85b362c138f72259770d1665d562b6367e80b5fd34650c3dd7f94d66ab1"
  }
}
```

summary 侧：`event_count = 117`、`max_event_size = 4`、`singleton_events = 88`、`event_size_distribution = {"1":88,"2":28,"4":1}`（88 + 2×28 + 4×1 = 148 条记录，与 `record_count` 自洽）。

事件 JSONL 首 2 行（真实落盘字节，`outs.jsonl_sha256` 与文件字节回读一致）：

```json
{"event_id":"evt-717c8f769afc6936","feeds":["toutiao-hot","weibo-hot"],"first_seen":"2026-10-16T15:02:34.438190+00:00","method":"rule","size":4,"titles":["徐艺洋：孩子是自己生的 已经2岁多","徐艺洋 孩子是我自己生的","徐艺洋 孩子是我自己生的","徐艺洋：孩子是自己生的 已经2岁多"],"urls":["https://www.toutiao.com/trending/7697216262065098283/?...","https://s.weibo.com/weibo?q=%23...%23","https://s.weibo.com/weibo?q=%23...%23","https://www.toutiao.com/trending/7697216262065098283/?..."]}
{"event_id":"evt-08747290dd599dba","feeds":["toutiao-hot"],"first_seen":"2026-10-16T15:02:34.475547+00:00","method":"rule","size":2,"titles":["张镇麟16分6板正负值+28全场最高","张镇麟16分6板正负值+28全场最高"],"urls":["https://www.toutiao.com/trending/7697149062910558250/?...","https://www.toutiao.com/trending/7697149062910558250/?..."]}
```

（首个事件是**跨源**事件：toutiao-hot 与 weibo-hot 的「徐艺洋 孩子是自己生的」并成一个 4 成员事件——这正是事件聚类要解决的问题。为可读性，上面两行的 `urls` 已用 `...` 省略中段；落盘字节是完整 URL。）

**规则阶段发现的 3 个边界对**（默认口径，供主线程判断模型必要性）：

| pair_id | 相似度 | 两侧（feed / title） |
| --- | --- | --- |
| p0001 | 0.571429 | `验收样本来源-20261015`「验收样本：独立内容」 vs 同源「验收样本：共同内容」 |
| p0002 | 0.571429 | `验收样本来源-20261015`「验收样本：独立内容」 vs 同源「验收样本：共同内容」 |
| p0003 | 0.451613 | `36kr-hot`「刚刚，Mac Studio 迎来小扎最强开源模型，苹果等来了最意外的助攻」 vs 同源「刚刚，Meta亮出美国最强开源权重模型，两台Mac Studio就能跑」 |

### 7.4 边界对模型代码路径：本地夹具实跑（**非真实模型**）

用本机回环夹具服务器（`C:\Users\Lenovo\AppData\Local\Temp\opencode\s4-fixture-server.py`，固定：请求里第 1 对判同、其余判不同；带 usage），以 `OPENCODE_GO_V1_FIXTURE=1`、`OPENCODE_GO_V1_ENDPOINT=http://127.0.0.1:8099/v1/chat/completions`（**未使用任何真实凭据**）实跑：

```json
{
  "status": "ok", "degraded": false, "event_count": 116, "max_event_size": 4,
  "model": { "enabled": true, "boundary_selected": 3, "model_call_count": 1,
             "pairs_same": 1, "pairs_different": 2, "errors": [],
             "tokens": {"prompt_tokens": 103, "completion_tokens": 20, "total_tokens": 123} },
  "outputs": { "jsonl": "...\\s4-out-fixture\\events-20261017T053341Z.jsonl", "jsonl_bytes": 71147,
               "jsonl_sha256": "0821e0b0fad0b127fc3f75e705ceb402db65ad358dfd97a446bc2c8f43ce7877",
               "summary_sha256": "f883a096cb98b5b891c42e95df5f73e2245553e2a27ab6c0c23e2b6f584f78d6" }
}
```

- 夹具判同的那个边界对使 `验收样本来源-20261015` 的 3 条记录并成一个事件，其 `method = "model"`（`evt-b128e4ce469ac909`，`size=3`）——**证明「边界对会调用模型、且模型判同会真正改变连通分量与 `method`」**。
- `summary.model.batches[0].usage` 与 `per_batch`/`totals` 一致；`endpoint` 记录为夹具地址。
- **这是桩数据，只证明模型调用代码路径、批量、token 汇总与 `method` 归属可用，不能当作真实模型判同质量的证据。**

多批与节流（`--model-batch 1 --min-interval 1`，同一夹具）：`model_call_count = 3`、3 个批次、`total_tokens = 363`、`event_count = 115`，整进程墙钟 **2.3 秒**（含 2 次 1 秒间隔等待），说明**分批与 `--min-interval` 真实生效**。

### 7.5 HTTP 失败保留与保守降级：错误夹具实跑

用固定回 **500** 的夹具（同一路径，无凭据）实跑：

```json
{
  "status": "ok", "degraded": true, "event_count": 117,
  "model": { "pairs_same": 0, "pairs_different": 3, "tokens": null,
             "errors": [ { "batch": 1, "stage": "http", "status": 500,
                           "body_prefix": "{\"error\":{\"type\":\"server_error\",\"message\":\"fixture forced failure 500 for S4 boundary pairs\"}}",
                           "pair_ids": ["p0001","p0002","p0003"] } ] },
  "outputs": { "jsonl": "...\\s4-out-500\\events-20261017T053343Z.jsonl",
               "jsonl_sha256": "4d0b6b16d24509d9f76e93f7995a01d74a6facb46f09396954aa3f88537069f3",
               "summary_sha256": "6999573c8ec68114e35976c1af22399ccb1ad26f2f938d99524b65177d1e3948" }
}
```

- **真实状态码 `500` 与响应体前 200 字被完整保留**；单次请求、**无重试**（错误夹具整轮只被 POST 1 次）。
- 该批 3 个边界对**全部按「不同事件」保守处理**：`event_count` 回到 117、jsonl SHA-256 与 7.3 纯规则结果**逐字节相同**（4d0b…），即降级没有污染规则结果。
- `degraded: true` 显式标出本次运行发生过模型失败，未把失败伪装成成功。
- HTTP 失败时**没有**服务端 `usage`，故 `tokens` 保持 `null`（不冒充 0）。

### 7.5b HTTP 200 但模型输出结构损坏：保守降级 + 保留真实 token

用「信封合法（`choices[0].message.content` 为 `{"items":[]}`，缺 `pairs` 数组）、`usage` 为 `{111,7,118}`」的夹具实跑：

```json
{
  "status": "ok", "degraded": true, "event_count": 117,
  "model": { "pairs_same": 0, "pairs_different": 3,
             "tokens": { "prompt_tokens": 111, "completion_tokens": 7, "total_tokens": 118 },
             "errors": [ { "batch": 1, "stage": "parse",
                           "message": "模型输出缺少 pairs 数组，整批按不同事件处理",
                           "pair_ids": ["p0001","p0002","p0003"] } ] },
  "outputs": { "jsonl_sha256": "4d0b6b16d24509d9f76e93f7995a01d74a6facb46f09396954aa3f88537069f3",
               "summary_sha256": "c4eeeac2b741a4036c06ab49957187ac7ec0a240928f599803653fae3efd103c" }
}
```

- 结构损坏**不抛错**：整批判「不同事件」（`event_count` 117、jsonl 与纯规则逐字节相同），并在 `model.errors` 以 `stage:"parse"` 留痕。
- 该次调用**确实发生了**（HTTP 200），故真实 `usage` 被保留进 token 汇总（`total_tokens = 118`），不因模型不守格式而丢账。

### 7.6 缺凭据（真实模型阶段无法进入）：**BLOCKED**

在没有 `OPENCODE_GO_V1_API_KEY`、也没有夹具的情况下实跑 `--max-model-pairs 3`，真实 stdout（退出码 `2`）：

```json
{
  "tool": "s4-cluster", "status": "blocked",
  "error": { "stage": "env", "exit_code": 2, "type": "AnnotationError",
             "message": "缺少环境变量 OPENCODE_GO_V1_API_KEY；未发送任何请求（纯规则请加 --no-model；离线夹具回放请设 OPENCODE_GO_V1_FIXTURE=1）",
             "credential_env": "OPENCODE_GO_V1_API_KEY", "boundary_selected": 3 }
}
```

`os.environ.get("OPENCODE_GO_V1_API_KEY") is None`。因此：**未发出任何真实 HTTP 请求**，也没有任何产物（`--out-dir s4-out-nocred` 未生成）。按项目规则**不读取 token/密码/cookie/认证 DB**，故未去别处寻找凭据。

**解除该 BLOCKED 所需的证据/条件**：在执行器进程（或其子进程环境）中提供 `OPENCODE_GO_V1_API_KEY`（可选 `OPENCODE_GO_V1_MODEL` 换模型），然后重跑，即可补上真实 `pairs_same/pairs_different`、真实 token 用量与真实 `method=model` 事件。

### 7.7 参数边界与失败路径（不产生上游请求）

| 场景 | 命令 | 观察结果（退出码） |
| --- | --- | --- |
| `--max-model-pairs` 越界 | `--dry-run --max-model-pairs 51` | `status:"blocked"`、`stage:"args"`、`"message":"--max-model-pairs 必须在 0-50 之间，收到 51"`（`2`） |
| `--model-batch` 越界 | `--dry-run --model-batch 11` | `stage:"args"`、`"message":"--model-batch 必须在 1-10 之间，收到 11"`（`2`） |
| `--threshold` 越界 | `--dry-run --threshold 1.5` | `stage:"args"`、`"message":"--threshold 必须在 (0,1] 之间，收到 1.5"`（`2`） |
| `--limit` 越界 | `--dry-run --limit 0` | `stage:"args"`、`"message":"--limit 必须在 1-200 之间，收到 0"`（`2`） |
| 缺库 | `--dry-run --db <不存在路径>` | `stage:"db"`、`"证据库不存在：..."`（`2`） |
| `--max-model-pairs 0`（无凭据、无夹具，真实运行） | `--max-model-pairs 0` | `status:"ok"`、`degraded:false`、`event_count:117`、`boundary_skipped_no_model:3`、`model_call_count:0`；jsonl SHA-256 同为 4d0b…（`0`）——**不需要凭据，结果与 `--no-model` 一致** |

### 7.8 `--similarity jaccard`（真实运行，dry-run）

`--dry-run --similarity jaccard`：`record_count = 148`、`event_count_if_rule_only = 115`、`rule_same = 40`、`boundary_total = 0`、`model_call_count = 0`（退出码 `0`）。两种口径结果不同（Dice 117 事件 / 3 边界对，Jaccard 115 事件 / 0 边界对），说明 `--similarity` 真实生效。

### 7.9 只读性实测

- 真实库与复制件 SHA-256 **都是 `401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51`**（512000 字节），运行前后**逐字节未变**。
- 复制件所在目录**只有 `sentiment.db`**，未多出 `-shm`/`-wal` 旁文件（本轮库无 `-wal`，走 `mode=ro&immutable=1`）。
- `git status --short` 无 `sentiment.db`、无 `events/`、无 `__pycache__`。

### 7.10 定向检查

| 检查 | 命令 | 观察结果 |
| --- | --- | --- |
| 语法（内存 compile，未落盘 pyc） | `python -B -c "compile(open('tools/cluster_events.py',encoding='utf-8').read(),'tools/cluster_events.py','exec')"` | `memory compile OK`，退出码 0 |
| 参数风格 | `--dry-run` 全部输出为统一 JSON 报告 | 与 `tools/annotate_relevance.py` 一致（同 `tool/status/mode/...` 结构） |

## 八、失败路径

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| `--limit` / `--threshold` / `--max-model-pairs` / `--model-batch` / `--min-interval` 越界 | 2 | `stage:"args"`，不读库、不发请求 |
| 证据库不存在 / 只读打开失败 / `SELECT` 失败 | 2 | `stage:"db"`，附库路径与原始异常 |
| 需要模型但缺 `OPENCODE_GO_V1_API_KEY`（且无夹具） | 2 | `stage:"env"`，**未发送任何请求、不写产物**（前置条件失败，不降级成空结果） |
| 某批 HTTP 非 200（含 400/401/403/429/5xx） | 0（`degraded:true`） | 复用 `analysis.ProductionHTTPError`：**真实状态码 + 响应体前 200 字**；单次请求、不重试；**该批边界对一律判「不同事件」**并在 `model.errors` 留痕 |
| 某批连接失败（DNS/TLS/超时） | 0（`degraded:true`） | `stage:"network"`，同一保守降级 |
| 响应非 JSON / 缺 `choices`/`content` / 模型输出非 JSON 对象 / 缺 `pairs` 数组 | 0（`degraded:true`） | 附响应或模型文本前 200 字；**整批判「不同事件」**；HTTP 200 时该次真实 `usage` **保留**（见 7.5b），HTTP 失败时 `usage` 为 `null` |
| 单个 `pair_id` 越批/重复/缺失、`same_event` 非布尔 | — | 逐条保守：越批忽略、重复取首次、缺失判 `false`，不放大成整批失败 |
| 产物写入或回读哈希不一致 | 7 | `stage:"write"`，附路径 |

「运行期模型失败」一律**降级并留痕**（保守判不同、`degraded:true`），不重试、不静默；「前置条件缺失（无凭据）」则**快速失败**、不产生半成品。

## 九、未实证项（本轮未做，不得当成已完成）

1. **真实模型判同结果未取得**（7.6，缺凭据）。未验证：真实 HTTP 200、真实 `usage`、模型对边界对的**判同质量**、真实 `method=model` 事件、真实 token 用量。7.4 是**夹具桩**，不能替代。
2. **真实模型阶段未在「多批 + `--min-interval` 默认 8 秒」下跑过**：多批节流用夹具以 `--model-batch 1 --min-interval 1` 验证（7.4），默认 8 秒间隔与真实端点的组合未跑。
3. **`--max-model-pairs` 超上限截断未用真实数据触发**：本轮库只有 3 个边界对（默认上限 20），`boundary_skipped_over_cap` 一直是 `0`；该分支只有静态依据（`select_pairs` 切片）。
4. **`--similarity jaccard` 的判同质量未评估**：只跑了 7.8 的计数。
5. **边界带宽度固定为 0.15**（契约给定），未做成参数；`--threshold` 与 0.15 的交互未做敏感性分析。
6. **相似度阈值/口径未与需求方确认**：`0.6` 与字符二元组 Dice 是契约默认值；是否应换语义相似度、是否应按标题 + 摘要，未确认。
7. **`--out-dir` 为本次新增的参数**：默认仍是契约要求的 `<仓库>/events/`，但该目录**不在 `allowed_paths`**，故本轮真实运行全部写 Temp；`events/` 是否加入 `.gitignore`（`.gitignore` 不在允许改动范围）待产品确认。
8. **`--limit ≤ 200` 为工具自设上限**，未与需求方确认；`O(n²)` 相似度在 200 条时约 2 万对，更大规模未做性能评估。
9. **未接入产品 UI / `server.py`**：本工具是独立 CLI，未接入任何 HTTP 路由，未改 `app.js`；聚类结果**未回写数据库**（本任务明确只读）。事件 `urls` 取记录的**原始 `url`**（非 `canonical_url`）——口径选择未与需求方确认。
10. **`open_readonly()` 的 `mode=ro` 回退分支未触发**（本轮库无 `-wal`），仅有静态依据；并发「服务正在写库」场景未测。

## 十、已知风险与边界

- **只读**：仅 `SELECT records/feeds`；只读 URI 打开（默认 `immutable`），不建表、不改表结构、不写 `records`，也不产生任何平台写动作。
- **凭据**：只从环境变量读取，值不落盘、不打印、不入库；错误报告只带环境变量**名字**。
- **成本**：规则阶段零模型调用；模型阶段每次真实运行最多 `ceil(selected/model_batch)` 次请求，每次一个 HTTP 请求；不重试、不并发、无续轮。
- **口径**：`same_event` 是**模型对标题字面是否同事件**的判断（夹具路径则是桩），不是立场、热度、评论数或群体态度；本工具**不推断群体立场**、不算评论立场比例、不采评论正文。
- 本工具不写桥的状态文件、不启动服务、不订阅定时任务。

## 十一、本轮改动范围

相对基线 `7335e89`，本 worktree 只新增两个文件（均在允许路径内）：

- `tools/cluster_events.py`（新增，SHA-256 `2ba5900204aee77e18fbab22f7f726cb08b536ae22caf06365bcde60bedb8e83`）
- `docs/S4-clustering.md`（本文）

`analysis.py`、`storage.py`、`tools/annotate_relevance.py` 及数据库**零改动**；未提交、未切分支、未修改 `.git`。

## 十二、交付前自检（`selfcheck.py --kind annotator`，离线）

命令：

```
python -B "<SELFCHECK>" --kind annotator --worktree . --allow "tools/,docs/" --artifacts "tools/cluster_events.py,docs/S4-clustering.md"
```

完整 JSON（verdict=pass）：

```json
{
 "kind": "annotator",
 "worktree": "D:\\DSH\\output\\2026-10-16\\2026-10-16-18-03-28\\_s4\\store\\tasks\\sentiment-s4-cluster-20261017\\attempts\\attempt-98e3e63cb93f41309d27863f66123980\\workspace",
 "allow": [
  "tools/",
  "docs/"
 ],
 "artifacts": [
  "tools/cluster_events.py",
  "docs/S4-clustering.md"
 ],
 "changed": [
  "docs/S4-clustering.md",
  "tools/cluster_events.py"
 ],
 "checks": [
  {
   "id": "changed_paths",
   "ok": true,
   "detail": {
    "changed": 2,
    "outside_allow": []
   }
  },
  {
   "id": "artifacts",
   "ok": true,
   "detail": {
    "missing": []
   }
  },
  {
   "id": "no_secrets",
   "ok": true,
   "detail": {
    "hits": []
   }
  },
  {
   "id": "python_ast",
   "ok": true,
   "detail": {
    "bad": []
   }
  },
  {
   "id": "annotator_contract",
   "ok": true,
   "detail": [
    {
     "check": "只读打开数据库（mode=ro / immutable）",
     "ok": true
    },
    {
     "check": "没有对 records 的写操作",
     "ok": true
    },
    {
     "check": "有枚举降级（conservative/fallback 之类）",
     "ok": true
    },
    {
     "check": "有 dry-run 且不发请求",
     "ok": true
    },
    {
     "check": "未把密钥写进仓库",
     "ok": true
    }
   ]
  }
 ],
 "verdict": "pass",
 "note": "离线自检：0 网络请求、只读、不改任何文件；它不能替代真实运行验收。"
}
```

