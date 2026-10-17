# S1 采集适配器：OpenCLI 多来源公开热榜/推荐流（只读）+ 内建限流

实现：`tools/collect_opencli.py`（**当前交付版本：39140 字节，SHA-256 `863fea3f02115bf2d557c2505403cebaafc32b9f20f2d55ed1eb2d61c3556bb3`**；其直接前身 S1-C 版 = **37673 字节，SHA-256 `1e786d488c9283c33059469ad3074f2796a285a3a86b338c4427fc74cc743c04`**，即独立审查报告 `_s1/audit-s1c-model.md` 所针对的字节；两版差异见 7.7 与第十一节）。

状态：

- **8 个端点已实机 `--dry-run` 通过**：头条 hot / 头条 recommend / 微博 hot / 贴吧 hot / 36氪 hot（沿用上轮）+ **知乎 hot / B站 hot / 小红书 feed（本轮新增）**；
- **限流已做进采集器本体**（四项参数 + 平台级冷却账 + 风控熔断），不再靠人手工 sleep；节流与跳过路径有两次真实调用证据（第七节）；
- **条目级坏账已改为 fail-closed（S1-C' 修复）**：平台条目缺 `last_attempt_at` 或不可解析 → 拒绝运行（`status="failed"`、`error.stage="throttle"`、退出码 `7`、`request_count=0`），**不再放开限流**；见 §6.2 与 §7.7；
- **非 `--dry-run` 的真实写库路径已实机跑过一轮**（S1-C 轮，在临时镜像目录内，避免污染 worktree；见 7.3 与 7.5）。本轮 S1-C' **未重跑非 dry-run**（该路径未改动；本轮修的是“读账”阶段，位于写库/写账之前）；
- **抖音（`douyin hashtag hot` / `douyin search`）当前不可用，未写入端点表**；证据性质见第五节末与第九节；
- 本轮**未**接 UI、未启动服务、未新增定时任务、未做全量/压力测试。

本文记录：用了哪个命令、完整参数、执行时间、返回条数、逐源原始字段到本地 schema 的映射、限流与冷却账设计、失败路径、实跑证据和未实证项。

## 一、本轮交付范围（做了什么 / 没做什么）

| 项 | 本轮 |
| --- | --- |
| 限流四项参数（`--min-interval` / `--gap-seconds` / `--max-per-run` / `--throttle-file`） | 新增，可重复传 `--endpoint` 让一轮覆盖多平台 |
| 平台级冷却账（JSON，默认 `<仓库根>/.collector-throttle.json`） | 新增；冷却中的平台直接跳过并记 `reason="throttled"`；只有真正发起采集（非 dry-run）才写账 |
| 风控熔断（非 0 退出 / `BROWSER_CONNECT` / `AUTH_REQUIRED` / `COMMAND_EXEC` / 空数组 / HTTP 429·5xx） | 新增；出现即停止本轮且不重试，保留真实错误码与原文 |
| `--dry-run` 语义 | 强化为：**不打开/创建数据库、不写节流账**（只读账），并如实拒绝非法参数 |
| 新端点（知乎 / B站 / 小红书） | 新增 3 个，均按“先实跑、再写入表”的顺序落地 |
| 抖音 | **不入表**，如实记为当前不可用（见五、九节） |
| 真实写库路径 | 在临时镜像目录内实跑 1 轮（2 个端点、2 条记录），见 7.3 / 7.5；**worktree 内零副作用** |
| UI 接入 / 服务启动 / 定时采集 / 评论与二级回复 | 未做（与既有边界一致） |

## 二、复用清单（先复用，后新写）

| 复用对象 | 真实路径 / 版本 | 用途 | 许可与使用方式 |
| --- | --- | --- | --- |
| OpenCLI 站点适配器（头条/微博/贴吧/36氪） | `C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\clis\{toutiao,weibo,tieba,36kr}\*.js`，npm `@jackwener/opencli` **1.8.8** | 取数：公开热榜、公开推荐流 | Apache-2.0。**只作为外部 CLI 调用（subprocess），不复制其代码** |
| OpenCLI 站点适配器（知乎/B站/小红书，本轮新增复用） | 同目录 `clis\{zhihu,bilibili,xiaohongshu}\{hot.js,feed.js}`（**未改动这些文件**） | 取数：知乎热榜、B站热门榜、小红书首页推荐流 | 同上：只调用 CLI，不复制代码 |
| 项目既有代码 | `storage.py`：`connect/start_run/import_batch/save_snapshot/publication/canonical_url/normalized_text/digest/json_bytes` | 写库、快照、时间质量、URL 规范化、正文哈希 | 直接复用，未新开数据库、未改表结构 |
| 项目既有代码 | `server.py`：`classify` | `records.sentiment` 的粗略情感标签 | 复用同一份词典；仍是未复核初筛 |
| 项目既有代码 | `tools/collect_opencli.py`（基线 `fdb48dc` 版本） | 端点声明表、CLI 定位、错误信封转换、`--dry-run` | 本轮在其上扩展，未另起实现 |

**必须新写的部分及理由**（其余都不重复实现）：

1. **限流与冷却账**：上游 OpenCLI 没有任何“同一平台最小间隔/跨平台错峰/一轮总量”机制，也没有本项目可用的冷却账；这部分是本项目特有的调度语义，只能自己写。
2. **声明式映射表 `ENDPOINTS` 的扩展**：上游字段名到项目 `records` 的对应关系是本项目特有的；各来源字段名互不相同（微博标题在 `word`、B站 ID 在 `bvid`、小红书 ID 在 `id`、知乎无摘要），必须逐源声明。
3. **多端点到单轮 `results[]` 报告 + 风险信号归一**：把上游退出码/JSON 错误信封转换成项目统一的可审计失败报告。
4. CLI 定位（`node` / OpenCLI `main.js` 探测）：OpenCLI 未提供“找到入口”的 API。

未使用 MediaCrawler；未新增登录、未读取或复制任何 cookie；未新增第三方依赖（仅 Python 标准库 + 上表本机已装组件）。

## 三、命令与参数

```
python -B tools/collect_opencli.py [--endpoint NAME]... [--limit N] [--dry-run]
                                   [--min-interval S] [--gap-seconds S] [--max-per-run N]
                                   [--throttle-file PATH]
                                   [--group-id G] [--db PATH] [--node PATH] [--opencli PATH] [--timeout S]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--endpoint` | `hot` | 读端点白名单，**可重复传多次**（一轮内按顺序采多个平台）。argv 由常量拼装，无法传入任何写动作 |
| `--limit` | `3` | 1–50，**每个端点**的条数；契约要求单次 ≤3 页或 ≤50 条 |
| `--dry-run` | 关 | 只采集并打印映射结果：**不打开/创建数据库、不写节流账**（只读账） |
| `--min-interval` | `1800` | 秒；同一平台两次采集的最小间隔（`0` = 关闭冷却） |
| `--gap-seconds` | `25` | 秒；一轮内两个**都会发起调用**的平台之间的错峰间隔 |
| `--max-per-run` | `80` | 条；一轮总量上限（按已返回条数累计，超出的端点跳过并记 `reason="max_per_run"`） |
| `--throttle-file` | `<仓库根>/.collector-throttle.json` | 平台冷却账 JSON 路径 |
| `--group-id` | 按端点 | 来源分组标识（落 `feeds.name`），**仅单端点运行可传**；多端点传该参数直接报错（避免歧义） |
| `--db` | `<仓库>/sentiment.db` | 既有证据库路径；`--dry-run` 下只读 `is_file()`，绝不打开/创建 |
| `--node` / `--opencli` | 自动探测 | 依次尝试环境变量 → PATH → 本机托管目录 → 常见安装路径 |
| `--timeout` | `60` | 单次调用超时秒数；超时即停止，不重试 |

实际拼装并执行的命令（每次成功采集 `results[].call.argv`，`request_count` 按成功端点数累计）：

```
<node> <opencli dist/src/main.js> toutiao    hot       --limit N -f json   # --endpoint hot
<node> <opencli dist/src/main.js> toutiao    recommend --limit N -f json   # --endpoint recommend
<node> <opencli dist/src/main.js> weibo      hot       --limit N -f json   # --endpoint weibo-hot
<node> <opencli dist/src/main.js> tieba      hot       --limit N -f json   # --endpoint tieba-hot
<node> <opencli dist/src/main.js> 36kr       hot       --limit N -f json   # --endpoint 36kr-hot
<node> <opencli dist/src/main.js> zhihu      hot       --limit N -f json   # --endpoint zhihu-hot
<node> <opencli dist/src/main.js> bilibili   hot       --limit N -f json   # --endpoint bilibili-hot
<node> <opencli dist/src/main.js> xiaohongshu feed     --limit N -f json   # --endpoint xiaohongshu-feed
```

