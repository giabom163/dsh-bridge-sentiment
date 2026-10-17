# S3-B 相关性 / 主题 / 性别相关标注（复用已接收的生产适配器）

实现：`tools/annotate_relevance.py`。状态：**离线路径（只读取数、分批、`--dry-run`、归一化降级、产物落盘）已在本机实跑验证通过；`--limit 5` 的真实模型调用本轮 BLOCKED（执行器进程没有 `OPENCODE_GO_V1_API_KEY`，未发送任何请求）**。详见第七节。

本文记录：复用清单、参数、提示词与枚举契约、归一化降级规则、产物格式、真实运行证据、失败路径、未实证项与边界。

工作区（attempt worktree）：`...\attempts\attempt-2c400e72f59541679f14eb54b79a4f7b\workspace`，基线 commit `345c08fd172e519acdd32eb1b579a1736291217f`。

## 一、复用清单（先复用，后新写）

| 复用对象 | 真实路径 | 复用方式 | 本文件是否新写 |
| --- | --- | --- | --- |
| 生产适配器 HTTP 细节 | `analysis.py`（已并入并独立审查） | **直接 import 其常量**：`PRODUCTION_ENDPOINT`、`PRODUCTION_MODEL`（**裸 id**）、`PRODUCTION_MODEL_ENV`（`OPENCODE_GO_V1_MODEL` 覆盖）、`PRODUCTION_KEY_ENV`（`OPENCODE_GO_V1_API_KEY`）、`PRODUCTION_USER_AGENT`、`PRODUCTION_SESSION`、`PRODUCTION_TIMEOUT`、`PRODUCTION_MAX_RESPONSE`、`PRODUCTION_ERROR_BODY`、`ProductionHTTPError` | 否，一个常量都不复制 |
| `reasoning_effort="none"` | 同上 | 请求体里固定写 `"none"`（实测：不带时模型把预算烧在思考上、返回非 JSON） | 否 |
| 三个必备头 | 同上 | `Authorization`（值只来自环境变量，不入库）/ `User-Agent`（Cloudflare 对非浏览器 UA 回 403 1010）/ `x-opencode-session`（缺失回 400 MissingSessionID） | 否 |
| 错误保留 | 同上 | 复用 `analysis.ProductionHTTPError`（真实状态码 + 响应体前 200 字），单次请求、不重试 | 否 |
| 证据库约定 | `storage.py` | `now()`（UTC）、`digest()`、`json_bytes()`（jsonl 行按项目既有规范序列化） | 否 |
| 只读取数 | `storage.py` 的 `records`/`feeds` 表约定 | 只读 URI 连接（详见 7.8）+ `SELECT` | 是（见下） |
| 工具做法 | `tools/collect_opencli.py` | `--limit`/`--dry-run`、失败保留真实原文不静默降级、统一 JSON 报告与退出码、Windows 控制台切 UTF-8 | 是（`_utf8_stdout` 为同一做法的本地实现） |

**必须新写的部分及理由**（其余都不重复实现）：

1. **标注提示词**：`analysis.production_messages(role, ...)` 把系统提示词硬绑在 A/B 证据角色上（A 只输出 `claims`、B 只输出 `reviews`）。本次要的是 `{"items":[...]}` 标注提示词，而 `analysis.py` **不在本任务允许改动的路径内**（只允许改 `tools/`、`docs/`），无法把提示词参数化。因此 `post_once()` 只重写「消息内容」，其**发送与解析逐行对齐 `analysis.production_adapter()` 的既有行为**（请求体字段、请求头、`build_opener` 不加重定向、超时、响应上限、`choices[0].message.content` 解析、单 JSON 对象要求）。
2. **只读打开**：`storage.connect()` 会 `CREATE TABLE IF NOT EXISTS` 并 `PRAGMA journal_mode=WAL`，属于写动作，与本任务「只读数据库、不改表结构」冲突。故用 URI 只读连接：无未检查点数据时用 `mode=ro&immutable=1`，存在非空 `-wal` 时退回 `mode=ro`（实测与理由见 7.8）；两种模式都不写库内容，任何写操作都会直接失败。
3. **枚举归一化**：`analysis.py` 里只有 `normalize_output()`（对齐引用偏移），没有枚举级归一化。按契约允许「若那里是私有的，就在本文件实现等价函数并注明来源」，在第 4.2 节实现等价函数并注明思路来源。
4. 参数解析、输出落盘与哈希回读。

