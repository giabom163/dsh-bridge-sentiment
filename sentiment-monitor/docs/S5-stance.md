# S5 立场标注（证据先行：A 主张 → B 复核 → 才给立场）

实现：`tools/annotate_stance.py`。状态：**离线路径（S4 产物/现场聚类两种输入、只读取数、`--dry-run`、`--no-model` 纯规则真实运行、参数与输入边界、预算超限即停、只读性与产物哈希回读）已在本机实跑验证通过（0 次真实模型调用）；A→B→立场代码路径、`validate` 逐条丢弃、`normalize_output` 偏移对齐、HTTP 500 / 结构损坏降级已用本机回环夹具（`OPENCODE_GO_V1_FIXTURE=1`，**非真实模型**）实跑验证；真实模型调用与真实立场分布本轮 BLOCKED（执行器进程没有 `OPENCODE_GO_V1_API_KEY`，未发送任何真实请求，留给主线程补跑）**。详见第九节。

本文记录：复用清单、参数、事件输入与 packet 构造、A/B 证据协议与丢弃口径、立场规则表、纯规则降级、产物格式、真实运行证据、失败路径、未实证项与边界。

工作区（attempt worktree）：`...\attempts\attempt-a9722340e5494bc3849858856ac5aa63\workspace`，基线 commit `dded5ebacff813f25d4cb8df90cffd7cb840a8f0`。

## 一、复用清单（先复用，后新写）

| 复用对象 | 真实路径 | 复用方式 | 本文件是否新写 |
| --- | --- | --- | --- |
| A/B 证据协议 | `analysis.py`（已并入并独立审查） | **直接调用，一个字都不重写**：`analysis.production_adapter(role, packet, a_output)` 出 A 的 `claims` / B 的 `reviews`；`analysis.normalize_output(packet, role, output)` 在 validate 前对齐引用偏移；`analysis.validate(packet, role, output, a_output)` 做结构与引用硬校验 | 否 |
| 生产 HTTP 细节 | 同上 | 全部由 `production_adapter` 内部承担（**裸模型 id**、`reasoning_effort="none"`、`Authorization`/`User-Agent`/`x-opencode-session` 三头、`PRODUCTION_TIMEOUT`、`PRODUCTION_MAX_RESPONSE`、单次请求不重试），本文件**不复制任何常量、不写任何 HTTP 代码** | 否，零复制 |
| 错误保留 | 同上 | 复用 `analysis.ProductionHTTPError`（真实 HTTP 状态码 + 响应体前 `PRODUCTION_ERROR_BODY`＝200 字）；`model.errors` 逐事件保留原文，不自动重试 | 否 |
| 离线夹具分支 | 同上 | 复用其 `OPENCODE_GO_V1_FIXTURE=1`：无凭据时可用本地端点回放整条 A/B 链路 | 否 |
| 聚类逻辑 | `tools/cluster_events.py`（S4） | `--events-from-db` 时按 importlib 载入其模块，复用 `load_records()` / `normalize_title()` / `SIMILARITY_FUNCS` / `rule_pass()` / `build_events()`；事件 `event_id`/`size`/排序与 S4 产物一致，**不另写一套聚类** | 否 |
| 只读打开 | `tools/annotate_relevance.py`（本任务 anchor） | 复用 `open_readonly()`：无未检查点数据时 `mode=ro&immutable=1`，存在非空 `-wal` 时退回 `mode=ro`；两种模式都不写库，任何写都会失败 | 否 |
| 失败类型与工具做法 | 同上 | 复用 `AnnotationError`（stage / exit_code / extra）、`utc_stamp()`、`clip()`、`_utf8_stdout()`（Windows 控制台默认 GBK）、`usage_totals()`（usage 缺失保留 null） | 否 |
| 证据库约定 | `storage.py` | `now()`（UTC）、`digest()`（SHA-256）、`json_bytes()`（jsonl 行按项目既有规范序列化：键排序、紧凑、`ensure_ascii=False`） | 否 |
| 事件输入 | `events/events-*.jsonl`（S4 产物） | 直接作为事件分组输入（`--events`）；成员回库补 `summary`/`raw_sha256` | 是（仅成员解析） |

**必须新写的部分及理由**：

