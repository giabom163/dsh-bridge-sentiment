# M0/M1 数据与接口契约

版本 m1-v1，2026-10-15。状态 BLOCKED：三项隔离样本检查已返回 PASS，未完成 DSH 审查和用户真实路径验收。Git 已初始化，未自动提交；五个原型文件的原始字节和 SHA-256 保存在本轮工作区证据中。

## 范围

标准库、SQLite、单进程写锁、手动限额批次。不接入模型、调度、真实来源、事件聚类、预警、通知、WorkBuddy 或部署。开发桥与业务服务独立。开发桥此次仍不能承诺系统注销或机器重启后存活；没有配置自启。

## 数据约定

- 时间全部存 UTC；页面上海时间。published_at 缺失、非法或无时区时为 null；time_quality 为 missing / invalid / timezone_missing，可信格式为 valid。Atom updated 不冒充发布时间。fetched_at 独立保留。
- 输入记录保留为内容寻址 JSON 快照。快照使用排序键、紧凑 UTF-8 JSON；raw_sha256 校验这个文件。RSS 的 raw_xml 为解析器序列化的条目元素，**不是 HTTP 原始字节存档**，不能用于声称字节级还原整个响应。
- records 保留来源 ID、来源内 ID、URL、内容哈希、原文哈希和路径、批次及规范记录 ID。API 证据详情回读时重新校验快照哈希。
- 正文按去 HTML、实体解码和空白折叠后的 [title,summary] JSON 计算哈希；精确相同才共用 canonical_records。规则 exact-text-v1。不做模糊聚类，不把规范记录视为事件。
- URL 规范化只处理协议/主机大小写、默认端口、空路径和片段；保留 query。相同 URL 的不同正文保留不同版本，通过 canonical_url 关联。相同来源和相同原文快照重复导入不新增记录；跨来源保留 provenance。
- 批次先落 running；输入验证先完成且不写记录，随后快照回读、记录和成功计数在同一 SQLite 事务完成。异常回滚记录/规范记录，再独立保存 failed。快照文件不参与数据库事务，失败时可能有无引用快照；保留作证据，不自动删除。
- 服务启动只将上次残留 running 改为 interrupted；不自动重试、重发请求或采集。
- 旧 items 表原样保留；无法确认旧时间是否回填，故不自动迁移。legacy_items_pending 必须显示待处理数量。初始只读清单未发现 sentiment.db，启动前仍需现场核对。

## API

POST 须同源 http://127.0.0.1:8790，整个 UTF-8 JSON 请求最多 1,000,000 字节，此限制优先于 schema 各字段字符上限。以服务端校验为准。数据库写入串行；RSS 网络抓取不占写锁，同一时刻只允许一个 refresh，重复请求返回 409。

| 路径 | 输入与返回 |
| --- | --- |
| GET /api/runtime | 实际 PID、入口、数据库/快照路径及启动时加载的后端哈希；仅回环诊断，不代表用户身份认证 |
| GET /api/data | feeds/items/keywords 保留兼容字段；新增 stats、runs、manual_limit、legacy_items_pending、schema_version |
| GET /api/runs/{id} | 批次详情，未知 ID 404 |
| GET /api/records/{id} | record 与 raw_snapshot；未知 ID 404，证据缺失/损坏 409 |
| POST /api/feeds | name/url；成功 201，重复 409 |
| POST /api/keywords | keywords 数组；成功 200 |
| POST /api/import | schemas/import.schema.json；有效批次 201（空数组为成功空批次）；记录校验失败 422，磁盘/数据库失败 500，含 run_id 和 failed；来源不存在 400 |
| POST /api/refresh | 手动依次处理按编号排列的前 limit 个来源，每源最多 limit 条；每个来源独立批次；结果含 succeeded/failed。空 results 是未配置来源，成功且 input_count=0 是空结果，不是失败 |

stats 基于全表：record_count、canonical_count、unknown_time_count、unknown_time_ratio（空表为 null）。items 最多 500，runs 最多 100，均明确限额；不能用展示条数冒充总量。所有标签仍是未复核词典粗筛。无总体代表性或准确率承诺。

## 手动验收与未完成项

用户已授权本项目小范围自动测试，规则见 AGENTS.md。tools/manual_m1_check.py --limit 3 的首次运行退出 0，三项检查均 PASS。脚本只在指定的独立工作目录创建样本库和快照，不访问网络、不启动服务。三项：三条来源样本、重复回放、写入中途故障回滚。它不能替代真实 HTTP/UI 验收。

尚需：真实业务服务入口验证、重启恢复验收、DSH 限定只读审查、用户接收。M2 前端完整状态展示和筛选未实现；现有页面只做 null 时间及限额用词兼容修正。