未引入任何第三方依赖（仅 Python 标准库 + 本仓 `analysis.py`/`storage.py`）；未新增登录、未读取或复制任何 cookie/凭据文件；未使用 MediaCrawler。

## 二、命令与参数

```
python -B tools/annotate_relevance.py [--limit N] [--feed NAME] [--batch N] [--dry-run]
                                      [--min-interval SECONDS] [--db PATH]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--limit` | `25` | 取最近 N 条 `records`（1–200）。排序 `fetched_at DESC`，同一时刻用 `id DESC` 兜底以保证确定性 |
| `--feed` | 无 | 按来源名**精确**过滤（`feeds.name`，如 `toutiao-hot`、`weibo-hot`） |
| `--batch` | `25` | 单批条数，**不得超过 25**（契约硬约束，超限直接报错，不静默截断） |
| `--dry-run` | 关 | 只打印将要发送的批次与条数，**不发起任何网络请求、不写输出文件、不建 `annotations/`** |
| `--min-interval` | `8` | 批次之间的最小间隔秒数（首批发完才计） |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径（只读打开） |

退出码：`0` 成功；`2` 参数/环境/数据库问题（含缺凭据）；`3` HTTP/连接失败；`4` 响应或模型输出解析失败；`7` 产物写入失败。

## 三、提示词契约

系统提示词要求模型**只输出一个 JSON 对象**，且仅含一个键 `items`，与输入一一对应，每条严格为六个字段：

```json
{"items":[{"id":"...","relevance":0,"topic":"其他","stance":"无关","gender_related":false,"summary":"..."}]}
```

| 字段 | 契约 |
| --- | --- |
| `id` | 必须原样回填输入的 `record_id`，不得新增、遗漏或重复 |
| `relevance` | `0` / `1` / `2`（2=直接讨论性别议题，1=间接相关，0=无关） |
| `topic` | `性别议题\|婚恋家庭\|职场\|公共安全\|教育\|医疗\|消费维权\|政策法规\|娱乐八卦\|其他` |
| `stance` | `反对\|支持\|中立\|无关` |
| `gender_related` | 布尔 |
| `summary` | 不超过 30 字的中文短语 |

送入模型的用户消息为 `{"items":[{"id","feed","title","summary"}]}`；`title` 超过 200 字、`summary` 超过 300 字时截断并加 `…` 标记（**只影响送入模型的输入，不改数据库原文**）。提示词明确要求「不推断作者性别、不接受也不输出统计数字」。

## 四、归一化与降级规则

### 4.1 结构问题：硬失败，不静默降级

`items` 缺失/非数组、`id` 不属于本批、`id` 重复、未覆盖本批全部记录 —— 一律抛 `AnnotationError`（退出码 `4`）并附缺失 `id` 列表，**不写任何产物**。这正是「失败保留真实原文、不静默降级」的要求。

### 4.2 枚举/类型越界：降级为保守值，不抛错、不原样输出

思路来源：`analysis.normalize_output()` 是同类「先归一化、再严格校验」的写法——它在 `validate()` 之前对齐引用偏移，但**绝不放宽**「引用必须是原文逐字子串」这一证据性要求。`analysis.py` 没有枚举级归一化函数，故在本文件实现等价函数并注明来源。判据如下（`tools/annotate_relevance.py`）：

| 字段 | 接受 | 其余一律降级为 |
| --- | --- | --- |
| `relevance` | `int` 且 ∈ {0,1,2}；或字符串 `"0"/"1"/"2"` | `0`（保守）。`type is int` 排除 `bool` |
| `topic` | 10 项枚举之一 | `其他` |
| `stance` | 4 项枚举之一 | `无关` |
| `gender_related` | `bool`；或字符串 `"true"/"yes"/"1"`（大小写不敏感） | `False` |
| `summary` | 字符串（去首尾空白后取前 30 字） | 非字符串 → `""`；超长 → 截断到 30 字 |

## 五、产物