报告结构（顶层）：`status` / `dry_run` / `endpoints` / `limit` / `min_interval` / `gap_seconds` / `max_per_run` / `throttle` / `access` / `request_count` / `returned_total` / `node` / `opencli` / `results[]` / `db` / `notes` / `collected_count` / `throttled_count`。逐端点结果在 `results[]` 内（`endpoint` / `platform` / `group_id` / `feed_url` / `field_map` / `status` / `reason` / `returned_count` / `call` / `records` / `throttle`）。

`status` 取值与含义：`ok`（至少一个端点采集成功）/ `throttled`（没有任何采集，但有端点被冷却跳过）/ `skipped` / `failed`（任一端点失败，退出码为第一个失败码）。**被冷却跳过不是失败，退出码仍为 0**；调用方应看 `results[].reason`。

## 四、声明式映射表（端点 → 子命令 / 平台 / 字段 / feed）

`tools/collect_opencli.py` 的 `ENDPOINTS` 是唯一的数据驱动来源；映射逻辑里**没有任何按站点分支的 if/else**，`map_rows()` 只读表里的 `fields`。表在导入时自检键完整性（`_validate_endpoints()`）。

| 端点 | opencli 子命令 | 冷却平台键 | feed 分组名 | feed_url（分组标识，见未实证项 6） |
| --- | --- | --- | --- | --- |
| `hot` | `toutiao hot` | `toutiao` | `toutiao-hot` | `https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc` |
| `recommend` | `toutiao recommend` | `toutiao` | `toutiao-recommend` | `https://www.toutiao.com/api/pc/feed/` |
| `weibo-hot` | `weibo hot` | `weibo` | `weibo-hot` | `https://s.weibo.com/top/summary` |
| `tieba-hot` | `tieba hot` | `tieba` | `tieba-hot` | `https://tieba.baidu.com/hottopic/browse/topicList` |
| `36kr-hot` | `36kr hot` | `36kr` | `36kr-hot` | `https://36kr.com/hot-list/catalog` |
| `zhihu-hot` | `zhihu hot` | `zhihu` | `zhihu-hot` | `https://www.zhihu.com/hot` |
| `bilibili-hot` | `bilibili hot` | `bilibili` | `bilibili-hot` | `https://www.bilibili.com/v/popular/all` |
| `xiaohongshu-feed` | `xiaohongshu feed`（**推荐流，不是热榜**） | `xiaohongshu` | `xiaohongshu-feed` | `https://www.xiaohongshu.com/explore` |

`fields`（本地字段 → 上游原始字段名，`None` = 上游没有该字段，一律留 null/空串，**不从 URL、标题或其它字段反推**）：

| 本地字段 | `hot` | `recommend` | `weibo-hot` | `tieba-hot` | `36kr-hot` | `zhihu-hot` | `bilibili-hot` | `xiaohongshu-feed` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `title` | `title` | `title` | **`word`** | `title` | `title` | `title` | `title` | `title` |
| `url` | `url` | `url` | `url` | `url` | `url` | `url` | `url` | `url` |
| `summary` | `query` | `abstract` | `category` | `description` | `None` | `None` | `None` | `None` |
| `source_id` | `group_id` | `group_id` | `None` | `None` | `None` | `None` | **`bvid`** | **`id`** |
| `published_at` | `None` | `published_at` | `None` | `None` | `None` | `None` | `None` | `None` |

`summary_empty_when_same_as_title`：`hot` 为 `true`（头条热榜的 `query` 就是标题本身，相同即置空），其余端点为 `false`。

## 五、逐源字段映射（实测）

**上游实测原始字段**（2026-10-16，opencli 1.8.8，`-f json`；头条两源沿用首轮实测，本轮未重跑）：

| 端点 | 上游原始字段（实测所见） |
| --- | --- |
| `toutiao hot` | `rank`、`title`、`query`、`group_id`、`hot_value`、`label`、`image_url` |
| `toutiao recommend` | `rank`、`title`、`abstract`、`group_id`、`published_at`、`comments`、`source`、`tag`、`image_url` |
| `weibo hot` | `rank`、`word`、`category`、`hot_value`、`label`、`url`（**无 `title`**） |
| `tieba hot` | `rank`、`title`、`description`、`discussions`、`url` |
| `36kr hot` | `rank`、`title`、`url`（实测仅此三字段） |
| `zhihu hot` | `rank`、`title`、`heat`、`answers`、`url`（**无摘要字段**；问题号只出现在 `url` 里） |
| `bilibili hot` | `rank`、`title`、`author`、`play`、`danmaku`、`bvid`、`url`（**无摘要、无发布时间**） |
| `xiaohongshu feed` | `id`、`title`、`type`、`author`、`likes`、`url`（**推荐流**，无摘要、无发布时间；`url` 自带 `xsec_token` 签名参数） |

上游每行先整体序列化进快照 `raw_xml`（`json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",",":"))`），再投影到 `records`：

| 本地 `records` 列 | 各来源取值 | 处理方式（复用哪个既有函数） |
| --- | --- | --- |
| `id` | — | `storage.import_batch` 生成 `record-<uuid4>` |
| `feed_id` | 按 `--group-id`（默认见上表） | 按 `feeds.url`（端点 `feed_url`）查既有来源；缺失时按 `server.py` 相同 INSERT 语句建行 |
| `source_id` | 头条两源取 `group_id`；B站取 `bvid`；小红书取笔记 `id`；微博/贴吧/36氪/知乎 = `null` | 上游没有独立 ID 字段的（微博/贴吧/36氪/知乎）**不**从 URL 反推，保持“缺字段留 null” |
| `title` | 见上表 | `storage.normalized_text`（去 HTML、实体解码、折叠空白） |
| `summary` | 见上表；36氪/知乎/B站/小红书无对应字段 → `""` | 同上。`records.summary` 为 NOT NULL，故缺字段用空串如实表示，不填猜测文本 |
| `url` | 见上表 | 原始值入库（不裁剪 query；小红书签名参数原样保留） |
| `canonical_url` | 各来源 `url` | `storage.canonical_url`（仅协议/主机大小写、默认端口、空路径、片段；保留 query） |
| `content_sha256` | `[title, summary]` | `storage.digest(storage.json_bytes(...))`，精确正文去重键 |
| `raw_sha256` / `raw_path` | 整行（含 `raw_xml`） | `storage.save_snapshot` → `snapshots/<sha256>.json`，写后回读校验 |
| `published_at` | 仅头条 recommend 有；其余恒 `null` | `storage.publication`；非法/无时区→`null` |
| `published_raw` | 同上（原样） | 原样 |
| `fetched_at` | 观测时间 | `storage.now()`（UTC） |
| `time_quality` | 八源实测均为 `missing`（头条 recommend 为 `valid`） | `storage.publication`：`valid/missing/invalid/timezone_missing` |
| `canonical_id` / `dedup_reason` | — | `exact-text-v1` 规则：相同正文复用，否则 `independent` |
| `run_id` | — | `storage.start_run(db, feed_id, "opencli-<endpoint>", limit)`（如 `opencli-zhihu-hot`） |
| `sentiment` | — | `server.classify`（未复核词典粗筛，不代表准确情感分析） |

**未映射到本地的上游字段**（只存在于 `raw_xml` 快照里，**不写入任何本地计数列**）：

| 端点 | 未映射字段 | 原因 |
| --- | --- | --- |
| `toutiao hot` | `rank`、`hot_value`、`label`、`image_url` | 热度值/标记非评论数，避免用未核实口径冒充评论总数 |
| `toutiao recommend` | `rank`、`comments`、`source`、`tag`、`image_url` | 同上（`comments` 口径未核实） |
| `weibo hot` | `rank`、`hot_value`、`label` | `hot_value` 是热度值不是评论数；`label` 是「热/新」标记 |
| `tieba hot` | `rank`、`discussions` | 形如 `"197.3W实时讨论"`：带单位的近似实时讨论量，既不是评论总数也不是精确值 |
| `36kr hot` | `rank` | 榜单名次 |
| `zhihu hot` | `rank`、`heat`、`answers` | `heat` 是热度展示串、`answers` 是回答数（口径未核实），均不当评论总数 |
| `bilibili hot` | `rank`、`author`、`play`、`danmaku` | 播放/弹幕不是评论数；`author` 非摘要 |
| `xiaohongshu feed` | `type`、`author`、`likes` | `likes` 是点赞展示值，**不得冒充评论总数**；`type` 是笔记形态 |

`rank` 仅作为 dry-run 预览的展示字段（`records` 无此列）；小红书推荐流实测**没有 `rank`**，预览里如实为 `null`。

### 抖音：当前不可用，不入端点表

