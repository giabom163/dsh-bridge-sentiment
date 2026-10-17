# B2 提示词去重复与缺标题任务辨识 · 实现说明

本文件只记录本次实际改动与真实运行结果。**这不是最终验收 PASS**：真实客户端/浏览器与用户真实路径验收仍未执行，整体状态为 `awaiting_review` + 机器验收 `BLOCKED`。

本修订（`bridge-b2-prompt-label-20261015`）只补齐 `docs/B2-contract.md` 尚缺项：明确提示词"默认不测试但授权有限检查"的语义、分行给出 `allowed_paths`/`artifacts` 与进度/验收语义、无 title 时文本列表直接显示完整 `task_id`，并在 schema/README 补字段语义。**未新增依赖、未联网、未改运行/取消/认证/锁/原生适配器。**

## 1. 实际复用的既有实现

- 复用已落地的 Cytoscape core（`web/vendor/cytoscape.min.js`）与内置布局：有边用 `breadthfirst`，无边用 `grid`；未加载任何布局扩展，未新增依赖、未联网、未走 CDN。
- 复用现有桥服务/轮询/CLI（`bridge.py`、`adapters.py`、`webui.py`）与 Python 标准库；未改 `run`／取消／认证／锁／原生适配器。
- `web/app.js` 的唯一定时器、选择联动、未知/空态判别等语义保持不动。
- 未复制 TrendRadar／BettaFish 或任何外部项目代码。

## 2. 本次修订修复

| 路径 | 改动 |
| --- | --- |
| `bridge.py` `prompt_for` | 包装行改为：**默认不执行测试／服务／桌面，但契约明确授权的有限语法或定向检查（如 `node --check`、内存 `compile`）可执行**；删去笼统的"不要自动执行测试"。新增分行语义：`allowed_paths` 是可修改范围、`artifacts` 是必交产物且越界拒收；进度（NOT_STARTED/READY/RUNNING/DONE/BLOCKED）与验收（PASS/FAIL/BLOCKED）两套独立字段、执行者不得自动判定人工 accept；允许阅读工作区源码、缺证据时报告阻塞。完整 task JSON 仍**只内联一次**，不复述字段值、时限或 anchors；工作区仍取真实 `attempt["worktree"]`、基线取 `attempt["base_commit"]`；`directory`（桥任务 store 目录）刻意不写进提示词。 |
| `bridge.py` `validate` + `parse_anchor` | 保持上版：可选 `anchors` 校验（格式、正整数行、正序区间、越界、锚点文件须列 `input_refs`），旧契约兼容，`allowed_paths`/`artifacts` 子集校验保留。本修订未改校验行为。 |
| `schemas/task.schema.json` | 补字段语义 `description`（**仅注解，不改校验行为**）：`allowed_paths`＝可修改范围、`artifacts`＝必交产物且须落在 `allowed_paths` 内、`input_refs`＝需固定 hash 的已跟踪文件、`completion_criteria`＝供人工/只读审计核对且不替代机器证据、`title`/`task_id`/`goal`/`dependencies`/`timeout_seconds` 语义。`anchors` 说明保持。 |
| `web/task-graph.js` | 新增 `listLabel`：文本列表**有 title 用 title，无 title 时直接显示完整 `task_id`**（列表均不截断，限长只在图内节点），使公共前缀相近的任务在列表里可分辨；列表按钮仍保留完整 `task_id` 的 `title` 属性（`title属性可留`）。图内节点仍用 `taskLabel` 尾部限长（`LABEL_MAX=28`，`…`＋末 27 字符），带 title 仍优先。`grid`/`breadthfirst`/隐藏修复/搜索/状态语义保持不变，无解析边仍明确"暂无依赖记录"。 |
| `tests/prompt_checks.py` | 三组定向离线检查同步更新：组 1 断言包装行含上述四条新语义；组 3 用 Node harness 解析列表按钮文本，断言无 title 任务文本＝完整 `task_id`、图内标签＝尾部限长、带 title 仍优先。仍支持 `--limit 1–3`（执行前截断）。 |
| `docs/B2-implementation.md`（本文件） | 实现与检查记录。 |
| `README.md` | 只补契约语义短节：字段语义、进度/验收独立、授权检查范围、列表完整 ID。 |

未修改：`adapters.py`、`web/app.js`、`web/index.html`、`web/style.css`、`webui.py`、执行/接收逻辑、真实 store、`.git`、认证与全局配置。

## 3. 定向检查（本机真实执行记录）

授权范围：本修订**明确授权 `tests/prompt_checks.py --limit 3` 一次、`node --check`、内存 `compile` 纯语法检查**；禁止全量与持久服务。均在本机离线执行；`model_requests: 0`，未启动持久服务、未联网、未跑全量、未自动重跑。

语法检查（在内存中 `compile` 与 `node --check`，不落盘执行）：

```
PY_COMPILE_OK        # bridge.py, tests/prompt_checks.py
NODE_CHECK_OK        # web/task-graph.js
SCHEMA_JSON_OK
```

`tests/prompt_checks.py --limit 3`（退出码 0；输出原样）：