成功一次运行写两个文件（`annotations/` 为运行期产物目录，默认相对仓库根，**不在** `allowed_paths` 内，故本轮未在 worktree 内生成）：

1. `annotations/relevance-<UTC时间戳>.jsonl`：每条一行，字段 `record_id/feed/title/relevance/topic/stance/gender_related/summary`（按项目既有 `storage.json_bytes` 规范序列化，键排序、紧凑、`ensure_ascii=False`）。
2. `annotations/summary-<UTC时间戳>.json`：`relevance/topic/stance` 各枚举分布、`gender_related` 的条数与占比、`per_feed` 每源分布、`tokens` 每批 usage 与汇总、`outputs`（两个产物的路径、字节数、SHA-256）。

写完后**回读哈希校验**：jsonl 的字节 SHA-256 必须与报告里的一致，否则报错（不把「写过了」当成「写对了」）。

`--dry-run` 与失败路径都**不写产物**：产物按批次累积在内存，全部批次成功后才落盘，避免出现半成品 jsonl 被误当完整结果。

## 六、节流

- 单批最多 25 条；每批**最多一个真实 HTTP 请求**，不重试、不并发、不翻页。
- 批次之间按 `--min-interval`（默认 8 秒）间隔，首批发完才计间隔。
- 无定时任务、无后台轮询，只由人工显式执行。

## 七、真实验证（本轮在本机实跑，均绑定最终文件哈希）

### 7.1 环境与文件指纹

- 被测文件 `tools/annotate_relevance.py`：**SHA-256 `bb433e0b52b20c02ee90e40f190544cf6328b259fc2076568d055a31cdfbca93`，25645 字节**（下方 7.2–7.6 的全部运行均在此字节版本上产生）
- Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`（3.13.14）
- 只读输入：把仓库 `sentiment.db` 复制到 `C:\Users\Lenovo\AppData\Local\Temp\opencode\s3b-readonly\sentiment.db`（512000 字节，SHA-256 `401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51`），**对复制件也只读打开**；真实库 `records` 148 条、`feeds` 8 个（`toutiao-hot`/`toutiao-recommend`/`tieba-hot`/`36kr-hot`/`weibo-hot`/`zhihu-hot` + 2 个验收用来源）
- 运行后核对：worktree 内 `git status --short` 只有 `?? tools/annotate_relevance.py`；`annotations/` 不存在、`sentiment.db` 不存在

### 7.2 `--dry-run --limit 5`（真实 stdout，`2026-10-16T15:44:17.781Z → 15:44:17.904Z`，退出码 `0`）

```json
{
  "tool": "s3b-relevance", "status": "ok", "mode": "dry-run", "network_requests": 0,
  "db": { "path": "...\\s3b-readonly\\sentiment.db", "present": true,
          "access": "mode=ro&immutable=1", "touched": false },
  "feed": null, "limit": 5, "batch_size": 25, "min_interval_seconds": 8.0,
  "model": "deepseek-v4.1-flash", "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
  "reasoning_effort": "none", "record_count": 5, "batch_count": 1,
  "batches": [{ "index": 1, "count": 5,
    "record_ids": ["record-ca492fd2b7df4c2daecca0ebe1c1bb73", "record-1de1150d6e5d4c0887f78311d79dee42",
                   "record-dfcf62f300ef466496c4ffd44ed77cd0", "record-86ff71820bdf4c89bf97d14cf186f7ae",
                   "record-8e8ce123abf14062a8eb8ac54f03e928"],
    "titles": ["中方：中国核力量与美俄不在一个量级", "专家：日本全境都在解放军射程范围内",
               "黄海南部连续47天禁入藏了什么深意", "法德对华快速反制获审议", "张镇麟16分6板正负值+28全场最高"] }],
  "out_dir": "...\\workspace\\annotations",
  "would_write": ["annotations/relevance-<UTC时间戳>.jsonl", "annotations/summary-<UTC时间戳>.json"]
}
```

### 7.3 `--dry-run` 不发起任何网络请求（护栏证据）

在**任何网络连接都会抛错**的护栏下运行 `--dry-run --limit 5`（本机内存运行，`2026-10-16T15:44:56.482Z → 15:44:56.597Z`）：

```
socket.socket.connect  -> 替换为 AssertionError("dry-run 不应发起任何连接")
urllib.request.build_opener -> 替换为 AssertionError("dry-run 不应调用 build_opener")
exit_code = 0 | connect_attempts = 0 | build_opener_calls = 0
```

`connect_attempts = 0`、`build_opener_calls = 0` 且仍退出 `0`，说明 dry-run 路径根本没有触达网络层。

### 7.4 枚举降级反例（走真实的 `normalize_items()`）

人造一条越界模型输出（`topic="军事科技"`、`stance="强烈反对"`、`relevance=7`、`gender_related="maybe"`、超长 summary），经真实归一化函数后写出的条目为：

```json
{"record_id": "record-demo", "feed": "demo-feed", "title": "演示标题",
 "relevance": 0, "topic": "其他", "stance": "无关", "gender_related": false,
 "summary": "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"}