- 命令：`douyin hashtag hot`、`douyin search`（OpenCLI 适配器文件真实存在：`clis\douyin\hashtag.js`、`clis\douyin\search.js`，均为 `access=read`）。
- 实测结果：两者均返回 **`COMMAND_EXEC: Detached while handling command`**，因此**不写入 `ENDPOINTS`**，也不会被 `--endpoint` 白名单接受。
- **证据性质（未实证）**：本错误原文来自任务契约中给出的既有探测记录，**本会话未复跑抖音**（既不把它当可用，也不把它当“本轮实测不可用”）。要把它记为“本轮实测不可用”，需要单独授权后再跑一次并保留真实 stdout/stderr 原文。

## 六、内建限流设计

### 6.1 一轮的执行顺序

1. 参数校验（非法值直接报错，**不发起任何上游请求**）；
2. 定位 `node` / OpenCLI `main.js`；
3. **读冷却账**（不存在=空账；存在但不可读/结构不对 → 报错停止，**不静默放行限流**）；结构检查分**顶层**（`platforms` 缺失或非对象）与**条目级**（任一条目不是对象、或 `last_attempt_at` 缺失/不可解析）两层，两层都 fail-closed；
4. 逐个端点：先判冷却 → 再判本轮余量 → 需要错峰则 `sleep(--gap-seconds)` → 调用上游 → 映射字段 → **立即写账**（仅非 dry-run）；
5. 非 dry-run 且本轮有采集结果时，才打开证据库并把每个端点的行按 `storage.import_batch` 写库；
6. 输出 `results[]` 报告与退出码。

### 6.2 平台级冷却账

- 键是**平台**（`toutiao` / `weibo` / `tieba` / `36kr` / `zhihu` / `bilibili` / `xiaohongshu`），不是端点：同一平台的 `hot` 与 `recommend` 共用一条冷却记录。
- 判定使用 `last_attempt_at`（**失败也算一次尝试**，即失败后同样进入冷却，避免对出问题的站点连打）；窗口内 → `status="skipped"`、`reason="throttled"`，并附 `seconds_since_last` / `seconds_remaining` / `last_status` / `last_endpoint`，且**该端点 `call=null`（未发起上游请求）**。
- 只有真正发起采集（非 `--dry-run`）才写账；`--dry-run` 只读账（`throttle.read_only=true`、`written=false`），文件字节与 mtime 前后一致（见 7.3）。
- 写账用“同目录临时文件 + `os.replace`”，避免半截文件被下一次运行当成有效账。
- 账文件结构（实测内容见 7.3）：

```json
{
  "version": 1,
  "updated_at": "<UTC ISO8601>",
  "platforms": {
    "<platform>": {
      "last_attempt_at": "<UTC ISO8601>",
      "last_status": "ok",
      "last_endpoint": "<端点名>",
      "last_signal": null,
      "last_error": null,
      "last_returned_count": 1,
      "last_success_at": "<UTC ISO8601>"
    }
  }
}
```

- **坏账一律 fail-closed（S1-C' 修复）**：`load_throttle()` 在返回账之前逐条检查 `platforms`——**任一条目不是对象、或 `last_attempt_at` 缺失/不可解析，都视为坏账**：`status="failed"`、`error.stage="throttle"`（附 `platform`）、退出码 `7`、**`request_count=0`**（不发起任何上游请求）。顶层结构错误（`platforms` 缺失/非对象）与之同码。**不存在“坏账 → 无冷却 → 继续采集”的路径。**
- `cooldown_of()` 另有一道方向安全的第二道防线：**条目存在但不是对象、或没有可解析的 `last_attempt_at`** 时按“冷却中”处理（绝不返回“无冷却”）。上面的逐条检查已让这条分支不可达，保留只为纵深防御。注意“**该平台尚无记录**”（首次采集，`entry is None`）**不是坏账**，照常采集。
- 健壮性提示（非契约项）：账文件若带 UTF-8 BOM（Windows 记事本“另存为 UTF-8”默认会带），`json.loads` 会报错，当前按“账不可读”fail-closed（方向安全，退出码 `7`）。**本轮已实跑核对**（7.7 post-M）：`exit 7`、`request_count=0`、`error.message` 原文 `冷却账不可读，为避免放开限流已停止：Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)`。手写账文件请用**不带 BOM** 的 UTF-8。

### 6.3 风控熔断

出现下列任一信号即**停止本轮且不重试**，并在 `results[].signal` / `results[].error` 里保留真实错误码与原文：

| 信号 | 识别方式 |
| --- | --- |
| 非 0 退出码 | `call.return_code != 0`（标签 `EXIT_CODE_<n>`） |
| `BROWSER_CONNECT` | JSON 错误信封 `error.code`，或 stderr/stdout 原文包含该串 |
| `AUTH_REQUIRED` | 同上 |
| `COMMAND_EXEC` | 同上 |
| 空数组 | 上游返回 `[]`（`stage="empty"`，标签 `EMPTY_RESULT`） |
| HTTP 429 / 5xx | 原文匹配 `\b429\b`，或 `HTTP/status/statusCode` + `5xx` |

其他失败（例如 stdout 不是合法 JSON）同样**中止本轮**（不在未知状态下继续打上游），但 `reason="error"` 而非 `risk_control`。设计取舍：熔断按“该平台 + 本轮”生效，一旦触发就把本轮剩余端点也记为 `reason="round_aborted_after_<signal>"` 并跳过——这比继续打其它站点更保守，属有意为之。

## 七、真实验证（本轮在 worktree 内实跑）

> 小节归属说明：**7.1–7.6 是上一版字节 `1e786d48…`（S1-C）的运行态证据，原样保留未改**；**7.7 是 S1-C' 本轮修复后的定向验证**（真实上游请求 0 次）。

### 7.1 环境与文件指纹

- 工作区（attempt worktree）：`...\attempts\attempt-2b159b8dca2945768c5618a63917b4cc\workspace`，基线 commit **`fdb48dcc9e77609e933233d677d2c409f44d4cb9`**。
- 被测文件 `tools/collect_opencli.py`：**37673 字节，SHA-256 `1e786d488c9283c33059469ad3074f2796a285a3a86b338c4427fc74cc743c04`**（下面 7.2–7.4 的全部输出都在此字节版本上产生）。
- Python `3.12.10`（`C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe`，`PYTHONIOENCODING=utf-8`）；node **v24.18.0**（`C:\Program Files\nodejs\node.EXE`，由采集器自动探测命中）。
- OpenCLI 入口：`C:\Users\Lenovo\.dsh\profiles\desktop\node_modules\@jackwener\opencli\dist\src\main.js`（npm `@jackwener/opencli` **1.8.8**）。
- 语法（内存 compile，不落盘 pyc）：`python -B -c "compile(open('tools/collect_opencli.py',encoding='utf-8').read(),'tools/collect_opencli.py','exec')"` → `memory compile OK`，退出码 0。
- **worktree 副作用核对（运行后）**：`git status --short` 只有 `tools/collect_opencli.py`（写本文档后为两处）；`Test-Path .collector-throttle.json / sentiment.db / snapshots` 全部 `False`。

**引用约定（为节省版面，全节统一）**：三个新端点的 stdout 为原样 JSON，仅把每条 `records[]` 中**固定占位值的 4 个键**（`id` / `feed_id` / `canonical_id` / `raw_path`）与常量说明键 `raw_sha256_method` 折叠为一次图例，其余字段（`raw_sha256` / `title` / `summary` / `url` / `canonical_url` / `source_id` / `published_at` / `published_raw` / `time_quality` / `rank`）逐字保留；顶层与 `results[]` 头部字段逐字保留。折叠键的占位值为：`id="(写库时由 storage.import_batch 生成 record-<uuid4>)"`、`feed_id="(写库时按来源分组解析/创建)"`、`canonical_id="(写库时按 exact-text-v1 精确正文规则分配)"`、`raw_path="snapshots/<raw_sha256>.json"`、`raw_sha256_method="sha256(storage.json_bytes(row))，与 save_snapshot 落盘字节一致"`。

### 7.2 三个新端点各一次 `--dry-run --limit 3`（三次调用起始时间间隔 ≥32 秒）

| # | 端点 | 命令 | 上游调用 UTC（起→止） | 退出码 | stderr | 返回条数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `zhihu-hot` | `python -B tools/collect_opencli.py --endpoint zhihu-hot --dry-run --limit 3` | `15:27:12.756289` → `15:27:16.066456`（3.313s） | `0` | 空 | **3** |
| 2 | `bilibili-hot` | 同上，`--endpoint bilibili-hot` | `15:27:48.184068` → `15:27:51.038714`（2.844s） | `0` | 空 | **3** |
| 3 | `xiaohongshu-feed` | 同上，`--endpoint xiaohongshu-feed` | `15:28:23.157557` → `15:28:27.803133`（4.640s） | `0` | 空 | **3** |

