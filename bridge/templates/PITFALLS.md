# PITFALLS：本会话踩过的坑（症状 → 原因 → 修法）

> 用途：派任务/写脚本前扫一眼，别重复踩。全部来自 2026-10-16 会话的实测。

## 1 编码类（最高频）

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| E1 | `UnicodeEncodeError: 'gbk' codec can't encode character '\u2022'` | Windows 控制台默认 gbk，打印 `•`/emoji/某些中文符号必崩 | 脚本开头：`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（stderr 同样） |
| E2 | 子进程输出读出来是乱码或直接抛 `UnicodeDecodeError` | `subprocess.run(text=True)` 用系统代码页（gbk）解码 | 显式 `encoding="utf-8", errors="replace"` |
| E3 | 用 PowerShell `1> out.json` 存 JSON，读回报 `0xff` 开头 / 不是 UTF-8 | PowerShell 5.1 的 `>` 重定向写 **UTF-16LE** | 别用重定向：Python `subprocess` 捕获 UTF-8 stdout，或 `Out-File -Encoding utf8`（注意会带 BOM） |
| E4 | 写进仓库的文件被桥判"与批准的计划不一致" | Python 文本写默认用 `os.linesep`（Windows=CRLF），而仓库 `eol=lf`；桥按磁盘字节算哈希 | 一律 `write_bytes(raw.replace(b"\r\n", b"\n"))`；JSON 契约不得带 BOM |
| E5 | JSON 读到 `\ufeff` 导致 `json.loads` 失败 | `Set-Content -Encoding UTF8` / 某些编辑器写 BOM | 读时 `encoding="utf-8-sig"`；写时 `utf-8` 无 BOM |

## 2 PowerShell 类

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| P1 | `AssertionError: ('锚点', 0)` 或 `SyntaxError: unterminated string literal` | 内联 `python -c` 里再嵌引号/反斜杠，PS 先吃一层、Python 再吃一层 | **别内联**：把补丁/脚本写成文件再跑 |
| P2 | `$args` 被当成特殊变量 | `$args` 是 PowerShell 自动变量 | 换名（`$argv`/`$items`） |
| P3 | `foreach` 后跟 `)){` 报语法错 | PS 里 `@(...)` 后面多了一层括号 | 检查括号配对；复杂循环写成脚本文件 |

## 3 验证/流程类

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| V1 | "我打回了但账本没记" | 用 `Select-String` 过滤命令输出，错误行没匹配就被丢掉 | **会改账本状态的命令一律看完整输出**；用 `bridge_call.py` |
| V2 | 自检判 `not connected` 为"已连接" | 关键词 `connected` 出现在否定句里 | 看 `[MISSING]/[FAIL]` 否定标记，不看关键词 |
| V3 | 测试用 `git clone --local` 报 `Improper link` | 本地克隆默认硬链接，跨某些卷/权限失败 | `git clone --no-hardlinks` |
| V4 | 工具明明写好了却"文件不存在" | 部署顺序：先调用后复制 | 部署步骤放最前，或先 `Test-Path` 断言 |
| V5 | 任务 `stop_reason=timeout` 但交付物其实是完整的 | 超时只是预算边界，文档可能已写完 | **先核交付物再决定 revise**（S1-C 实例：代码+文档+证据都在） |
| V6 | 桥 agent 卡在 `BLOCKED（无凭据）` | 桥任务环境没有模型 key，也不允许硬编码 | 契约写明"凭据类证据由主线程在产品侧补"（模板已内置） |
| V7 | 连续 3 次 900s 超时 | 契约包太大 / 证据要求要真跑多次 | 按类型设超时（docs 300 / adapter 600 / endpoint·annotator 900）+ `check_contract.py` 拦包大 |
| V8 | 桥的 `reviewable` 显示"不可审"但代码已修 | 列表/详情用的是采集时的存量 `review_gate` | 详情页已改实时复算；列表仍是存量（有意取舍） |
| V9 | 新建文件被判 `artifacts_incomplete` | 老逻辑要求 `before_sha256` 非空，新文件没有 | 已修（`6243d42`）：只要求 `after_sha256` |
| V10 | 限流"坏账反而放开"（fail-OPEN） | 条目级 `last_attempt_at` 缺失时返回"无冷却" | 已修（v2）：条目级坏账也 fail-closed；`selfcheck` 覆盖不到，靠独立审查发现 |

## 4 模型/API 类

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| M1 | `400 Model is unavailable` | 请求体里模型 id 带了 provider 前缀（`opencode-go/xxx`） | zen 直连用**裸 id**（`deepseek-v4.1-flash`） |
| M2 | `403 error 1010`（Cloudflare） | 缺浏览器风格 `User-Agent` | 三头齐：`Authorization` + 浏览器 `User-Agent` + `x-opencode-session` |
| M3 | `400 MissingSessionID` | 缺 `x-opencode-session` | 同上 |
| M4 | 返回不是合法 JSON / 很慢（10s+） | 思考模式把预算烧在推理上 | 请求体加 `reasoning_effort: "none"`（实测 10.8s→3.5s，reasoning 900→0） |
| M5 | `validate` 失败（引用 start/end 偏差） | 模型给的 quote 与原文**逐字相同**，但它自算的 start/end 有偏差 | `normalize_output` 用 `text.find(quote)` 把 start/end 改写成真实位置；**找不到就原样交给 validate 硬失败**（绝不放宽「quote 必须是原文逐字子串」） |
| M6 | 平台适配器报 `BROWSER_CONNECT` | Chrome 没开/扩展没连 | 开工先跑 `preflight.py`；`opencli doctor` 看 `Extension: connected` |
| M7 | 知乎返回空数组 `[]` | 未登录（`z_c0` cookie 缺失） | `zhihu whoami` 先验；空数组不是"没数据"，可能是未登录 |

## 5 采集/限额类

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| S1 | 热榜条目反复入库 | 内容哈希只对 title+summary 精确匹配 | 贴吧实测 20 条全部判重跳过（幂等生效）；标题微调仍会新增（已知） |
| S2 | 时间恒为 `missing` | 热榜没有发布时间字段 | 不猜；如实标 `time_quality=missing` |
| S3 | 抖音 `COMMAND_EXEC: Detached while handling command` | 适配器依赖创作者后台会话 | 记为不可用，不入端点表 |
| S4 | 冷却账被误改后限流失效 | 手工编辑账文件 | 只通过程序写；坏账 fail-closed（v2 已保证） |

## 6 独立复核发现（2026-10-17 补）

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| R1 | 夹具"没凭据也能跑"实际不成立 | 产品的凭据检查在**发送之前**（`analysis.py` L299-301），指向本地也要 key | 显式夹具模式：`OPENCODE_GO_V1_FIXTURE=1`（默认关闭，仅离线回放用） |
| R2 | 夹具永远 404 | 用**整包 body 的 SHA** 当匹配键，而产品 body（`stream`+system/user messages）与录制手写体永远不同 | 默认按 **messages 子集**匹配，另有 `--match model` / `--match body` |
| R3 | 一次 404 就吃掉一个模型槽位 | `request_once` 把该槽记成 `uncertain` 并**永久阻塞**（不重发，这是对的） | 先确认夹具能命中（`--match model` 或核对 body）再跑真实链路 |
| R4 | `prune --apply` 会删掉正在用的 workspace | 保留规则只认 `state.json` 的 `current_attempt`，accepted 任务的旧 attempt 会被删；未知状态落到"全删" | 已重写：**默认什么都不删**；未知状态一律保留并告警；`--protect` 保护指定路径；删除逐条核实 |
| R5 | 报告里的数字"无据可查" | 只在控制台打印、没落盘 | 生成证据文件（`--save`）并报告里写 sha256；没有落盘证据的一律记"待重验" |
| R6 | 接收被拒：`回收后工作区又被修改` | 我把功能测试跑在**证据 worktree 里**，工具把 `events/` 产物写进 worktree，桥的接收守卫发现工作区与执行时不一致 | 功能测试一律在**副本**里跑；若已污染，先把产物拷到 `_s4/functional-run/`，再删掉 worktree 里的产物与 `__pycache__`，`git status` 只剩应有改动后再接收 |
