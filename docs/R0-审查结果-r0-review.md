# R0 两文件补丁 · 独立静态审查

## 结论

**整体：PASS**（五处修复逐项通过，未发现阻断性缺陷）。

本次仅对契约内联的「提案输出文本」做静态审查，未重建/应用补丁、未运行 shell 或测试、未写临时文件、未读外部路径，也不含任何 GUI/运行态声明。`node --check`／内存 compile 的 JS 语法结果、通用 selfcheck 与真实 GUI 验收由主线程补做（见「运行态未验证」）。

---

## 五项修复逐项评估

### 1. 当前状态与历史结果分列；dismissed 不作为有效 reject —— PASS
- `task_summary` 只回传当前态 `state.acceptance`（表格「接收」列即此值），历史执行态 `result.acceptance` 仅在 `task_detail` 原样回传的 `result` 中。
- 详情摘要明确双列并带标签：`acceptance(state)`（当前）与 `执行结束时记录 result.acceptance`（历史），二者不再混淆。
- `human_dismissed` 单独保留为布尔字段，`humanLabel` 对 dismissed 显示「已撤销/待接收」而非原 decision；详情行显示 `decision（已撤销/待接收）`。即「打回被撤销但保留、且不作为有效 reject」成立。
- 表格 `human` 列用 `humanLabel` 隐藏原 decision 文本、详情保留原 decision，仅为展示粒度差异，不构成缺陷。

### 2. 相同内容不重绘；变化后焦点/展开/滚动保留；详情版本失效与旧响应隔离 —— PASS
- `contentSignature` 显式忽略 `updated_at`，仅按 store/task 真实内容 + `detail_version` 取签名；`load` 中 `signature === lastContent` 时只更新时间戳、不 `render`。满足「相同内容不重绘」。
- `render` 重绘前捕获 `activeElement`（限 `.task-row`）与 `window.scrollY`，重绘后 `row.focus()` + `scrollTo(0, scrollY)` 恢复；`expanded` 为模块级 Set，重绘后按 key 恢复 `aria-expanded` 与详情行 `hidden`，并继续取/拉详情。焦点/展开/滚动保留成立。
- 详情缓存按 `key -> {version, data?, error?, pending, token}` 绑定版本：`requestSeq` 令牌使旧响应在 `current.token !== token` 时直接丢弃，不覆盖新版本；`fillDetail` 对 `cached.version !== version || cached.pending` 显示占位。旧响应隔离成立。
- 去重：pending 同版本并发返回、非 retry 且已有 data/error 直接复用；错误项仅在手动刷新（`retryFailedDetails`）或点「重试读取详情」时清除/重试，自动路径不无限重试。成立。

### 3. 默认注册 P0；环境覆盖/只读/原路由兼容 —— PASS
- `DEFAULT_TASK_STORES` 首项 `_p0`（label「P0」），默认注册成立。
- `task_store_list` 读 `SENTIMENT_TASK_STORES`，非空时校验为「非空 JSON 数组、每项对象含非空 key/path」，非法抛 `ValueError`；覆盖逻辑成立。
- `read_json` 只读（`read_text`/`is_file`），`task_overview`/`task_detail` 无写操作，只读成立。
- 404 语义完整：store 不存在 / tasks 目录缺失 / 任务不存在 / state.json 缺失 均 404；解析失败 500。前端 `fetchDetail` 对非 ok 抛 `body.error`。环境/404 兼容成立。
- 注：路由装饰器（`/api/tasks`、`/api/tasks/<store>/<task>`）未在本次内联文本中，函数名与签名（`task_overview()`、`task_detail(store_key, task_id)`）及前端 URL 拼装（`key.split("/")` 后 `encodeURIComponent` 拼接）与文档路由一致，判定兼容，但属「运行态未验证」。

### 4. 仅两个文件变化 —— PASS（契约声明）
- 实现目标仅 `server.py` 与 `tasks.html` 两个文件，契约已附二者 SHA。本审查未读仓库 diff，仅采信契约声明；文件数/哈希由主线程核对。

---

## 非阻断提示（可选改进，不影响通过）

1. `task_summary` 在 `state.json` 缺失时，因 `read_json` 返回 `(None, None)` 而把 `problem` 置为「state.json 不是 JSON 对象」，把「文件不存在」误报为「非法 JSON」；`task_detail` 已正确区分（缺失 404、解析失败 500）。建议 `task_summary` 对 None 且无 error 的场景单独标注「缺失」，属文案级问题。
2. `task_store_list` 对环境变量非法抛出的 `ValueError` 在路由层是否捕获并转成明确的 4xx/5xx 未在文本中体现；若未捕获会以 500 冒泡。属健壮性提示，非本次修复目标。
3. 表格「人工」列与详情对 dismissed 的展示口径不一致（前者仅「已撤销/待接收」，后者保留原 decision 加后缀），如需强一致可统一。

---

## 运行态未验证（由主线程补做，不纳入 PASS 依据）

- `node --check` / 内存 compile 的 JS 语法结果（本次未执行）。
- 真实浏览器 GUI：焦点/展开/滚动在内容变化后是否实际保留、`updated_at` 变化下 DOM 是否真不重绘。
- 路由注册与真实 HTTP 状态码（404/500 实跑）、环境变量覆盖实跑。
- 上述均缺证据时，运行态验收应记为 BLOCKED，不以本次静态审查结论替代。