三次之间的真实间隔：**35.43s**（#1→#2）与 **35.0s**（#2→#3），均 ≥30 秒。三次报告共同点：`status="ok"`、`dry_run=true`、`request_count=1`、`throttle={"file": "<仓库>/.collector-throttle.json","state":"absent","read_only":true,"written":false}`、`db={"present":false,"touched":false}`、`node="C:\\Program Files\\nodejs\\node.EXE"`。

**#1 `zhihu-hot` 真实 stdout（原文）**：

```json
{
  "collector": "s1-opencli", "status": "ok", "dry_run": true, "endpoints": ["zhihu-hot"],
  "limit": 3, "min_interval": 1800, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\workspace\\.collector-throttle.json", "state": "absent", "read_only": true, "written": false },
  "access": "read", "request_count": 1, "returned_total": 3,
  "node": "C:\\Program Files\\nodejs\\node.EXE",
  "opencli": "C:\\Users\\Lenovo\\.dsh\\profiles\\desktop\\node_modules\\@jackwener\\opencli\\dist\\src\\main.js",
  "results": [
    { "endpoint": "zhihu-hot", "platform": "zhihu", "group_id": "zhihu-hot",
      "feed_url": "https://www.zhihu.com/hot",
      "field_map": { "title": "title", "url": "url", "summary": null, "source_id": null, "published_at": null },
      "limit": 3, "status": "ok", "reason": "collected", "gap_waited_seconds": 0.0, "returned_count": 3,
      "call": { "endpoint": "zhihu-hot",
        "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "zhihu", "hot", "--limit", "3", "-f", "json"],
        "cwd": "...\\attempt-2b159b8dca2945768c5618a63917b4cc\\workspace", "timeout_seconds": 60,
        "started_at": "2026-10-16T15:27:12.756289+00:00", "finished_at": "2026-10-16T15:27:16.066456+00:00",
        "duration_seconds": 3.313, "return_code": 0 },
      "records": [
        { "raw_sha256": "02d044cf1e0ce370e61528d8b4f80d000a7a1bfa89dfb06d9961e1bfbefbbcda",
          "title": "美国肯德基 2027 年起将改用可口可乐，背后是什么原因？消费者实际感受差别大吗？",
          "summary": "", "url": "https://www.zhihu.com/question/2094346221913690254",
          "canonical_url": "https://www.zhihu.com/question/2094346221913690254",
          "source_id": null, "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 1 },
        { "raw_sha256": "dc9b235df052828284dffbf131b5cbdc0f9407a8b87494382565015ca30c4497",
          "title": "大厂员工吐槽新来的校招生特别mean，被回复不mean根本过不了面试，真的吗？面试到底在选什么样的人？",
          "summary": "", "url": "https://www.zhihu.com/question/2094093125488566874",
          "canonical_url": "https://www.zhihu.com/question/2094093125488566874",
          "source_id": null, "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 2 },
        { "raw_sha256": "9a83599bf13ac6ddf8c1505fe5f1c61eea23b6d8d0705f48a90fc525ad7ee0c4",
          "title": "传国玉玺最后一次可信的出现是什么时候？", "summary": "",
          "url": "https://www.zhihu.com/question/592258573",
          "canonical_url": "https://www.zhihu.com/question/592258573",
          "source_id": null, "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 3 }
      ],
      "effective_limit": 3, "row_count": 3 }
  ],
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "notes": [
    "本轮为 --dry-run：不打开/创建数据库、不写节流账（只读账）；真实写库路径未在本轮执行，属未实证项。",
    "feed 行沿用既有 feeds 表；records/snapshot 全部经 storage.py 既有函数写入。",
    "上游每次轮询 hot_value / comments 变化会产生新的 raw_sha256，按 UNIQUE(feed_id,raw_sha256) 视为一次新观测。",
    "sentiment 仍是未复核词典粗筛，时间质量由 storage.publication 判定。",
    "多来源字段名互不相同，逐源映射见 ENDPOINTS；上游未提供的字段（发布时间、source_id、摘要）一律留 null/空串，不从 URL 或标题反推。",
    "冷却账按“平台”记：同一平台的多个端点（如头条 hot / recommend）共用一条记录；被冷却跳过的平台不发起上游请求，reason=throttled。",
    "风控信号（非 0 退出码 / BROWSER_CONNECT / AUTH_REQUIRED / COMMAND_EXEC / 空数组 / HTTP 429·5xx）触发即停止本轮，不重试，保留真实错误码与原文。"
  ],
  "collected_count": 1, "throttled_count": 0
}
```

知乎实测：`title`/`url` 直接可用，**无摘要字段**（`summary=""`）、`source_id=null`（问题号在 `url` 里，按“不从 URL 反推”不取）。

**#2 `bilibili-hot` 真实 stdout（按 7.1 约定折叠后）**：

```json
{
  "collector": "s1-opencli", "status": "ok", "dry_run": true, "endpoints": ["bilibili-hot"],
  "limit": 3, "min_interval": 1800, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\workspace\\.collector-throttle.json", "state": "absent", "read_only": true, "written": false },
  "access": "read", "request_count": 1, "returned_total": 3,
  "results": [
    { "endpoint": "bilibili-hot", "platform": "bilibili", "group_id": "bilibili-hot",
      "feed_url": "https://www.bilibili.com/v/popular/all",
      "field_map": { "title": "title", "url": "url", "summary": null, "source_id": "bvid", "published_at": null },
      "limit": 3, "status": "ok", "reason": "collected", "gap_waited_seconds": 0.0, "returned_count": 3,
      "call": { "endpoint": "bilibili-hot",
        "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "bilibili", "hot", "--limit", "3", "-f", "json"],
        "cwd": "...\\attempt-2b159b8dca2945768c5618a63917b4cc\\workspace", "timeout_seconds": 60,
        "started_at": "2026-10-16T15:27:48.184068+00:00", "finished_at": "2026-10-16T15:27:51.038714+00:00",
        "duration_seconds": 2.844, "return_code": 0 },
      "records": [
        { "raw_sha256": "f459b879eb7fc1044965021ee1f2effe381ef1f65a1c59635e5698f605399486",
          "title": "欠钱不还却有钱出道？是谁又在给老赖之女洗白？深扒李秀满新女团成员盛艾敬！",
          "summary": "", "url": "https://www.bilibili.com/video/BV1s7JX6gELt",
          "canonical_url": "https://www.bilibili.com/video/BV1s7JX6gELt",
          "source_id": "BV1s7JX6gELt", "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 1 },
        { "raw_sha256": "391676ae5b90c2e86d31b993c27868fe98c720686f268e6a8e376a82986fa948",
          "title": "【A2O】A2O SOUL 'SUPERSTAR' Official Music Video",
          "summary": "", "url": "https://www.bilibili.com/video/BV1tGJX6CEFy",
          "canonical_url": "https://www.bilibili.com/video/BV1tGJX6CEFy",
          "source_id": "BV1tGJX6CEFy", "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 2 },
        { "raw_sha256": "bc97b938ea7ed624abc6d20f6286c0cb16b727dad2f0fbde5d5b80c32fe5fa74",
          "title": "朱棣：“我的侄子走丢了，有好心人能帮帮我吗？”", "summary": "",
          "url": "https://www.bilibili.com/video/BV1HCpS6iEXh",
          "canonical_url": "https://www.bilibili.com/video/BV1HCpS6iEXh",
          "source_id": "BV1HCpS6iEXh", "published_at": null, "published_raw": null, "time_quality": "missing", "rank": 3 }
      ],
      "effective_limit": 3, "row_count": 3 }
  ],
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "collected_count": 1, "throttled_count": 0
}
```

B站实测：`source_id` 取上游真实 ID 字段 `bvid`；`play`/`danmaku`/`author` 未映射（不是评论数）。

**#3 `xiaohongshu-feed` 真实 stdout（按 7.1 约定折叠后）**：

