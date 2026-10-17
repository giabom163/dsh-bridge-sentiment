# B1 UI 修复说明

## 问题一：hidden 遮挡（已修）
`.graph-error,.graph-empty{display:grid}` 压过 `[hidden]` 默认规则，设 hidden 后仍盖住画布；补 `.graph-empty[hidden],.graph-error[hidden]{display:none}`。

## 问题二：图几乎成细线（改 task-graph.js）
1. label 原用 goal（旧任务缺 title），fit 被撑到极小；现 taskLabel 优先 title，无 title 用 task_id，限 28 字符；列表同改，goal 仍留详情。
2. 无依赖边原单排成行，现用内置 grid 多列；有边仍 breadthfirst。
3. 过滤后 fitVisible() 按可见节点取景。

未改依赖/API。

## 未验项
未起服务与浏览器，未实机验证；交主线程复测，不给 PASS。