1. **事件输入的读取与成员解析**：S4 产物每条只有 `event_id/size/feeds/titles/urls/first_seen/method`——**没有** `record_id`、`summary`、`raw_sha256`，而证据 packet 必须带这三个字段（`analysis.citation()` 用它校验引用）。故按 `(url, title)` 回只读库关联 `records`：同键多候选按 `(fetched_at, id)` 升序**确定性**取用，重复成员按序消费；解析不到的成员**硬失败**（拒绝伪造 packet）。这是本文件新写的唯一「读取」逻辑。
2. **立场规则表**：只把「通过 validate 且被 B 复核为 `supported` 的 claim」按 `target` 分布（`context` 作方向细化）映射为 `支持/反对/中立`；无 supported claim 一律 `无关`。规则表写死在第五节，**模型从不直接输出立场**。
3. **纯规则降级**（`--no-model`）：关键词 + 目标词表，输出强制标 `method="rules"`，绝不冒充模型结论。
4. 参数解析、预算控制、产物落盘与哈希回读。

未引入任何第三方依赖（仅 Python 标准库 + 本仓 `analysis.py`/`storage.py` + 两个同目录工具）；未新增登录、未读取或复制任何 cookie/凭据文件；未使用 MediaCrawler；不碰任何平台。

## 二、命令与参数

```
python -B tools/annotate_stance.py [--events PATH | --events-from-db] [--db PATH] [--limit N]
                                   [--max-events N] [--max-model-calls N] [--min-interval SECONDS]
                                   [--no-model] [--dry-run] [--out-dir DIR]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--events` | `<仓库>/events/` 下最新 `events-*.jsonl` | S4 事件产物路径；缺省取最新一个（无产物则 `stage:"input"` 失败） |
| `--events-from-db` | 关 | 不用 S4 产物，改为 import `cluster_events` 复用其**规则阶段**现场聚类（零模型调用）；与 `--events` **互斥** |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径（**只读**打开）；`--events` 模式也用它取 `summary`/`raw_sha256` |
| `--limit` | `200` | 仅 `--events-from-db` 的聚类输入条数（1–200），与 `cluster_events` 同口径 |
| `--max-events` | `10` | 最多处理的事件数（1–**50**，契约硬上限）；按 `(size DESC, event_id)` 取前 N |
| `--max-model-calls` | `20` | 模型调用总上限（**每次 A 或 B 各算 1 次**；0–100）。超限即停 |
| `--min-interval` | `8` | **事件之间**的最小间隔秒数（同一事件的 A、B 之间不等待） |
| `--no-model` | 关 | 完全跳过模型，纯规则降级；输出标 `method="rules"` |
| `--dry-run` | 关 | 只打印将处理多少事件、预计多少次模型调用；**0 次网络请求、不写输出** |
| `--out-dir` | `<仓库>/stance` | 产物目录（运行期产物，**不在** `allowed_paths` 内；本轮验证写入 Temp 以保持 worktree 干净） |

退出码：`0` 成功；`2` 参数/环境/数据库/输入问题（含缺凭据、事件产物关联不上）；`3` HTTP/连接失败；`4` 解析失败；`7` 产物写入失败。

## 三、事件输入与 packet 构造

### 3.1 两种输入

- `--events <jsonl>`（默认取 `events/` 下最新）：读 S4 产物的事件分组（`event_id` + `titles`/`urls` 等长同序），再把每个成员按 `(url, title)` **只读**关联回 `records`，补出 `id`/`summary`/`raw_sha256`/`canonical_id`/`published_at`。同键多候选（同一内容被多次采集）按 `(fetched_at, id)` 升序确定性取用；一个事件内重复的成员按序消费不同候选，候选不足时复用最后一个，保证成员数与 `size` 不缩水（歧义条数以 `ambiguous_members` 记录，不隐藏）。
- `--events-from-db`：复用 `cluster_events` 的**规则阶段**（`canonical_url` 相同 + 标题 Dice ≥ 0.6 的连通分量，**零模型调用**；边界对按「不同事件」保守处理），事件 `event_id` 与 `size` 由 `cluster_events.build_events()` 给出，成员直接按 id 取完整字段。本工具**不再**为判同调用模型——模型预算只留给 A/B 证据协议。

两种输入都**只读**同一份库，因此事件产物的成员必须能在库中关联到；关联不到即 `stage:"input"` 硬失败（事件产物与证据库可能不是同一快照，拒绝伪造 packet）。

### 3.2 packet（与 `analysis.evidence()` 同形）

```
{"records": [{"id", "raw_sha256", "title", "summary", "canonical_id", "published_at"}],
 "coverage": "sample_only", "prompt_version": <analysis.PROMPT>,
 "statistics": {"record_count", "canonical_count", "unknown_time_count"}}
```

`records` 里 `title`/`summary` 取库内规范化文本；`analysis.citation()` 直接以该记录的 `raw_sha256` 与 `title`/`summary` 原文校验 `text[start:end] == quote`。

## 四、A → B 证据协议与丢弃口径（全部走 `analysis.py`，不重写）

对每个事件：