```json
{
  "collector": "s1-opencli", "status": "ok", "dry_run": true, "endpoints": ["xiaohongshu-feed"],
  "limit": 3, "min_interval": 1800, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\workspace\\.collector-throttle.json", "state": "absent", "read_only": true, "written": false },
  "access": "read", "request_count": 1, "returned_total": 3,
  "results": [
    { "endpoint": "xiaohongshu-feed", "platform": "xiaohongshu", "group_id": "xiaohongshu-feed",
      "feed_url": "https://www.xiaohongshu.com/explore",
      "field_map": { "title": "title", "url": "url", "summary": null, "source_id": "id", "published_at": null },
      "limit": 3, "status": "ok", "reason": "collected", "gap_waited_seconds": 0.0, "returned_count": 3,
      "call": { "endpoint": "xiaohongshu-feed",
        "argv": ["...node.EXE", "...\\opencli\\dist\\src\\main.js", "xiaohongshu", "feed", "--limit", "3", "-f", "json"],
        "cwd": "...\\attempt-2b159b8dca2945768c5618a63917b4cc\\workspace", "timeout_seconds": 60,
        "started_at": "2026-10-16T15:28:23.157557+00:00", "finished_at": "2026-10-16T15:28:27.803133+00:00",
        "duration_seconds": 4.64, "return_code": 0 },
      "records": [
        { "raw_sha256": "60be1cd72d5e2f50c4a01833752d59ee4d3ba165d33cae727eb1d992f60f2534",
          "title": "求助", "summary": "",
          "url": "https://www.xiaohongshu.com/explore/6aaf3e320000000012034234?xsec_token=ABXBo_2QHl7Ek_Ux32w3BcuMMzSZmaDVvbDQ0__DE4MBA%3D&xsec_source=",
          "canonical_url": "(同上，原样保留 query)",
          "source_id": "6aaf3e320000000012034234", "published_at": null, "published_raw": null,
          "time_quality": "missing", "rank": null },
        { "raw_sha256": "74f05aaddeeecec4af2cfa0108a5c4cf057e6a6dda8c345c690588a496800fe6",
          "title": "[叹气R][叹气R]一个星期前办了张健身卡，由于本人不想节食但是想减肥，哼哧哼哧", "summary": "",
          "url": "https://www.xiaohongshu.com/explore/6aab3a28000000001202480e?xsec_token=ABtIynrs3cIROkpcFzhX0fKBAz_pPZm-3CLAM77iV5mbU%3D&xsec_source=",
          "source_id": "6aab3a28000000001202480e", "published_at": null, "published_raw": null,
          "time_quality": "missing", "rank": null },
        { "raw_sha256": "2951dcb4640e1099ec7fec4884aaf18477e40d24308be8c4cfa0c028d3f66eab",
          "title": "控糖一点都不能占的食物是啥？", "summary": "",
          "url": "https://www.xiaohongshu.com/explore/6aa5cecd0000000027009851?xsec_token=AB6KOocyXljrCr6yejNQzijDI71JGJJHfho8yi7sUdX5o%3D&xsec_source=",
          "source_id": "6aa5cecd0000000027009851", "published_at": null, "published_raw": null,
          "time_quality": "missing", "rank": null }
      ],
      "effective_limit": 3, "row_count": 3 }
  ],
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "collected_count": 1, "throttled_count": 0
}
```

小红书实测要点：**这是首页推荐流（`xiaohongshu feed`），不是热榜**，轮询结果会随推荐变化；`rank` 为 `null`（上游无该字段）；`url` 带 `xsec_token` 签名参数，`canonical_url` 原样保留 query（**不裁剪**，否则链接触达不了）；`likes`（点赞展示值）不进任何计数列。

### 7.3 节流生效的两次调用证据

**Round A（非 dry-run，一次真实写账 + 一次真实写库）**：命令

```
python -B <mirror>/tools/collect_opencli.py --endpoint hot --endpoint 36kr-hot --endpoint weibo-hot \
  --limit 1 --max-per-run 2 --gap-seconds 4 --min-interval 3600 \
  --throttle-file <temp>/ledger-A.json --db <temp>/round-a.db
```

运行在**临时镜像目录**（`C:\Users\Lenovo\AppData\Local\Temp\opencode\s1-c-2b159b8d`）内：镜像里 `tools/collect_opencli.py`、`server.py`、`storage.py`、`analysis.py` 与工作区**逐文件 SHA-256 相同**（脚本已在运行前核对，被测脚本哈希 = `1e786d48…743c04`，与 7.1 一致），因此 `ROOT`=镜像目录，快照与证据库落在临时目录，**真实 worktree 不产生 `snapshots/`、不产生 `sentiment.db`、不产生节流账**。退出码 `0`，stderr 空，报告关键字段（原文）：

```json
{ "collector": "s1-opencli", "status": "ok", "dry_run": false,
  "endpoints": ["hot", "36kr-hot", "weibo-hot"],
  "limit": 1, "min_interval": 3600, "gap_seconds": 4, "max_per_run": 2,
  "throttle": { "file": "...\\s1-c-2b159b8d\\ledger-A.json", "state": "absent", "read_only": false, "written": true },
  "access": "read", "request_count": 2, "returned_total": 2, "collected_count": 2, "throttled_count": 0,
  "results": [
    { "endpoint": "hot", "platform": "toutiao", "status": "ok", "reason": "collected", "gap_waited_seconds": 0.0,
      "effective_limit": 1, "returned_count": 1,
      "call": { "started_at": "2026-10-16T15:28:27.933950+00:00", "finished_at": "2026-10-16T15:28:28.347724+00:00",
                "duration_seconds": 0.406, "return_code": 0 },
      "feed": { "id": 1, "state": "created", "name": "toutiao-hot", "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc" },
      "run": { "id": "run-434b3af6f3e14b6181ce5e4f6fb21a9b", "status": "succeeded", "input_count": 1, "added": 1, "duplicates": 0,
               "input_sha256": "e9da1da8d4b0a9dc62d7dda3a5e693637284c9e8450ba7d515adcc6412cbdbf3" },
      "records": [ { "raw_sha256": "eee91ca1eb3bb44af20625d8a74244877b3f867f5343d4ec8df3cd3352ebc7e8", "rank": 1,
                     "title": "妈妈将大一儿子生活费从4000降至2000", "source_id": "7696880212247887935",
                     "summary": "", "time_quality": "missing" } ] },
    { "endpoint": "36kr-hot", "platform": "36kr", "status": "ok", "reason": "collected",
      "gap_waited_seconds": 4.0, "effective_limit": 1, "returned_count": 1,
      "call": { "started_at": "2026-10-16T15:28:32.349799+00:00", "finished_at": "2026-10-16T15:28:33.877841+00:00",
                "duration_seconds": 1.531, "return_code": 0 },
      "feed": { "id": 2, "state": "created", "name": "36kr-hot", "url": "https://36kr.com/hot-list/catalog" },
      "run": { "id": "run-0223646ae0c64a3c8e7ee4e94b139309", "status": "succeeded", "input_count": 1, "added": 1, "duplicates": 0 },
      "records": [ { "raw_sha256": "22e219db160e8fd9597aed469d3e86ad846f155ce167ad40380deeb68c149b9f", "rank": 1,
                     "title": "8点1氪丨三部门联合调查“尊界V800汽车刹车踏板支架断裂问题”；国内一半AI算力被指在造爽剧；台积电第三季度净利润7631亿元台币",
                     "source_id": null, "summary": "", "time_quality": "missing" } ] },
    { "endpoint": "weibo-hot", "platform": "weibo", "status": "skipped", "reason": "max_per_run",
      "gap_waited_seconds": 0.0, "returned_count": 0, "call": null, "records": [] }
  ],
  "db": { "path": "...\\s1-c-2b159b8d\\round-a.db", "present": true, "touched": true } }
```

这一轮同时证明了三件事：①`--gap-seconds 4` 真实生效（`hot` 结束 `15:28:28.347` → `36kr-hot` 调用开始 `15:28:32.349`，`gap_waited_seconds=4.0`）；②`--max-per-run 2` 真实生效（前两个端点各返回 1 条后余量归零，`weibo-hot` **没有发起调用**，`call=null`，`reason="max_per_run"`）；③非 dry-run 才写账（`throttle.written=true`）且真实写库成功（`feeds` 2 行、`collection_runs` 2 行 `succeeded/added=1/duplicates=0`、`records` 2 行）。

**Round A 之后的账文件内容（原文，690 字节，SHA-256 `45ca2986b32b55f16e5404bf2c6074b618081c66cc96c2ce962af4ee74652f9f`）**：

```json
{
  "platforms": {
    "36kr": {
      "last_attempt_at": "2026-10-16T15:28:33.878344+00:00",
      "last_endpoint": "36kr-hot",
      "last_error": null,
      "last_returned_count": 1,
      "last_signal": null,
      "last_status": "ok",
      "last_success_at": "2026-10-16T15:28:33.878344+00:00"
    },
    "toutiao": {
      "last_attempt_at": "2026-10-16T15:28:28.348233+00:00",
      "last_endpoint": "hot",
      "last_error": null,
      "last_returned_count": 1,
      "last_signal": null,
      "last_status": "ok",
      "last_success_at": "2026-10-16T15:28:28.348233+00:00"
    }
  },
  "updated_at": "2026-10-16T15:28:33.878344+00:00",
  "version": 1
}
```

注意：被 `max_per_run` 跳过的 `weibo` **没有**进入账文件——没有真正发起采集就不写账。

**Round B（同一账文件、同一平台、冷却窗口内，`--dry-run`）**：命令

```
python -B <mirror>/tools/collect_opencli.py --endpoint hot --dry-run --min-interval 3600 \
  --throttle-file <temp>/ledger-A.json --db <temp>/round-b.db
```

