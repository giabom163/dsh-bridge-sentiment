# M3 证据、预算、恢复契约

状态：程序内核已实现；生产模型链路 BLOCKED。没有生产 HTTP 客户端、认证或模型配置的真实读取证据。开发 CLI 不作为业务模型，不将开发执行器请求数冒充业务预算。

## 入口与数据

- `POST /api/analysis` 仅 `{group_id,record_ids}`。人工分组不等于自动事件。受服务 `--limit` 限制；回读原文件、校验 sha256、核对标准化字段，再保存不可变证据包。
- 相同分组、输入哈希、规则和模型版本复用同一 ID。输入包含提示版本，原文变化、成员或提示变化产生新输入哈希。正式适配器必须固定真实 model_version，当前是 not-configured。
- `GET /api/analysis` 返回最多 100 批的证据包、请求账本、质量候选。费用未知为 null。
- `POST /api/analysis/decision` 仅 `{job_id,decision,reason}`。只有 B 全部 supported 后的 human_review 可人工 accept/reject；理由必填，事务记录。
- 创建成功为 201，生产未接入状态仍是 blocked，保留 adapter_not_configured。201 仅表示证据包创建成功。不存在假模型成功响应或自动接收。

## 结构化契约

A 输出仅 `claims` 数组，每条严格为：`id,text,target,context,citations,uncertainty`。target 枚举 male/female/both/unspecified；context 枚举 assertion/quotation/rebuttal/uncertain。只分析文本明确讨论对象，不推断作者性别。不接受模型提供的统计数字字段；程序统计在 packet.statistics。

B 输出仅 `reviews` 数组，每条严格为：`claim_id,verdict,reason,counterevidence`，必须一一覆盖 A。verdict 为 supported/partially_supported/unsupported/unverifiable。部分支持、不支持必须附反例；只有全部 supported 进入 human_review，其他阻塞。文本语义仍需真正模型和人工判断，字符校验不会自动识别反讽或冲突。

引用严格为 `{record_id,raw_sha256,field,start,end,quote}`。field 为 title/summary，位置是原始快照 JSON 字段解码后的 Unicode 字符（Python code point），`[start,end)`；不是 UTF-8 字节或 JavaScript UTF-16 索引。允许快照中的原始 HTML/空白原样参与引用，不把清洗后偏移当原始偏移。校验哈希、字段、区间和完整相等；缺失或错误直接阻塞，不自动修 JSON 或追加模型请求。

## 请求与恢复

`request_once` 是内部适配接口，不对 HTTP 页面开放。SQLite BEGIN IMMEDIATE 抢占角色槽并提交后，才调用 adapter。A→B，最多两个角色槽，不回收失败配额，不重发同角色，不自动工具续轮。异常记 uncertain，启动将 reserved 转 uncertain 并阻塞。结果已经送达但落库失败也会保留 reserved，重启后不自动重发。

当前仅隔离 fixture 使用单次本地回调验证状态机；不能据此宣称生产 HTTP 两请求上限已实证。后续生产客户端须每次最多一个真实 HTTP 请求、关闭 SDK/连接层重试、禁用工具续轮，服务端 usage 原样落账，完成配置读取方、凭据、真实调用证据后才接线。

## 候选规则与边界

先质量再比较：未知时间比例 >20% 暂停比较；不足 14 个经完整覆盖确认的有效日为 baseline_insufficient。生产目前没有完整日覆盖证明，因此不会把没抓到记录的日期补成 0。候选不发送任何通知。

纯程序函数保留未校准候选参数：14 日、日计数最小样本 3、MAD 倍数 3；低样本明确返回 insufficient。MAD=0 用 `median+max(3,median)`，否则 `median+3*MAD`，严格大于才候选。缺失日必须使 history 少于14，不能补0冒充完整覆盖。此函数尚未接入真实连续窗口或可靠预警。

候选按人工分组＋规则＋级别幂等，保留首次触发快照，不覆盖历史指标。输入变化可新建分析批次；显式预警重评、连续窗口、升级历史、自动聚类和报告接收结果导出尚未实现，不能声明全套预警已完成。

## 定向检查

`tools/manual_m3_check.py --limit 3 --work-dir <隔离目录>`：引述反驳、来源冲突、样本不足三组；同时核对预算不重发、未知请求重连恢复、引用区间、输入幂等和人工状态门。无网络，无业务模型，隔离响应不进入产品数据库。

最终 M3 仍需 DSH 独立核查和生产真实路径；本轮不追加第三个外部模型任务。固定反例通过不代表生产语义准确率。