```json
{"check": "prompt_single_json_workspace_basecommit", "status": "PASS", "detail": {"contract_json_once": true, "contract_json_content_matches": true, "goal_not_repeated_outside_json": true, "wrapper_states_allowed_artifacts_semantics": true, "wrapper_separates_progress_and_acceptance": true, "wrapper_authorizes_only_limited_checks": true, "wrapper_allows_source_or_evidence": true, "workspace_from_attempt_worktree": true, "store_dir_not_used_as_workspace": true, "base_commit_inline": true, "legacy_contract_validates": true, "legacy_json_has_no_title": true, "legacy_prompt_single_json": true}}
{"check": "anchors_and_artifacts_boundaries", "status": "PASS", "detail": {"legal_anchor_accepted": true, "out_of_bounds_rejected": true, "reversed_range_rejected": true, "nonpositive_line_rejected": true, "bad_format_rejected": true, "unanchored_file_not_in_input_refs_rejected": true, "artifact_outside_allowed_paths_rejected": true, "artifact_inside_allowed_paths_accepted": true, "legacy_no_anchors_accepted": true}}
{"check": "titleless_id_tail_and_full_list_node", "status": "PASS", "detail": {"graph_tail_matches_real_node_labels": true, "graph_tails_pairwise_distinct": true, "graph_title_still_prioritized": true, "list_shows_full_task_id_when_titleless": true, "list_full_ids_pairwise_distinct": true, "list_title_still_prioritized": true, "list_keeps_full_id_title_attr": true, "node_labels": {"opencode-go-fullchain-docs-r3": "…encode-go-fullchain-docs-r3", "opencode-readme-update-20261015": "…code-readme-update-20261015", "ui-real-opencode-20261015-1238": "…real-opencode-20261015-1238", "titled-fixture": "B0 状态映射审查"}, "list_items": {"opencode-go-fullchain-docs-r3": {"id": "opencode-go-fullchain-docs-r3", "title": "opencode-go-fullchain-docs-r3", "text": "opencode-go-fullchain-docs-r3"}, "opencode-readme-update-20261015": {"id": "opencode-readme-update-20261015", "title": "opencode-readme-update-20261015", "text": "opencode-readme-update-20261015"}, "ui-real-opencode-20261015-1238": {"id": "ui-real-opencode-20261015-1238", "title": "ui-real-opencode-20261015-1238", "text": "ui-real-opencode-20261015-1238"}, "titled-fixture": {"id": "titled-fixture", "title": "titled-fixture", "text": "B0 状态映射审查"}}}}
{"limit": 3, "executed_checks": ["prompt_single_json_workspace_basecommit", "anchors_and_artifacts_boundaries", "titleless_id_tail_and_full_list_node"], "retained_directory": "…\\workspace\\data\\prompt-check-c6ece99542b8", "model_requests": 0, "acceptance": "BLOCKED"}
```

检查覆盖边界：

- 组 1 为**结构化断言**：完整契约 JSON 在 prompt 中恰好出现一次且内容与 `validate` 后任务一致；除 JSON 行外的包装行等于固定模板（据此断言 `goal` 等字段不在 JSON 外重复，避免对任意短 goal 做 count 或长度上限误判）；包装行含 `allowed_paths`/`artifacts` 语义、进度与验收独立、授权有限检查、允许读源码/报缺证据；工作区来自 `attempt.worktree` 且 store 目录未被当成工作区；旧契约（无 `title`/`anchors`/`opencode_mode`）可校验且 prompt 仍单次内联。
- 组 2 为 `bridge.validate` 的真实正/反例：合法锚点与产物被接受；越界、反向区间、非正整数、格式错误、锚点文件未列 `input_refs`、产物越出 `allowed_paths` 均被拒绝。
- 组 3 **用本机已有 Node 实际执行** `web/task-graph.js`：`vm` 载入组件、以最小伪 DOM/伪 Cytoscape 调 `mount`+`update`，真实运行 `taskLabel`/`listLabel`/`renderList`/`renderNote`，再从生成的列表 HTML 解析出每个按钮的 `data-id`／`title`／文本。核对三条历史缺 title 任务（`opencode-go-fullchain-docs-r3`、`opencode-readme-update-20261015`、`ui-real-opencode-20261015-1238`，来源 `docs/ui-restored.txt`）：图内标签互不相同、各等于 `…`＋末 27 字符；文本列表按钮文本直接等于完整 `task_id`；带 title 任务仍显示 title。不是对源码的纯字符串出现断言。

保留现场（未清理）：`data/prompt-check-c6ece99542b8`（`data/` 已 gitignore）。

## 4. 未执行的验证与缺失项（如实列出）

- 未在真实浏览器/真实客户端核对列表"无 title 直接显示完整 task_id"的排版效果、`title` 悬停提示的可发现性，以及图注"暂无依赖记录"的实际呈现。
- 组 3 用伪 Cytoscape/伪 DOM 执行组件逻辑，验证的是标签与列表文本生成，**不是**真实 Cytoscape 渲染或真实浏览器 AX 树。
- 未运行 `tests/graph_checks.py`、`tests/manual_checks.py`（未获授权），未验证本次改动对既有图谱定向检查的联动影响（已只读确认 `tests/graph_checks.py` 不依赖列表标签文本或旧提示词行）。
- 三条历史缺 title ID 取自本工作区 `docs/ui-restored.txt` 的真实界面记录；未读取真实 store 复核其当前 title 字段。
- 未做真实锚点文件的端到端执行；未修改/删除任何桥 store、状态文件或人工验收证据；未提交、未推送、未切分支。

## 5. 结论

代码与三组定向离线检查在本机授权范围内真实通过（退出码 0，语法检查通过）。**运行成功 ≠ 最终验收 PASS**：真实浏览器/客户端与用户真实路径验收仍未执行，整体结论保持 `BLOCKED`，执行者不代填人工 accept。