真实 stdout（原文，`notes` 同 7.4 的静态列表，此处略）：

```json
{
  "collector": "s1-opencli", "status": "throttled", "dry_run": true, "endpoints": ["hot"],
  "limit": 3, "min_interval": 3600, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\s1-c-2b159b8d\\ledger-A.json", "state": "loaded", "read_only": true, "written": false },
  "access": "read", "request_count": 0, "returned_total": 0,
  "results": [
    { "endpoint": "hot", "platform": "toutiao", "group_id": "toutiao-hot",
      "feed_url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
      "field_map": { "title": "title", "url": "url", "summary": "query", "source_id": "group_id", "published_at": null },
      "limit": 3, "status": "skipped", "reason": "throttled", "gap_waited_seconds": 0.0,
      "returned_count": 0, "call": null, "records": [],
      "throttle": { "last_attempt_at": "2026-10-16T15:28:28.348233+00:00", "last_status": "ok",
                    "last_endpoint": "hot", "seconds_since_last": 19.99,
                    "seconds_remaining": 3580.01, "min_interval": 3600 } }
  ],
  "db": { "path": "...\\s1-c-2b159b8d\\round-b.db", "present": false, "touched": false },
  "collected_count": 0, "throttled_count": 1
}
```

- **退出码 `0`**（被冷却跳过不是失败），stderr 空；
- `request_count=0`、`results[0].call=null`：**没有发起任何上游请求**；
- `reason="throttled"`，并给出 `last_attempt_at` / `last_status` / `last_endpoint` / `seconds_since_last=19.99` / `seconds_remaining=3580.01`；
- **账文件未被改写**：Round B 前后 `SHA-256 = 45ca2986…652f9f`、`mtime_ns = 1792164513879137900`、`bytes = 690` 三项完全一致；
- **数据库未被创建**：`round-b.db` 不存在，报告 `db.present=false` / `touched=false`（`--dry-run` 不打开/创建数据库）；
- 对照 Round A 的 `round-a.db` 存在且 `touched=true`。

### 7.4 负面核对（6 项，全部 0 次上游请求）

| # | 命令 | 退出码 | stdout/stderr 关键原文 |
| --- | --- | --- | --- |
| 1 | `--endpoint nosuch --dry-run` | `2` | stderr 原文：`collect_opencli.py: error: argument --endpoint: invalid choice: 'nosuch' (choose from 36kr-hot, bilibili-hot, hot, recommend, tieba-hot, weibo-hot, xiaohongshu-feed, zhihu-hot)`（白名单拒绝） |
| 2 | `--min-interval abc --dry-run --endpoint hot` | `2` | stderr 原文：`collect_opencli.py: error: argument --min-interval: invalid int value: 'abc'` |
| 3 | `--min-interval -5 --dry-run --endpoint hot` | `2` | stdout：`{"status":"blocked","error":{"stage":"args","exit_code":2,"message":"--min-interval 必须 >= 0，收到 -5"}}`，`request_count=0` |
| 4 | `--max-per-run 0 --dry-run --endpoint hot` | `2` | stdout：`{"status":"blocked","error":{"stage":"args","exit_code":2,"message":"--max-per-run 必须 >= 1，收到 0"}}`，`request_count=0` |
| 5 | `--limit 0 --dry-run --endpoint hot` | `2` | stdout：`{"status":"blocked","error":{"stage":"args","exit_code":2,"message":"--limit 必须在 1-50 之间，收到 0"}}`，`request_count=0` |
| 6 | `--dry-run --endpoint hot --throttle-file <损坏的 JSON>` | `7` | stdout：`{"status":"failed","error":{"stage":"throttle","exit_code":7,"message":"冷却账不可读，为避免放开限流已停止：Expecting value: line 1 column 27 (char 26)","throttle_file":"...\\ledger-broken.json"}}`，`request_count=0` |

第 3–6 项的校验都发生在**调用上游之前**（`request_count=0`）；第 6 项证明冷却账损坏时**fail-closed**（报错停止，不会把坏账当空账、不会静默放开限流）。

### 7.5 本轮上游请求计数（如实披露）

| 类别 | 次数 | 说明 |
| --- | --- | --- |
| **交付证据**（字节 `1e786d48…743c04`） | **5** | 7.2 的三个新端点 dry-run（各 1 次）+ 7.3 Round A（2 个端点各 1 次） |
| 交付证据中**未产生调用**的部分 | 0 | 7.3 Round B（被冷却）、7.4 全部 6 项负面核对 |
| 作废的重复运行 A（同一冻结字节，输出抓取失败） | 3 | 7.2 的第一次尝试曾用 PowerShell 管道抓 stdout，中文被控制台编码损坏，产物不可用 → 用 Python 驱动在**同一字节**上重跑（即交付证据那 3 次） |
| 作废的重复运行 B（前一字节版本 `c1c51aa27e3ae485…`） | 5 | 3 次新端点 dry-run + 2 次 Round A；该版本与冻结版本**只差报告里的模式说明文案**（见第十一节第 6 条），差异记录保留 |

合计 **13 次**只读上游调用（每次单次调用、无分页、无重试、每条 ≤50）。**未观测到限流/风控响应**；这**不能**当作“上游长期可用”的证据（见第九节）。

### 7.6 首轮（头条/微博/贴吧/36氪）历史证据（保留，未覆盖）

首轮在同一 worktree 家族的另一 attempt（`attempt-6f46943ed94c4e95ad37a14d374dba8b`、`attempt-1a53ff294ef746c697ae6463c7d00df0`）内实跑，被测文件 SHA-256 分别为 `abb81ebae25521d1d5971e277d2154a8706a3b96f4e22b9c17beaecf7b1dd4fd`、`b350e69594fd77434bcba0282b445cfd58112bc1eefcf46917751d349299b2ac`：5 个端点（头条 hot/recommend、微博/贴吧/36氪 hot）各 `--dry-run --limit 3` 退出码 0、返回 3 条；首轮修掉两个实机缺陷（Windows GBK 下含 emoji 的 `print` 崩溃、`--limit` 越界时 Traceback 逃出 `main`）。这两个修复本轮仍在。首轮的完整 stdout 见该轮报告，本轮未重跑这 5 个端点。

### 7.7 S1-C' 本轮修复的定向验证（真实上游请求 0 次）

