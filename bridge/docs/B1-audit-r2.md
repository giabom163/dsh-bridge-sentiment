# B1 最终只读复审（r2）

**结论：** F1/F2/F3 源码修复均有依据；有限浏览器样本通过不等于完整产品验收，整体仍 **BLOCKED**。`awaiting_review`/机器 `BLOCKED` 是记录状态，不是产品显示失败。

| 项目 | 结论与依据 |
|---|---|
| F1 未知/空态 | **PASS（有限实机）**：`web/app.js:37-52,88-112`、`web/task-graph.js:171-229` 区分未加载与成功空列表；首次 503 DOM 显示无法判空且无空态（`docs/ui-first-failure-final.txt:15-47`），成功空列表显示空态（`docs/ui-empty-final.txt:38-50`），断线 DOM 保留11项及旧时间、恢复后重连（`docs/ui-disconnected.txt:5-15,27-49`、`docs/ui-restored.txt:13-15`）。CSS `web/style.css:6` 明确 hidden 覆盖层 `display:none`；空列表 DOM 仅见空层。仅覆盖所记录夹具/样本。 |
| F2 `--limit` | **PASS（代码＋记录）**：`tests/graph_checks.py:293-300` 执行前切片；`docs/B1-implementation.md:140-145` 记录 limit=1 仅执行一组。未在本次复跑。 |
| F3 哈希 | **PASS（watchdog 实际运行记录）**：`docs/B1-watchdog-evidence.json:20-55` 五条 UI 路由的 disk/http/loaded 全相等，且与当前本地源码重算值一致。注意 `docs/B1-implementation.md:137` 的 style/task-graph 哈希与当前源码及 watchdog 不同，属过期记录，勿引用为当前版本证据。 |
| 长 goal 缩图 | **源码修复 PASS**：`web/task-graph.js:50-56,251-267,284-289` 标签限长、无边用 grid、过滤后按可见节点 fit；DOM 记录有11任务、过滤为1、实际画布节点点击并保留完整目标（`docs/B1-ui-evidence.json:5-20`、`docs/ui-node-detail.txt:34-48,77-90`）。**视觉可读性 BLOCKED**：未找到证据所引用的 `ui-graph-final.png` / `ui-filter-one.png`，DOM 不能证明长标签下布局尺寸确实可读。 |
| 契约及覆盖边界 | 状态/验收映射见 `web/task-graph.js:12-49`；仅依赖 `task.dependencies` 连边见 `:145-168`；唯一5秒轮询见 `web/app.js:257`；白名单/CSP见 `webui.py:67,137-158`，静态核对符合契约。真实 DOM 只呈现11项 `awaiting_review`/`awaiting_user` 且均 `BLOCKED`，不能代表十状态触发；样本无真实依赖边。窄屏、取消、依赖边实机、完整 Demo/生命周期及真实全状态仍 **BLOCKED/未覆盖**（`docs/B1-ui-evidence.json:49-54`）；npm `dist.integrity` 未留存。 |

**最小后续：** 更正实现说明中过期哈希记录；补齐所引用截图及真实依赖、状态/窄屏/取消的获准浏览器证据后再验收。本次只读复审，未运行测试、服务或浏览器，未执行人工 accept；未改代码、配置或 store。
