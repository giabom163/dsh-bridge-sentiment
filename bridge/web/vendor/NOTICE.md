# 本地第三方依赖说明（B1 桥协同图）

本目录只包含桥 UI 协同图所需的一个固定版本第三方 core；不含任何布局扩展，也不通过 CDN 加载。

## cytoscape（core）

- 包：`cytoscape`
- 固定版本：`3.34.3`（官方 npm `dist-tags.latest`）
- 来源注册表：`https://registry.npmjs.org/`（官方 npm registry，唯一来源）
- 元数据请求：`https://registry.npmjs.org/cytoscape`（HTTP 200，2026-10-15T12:52:06Z–12:52:07Z UTC）
- 包 tarball：`https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.3.tgz`（HTTP 200，2026-10-15T12:52:31Z–12:52:32Z UTC）
- 两次请求间隔：24.4 秒（≥ 20 秒约束）。仅执行了这两次外部请求，未做其他外部探测。
- 许可证：MIT（见同目录 `cytoscape-LICENSE`）

### 校验值（SHA-256）

| 文件 | 字节 | SHA-256 |
| --- | --- | --- |
| `web/vendor/cytoscape.min.js`（取自 tarball `package/dist/cytoscape.min.js`） | 435503 | `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |
| `web/vendor/cytoscape-LICENSE`（取自 tarball `package/LICENSE`） | 1082 | `eb319c6e6f233607f71e8e2f450391751883cfc0eeb3ca7ef574c13d1d9c2203` |
| 上游 tarball `cytoscape-3.34.3.tgz` | 1378263 | `5d9e479216ed58a5f1ac639e746fd7d9770c4393f8c355dd133abc90bcc9ff3f` |

tarball 的 `sha512`（base64，本地按 tarball 字节计算）：
`sha512-yfYGhRcGAntq6YBD583j4n0Eg3jIxvWmZtz/5uz9UYkeIStSlMxuUja+ec5j3iBD8nv1rwaOAYMW09tBdkSeaQ==`

### 未能核对项（如实记录）

- 受“仅允许 metadata 与 tarball 两次下载”约束，未在 metadata 响应中留存并回读 npm 发布的 `dist.integrity` 字段与本地 `sha512` 逐字节比对；上表 `sha256`/`sha512` 为下载后本地实算值。
- 未运行 Cytoscape 前端，仅在浏览器离线场景由主线程实机核对（见 `docs/B1-implementation.md`）。

未写入任何口令、token 或本机敏感路径。