1. **A 出 claims**：`analysis.production_adapter("A", packet, None)`。外层必须为仅含 `claims` 的 JSON 对象，且条数在 1–20；否则记 `a_structure` 丢弃整批。
2. **逐条校验**：对每条 claim 包成 `{"claims":[claim]}`，先 `analysis.normalize_output(packet,"A",...)` 对齐引用偏移，再 `analysis.validate(packet,"A",...)`。**判据完全用 `analysis.validate` 原样，不放宽、不修补**；不过的逐条丢弃并记 `a_validation`（附真实失败原因前 200 字与 `claim_id`）。逐条校验只是缩小校验粒度，使「validate 不过的 claim 丢弃并计数」成立。
3. **B 逐条复核**：以**通过校验的 claims** 作为 `a_output` 调 `analysis.production_adapter("B", packet, a_output)`；`normalize_output` + `validate`（B 必须逐条覆盖 A 的全部主张）。整批不过记 `b_validation`/`b_structure` 并全部不予采纳。
4. **准入**：只有 `verdict == "supported"` 的 claim 进入证据；`partially_supported`/`unsupported`/`unverifiable` 记 `b_<verdict>`；B 漏复核对记 `b_missing_review`。
5. **立场**：只由**准入的 supported claim** 按第五节的规则表推导。

`dropped_claims`（每条输出）＝该事件从 A 到准入共丢弃/未采纳的 claim 条数；`supported_claims` 为进入证据的条数。汇总里另有 `drop_reasons` 与 `drops` 逐条明细。

## 五、立场规则表（保守、写死、可审计）

**输入**：该事件**准入的 supported claim**。模型从不直接输出立场；立场只能由此表推出。

| # | 条件（对 supported claim 统计） | 立场 |
| --- | --- | --- |
| 1 | 无 supported claim（含 A/B 失败、预算不足、全部被丢弃） | `无关` |
| 2 | 全部 `target=unspecified`（没有性别指向） | `无关` |
| 3 | 含 `target=both`，或同时含 `target=male` 与 `target=female` | `中立`（两侧都在场，保守不选边） |
| 4 | 仅单侧（只有 `male` 或只有 `female`） | 见 4a–4c |
| 4a | 该侧 `context=assertion` 条数 > `rebuttal` 条数 | `支持` |
| 4b | 该侧 `context=rebuttal` 条数 > `assertion` 条数 | `反对` |
| 4c | 其余（`quotation`/`uncertain` 居多，或各半） | `中立` |

`target`/`context` 的取值由 `analysis.validate()` 强制为 `male|female|both|unspecified` 与 `assertion|quotation|rebuttal|uncertain`。规则 4a/4b 用 `context` 判「内容在主张还是在反驳该说法」的方向；方向不可判定时一律 `中立`。**该表未与需求方确认**（见第十一节第 1 条），是保守口径：证据不足宁可 `无关`/`中立`，也不推断群体立场。

## 六、纯规则降级（`--no-model`）

无网络、无模型、无凭据要求。对事件成员的 `title + summary` 做粗关键词匹配（表写死在源码顶部）：

| 条件 | 立场 |
| --- | --- |
| 无女性/男性词表命中 | `无关` |
| 女性与男性词表都命中 | `中立` |
| 仅单侧命中，且命中反驳词（反对/反驳/辟谣/澄清/否认/假的/不实/造谣/…）而无支持词 | `反对` |
| 仅单侧命中，且命中支持词（支持/力挺/点赞/赞同/认可/声援/…）而无反驳词 | `支持` |
| 其余 | `中立` |

`--no-model` 的输出**强制** `method="rules"`，`supported_claims=0`、`dropped_claims=0`、`evidence=[]`，绝不冒充模型结论。

## 七、成本纪律

- 计费单位是**模型调用**：每次 A 或 B 各记 1 次，写进 `model.calls`。
- `--max-model-calls`（默认 20）：进入某事件的 A 前若预算已用尽，**立即停止**后续事件（未处理事件进 `events_skipped` 并标 `reason:"max_model_calls"`，`model.budget_exhausted:true`）；若 A 用掉了最后一个额度，B 记 `b_skipped_budget` 该事件按「无证据→无关」处理并停止。
- **事件之间**按 `--min-interval`（默认 8 秒）节流；同一事件的 A、B 之间不等待。单次请求、不重试、不并发、无续轮。
- 无定时任务、无后台轮询，只由人工显式执行。

## 八、产物（不写数据库）

成功一次运行写两个文件（默认落在 `<仓库>/stance/`）：

