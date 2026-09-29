# Phase 4C 完成报告 — MCP Server

## 1. 文件结构（本次新增/修改）
```
/root/torrent-tool/
├── server.py                      # 修改：mcp 挂载 + /mcp Bearer 中间件 + mcp_tokens admin 接口 + _get_torrent_cached + _mcp_deep_check
├── mcp_server.py                  # 新增：FastMCP 实例 + 4 个工具（纯薄适配，不重新实现搜索/健康逻辑）
├── torrent_core/db.py             # 修改：mcp_tokens 表 + mcp_token_create/list/auth/action + find_cached_result/cached_l1/cached_l2
├── static/index.html              # 修改：新增 "MCP" 后台 tab（create/禁用/删除，token 仅创建时显示）
├── venv/                          # 新增：Python 3.13 虚拟环境（fastapi 0.141.1 / fastmcp 4.0.3 / libtorrent / starlette 1.6.0 / uvicorn）
└── torrent.db                     # 表 mcp_tokens 已建
deploy/torrent-tool.service         # 修改：ExecStart 使用 venv 路径
```

## 2. 端点
- 外部（反代）：`https://hxicc.catmicos.dpdns.org/mcp`（Streamable HTTP，仅此一条新增）
- 内网：`http://127.0.0.1:10011/mcp`（uvicorn 同一进程内 sub-app mount）
- 后台接口新增：`GET /api/admin/mcp_tokens`、`POST /api/admin/mcp_tokens`（创建，明文仅返回一次）、`POST /api/admin/mcp_tokens/{id|name}/action`（disable/enable/delete）

## 3. FastMCP 版本
- **fastmcp 4.0.3**（pip show 已确认），Python 3.13.5。
- 仅使用 `http_app(path="/mcp", stateless_http=True)` Streamable HTTP + `mcp.tool` 注册；未用 SSE。
- 与既有 FastAPI 通过 `app.mount("/", _mcp_app)` 并把 `_mcp_app.router.lifespan_context` 作为 FastAPI lifespan，解决 FastMCP task group 未 init 的 500 错。**无第二个长期 Python 服务进程**（ps 只有一个 server.py 实例）。

## 4. 认证
- 仅支持 `Authorization: Bearer <MCP_TOKEN>`；无 token / 错 token / disable / 过期 → **401 `{"error":{"code":"AUTH_REQUIRED"}}`**（本地与 HTTPS 反代两条路径都返回 401，见测试）。
- `/mcp` 不走 Web session cookie，`/api/*` 不接受 Bearer MCP token（二者完全独立）。

## 5. Token 架构 / Schema
```sql
CREATE TABLE mcp_tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,   -- sha256 hex；仅此字段
  created_at REAL, last_used_at REAL,
  enabled INTEGER DEFAULT 1,
  expires_at REAL                    -- NULL = 永不过期
);
```
- 明文 token 仅在 `POST /api/admin/mcp_tokens` 创建时返回一次（`token` 字段），之后任何接口/日志/DB 均无法还原。
- 使用时 sha256 输入与 DB 内唯一 hash 对比（`secrets.compare_digest`），成功即更新 `last_used_at`。

## 6. 工具 Schema（严格 4 个；不新增其它工具）
| Tool | 主要入参 | 行为 |
|---|---|---|
| `search_torrents` | query(1-256), limit(1-50), precision(strict/balanced/broad), sort(relevance/seeders/freshness/health), filters(year/quality/language/min_size/max_size/category/source) | 完全复用 torrent_core.parse_query+recall+dedup+apply_ranking+precision_filter。结果带 rank/title/info_hash/magnet/size/category/published_at/api_seeders/api_leechers/score_breakdown/sources/health_status=not_checked + source_status[] + skipped + note |
| `check_torrent` | info_hash(40hex) | 复用 l1_core（UDP BEP15 scrape，TTL 缓存）。status=🟢 active / 🔴 no_seed / 🟡 insufficient / （⚪ not_checked） |
| `deep_check_torrent` | info_hash(40hex), timeout(默认60) | 复用 L2：子进程 + libtorrent 短会话。并发=1；忙时立即返回 `{"status":"busy","message":...}`，无队列 |
| `get_torrent` | info_hash(40hex) | 仅从 cache 表 (s:* / l1:* / l2:*) 查找；若未在缓存则返回 `not_found`；不做互联网搜索 |

