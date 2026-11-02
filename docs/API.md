# HTTP 接口（0.1.0）

服务只监听 `127.0.0.1`。除静态页面外，所有接口都需要
`Authorization: Bearer <access-token.txt>`。响应统一 `application/json; charset=utf-8`。

## 静态页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 管理界面（打包在 `she_memory/web/`） |
| GET | `/app.js` `/style.css` | 前端资源 |

## 核心

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 版本、模式、`revision`、节点/边数、解析覆盖、profile 摘要 |
| GET | `/v1/capabilities` | 能力开关（当前 `source_write=false`） |
| POST | `/v1/refresh` | 重新读来源并建图；返回 `revision/units/nodes/edges` |
| POST | `/v1/query` | `{text, max_items?, context?}` → 见下 |
| POST | `/v1/fetch` | `{unit_id, expected_source_version}` → 原文；版本不符返回 409 `source_changed` |

`/v1/query` 的 `status`：`ok` / `ambiguous` / `insufficient` / `no_match` / `navigation`（还有 `unavailable`）；
条目带 `citation.path`、`citation.lines`、`source_version`、`path.edges`（真实边 id）。

## 图

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/graph/view` | `{mode: "overview"}` 取全量；`{mode:"local", focus?, limit?}` 取有界局部；不传 `mode` 保持旧的局部行为 |
| POST | `/v1/candidates/detail` | `{candidate_id}` 或 `{candidate_ids:[...]}`：两端标题/路径/小节/行号/版本状态与 `can_accept` |
| POST | `/v1/graph/edit` | `{action, payload, expected_revision, idempotency_key}`；action ∈ `connect/disable/restore/accept/reject` |

`overview` 响应字段：`revision, mode, total_nodes, total_edges, returned_nodes, returned_edges,
partial=false, coverage, nodes, edges, candidates, candidate_meta`。
边的 `lifecycle` 为 `active|disabled`；停用边仍在视图里，但不参与检索遍历。

`candidates/detail` 的端点 `status`：`ok` / `stale`（版本变了）/ `missing`（文件不在了或快照无此单元）/ `unreadable`（权限或读错），
并带 `reason`（`version_changed` / `file_not_found` / `unit_not_in_snapshot` / `permission_denied` / `oserror:<errno>`）。
`can_accept=false` 时界面会禁用接受；拒绝仍然可以记录（reject 不依赖来源可读）。

编辑并发：`expected_revision` 不等于当前版本 → 409 `revision_conflict`，**不自动重试**。

## 布局

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/views/default` | `{revision, positions}` |
| POST | `/v1/views/default` | `{expected_view_revision, positions}`；`positions` 是 `{key: {x, y}}`，键按视图作用域命名（`overview\|<id>`、`local:<focus>\|<id>`），旧版裸 id 仍可读 |

## 错误

| code | HTTP |
|---|---|
| `unauthorized` | 401 |
| `forbidden_host` / `forbidden_path` | 403 |
| `revision_conflict` / `source_changed` / `idempotency_conflict` | 409 |
| `not_found` / `trace_expired` | 404 |
| `not_supported` | 501 |
| 其余（`invalid_*`、`request_size_limit`、`invalid_layout`…） | 400 |
| 服务内部不可用 | 503 |
