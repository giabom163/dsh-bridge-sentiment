# 本次桥UI工作范围
主线程指挥与实机；OpenCode Luna拆解/只读审查，V4.1 Flash编码/修复/定向检查。只改任务allowed_paths，不启动其他模型/子代理。
原源码还有其他修改，不回写或覆盖；本副本是完整固定基线。禁止删除/清理/提交/推送；不读token/密码/cookie/认证DB或环境秘密，不改全局配置。
Luna只读代码，仅写指定审查文档；Flash测试脚本支持--limit N，首次--limit 3，不跑全量。服务/浏览器由主线程负责。
允许Flash从官方npm注册表下载固定Cytoscape core及LICENSE，记录hash，不增加其他外部探测、不走CDN。
报告中文；失败保留真实错误，未测试不称通过；awaiting_review与验收PASS分开，不改桥store、不补造人工证据。
