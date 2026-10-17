# DSH 桥 + 舆情监测 · 架构与文档

本仓库汇总两个在用的本机系统，以及它们的架构文档、方案与审查记录。

| 目录 | 内容 | 上游位置 |
|---|---|---|
| `bridge/` | 协议桥 agent-bridge 源码（bridge.py / adapters.py / webui.py / schemas / skills / templates / tests / web / docs） | `…\work\bridge-b1-source`（HEAD 见下方快照） |
| `sentiment-monitor/` | 舆情监测平台源码（server.py / taskboard.py / storage.py / analysis.py / 前端） | `…\outputs\sentiment-monitor`（HEAD `a035e7d`，tag `v0.05`） |
| `docs/` | 交接文档、系统方案、R0 审查结果 | `D:\DSH\output\2026-10-17\2026-10-17-16-23-06` |

## 快照信息

- 导出时间：2026-10-17T19:59:22
- 桥源码 HEAD：见 `bridge/EXPORT-HEAD.txt`
- 舆情 HEAD：见 `sentiment-monitor/EXPORT-HEAD.txt`
- 未包含：`data/`（桥的 store 与夹具）、`*.db`（舆情生产库）、日志、`__pycache__`、备份目录

## 两个系统的关系

协议桥负责「契约 → 计划 → 独立 worktree 执行 → 证据回传 → 评审门」，是通用执行器；
舆情监测平台是它的一个业务落点（采集/去重/分析产物进 `sentiment.db`），`taskboard.py` 是桥任务只读大屏。

架构细节见 `docs/系统方案-舆情平台架构与复用-20261017.md`，系统现状见 `docs/交接文档-协议桥与舆情系统-20261017.md`。