```

越界值 `军事科技`/`强烈反对`/`7`/`maybe` 均**未出现在结果里**，被降级为保守值（`其他`/`无关`/`0`/`false`），且未抛错。对照组（合法值 `relevance=2`、`topic="性别议题"`、`stance="反对"`、`gender_related=true`）原样保留，未被改写。

### 7.5 live 路径结构验证（人造桩，**不是**真实模型结果）

把 `post_once()` 换成返回固定 JSON 的桩、把输出目录指向临时目录，走完整 `normalize → summary → write_outputs`（`2026-10-16T15:45:03.169Z → 15:45:03.291Z`）：

- jsonl 首 3 行（真实落盘字节）：

```json
{"feed":"toutiao-hot","gender_related":true,"record_id":"record-ca492fd2...","relevance":2,"stance":"反对","summary":"军事议题，非性别","title":"中方：中国核力量与美俄不在一个量级","topic":"性别议题"}
{"feed":"toutiao-hot","gender_related":false,"record_id":"record-1de1150d...","relevance":1,"stance":"中立","summary":"安全相关","title":"专家：日本全境都在解放军射程范围内","topic":"公共安全"}
{"feed":"toutiao-hot","gender_related":false,"record_id":"record-dfcf62f3...","relevance":0,"stance":"无关","summary":"越界演示","title":"黄海南部连续47天禁入藏了什么深意","topic":"其他"}
```

（第 3 行来自 7.4 的越界桩数据，可再次看到 `topic`→`其他`、`stance`→`无关`、`relevance`→`0`。）

- `summary-*.json`：`gender_related = {"count": 2, "total": 5, "ratio": 0.4}`；`tokens.totals = {"prompt_tokens": 321, "completion_tokens": 87, "total_tokens": 408}`；`per_feed = {"toutiao-hot": {"count": 5, "gender_related": 2, "topic": {...}}}`
- 产物：`relevance-20261016T154503Z.jsonl`（1177 字节，SHA-256 `7d78bdeae286dc0a35c6186424926d67200cb56a3e9240bce6bac276a1684f5f`）、`summary-20261016T154503Z.json`（SHA-256 `c251934275cfbd45d706ba352018b17eea811eb28e5099d5e26270c972f2361f`）；`outputs.jsonl_sha256` 与文件字节回读一致

**这里是桩数据，只证明代码路径与产物格式可用，不能当作模型标注质量或真实分布的证据。**

### 7.6 `--limit 5` 真实模型调用：**BLOCKED**（无凭据，未发送请求）

在 worktree 内实跑 `python -B tools/annotate_relevance.py --limit 5 --db <只读复制库>`，真实 stdout（退出码 `2`）：

```json
{
  "tool": "s3b-relevance", "status": "blocked",
  "error": { "stage": "env", "exit_code": 2, "type": "AnnotationError",
             "message": "缺少环境变量 OPENCODE_GO_V1_API_KEY；未发送任何请求",
             "credential_env": "OPENCODE_GO_V1_API_KEY" },
  "db": "...\\s3b-readonly\\sentiment.db", "feed": null, "limit": 5, "batch": 25, "dry_run": false
}
```

`os.environ.get("OPENCODE_GO_V1_API_KEY") is None`（`credential_env_present = False`）。因此：

- 未发出任何 HTTP 请求，也没有 `annotations/*.jsonl` 产物；
- 按项目规则**不读取 token/密码/cookie/认证 DB**，故未去别处寻找凭据。

**解除该 BLOCKED 所需的证据/条件**：在执行器进程（或其子进程环境）中提供 `OPENCODE_GO_V1_API_KEY`（可选 `OPENCODE_GO_V1_MODEL` 换模型），然后重跑 `--limit 5`，即可补上本节的真实 stdout、`annotations/*.jsonl` 前 3 条与真实 token 用量。

### 7.7 `gender_related` 真实计数：**BLOCKED**

真实计数只能来自 7.6 的真实运行；该运行未发生，故**没有真实 `summary-*.json`**，`gender_related` 真实条数**不可得**（不填 0 冒充，也不拿 7.5 的桩数据顶上）。可如实报告的只有**输入侧**事实：`--limit 5` 选中 5 条记录，全部来自 `toutiao-hot`，即 7.2 中列出的 5 个 `record_id`。

### 7.8 只读打开与 WAL 旁文件（实测口径）

`storage.connect()` 不能用于只读读取（它会 `CREATE TABLE IF NOT EXISTS` 并切 WAL 日志模式）。本工具用 URI 只读连接，并对 WAL 旁文件做了实测（本机 `2026-10-16`，Python 3.13.14 自带 SQLite）：

```
目标库 pragma journal_mode = wal
mode=ro              → 读完 148 行后发现目录里多出 sentiment.db-shm(32768) 与 sentiment.db-wal(0)
mode=ro&immutable=1  → 读完 148 行，目录里没有多出任何文件
两次打开前后，sentiment.db 的 SHA-256 都是 401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51（库文件字节不变）
```

因此 `open_readonly()` 的规则是：**不存在非空 `-wal` 时用 `mode=ro&immutable=1`（零旁文件、不加锁）；存在非空 `-wal`（有未检查点的已提交数据）时退回 `mode=ro`**，以免 `immutable` 跳过 WAL 读到旧数据。本轮复制件无 `-wal`，7.2–7.6 全部运行都走 `immutable`，运行后 `annotations/` 内除桩产物外无新增、库目录无 `-shm`/`-wal`、库哈希不变。

### 7.9 定向检查（不产生上游请求）

| 检查 | 命令 | 观察结果 |
| --- | --- | --- |
| 语法（内存 compile，未落盘 pyc） | `python -B -c "compile(open('tools/annotate_relevance.py',encoding='utf-8').read(),'tools/annotate_relevance.py','exec')"` | `memory compile OK`，退出码 0 |
| `--batch` 越界 | `--dry-run --batch 30` | `status:"blocked"`，`stage:"args"`，`"message":"--batch 必须在 1-25 之间，收到 30"`，退出码 2；不读库、不发请求 |
| `--limit` 越界 | 代码判据 `1<=limit<=200` | 同上（`stage:"args"`，退出码 2） |
| 缺库 | `--dry-run --db <不存在路径>` | `stage:"db"`，`"证据库不存在：..."`，退出码 2，不发请求 |
| `--feed` 过滤 | `--dry-run --limit 2 --feed weibo-hot` | `record_count=2`，只取该来源；退出码 0 |
| 分批上限 | `--dry-run --limit 200`（库内 148 条） | `record_count=148`、`batch_count=6`，每批 `25/25/25/25/25/23`，无一批超过 25 |
| 只读性 | 7.2–7.6 全部运行 | 全部走只读 URI；运行后真实库与复制件字节未变、无 WAL 旁文件、`git status` 无 `sentiment.db` |

## 八、失败路径

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| 缺 `OPENCODE_GO_V1_API_KEY` | 2 | `status:"blocked"`，`stage:"env"`，**未发送请求** |
| `--limit` / `--batch` / `--min-interval` 越界 | 2 | `stage:"args"`，不读库、不发请求 |
| 证据库不存在 / 只读打开失败 / `SELECT` 失败 | 2 | `stage:"db"`，附库路径与原始异常 |
| HTTP 非 200（含 400/401/403/429/5xx） | 3 | 复用 `analysis.ProductionHTTPError`：**真实状态码 + 响应体前 200 字**；单次请求，不自动重试 |
| 连接失败（DNS/TLS/超时） | 3 | `stage:"network"`，附 `exc.reason` |
| 响应非 JSON / 缺 `choices`/`content` / 模型输出非 JSON 对象 | 4 | 附响应体或模型文本前 200 字 |
| `items` 缺字段、`id` 越批/重复、覆盖不全 | 4 | 附缺失 `id` 列表，不写产物 |
| 产物写入或回读哈希不一致 | 7 | `stage:"write"`，附路径 |

失败一律不重试、不降级成「成功但空」，也不留下半成品产物。

## 九、未实证项（本轮未做，不得当成已完成）

1. **真实模型调用与真实标注结果未取得**（7.6/7.7，缺凭据）。未验证：真实 HTTP 200、真实 `usage`、真实 `gender_related` 计数、模型对 10 项 `topic` 的实际分布、模型是否稳定回填 `id` 且不遗漏。
2. **多批次与 `--min-interval` 节流未真跑**：本轮真实运行都是单批（`--limit 5`、`--batch 25` → 1 批）。分批切分本身已用 dry-run 验证（`--limit 200` → 6 批 `25/25/25/25/25/23`，见 7.9），但**多批的真实连续调用、批次间 `--min-interval` 实际等待**未执行（`sleep()` 只经过 7.5 的单批结构验证）。
3. **`--feed` 为精确名匹配**：本实现解释为 `feeds.name` 精确相等。若需求原意是模糊匹配或按来源 URL 过滤，需改语义——未与需求方确认。
4. **提示词质量未评估**：没有任何真实模型输出，故 10 项 `topic` 与 4 项 `stance` 的定义边界、`relevance` 0/1/2 的判据是否与产品预期一致，**未验证**；上一轮「不带 `reasoning_effort=none` 就返回非 JSON」的实测结论沿用自 `analysis.py`，本轮未重测。
5. **输入截断口径未与需求确认**：`title`>200 字、`summary`>300 字会截断加 `…`；是否应截断、截多少，未与需求方确认。
6. **`annotations/` 未纳入 `.gitignore`**：本轮未在 worktree 内生成该目录（`--dry-run` 与缺凭据失败都不落盘），因此没有出现越界文件；真实运行会新增该未跟踪目录，是否加入忽略清单待产品确认（`.gitignore` 不在本任务允许改动范围内）。
7. **未接入产品 UI / `server.py`**：本工具是独立 CLI，未接入任何 HTTP 路由，未改 `app.js`；标注结果未回写数据库（本任务明确只读）。
8. **本工具自设 `--limit ≤ 200`**：为避免单次运行产生不可控模型花费；该上限未与需求方确认。
9. **`open_readonly()` 的 `mode=ro` 回退分支未触发**：本轮库无 `-wal`，只走了 `mode=ro&immutable=1`。存在非空 `-wal` 时退回 `mode=ro` 的分支只有静态依据（SQLite `immutable` 不读 WAL），未用「有未检查点数据」的库实测；也未在「服务正在写库」的并发场景下实测。

## 十、已知风险与边界

- **只读**：仅 `SELECT records/feeds`；URI 只读打开（默认 `immutable`），不建表、不改表结构、不写 `records`，也不产生任何平台写动作（无发布/评论/关注/删除）。
- **凭据**：只从环境变量读取，值不落盘、不打印、不入库；错误报告只带环境变量**名字**。
- **成本**：每次真实运行最多发出 `ceil(limit/batch)` 次请求，每次最多一个 HTTP 请求；不重试、不并发、无续轮。
- **口径**：`relevance`/`gender_related` 是模型判断，**不是**热度、评论数或立场比例；本工具不计算评论立场比例、不构建评论树、不采集评论正文。
- 本工具不写桥的状态文件、不启动服务、不订阅定时任务。

## 十一、本轮改动范围

相对基线 `345c08f`，本 worktree 只新增两个文件（均在允许路径内）：

- `tools/annotate_relevance.py`（新增，SHA-256 `bb433e0b52b20c02ee90e40f190544cf6328b259fc2076568d055a31cdfbca93`）
- `docs/S3-relevance.md`（本文）

`analysis.py`、`storage.py`、`tools/collect_opencli.py` 及数据库**零改动**；未提交、未切分支、未修改 `.git`。