1. `stance/stance-<UTC时间戳>.jsonl`：每条事件一行，字段 `event_id / size / stance / method / supported_claims / dropped_claims / evidence`（按 `storage.json_bytes` 规范序列化）。
   - `method`：`model`＝走了 A/B 证据协议；`rules`＝`--no-model` 纯规则降级。
   - `evidence`：`[{claim_id, targets:[target], quotes:[引用原文]}]`，只含 supported claim。
2. `stance/summary-<UTC时间戳>.json`：各立场计数、`method` 分布、supported/dropped 合计、`drop_reasons` 与 `drops` 明细、`events_skipped`、模型调用次数与逐次调用、token 合计、输入与产物哈希。

写完后**回读哈希校验**：jsonl 的字节 SHA-256 必须与报告里的一致，否则报错（不把「写过了」当成「写对了」）。`--dry-run` 与失败路径都**不写产物**。

## 九、真实验证（本轮在本机实跑）

### 9.1 环境与文件指纹

- 被测文件 `tools/annotate_stance.py`：**SHA-256 `130ad5b7cf284ca0494727001a4e93351a4dea0074bad65d11b65f26ee3e3784`，35366 字节**（9.2–9.9 的全部运行均在此字节版本上产生）
- Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`（3.13.14），全部以 `python -B` 运行（不落 `.pyc`）
- 输入事件产物：`...\_s4\functional-run\events-20261017T053721Z.jsonl`（S4 真实产物，**117 个事件**，SHA-256 `4d0b6b16d24509d9f76e93f7995a01d74a6facb46f09396954aa3f88537069f3`，与 9.2 报告里 `input.events_sha256` 一致）
- 只读输入库：把仓库 `sentiment.db` 复制到 `C:\Users\Lenovo\AppData\Local\Temp\opencode\s5-readonly\sentiment.db`（512000 字节，SHA-256 `401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51`），**对复制件也只读打开**；库内 `records` **148 条**
- 产物目录用 `--out-dir` 指向 Temp，**未在 worktree 内生成 `stance/`**
- 运行后核对：worktree 内 `git status --short` 只有 `?? tools/annotate_stance.py`；`stance/`、`events/`、`__pycache__/`、`sentiment.db` 均不存在

### 9.2 `--dry-run`（真实 stdout，退出码 `0`，`network_requests: 0`）

```json
{
  "tool": "s5-stance", "mode": "dry-run",
  "db": { "path": "...\\s5-readonly\\sentiment.db", "present": true,
          "access": "mode=ro&immutable=1", "touched": false },
  "input": { "kind": "events-jsonl", "events_file": "...\\events-20261017T053721Z.jsonl",
             "events_sha256": "4d0b6b16d24509d9f76e93f7995a01d74a6facb46f09396954aa3f88537069f3",
             "total_events_available": 117, "events_selected": 10, "max_events": 10 },
  "events": [ { "event_id": "evt-717c8f769afc6936", "size": 4, "records": 4, "ambiguous_members": 2 },
              { "event_id": "evt-08747290dd599dba", "size": 2, "records": 2, "ambiguous_members": 0 },
              { "event_id": "evt-107d23805c6ed41b", "size": 2, "records": 2, "ambiguous_members": 2 } ],
  "no_model": false, "min_interval_seconds": 8.0,
  "network_requests": 0,
  "model": { "enabled": true, "max_model_calls": 20, "per_event_max_calls": 2,
             "total_max_calls": 20, "planned_max_calls": 20 },
  "would_write": ["stance/stance-<UTC时间戳>.jsonl", "stance/summary-<UTC时间戳>.json"]
}
```

即：可处理 **117** 个事件，按规模取前 **10**（1 个 size=4、9 个 size=2），每事件最多 **2** 次模型调用、预计不超过 **20** 次，**0 次网络请求**。

### 9.3 `--no-model`（真实运行，纯规则，0 次模型调用，退出码 `0`）

```json
{
  "status": "ok", "degraded": false, "events_processed": 10, "events_skipped": [],
  "stance_distribution": { "支持": 0, "反对": 0, "中立": 1, "无关": 9 },
  "method_distribution": { "model": 0, "rules": 10 },
  "supported_claim_total": 0, "dropped_claim_total": 0, "drop_reasons": {},
  "model": { "enabled": false, "calls": 0, "tokens": null },
  "outputs": { "jsonl": "...\\out-nomodel\\stance-20261017T061241Z.jsonl", "jsonl_bytes": 1340,
               "jsonl_sha256": "85deb8c48072ef0b12db1b48d51a3aa8871d354efe60f506c833c70719c77700",
               "summary": "...\\summary-20261017T061241Z.json" }
}
```

jsonl 首 3 行（真实落盘字节，`outputs.jsonl_sha256` 与文件字节回读一致）：

```json
{"dropped_claims":0,"event_id":"evt-717c8f769afc6936","evidence":[],"method":"rules","size":4,"stance":"无关","supported_claims":0}
{"dropped_claims":0,"event_id":"evt-08747290dd599dba","evidence":[],"method":"rules","size":2,"stance":"无关","supported_claims":0}
{"dropped_claims":0,"event_id":"evt-107d23805c6ed41b","evidence":[],"method":"rules","size":2,"stance":"无关","supported_claims":0}
```

10 个事件里唯一判 `中立` 的是「妻子监控取证丈夫出轨不被法院采纳」（女性词表与男性词表同时命中，规则表走「都命中→中立」）；其余 9 个标题没有命中性别词表 → `无关`。**这是纯规则降级的结果，不是模型结论；`method` 全为 `rules`，`supported_claims` 全为 0。**

`--no-model --events-from-db --limit 200 --max-events 50`（现场聚类、纯规则）：50 个事件 → `中立 7 / 无关 43`，`method` 全 `rules`，退出码 `0`；本语料标题内**没有**触发规则表的 `支持`/`反对` 分支（词表过粗，见 9.8 的定向反例）。

### 9.4 A → B → 立场：本地夹具实跑（**非真实模型**）

用本机回环夹具服务器（`fixture-ab.py`：按 `role` 分派；A 出 3 条 claim，其中 1 条引用非原文应被丢弃，且 2 条有效 claim 的 `start/end` **故意给错**以验证 `normalize_output` 对齐；B 首条 `supported`、其余 `partially_supported`），以 `OPENCODE_GO_V1_FIXTURE=1`、`OPENCODE_GO_V1_ENDPOINT=http://127.0.0.1:8097/v1/chat/completions`（**未使用任何真实凭据**）实跑 3 个事件：

