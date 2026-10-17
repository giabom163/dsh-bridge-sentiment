# B0：桥 UI 状态映射与最小改动审查

## 范围与结论

本审查只读核对 `docs/B1-contract.md`、`bridge.py`、`webui.py`、`web/app.js`、`web/index.html`、`web/style.css` 和 `schemas/task.schema.json`。任务数据 API 已能提供绘图所需的任务契约、状态和依赖；无需修改桥执行/存储逻辑或 schema。当前 UI 没有依赖图，也没有连接状态/最后成功刷新时间，模型字段为 null 时详情显示为空。

详情 API 返回 `state.current_attempt` 对应的当前尝试详情；本包没有完整尝试历史接口，也不应推断或补造历史及主线程/Luna/Flash 交接。以下是供 Flash 实现和主线程实机验收的有限审查，不代表 UI 已实现或通过验收。

## 10 个任务状态映射

以 `state.status` 为唯一任务状态来源。图节点、现有表格和详情应共用同一映射；未知状态须保留并显示原值，不可静默折叠成某个已知状态。

| 原值 | 中文显示 |
| --- | --- |
| `ready` | 待规划 |
| `prepared` | 空会话待确认 |
| `running` | 执行中 |
| `awaiting_review` | 待审 |
| `awaiting_user` | 待用户验收 |
| `accepted` | 已接收 |
| `rework` | 返修待规 |
| `failed` | 失败 |
| `cancelled` | 已取消 |
| `stop_unconfirmed` | 停止未确认 |

`cancel_requested` 是现有 UI 对取消过程的额外临时标签（桥取消 API 返回该值，但不属于上述契约 10 状态）；如需显示，应单独保留为过程提示，不得挤占或改变 10 状态映射。

## 3 个验收值（独立于任务状态）

读取并单独显示 `state.acceptance` 或机器结果的 `result.acceptance`；不从任务状态推算验收结论。

| 原值 | 中文显示/含义 |
| --- | --- |
| `PASS` | 通过（仅已完成验收） |
| `FAIL` | 失败 |
| `BLOCKED` | 阻塞/尚未完成验收 |

特别注意：`awaiting_review` 通常仍为 `BLOCKED`；只读审查后也仍为 `BLOCKED`。`accepted` 且完成证据审查后才会置为 `PASS`。准备/运行中通常为 `BLOCKED`，准备失败会是 `FAIL`。缺失或未知验收值应明确显示未知/原值，不可默认 `PASS`。

## 数据、API 与现有行为核实

- `GET /api/tasks` 返回 `{tasks:[{task_id, task, state, job}]}`；`task.dependencies` 来自契约，是边的唯一事实来源。不要生成推测边或虚构节点。列表端遇到单条记录读取/校验错误会跳过该条，因此不得把接口返回的列表称为完整历史。
- `GET /api/tasks/{id}` 返回 `task/state` 以及 `result/job/native/native_observation/attempt/activity` 等当前详情；只包含当前 `current_attempt`，不是完整历史。点击节点应选中真实 `task_id` 并复用此详情接口。
- `schemas/task.schema.json` 声明 `dependencies` 为任务编号数组；`bridge.validate` 校验格式，规划时还要求依赖任务存在且已 `accepted`。UI 仍须能明确呈现缺失依赖，不能因依赖记录未出现在返回列表而伪造其状态。
- `task.model` 可为 null（提交校验并不强制模型字段）。null/缺省模型显示“未记录”，不可显示成 `null`、空白或伪造模型名；非空值照实显示。
- `web/app.js` 目前仅有一个 `setInterval(refresh, 5000)`，另有手动刷新。保留唯一轮询器并将图更新接入现有刷新/列表状态；不要给图另加轮询。记录最后一次成功刷新时间；刷新失败应可见显示断连及旧时间，保留已有数据但停止图动画/自动运行，不得让旧时间看起来像刚刷新成功。不要触发 `native-status` 或模型请求。
- `webui.py` 目前只显式提供 `/`、`/app.js`、`/style.css` 静态路由及 `/api/...`；继续使用逐项白名单，禁止由请求路径拼接任意文件路径。响应 CSP 当前 `script-src 'self'`、`style-src 'self'`，应保持，不使用 CDN 或内联脚本/样式绕过限制。
- `BridgeHTTPServer.runtime.loaded_sha256` 当前记录 bridge/adapters 和四个现有 UI 文件哈希。新增且实际加载的本地 JS/vendor 资源必须纳入实际字节 SHA-256；同时更新 build 标识。不得把未加载的资源宣称为 runtime 已加载哈希。

