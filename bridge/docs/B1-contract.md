# 桥只读协同图契约
在桥UI增加本地Cytoscape core任务依赖图，内置breadthfirst布局，不加布局扩展；只GET /api/tasks及选中GET /api/tasks/{id}，不改舆情产品、不跨系统。
10状态ready待规划/prepared空会话待确认/running执行中/awaiting_review待审查/awaiting_user待用户验收/accepted已接收/rework返修待规划/failed失败/cancelled已取消/stop_unconfirmed停止未确认。acceptance PASS/FAIL/BLOCKED独立显示，未知原值可见，null模型显示未记录。
节点是真任务，边来自task.dependencies；当前尝试复用详情，不实现完整历史接口、不编造主线程/Luna/Flash交接。图筛选与现有搜索同步，点击打开真实详情，有可访问文本列表、来源和最后成功刷新时间。复用唯一5秒刷新器，不重复轮询；失败可见断连与旧时间，不继续运行动画，不触发native-status/模型请求。空图与依赖缺失明确。组件缺失报错，表格仍可用，不假图降级。
静态资源路由仅允许webui.py显式白名单，无任意路径读取，不削弱CSP self；runtime.loaded_sha256加入新本地JS/vendor实际加载哈希，变更有新版build标识。
候选文件：web/index.html,web/app.js,web/style.css,web/task-graph.js,web/vendor/cytoscape.min.js,web/vendor/cytoscape-LICENSE,web/vendor/NOTICE.md,webui.py,tests/graph_checks.py,docs/B1-implementation.md。Luna先确定清单，不改bridge.py/adapters.py/认证/原store/执行接收逻辑。
Flash检查限三组且脚本支持--limit 3：状态/验收/未知与null、依赖选择与断线、静态路由与原UI兼容；不得调用模型或持久启动服务。每组边界如实报告。Luna拆解600s、Flash实现900s、Luna复审600s，包内模型累计3600s，主线程实机不计入；不自动重试。