## 7. 返回示例
实际抓包响应（ubuntu 查询）：
```json
{"error": null, "payload": {
  "query": "ubuntu", "total_candidates": 10, "returned": 1,
  "results": [{"rank": 1, "title": "ubuntu-23.04-desktop-amd64.iso",
             "info_hash": "443c7602b4fde83d1154d6d9da48808418b181b6", "size": 4932407296,
             "api_seeders": 163, "api_leechers": 340, "ranking_score": 0.7788,
             "relevance_score": 0.75,
             "score_breakdown": {"relevance": 0.3, "seeders": 0.2, "freshness": 0.1498,
                                  "source_quality": 0.079, "size": 0.05, "health_evidence": 0.0},
             "sources": [{"name": "solidtorrents", "seeders": 163, "leechers": 340}],
             "health_status": "not_checked"}],
  "source_status": [{"name":"apibay","status":"ok"},{"name":"nyaa","status":"ok"},{"name":"solidtorrents","status":"ok"}]}}
```
错误统一外层 `{"error":{"code":"...","message":"..."},"payload":null}`；api_seeders 与 live/实时证据（`tracker_results` / L2 数值）保持独立字段，绝不混算。

## 8. 共享核心（Web 与 MCP 不出现双套逻辑）
- `/api/search` 与 MCP `search_torrents` 调用同一个函数 `search_core()`。
- L1：同一 `l1_core()`。L2：同一 `_L2_LOCK`/子进程路径。
- 校验（MCP-11）：同一 query 内部回放 Web 与 MCP → `web 条数 10 / mcp 条数 10 / 两路结果一致 True`。

## 9. L1 / L2 复用
- `check_torrent` → 直接调用 `l1_core()`，返回与 Web `/api/l1` 完全一致的字段 + machine 状态（`active`/`no_seed`/`insufficient`）。
- `deep_check_torrent` → `_mcp_deep_check()`：非阻塞 `_L2_LOCK.acquire(False)` 忙则 busy 返回；同 run_l2 相同的子进程+超时+`/tmp/tt-*` 清理机制；timeout 覆盖语义。

## 10. 测试结果（MCP-1..MCP-14）
| # | 项目 | 结果 | 证据 |
|---|---|---|---|
| 1 | 无 token | 401 | `curl POST /mcp` → 401 |
| 2 | 错 token | 401 | `wrong-wrong` → 401 |
| 3 | tools/list | ok | `['search_torrents','check_torrent','deep_check_torrent','get_torrent']` |
| 4 | search_torrents | ok | the matrix 5 条, ranks 1-5, source_status 3/3 ok |
| 5 | check_torrent | ok | status active / responded 4/6, elapsed 5.1s |
| 6 | 并发 L2 busy | ok | A(0.1s,busy) B(62.0s,observed) |
| 7 | get_torrent | ok | cached & 历史 & not_found 三种（见 §6/§7） |
| 8 | source failover | ok | 禁用 apibay → source_status 只剩 nyaa/solidtorrents 仍返回 3 条；再启用恢复 |
| 9 | revoke → 401 | ok | `temp-revoke-test` 创建→调用 ok→delete→后续 curl 401 |
| 10 | 日志不含 token | ok | 209 条日志里 `1QPz_* / uk1k_* / Bearer / Authorization` 0 命中 |
| 11 | Web/MCP 结果一致 | ok | True（见 §8） |
| 12 | 资源/RAM/CPU | ok | RSS 122MB（venv 引入 pydantic/fastapi），sleep 后回到 ~3MB idle；CPU 1.2% |
| 13 | systemd 重启 | ok | restart 后 active，web:200 / mcp:401 / https-mcp:401（token 失效依旧） |
| 14 | L2 kill | ok | kill -9 l2_check 后 5s 内 pgrep=0、/tmp/tt-*=0，锁自动释放，后续请求正常 |

## 11. 已知问题
- `ps -ef | grep [m]cpvenv` 残留：`/tmp/mcpvenv/` 旧 bash 探测进程（PID 12584，07:12 起，0 CPU），与当前服务无关，未删（非本会话创建）。
- `get_torrent` 若 `_cache_live=False`（TTL 过期但记录还在）返回 title + `_cache_live:false` 提示；严格 TTL 内才默认。
- sync（`sort=health`）暂只在 ranking 权重里为 0（暂不展示数值）。
- HTTPS 反代 `/mcp` 直接走通（curl 401 即证明可达）；未额外做 nginx location 插入（按您约定不自改现有 nginx）。

## 12. 4D 建议
暂不进入。待 4C 验收后您再决定方向。