## 给 Flash：逐文件允许清单与最小实现指令

以下是 `docs/B1-contract.md` 给出的本项工作候选/允许路径，实施应只触及实际必要项；不得借此扩大到桥逻辑、认证或存储。图功能主体以 `web/task-graph.js` 单独实现，组件缺失须报错；组件异常时既有任务表必须仍可用，不做假图降级。

| 路径 | 用途/边界 |
| --- | --- |
| `web/index.html` | 加入图容器、可访问文本/任务列表入口，并按 CSP 以本地脚本方式加载组件。 |
| `web/app.js` | 复用现有任务列表、详情选择、搜索及唯一 5 秒刷新器；同步图与搜索过滤；接入断连和最后成功刷新提示。 |
| `web/style.css` | 只增加图、状态、断连及窄屏布局样式；不改变无关 UI。 |
| `web/task-graph.js` | 新图组件；真实任务节点、仅按 `dependencies` 连边、内置 `breadthfirst` 布局、节点选择打开真实详情、空图/缺失依赖/未知状态可见，并提供可访问文本列表。 |
| `web/vendor/cytoscape.min.js` | 仅在使用 Cytoscape 时加入固定版本的本地 core；不引入布局扩展或 CDN。依赖下载和来源/hash 按 B1 约束记录。 |
| `web/vendor/cytoscape-LICENSE` | 随固定 Cytoscape core 保存其对应许可证。 |
| `web/vendor/NOTICE.md` | 记录本地依赖来源、固定版本、校验值及许可证信息；不得写入秘密。 |
| `webui.py` | 仅为新增静态资源增加精确白名单路由；将实际加载的新 JS/vendor 纳入 `loaded_sha256` 并更新 build 标识；保留原 API 和 CSP。 |
| `tests/graph_checks.py` | 如新增定向检查脚本，须支持 `--limit N`；按 B1 的三组定向范围实现，不运行全量任务。 |
| `docs/B1-implementation.md` | 如需记录实现/检查结果，仅报告实际变更和真实结果；失败保留原错误，未执行不得称通过。 |

明确不改 `bridge.py`、`adapters.py`、schema、认证、原 store、执行/接收逻辑；不新增完整历史 API、不启动模型、不伪造人工验收证据。新增第三方文件限 B1 明确允许的固定 Cytoscape core 与其 LICENSE/NOTICE，禁止其他外部探测。

## 实机验收清单（主线程执行；本次未执行）

1. 在主线程指定的本机环境启动/使用服务；确认只监听 `127.0.0.1`。核对 runtime build 已更新，`loaded_sha256` 与实际加载资源字节一致；确认 CSP 仍为 self 限定、无 CDN 请求、无 CSP 控制台错误。
2. 使用主线程提供的真实、获准任务数据核对表格、图节点、详情三处的 10 状态映射及未知原值；逐项核对 `PASS/FAIL/BLOCKED` 独立显示。`awaiting_review`/只读审查不得显示最终 PASS。无合适真实样本时记录未覆盖，不手工伪造状态或验收证据。
3. 核对 null/缺省模型显示“未记录”；含依赖任务按真实 `dependencies` 绘边。缺失依赖有明确提示且不伪造节点/状态；点真实节点打开对应任务详情，详情明确只是当前尝试而非完整历史。
4. 输入现有搜索词，确认图与任务表同步过滤；确认图有可访问文本任务列表、空数据提示及窄屏布局。Cytoscape 缺失/加载失败时应明确报错且表格仍可操作，不出现假图降级。
5. 观察网络/请求节奏：只有一个 5 秒刷新器，手动刷新正常；不新增轮询、不请求 `native-status`/模型。断网或 GET 失败时可见断连与最后成功刷新时间，已有数据不被误报为新鲜，也不继续图动画。
6. 核对白名单内各静态资源可访问，未知路径、路径穿越及任意文件读取均被拒绝；原任务列表、选择、详情和现有操作入口仍可用。
7. 按 B1 三组定向检查范围核对状态/验收/未知/null、依赖选择/断线、静态路由/原 UI 兼容。若 Flash 提供检查脚本，先按要求使用 `--limit 3`，只报告实际运行结果；不得以 `awaiting_review`、代码已写或脚本未运行宣称最终验收 PASS。

## 本次执行记录

仅新增本审查文档。未修改代码/API/schema/store；未启动模型、服务或浏览器；未联网、未运行测试。审查限制：未提供完整历史接口或当前尝试以外的历史数据，故本报告不对历史视图作实现/验收结论；实机验收须由主线程执行并提供真实观察结果。