```json
{
  "status": "ok", "degraded": false, "events_processed": 3,
  "stance_distribution": { "支持": 1, "中立": 1, "无关": 1 },
  "method_distribution": { "model": 3, "rules": 0 },
  "supported_claim_total": 3, "dropped_claim_total": 6,
  "drop_reasons": { "a_validation": 3, "b_partially_supported": 3 },
  "model": { "enabled": true, "calls": 6, "max_calls": 20, "budget_exhausted": false,
             "errors": [], "tokens": { "prompt_tokens": 666, "completion_tokens": 132,
                                       "total_tokens": 798 } }
}
```

落盘 jsonl（真实字节）：

```json
{"dropped_claims":2,"event_id":"evt-717c8f769afc6936","evidence":[{"claim_id":"c1","quotes":["徐艺洋："],"targets":["female"]}],"method":"model","size":4,"stance":"支持","supported_claims":1}
{"dropped_claims":2,"event_id":"evt-08747290dd599dba","evidence":[{"claim_id":"c1","quotes":["张镇麟1"],"targets":["both"]}],"method":"model","size":2,"stance":"中立","supported_claims":1}
{"dropped_claims":2,"event_id":"evt-107d23805c6ed41b","evidence":[{"claim_id":"c1","quotes":["傅首尔新"],"targets":["unspecified"]}],"method":"model","size":2,"stance":"无关","supported_claims":1}
```

这组结果证明：**A→`normalize_output`→`validate`→B→只留 supported→按规则表出立场** 整条链路可用；夹具让三次 A 的 `target` 轮换（`female`→`both`→`unspecified`），立场分别落到 `支持`/`中立`/`无关`，与第五节规则表逐条吻合；每条都有通过校验的引用作为 `evidence.quotes`。**这是桩数据，只证明代码路径与规则表可用，不能当作真实模型判立场质量的证据。**

### 9.5 `validate` 逐条丢弃 + 偏移对齐（夹具 + 内存定向）

同一夹具轮里，3 个事件各丢弃 3 条：`a_validation` 3（引用非原文）＋ `b_partially_supported` 3；而每次 A 都产出了 2 条有效 claim 并被 B 复核（`supported_claim_total = 3`，每事件 1 条）。内存定向（`probe-result.json`，见 9.8）另证：

- 引用 `quote` 为原文逐字子串但 `start/end` 给错时，`normalize_output` 把偏移改写成 `text.find(quote)`，**全部 8 条引用对齐后 `text[start:end] == quote` 均为真**；
- 引用非原文的 claim 被 `a_validation` 丢弃，**不放宽校验**；
- 外层结构损坏（`{"items":[]}`、21 条 claim、空 `claims`）均记 `a_structure` 丢弃整批。

### 9.6 预算超限即停（夹具实跑）

`--max-events 5 --max-model-calls 3`（同一夹具）：