**为什么用 dummy**：上一轮独立审查已把该任务的上游请求预算用尽（`_s1/audit-s1c-model.md` §1.1：3 次），故本轮**不碰任何真实平台**，全部用本地 dummy opencli（`node <dummy-opencli.js>`，脚本只 `process.stdout.write` 一个固定 JSON 数组，**无网络**）+ 纯静态 ledger fixture，配合 `--dry-run --min-interval 3600` 完成。dummy 与 fixture 的落地路径：`C:\Users\Lenovo\AppData\Local\Temp\opencode\s1cp\`（**工作区之外**，未纳入交付）。

**环境与字节**

- 工作区：`...\attempts\attempt-3bcb0eca0e4b4cec9ccc2ca92b33bbaf\workspace`，基线 commit `345c08f`。
- 修复版 `tools/collect_opencli.py`：**39140 字节，SHA-256 `863fea3f02115bf2d557c2505403cebaafc32b9f20f2d55ed1eb2d61c3556bb3`**。
- 直接前身（修复前，即独立审查对象）：**37673 字节，SHA-256 `1e786d48…743c04`**。该字节**不在本工作区基线（345c08f）里**——基线的 `tools/collect_opencli.py` 是更早的 `abb81eb…`（无节流/冷却账）。经 SHA-256 核对后从上一轮产物恢复为修复起点；恢复后与 `_s1/throttle-test`、`_s1/_audit-tmp` 两处副本**逐字节一致**。
- 修复本身：相对 `1e786d48…` 为 **24 增 4 删**，只落在 `load_throttle()` / `cooldown_of()` 内，且只改 `tools/` 与 `docs/`。
- node `C:\Program Files\nodejs\node.exe`（v24.18.0）；Python `3.13.14`；全部运行带 `-B`（不落盘 pyc）。

**检查矩阵**（退出码为真实进程退出码）：

| # | `--throttle-file` fixture | 被测字节 | 退出码 | `status` | 关键字段 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| pre-F | `{"version":1,"platforms":{"zhihu":{}}}` | `1e786d48…`（修前） | `0` | `ok` | `request_count=1`、`results[0].reason="collected"`、`call` 已设置 | **缺陷复现**：坏账 → 放开限流继续采集 |
| pre-G | `{"version":1,"platforms":{"zhihu":{"last_attempt_at":"not-a-date"}}}` | `1e786d48…`（修前） | `0` | `ok` | 同上 | **缺陷复现** |
| post-F | 同 pre-F | `863fea3f…`（修后） | **`7`** | `failed` | `error.stage="throttle"`、`error.platform="zhihu"`、**`request_count=0`**、`results=[]` | 已修好 |
| post-G | 同 pre-G | `863fea3f…`（修后） | **`7`** | `failed` | 同上 | 已修好 |
| post-H | 有效账（`last_attempt_at=now`） | `863fea3f…` | `0` | `throttled` | `results[0].reason="throttled"`、`call=null`、`request_count=0`、`throttled_count=1` | 回归通过 |
| post-C | `{}` | `863fea3f…` | `7` | `failed` | `error.stage="throttle"`、`request_count=0` | 回归通过 |
| post-D | `{"platforms":[]}` | `863fea3f…` | `7` | `failed` | 同上 | 回归通过 |
| post-E | `{"version":1}` | `863fea3f…` | `7` | `failed` | 同上 | 回归通过 |
| post-I | `{"version":1,"platforms":{}}`（空账，平台无记录） | `863fea3f…` | `0` | `ok` | `request_count=1`（dummy）、`reason="collected"` | 首次采集未被误判为冷却 |
| post-L | 不传 `--throttle-file`（默认路径不存在 = 空账） | `863fea3f…` | `0` | `ok` | `throttle.state="absent"`、`request_count=1`（dummy） | 空账路径未破坏 |
| post-M | 有效账但文件带 UTF-8 BOM（首三字节 `EF BB BF`） | `863fea3f…` | `7` | `failed` | `error.stage="throttle"`、`request_count=0`、原文含 `Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)` | BOM → 按“账不可读”fail-closed（方向安全） |
| post-FND | 同 post-F（条目级坏账），但**不带 `--dry-run`** | `863fea3f…` | `7` | `failed` | `error.stage="throttle"`、`request_count=0`、`throttle.written=false`、`db.touched=false` | 非 `--dry-run` 下同样 fail-closed，且**零副作用** |
| post-HND | 同 post-H（有效账冷却），但**不带 `--dry-run`** | `863fea3f…` | `0` | `throttled` | `results[0].reason="throttled"`、`request_count=0`、`throttle.written=false`、`db.touched=false` | 被冷却跳过在任何模式下都**不写账/不建库** |

**post-F 真实 stdout（原文）**：

```json
{
  "collector": "s1-opencli",
  "status": "failed",
  "error": {
    "stage": "throttle",
    "exit_code": 7,
    "message": "冷却账结构不符合预期（平台条目缺少可解析的 last_attempt_at），为避免放开限流已停止",
    "throttle_file": "C:\\Users\\Lenovo\\AppData\\Local\\Temp\\opencode\\s1cp\\ledger-entry-missing-ts.json",
    "platform": "zhihu"
  },
  "dry_run": true,
  "endpoints": ["zhihu-hot"],
  "limit": 1, "min_interval": 3600, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\s1cp\\ledger-entry-missing-ts.json", "state": null, "read_only": true, "written": false },
  "access": "read", "request_count": 0, "returned_total": 0,
  "node": "C:\\Program Files\\nodejs\\node.exe",
  "opencli": "C:\\Users\\Lenovo\\AppData\\Local\\Temp\\opencode\\s1cp\\dummy-opencli.js",
  "results": [],
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false }
}
```

（`notes` 数组与 7.2 同构，此处省略。注意 `throttle.state` 为 `null`：读账即在结构检查阶段失败，尚未进入“已加载”状态。）

**post-H 真实 stdout（关键字段原文）**：

```json
{
  "collector": "s1-opencli", "status": "throttled", "dry_run": true, "endpoints": ["zhihu-hot"],
  "limit": 1, "min_interval": 3600, "gap_seconds": 25, "max_per_run": 80,
  "throttle": { "file": "...\\s1cp\\ledger-zhihu-now.json", "state": "loaded", "read_only": true, "written": false },
  "access": "read", "request_count": 0, "returned_total": 0,
  "results": [
    { "endpoint": "zhihu-hot", "platform": "zhihu", "group_id": "zhihu-hot",
      "feed_url": "https://www.zhihu.com/hot",
      "field_map": { "title": "title", "url": "url", "summary": null, "source_id": null, "published_at": null },
      "limit": 1, "status": "skipped", "reason": "throttled", "gap_waited_seconds": 0.0,
      "returned_count": 0, "call": null, "records": [],
      "throttle": { "last_attempt_at": "2026-10-16T15:55:00.625743+00:00", "last_status": "ok",
                    "last_endpoint": "zhihu-hot", "seconds_since_last": 4.194,
                    "seconds_remaining": 3595.806, "min_interval": 3600 } }
  ],
  "db": { "path": "...\\workspace\\sentiment.db", "present": false, "touched": false },
  "collected_count": 0, "throttled_count": 1
}
```

**本轮副作用核对**：全部用例均 `--dry-run`，运行后工作区 `git status --short` 仍只有 `tools/collect_opencli.py` 与本文档两处改动；`sentiment.db` / `.collector-throttle.json` / `snapshots/` / `__pycache__` **均不存在**。语法（内存 compile、不落盘 pyc）：`memory compile OK`，退出码 `0`。

**如实披露（一次被实机抓到的回归）**：我的第一版修法把 `cooldown_of` 里“条目不是 dict”也一并改判为“冷却中”，结果 `post-I`（空账）与 `post-L`（无账）首跑得到 `status="throttled"`——即“**首次采集被误判为冷却、该平台再也采不到**”。这是 dummy 实机复跑才暴露的（只读代码会漏），已改为区分“**条目不存在**（首次采集 → 照常采集）”与“**条目存在但坏**（fail-closed）”；改后 post-I / post-L 复跑均为 `status="ok"`、`request_count=1`。

**本轮真实上游请求 = 0 次**：所有子进程调用都是本地 dummy（`dummy-opencli.js` 只写固定 JSON、无网络）。报告里的 `request_count=1` 只表示“调用了一次 dummy 子进程”，**不是**真实上游请求。

## 八、失败路径与退出码

| 场景 | 退出码 | 行为 |
| --- | --- | --- |
| `--endpoint` 非白名单 / 非整数参数 | `2` | argparse 原始错误原文（stderr） |
| `--limit` / `--min-interval` / `--gap-seconds` / `--max-per-run` / `--timeout` 越界，或 `--group-id` 配多端点 | `2` | `status:"blocked"`，`error.stage="args"`，**不调上游** |
| 找不到 node / OpenCLI `main.js` | `2` | 同上，附全部探测过的候选路径 |
| 冷却账不可读 / 顶层结构不符 / **条目级坏账**（条目不是对象，或 `last_attempt_at` 缺失、不可解析） | `7` | `status="failed"`，`error.stage="throttle"`（条目级另附 `platform`），**fail-closed**：不放开限流、`request_count=0`、不调上游 |
| opencli 退出码非 0（HTTP 非 2xx / 风控 / `BROWSER_CONNECT` 等） | `3` | 附**该端点名**、真实 `return_code`、stderr 原文、stdout 头部；带 JSON 错误信封时附 `error.code`，并给出 `signal`。**不用别的站点数据顶替**；**停止本轮，不重试** |
| 调用超时 | `3` | 已停止，不重试，附该端点名与 argv（`signal="TIMEOUT"`） |
| stdout 非合法 JSON | `4` | 附 stdout 前 2000 字符；停止本轮，不重试 |
| 上游返回空列表 | `5` | 明确报空（`signal="EMPTY_RESULT"`），不当成功 |
| 条目缺 `title`/`url` | `6` | 附该条原始 JSON，**不静默丢弃** |
| 打开证据库失败 | `7` | 附库路径与原始异常；`status` 仍为 `failed`（**本轮修掉了旧版把 `status` 覆盖回 `ok` 的缺陷**） |
| 写库异常（校验/磁盘/SQLite） | `7` | 该端点 `reason="db_import"` 并附异常类型与原文；`import_batch` 内部已把批次置 `failed` |

每个端点各自独立调用；任一站点失败只影响该端点的结果，其余站点不会被拿来顶替，也不会把失败写成空结果。

## 九、未实证项（本轮未做，不得当成已完成）

1. **风控熔断分支未真实触发**：本轮 8 个端点全部退出码 0、非空，第七节的熔断/超时/非 JSON/空列表分支**没有真实发生**，未取得原文证据（代码路径沿用既有的失败处理，只新增了 `signal` 归一与“停止本轮”语义）。
2. **`--min-interval` 到期后放行未实测**：只验证了“窗口内被跳过”，未等 3600 秒验证“窗口外正常采集”（Round A 已证明首次采集正常）。
3. **抖音未由本会话复跑**：`COMMAND_EXEC: Detached while handling command` 来自任务契约给出的既有探测记录，本轮**未实证**（见第五节末）。因此“B站/小红书/知乎是否需要浏览器桥”本轮同样只以“实跑成功”为准：三者都是普通 subprocess 调用即成功，但**没有**从上游源码核实其 `strategy`/`browser` 声明，也**不能**断言它们“不依赖浏览器桥”。运行前应确认浏览器扩展处于连接状态，否则会把连接层故障当成站点故障。
4. **`xiaohongshu feed` 的推荐流性质**：本轮实测确认它返回首页推荐（无 `rank`、无发布时间、`url` 带签名 token），但**未做**多轮对照以量化其随时间/账号的变化幅度；“不是热榜”这一结论仅基于适配器语义与本次返回形态。
5. **`--group-id` 语义为假设**：本实现解释为“来源分组标识”（决定 `feeds.name`/`feed_id`）。若需求原意是“头条单帖 `group_id` 过滤”，需要改参数语义——未与需求方确认。
6. **`feed_url` 仅作分组标识，未取证**：各端点的 `feed_url` 是“该来源公开热榜页地址”，本轮**未验证**它与 OpenCLI 内部实际请求的上游接口是否等价，也**未**对它发起任何请求（新三源的 `feed_url` 同理，其中知乎/B站/小红书的接口路径只是适配器语义下的入口，未做等价性取证）。
7. **`time_quality` 恒为 `missing` 的端点较多**：微博/贴吧/36氪/知乎/B站/小红书热榜条目实测均不含发布时间字段，故 `published_at` 恒为 `null`。是否要用抓取时刻另立“观测时间”列，未与产品确认。
8. **OpenCLI 版本漂移**：字段名固定按 1.8.8 实测；升级后是否变化未验证（`--limit 3` 只观测到榜单头部若干条，非全量字段清单）。
9. **快照膨胀未评估**：上游 `hot_value`/`discussions`/`likes`/`heat` 每次变化都会产生新的 `raw_sha256`（按 `UNIQUE(feed_id,raw_sha256)` 视为一次新观测）。长期是否需要按天/按窗截断，本轮未设计。
10. **冷却账的并发与多机**：写账是“临时文件 + `os.replace`”，**没有**跨进程文件锁；两个采集进程同时跑同一账文件时，后者可能覆盖前者的记录（不会产生半截文件，但会丢一次记录）。本轮未做并发测试。
11. **`--dry-run` 不写账的直接后果**：同一轮 dry-run 里若同时选了同一平台的两个端点（如 `hot` + `recommend`），冷却不会在轮内生效（账未写），只能靠 `--gap-seconds` 错峰。这是“dry-run 只读账”的必然结果，未额外在内存里模拟冷却。
12. **未接 UI**：`tools/` 采集器未接入 `server.py` 的任何 HTTP 路径；本文档只描述采集器本身。
13. **S1-C' 本轮（修复轮）的验证范围**：全部用**本地 dummy opencli**（真实上游请求 0 次），覆盖条目级 fail-closed（含**非 `--dry-run`**：post-FND）、有效账冷却（含**非 `--dry-run`**：post-HND）、顶层三例坏账、空账/无账、BOM 共 13 个用例（见 7.7）。**未重跑**真实上游端点与非 dry-run 的真实写库路径——修复点位于“读账”阶段（流程第 3 步），早于任何上游调用、写账与建库；post-FND/post-HND 已实测证明非 dry-run 下坏账仍 fail-closed、被冷却跳过仍不写账不建库、且 worktree 零副作用。本轮**不把“真实上游仍可用”当成结论**（上一轮的 3 次预算已用尽，未再消耗）。

## 十、已知风险与边界

- 采集器写入的 `feeds` 行**不是 RSS 源**，只是 `records.feed_id` 的关联分组（名称如 `weibo-hot`、`zhihu-hot`）。若在页面对其点击“刷新来源”，`server.py` 会按 RSS 解析该公开页地址并失败——这是既有 `/api/refresh` 的语义，本轮未改动，也**未加防护**。
- 只读范围：仅 `access=read` 的 8 个公开榜单/推荐流读取命令；无发布/评论/关注/删除动作，不绕过登录与风控，不复制 cookie，不读取凭据。`ENDPOINTS` 的 argv 是常量，调用方无法注入任意子命令（例如 `douyin publish`）。
- 上游“热度值/讨论量/播放量/点赞数”一律不进本地计数列，也不冒充评论数。
- 冷却账默认落在**仓库根**（`<仓库>/.collector-throttle.json`，未被 `.gitignore` 覆盖）。运行采集会产生一个未跟踪文件；本轮验证用 `--throttle-file` 指到临时目录，以保持 worktree 干净。是否把该文件加入忽略清单，交由仓库维护者决定（本轮未改 `.gitignore`）。
- 采集器不写桥的状态文件，不启动服务、不订阅定时任务。
- 退出码语义：`0` = 本轮没有失败（**含“全部被冷却跳过”**）；`2/3/4/5/6/7` 见第八节。调度方若要区分“跳过”，请读 `status` 与 `results[].reason`。

## 十一、本轮代码改动清单（相对基线 `fdb48dc`，只改 `tools/collect_opencli.py` 与本文档）

1. 新增限流四项参数与实现：`--min-interval` / `--gap-seconds` / `--max-per-run` / `--throttle-file`，以及 `load_throttle` / `save_throttle` / `cooldown_of` / `record_attempt` 与一轮内的余量、错峰、跳过逻辑。
2. 新增平台级冷却账（JSON，默认 `<仓库根>/.collector-throttle.json`）：冷却中跳过并记 `reason="throttled"`；只有真正发起采集才写账；账损坏时 fail-closed。
3. 新增风控熔断归一 `risk_signal()`（非 0 退出 / `BROWSER_CONNECT` / `AUTH_REQUIRED` / `COMMAND_EXEC` / 空数组 / HTTP 429·5xx），任何失败都停止本轮且不重试。
4. `--endpoint` 由单选改为**可重复传**，一轮可采多个平台；报告由单端点改为 `results[]` 多端点结构（新增 `platform` / `status` / `reason` / `gap_waited_seconds` / `effective_limit` / `signal` / `throttle` 字段；顶层新增 `endpoints` / `min_interval` / `gap_seconds` / `max_per_run` / `throttle` / `returned_total` / `collected_count` / `throttled_count`；**移除**顶层 `endpoint` / `group_id` / `records` / `feed_url` / `field_map` / `call` / `request_count=1` 的单端点写法）。
5. `ENDPOINTS` 新增 `zhihu-hot` / `bilibili-hot` / `xiaohongshu-feed` 三个端点，并为每条声明新增 `platform`（冷却键）字段，`_validate_endpoints()` 一并校验。
6. 修正 `emit_error()` 覆盖 `status` 的既有缺陷（旧版在“打开证据库失败”分支会把 `status:"ok"` 覆盖回 `ok`；现改为“异常决定 `status`/`error`，上下文只补充不覆盖”）；报告 `notes[0]` 改为随 `dry_run` 切换的模式说明，避免在真实写库那一轮说“本轮只跑了 dry-run”。
7. 阈值校验统一收敛到 `validate_args()`，全部在调用上游之前完成。
8. **S1-C' 修复（本轮唯一的功能改动）**：把**条目级**坏账也改为保守方向——`load_throttle()` 在读账后逐条校验 `platforms`，任一条目不是对象、或 `last_attempt_at` 缺失/不可解析，即以 `CollectError("throttle", …, 7)` **拒绝运行**（`status="failed"`、`error.stage="throttle"`、`request_count=0`）；`cooldown_of()` 里同样的“条目存在但坏”分支由“返回无冷却（fail-OPEN）”改为“按冷却处理”，作为不可达的第二道防线；“**该平台尚无记录（首次采集）**”仍照常采集。限流四参数、平台级冷却账 schema、`--endpoint` 可重复、三个新端点、风控熔断、`--dry-run` 不建库/不写账、`emit_error()` 的 status 修复**一律未动**（差异与验证见 7.7）。
9. **本轮基线说明**：本轮工作区基线为 commit `345c08f`，该 commit 内 `tools/collect_opencli.py` 仍是更早的 `abb81eb…`（无节流/冷却账）；上一版 S1-C 字节 `1e786d48…` 不在该 commit 内，已按 SHA-256 核对后从上一轮产物恢复为修复起点（见 7.7），因此本轮相对 `345c08f` 的 diff 会同时包含“上一版 S1-C 的全部内容”与“本轮 24 增 4 删的修复”。

以上第 4 项的报告结构变化与第 5 项的端点扩充是**有意为之并在此声明**；头条/微博/贴吧/36氪的取值语义、`--dry-run` 语义、错误信封转换、`snapshots/<sha256>.json` 写法、`records` 字段映射均未改变。第 8 项是本轮（S1-C'）唯一的功能改动，其余行为与上一版 `1e786d48…` 一致。