```json
{
  "status": "ok", "events_processed": 2,
  "events_skipped": [ {"event_id": "…", "size": 2, "reason": "max_model_calls"}, … ],
  "stance_distribution": { "支持": 1, "中立": 0, "反对": 0, "无关": 1 },
  "drop_reasons": { "a_validation": 2, "b_partially_supported": 1, "b_skipped_budget": 1 },
  "model": { "calls": 3, "max_calls": 3, "budget_exhausted": true }
}
```

第 1 个事件用满 A+B（2 次），第 2 个事件只够 A（第 3 次），B 记 `b_skipped_budget`；剩余 3 个事件进 `events_skipped` 并明标 `max_model_calls`。**汇总显式声明预算已用尽，不静默。**

### 9.7 HTTP 500 与结构损坏：保守降级 + 保留真实原文

- 固定回 **500** 的夹具（`fixture-500.py`），`--max-events 2`：

```json
{
  "status": "ok", "degraded": false,
  "stance_distribution": { "支持": 0, "反对": 0, "中立": 0, "无关": 2 },
  "model": { "calls": 2,
    "errors": [ { "event_id": "evt-717c8f769afc6936", "role": "A", "type": "ProductionHTTPError",
                  "status": 500,
                  "body_prefix": "{\"error\": {\"type\": \"server_error\", \"message\": \"fixture forced failure 500 for S5 stance\"}}",
                  "message": "HTTP 500: {\"error\": …}" } ] },
  "model.tokens": null }
```

**真实状态码 `500` 与响应体前 200 字被完整保留**；两个事件各 1 次请求，**无重试**（该夹具本轮只被 POST **2** 次，已用其 `/posts` 计数实证）。模型失败的事件按「无证据→无关」处理；`tokens` 为 `null`（不冒充 0）。注意 `degraded` 只统计 `model.errors` 之外的告警——本轮 HTTP 失败记在 `model.errors` 里（见第十节口径）。

- HTTP 200 但模型输出结构损坏（`fixture-badstruct.py` 回 `{"items":[]}`），`--max-events 2`：`drop_reasons = {"a_structure": 2}`、两个事件 `无关`、`calls = 2`，同样不重试、不静默。

### 9.8 规则表与丢弃逻辑：内存定向检查（0 网络）

`probe-result.json`（直接 import 工具模块，用合成 packet/输出调用真实函数）：

- `stance_from_claims` 九例：`[] → 无关`、`unspecified → 无关`、`both → 中立`、`male+female → 中立`、`female+assertion → 支持`、`male+rebuttal → 反对`、`assertion 多于 quotation → 支持`、`assertion 与 rebuttal 各一 → 中立`、`仅 quotation → 中立`——与第五节规则表逐条吻合；
- `rules_stance` 五例：无词命中 `→ 无关`、仅女性+支持词 `→ 支持`、仅男性+反驳词 `→ 反对`、男女都命中 `→ 中立`、单侧中性 `→ 中立`；
- `keep_valid_claims`：3 条 A 输出 → 保留 2、丢弃 1（`a_validation`）；`keep_reviews`：覆盖齐全 → 2 条，漏覆盖 → 整批 `None`（`b_validation`）；`admission`：只留 `supported`，出 `stance=支持`，`evidence` 引用正确。

### 9.9 只读性实测

- 真实库与其复制件 SHA-256 **都是 `401bf865a21ef91b8924b48ed1aa568c9df7c4688d574cbfc1d5cfa1a954aa51`**（512000 字节），运行前后**逐字节未变**（driver 在全部用例前后各算一次哈希）。
- 复制件所在目录**只有 `sentiment.db`**，未多出 `-shm`/`-wal` 旁文件（复制件无 `-wal`，走 `mode=ro&immutable=1`）。
- `git status --short` 只有 `?? tools/annotate_stance.py`；无 `stance/`、无 `events/`、无 `__pycache__`、无 `sentiment.db`。

### 9.10 参数/输入边界与失败路径（不产生上游请求）

| 场景 | 命令 | 观察结果（退出码） |
| --- | --- | --- |
| 默认产物目录无 `events/` | `--dry-run --db <只读库>` | `status:"blocked"`、`stage:"input"`、`message` 提示用 `--events` 或 `--events-from-db`（`2`） |
| `--max-events` 越界 | `--dry-run --max-events 51` | `stage:"args"`、`--max-events 必须在 1-50 之间`（`2`） |
| `--max-model-calls` 越界 | `--dry-run --max-model-calls 101` | `stage:"args"`、`--max-model-calls 必须在 0-100 之间`（`2`） |
| `--limit` 越界 | `--dry-run --events-from-db --limit 0` | `stage:"args"`（`2`） |
| `--events` 与 `--events-from-db` 同用 | `--dry-run --events <f> --events-from-db` | `stage:"args"`、`互斥`（`2`） |
| `--events` 文件不存在 | `--dry-run --events <不存在>` | `stage:"input"`（`2`） |
| 库不存在 | `--dry-run --events <f> --db <不存在>` | `stage:"db"`（`2`） |
| 需要模型但缺凭据且未设夹具 | `--events <f> --max-events 2` | `stage:"env"`、`未发送任何请求`（`2`） |

### 9.11 定向检查

| 检查 | 命令 | 观察结果 |
| --- | --- | --- |
| 语法（内存 compile，未落盘 pyc） | `python -B -c "compile(open('tools/annotate_stance.py',encoding='utf-8').read(),…,'exec')"` | `memory compile OK`，退出码 0 |
| 参数风格 | `--dry-run` 输出统一 JSON 报告 | 与 `tools/annotate_relevance.py`/`tools/cluster_events.py` 一致（同 `tool/mode/db/…` 结构） |
| `--min-interval` 生效 | 夹具 `--max-events 2 --min-interval 1` | 整进程墙钟 **1.27 秒**（含 1 次 1 秒事件间隔、4 次夹具调用），说明事件间节流真实生效；默认 8 秒时 3 个事件墙钟 **16.33 秒**（2×8 秒） |

## 十、失败路径

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| `--max-events` / `--max-model-calls` / `--limit` / `--min-interval` 越界、`--events` 与 `--events-from-db` 同用 | 2 | `stage:"args"`，不读库、不发请求 |
| 无 `events/` 产物 / `--events` 文件不存在 / 事件 jsonl 结构损坏 / 成员关联不上库 | 2 | `stage:"input"`，附事件文件与缺失成员；**不伪造 packet** |
| 库不存在 / 只读打开失败 / `SELECT` 失败 | 2 | `stage:"db"`，附库路径与原始异常 |
| 需要模型但缺 `OPENCODE_GO_V1_API_KEY`（且无夹具） | 2 | `stage:"env"`，**未发送任何请求、不写产物**（前置条件失败，不降级成空结果） |
| 某事件 HTTP 非 200（含 400/401/403/429/5xx） | 0（`model.errors` 留痕） | 复用 `analysis.ProductionHTTPError`：**真实状态码 + 响应体前 200 字**；单次请求、不重试；该事件「无证据→无关」 |
| 某事件连接失败（DNS/TLS/超时） | 0（`model.errors` 留痕） | `type:"RuntimeError"`，同一保守降级 |
| 响应非 JSON / 缺 `choices`/`content` / 模型输出非 JSON | 0（`model.errors` 留痕） | 附真实文本前 200 字；该事件「无证据→无关」 |
| A 输出非「仅 claims」/ 条数越界 | 0（`drop_reasons.a_structure`） | 整批丢弃并计数 |
| 单条 claim 引用/字段不合契约 | 0（`drop_reasons.a_validation`） | **逐条**丢弃并计数，不放宽 |
| B 未逐条覆盖 / 结构损坏 | 0（`drop_reasons.b_structure`/`b_validation`） | 整批不予采纳，该事件「无证据→无关」 |
| 预算用尽 | 0（`budget_exhausted:true`） | **超限即停**，剩余事件进 `events_skipped` |
| 产物写入或回读哈希不一致 | 7 | `stage:"write"`，附路径 |

「运行期模型失败」一律**降级并留痕**（保守 `无关` + `model.errors`），不重试、不静默；「前置条件缺失（无凭据）」则**快速失败**、不产生半成品。

## 十一、未实证项（本轮未做，不得当成已完成）

1. **立场规则表未与需求方确认**：`claim.target` 的四种取值（性别指向）与 `context`（引述语境）都不直接表达「支持/反对」方向，第五节的「单侧 + assertion/rebuttal 多数」是**保守占位口径**，不是经确认的产品语义。真实模型输出的 `target`/`context` 分布到手后，需回看该表是否需要调整。
2. **真实模型调用与真实立场结果未取得**（9.4 是夹具桩）。未验证：真实 HTTP 200、真实 `usage`、模型对事件的 A 主张与 B 复核质量、真实 `target`/`context` 分布、真实 `stance_distribution`、真实 token 用量。**留给主线程在产品侧补跑**（可在执行器进程提供 `OPENCODE_GO_V1_API_KEY`，或用 `OPENCODE_GO_V1_FIXTURE=1` 回放）。
3. **`--events` 模式的成员关联依赖**：S4 产物只有 `title`/`url`，本工具需回库按 `(url,title)` 关联；若事件产物与库不是同一快照（或库被换库），会 `stage:"input"` 失败。真实库中同键多候选（多次采集的同一内容）确存在（如 `evt-717c8f769afc6936` 有 2 个成员命中歧义），本工具按 `(fetched_at,id)` 确定性取序并把歧义条数写进 `ambiguous_members`，但**「取哪一个」本身未与需求方确认**。
4. **`--events-from-db` 只用规则阶段聚类**：本工具不替判同花模型预算，边界对按「不同事件」保守处理；与 `cluster_events` 带模型裁量的结果可能不同（本工具**不做**模型判同）。
5. **`--no-model` 关键词表为粗粒度**：本语料 50 个事件里只落到 `中立/无关`（9.3）；`支持`/`反对` 分支仅在 9.8 的合成输入上验证过，未在真实语料触发，也**未与需求方确认**词表内容。
6. **`--max-model-calls` 上限 100 为工具自设**（= `--max-events` 上限 50 × 每事件 2 次）；契约只给了默认 20，未给上限。未与需求方确认。
7. **`--min-interval` 的口径**：本工具解释为「事件之间」节流（同一事件 A、B 之间不等待），与 S3/S4「批次之间」同类；是否应在每次调用之间都等待，未确认。
8. **`--limit ≤ 200` 仅用于 `--events-from-db`**：沿用 `cluster_events` 自设上限，未与需求方确认。
9. **`--out-dir` 为本次新增参数**：默认仍是契约要求的 `<仓库>/stance/`，但该目录**不在 `allowed_paths`**，故本轮真实运行全部写 Temp；`stance/` 是否加入 `.gitignore`（`.gitignore` 不在允许改动范围）待产品确认。
10. **未接入产品 UI / `server.py`**：本工具是独立 CLI，未接入任何 HTTP 路由，未改 `app.js`；立场结果**未回写数据库**（本任务明确只读）。
11. **`open_readonly()` 的 `mode=ro` 回退分支未触发**（复制件无 `-wal`），只有静态依据；并发「服务正在写库」场景未测。
12. **`degraded` 字段口径**：当前只在 `model.errors` 之外出现告警时才置真；HTTP/连接失败已逐条记入 `model.errors`（9.7）。该字段的语义未与需求方统一。

## 十二、已知风险与边界

- **只读**：仅 `SELECT records/feeds`；只读 URI 打开（默认 `immutable`），不建表、不改表结构、不写 `records`，也不产生任何平台写动作。
- **凭据**：只从环境变量读取（由 `analysis.production_adapter` 承担），值不落盘、不打印、不入库；错误报告只带环境变量**名字**。
- **成本**：每个事件最多 2 次请求（A、B 各一），每次最多一个 HTTP 请求；`--max-model-calls` 硬顶；不重试、不并发、无续轮；事件间按 `--min-interval` 节流。
- **口径**：`stance` 是**由有引用支撑的 claim 按规则表推出的表达倾向**，不是人群态度、评论立场或热度；本工具**不推断群体立场**、不算评论立场比例、不构建评论树、不采评论正文；`--no-model` 结果明确标 `method="rules"`。
- 本工具不写桥的状态文件、不启动服务、不订阅定时任务。

## 十三、本轮改动范围

相对基线 `dded5eb`，本 worktree 只新增两个文件（均在允许路径内）：

- `tools/annotate_stance.py`（新增，SHA-256 `130ad5b7cf284ca0494727001a4e93351a4dea0074bad65d11b65f26ee3e3784`，35366 字节）
- `docs/S5-stance.md`（本文）

`analysis.py`、`storage.py`、`tools/annotate_relevance.py`、`tools/cluster_events.py` 及数据库**零改动**；未提交、未切分支、未修改 `.git`。

## 十四、交付前自检（`selfcheck.py --kind annotator`，离线）

命令：

```
python -B "<SELFCHECK>" --kind annotator --worktree . --allow "tools/,docs/" --artifacts "tools/annotate_stance.py,docs/S5-stance.md"
```

完整 JSON（verdict=pass）：

```json
{
 "kind": "annotator",
 "worktree": "D:\\DSH\\output\\2026-10-16\\2026-10-16-18-03-28\\_s5\\store\\tasks\\sentiment-s5-stance-20261017\\attempts\\attempt-a9722340e5494bc3849858856ac5aa63\\workspace",
 "allow": [
  "tools/",
  "docs/"
 ],
 "artifacts": [
  "tools/annotate_stance.py",
  "docs/S5-stance.md"
 ],
 "changed": [
  "docs/S5-stance.md",
  "tools/annotate_stance.py"
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

