# Phase 4B 完成报告

## 日期 / 环境
- 2026-09-15（UTC)；VPS `rn`（107.173.154.178, Debian 13, 1核 / 987MB RAM / 14GB盘）
- Python 3.13 + FastAPI + uvicorn；无 Node/无 npm/无任何外部框架
- 服务端监听 `127.0.0.1:10011`（systemd `torrent-tool.service`，`MemoryMax=400M`）

## 一、本次做的事（对照 4A 批准的范围）
| 交付物 | 状态 |
|---|---|
| `torrent_core/` 子包（db, parser, dedup, ranking, sources/*） | ✅ 完成 |
| 插件式 Source：BaseSource + 3 个内置（apibay / nyaa / solidtorrents）自动注册 | ✅ 完成 |
| L1（UDP BEP15 tracker scrape）& L2（libtorrent 子进程）复用 | ✅ 完成 |
| 用户/角色/会话（scrypt + HttpOnly Cookie + 登录失败锁定） | ✅ 完成 |
| 管理后台 4 面板（数据源 / 设置 / 用户 / 系统+日志） | ✅ 完成 |
| SQLite `users/sessions/sources/search_settings/logs` + TTL 缓存 | ✅ 完成 |
| 新 UI（登录页 + 面板 tab + 搜索/检测流程，保持既有功能） | ✅ 完成 |
| 13 项验收测试 | ✅ 全部通过（下详） |
| **MCP / MCP Token 表 / 4C 内容** | ❌ **未做（按指令禁止）** |

## 二、核心改动文件（本地 = `/root/deepseek/torrent-tool/`，VPS = `/root/torrent-tool/`）
```
server.py                          # 重写：API 编排（/recall/dedup/rank/L1/L2/admin）
torrent_core/                      # 核心（全部重写/新写）
  __init__.py                      # load_sources() 自动注册插件的入口
  db.py                            # users/sessions/sources/search_settings/logs + TTL cache
  parser.py                        # 纯正则 tokenize + 相关度
  dedup.py                         # 严格 info_hash 主键，多源合并
  ranking.py                       # 可配置权重，precision 过滤（strict/balanced/broad）
  sources/
    __init__.py
    base.py                        # BaseSource + TorrentResult + _http_get (Phase 3 语义)
    apibay.py                      # ✅ 实测 200，中文跳过
    nyaa.py                        # RSS UI，中文资源
    solidtorrents.py               # bitsearch.eu + UA + 1.5s retry
static/index.html                  # 重写：登录页 + admin tabs (数据源/设置/用户/系统)
config.json                        # 移除 auth_user/auth_pass（改走 scrypt DB users）
deploy/torrent-tool.service        # 更新 ExecStart + TT_LISTEN 环境
```

## 三、验收测试（编号、操作、真实证据）
### T1 匿名访问受限
```
/api/search?q=x            401
/api/l1?hash=d160…1f7      401
/api/l2                    401
/api/me                    401
```
### T2 登录/登出会话
- `POST /login admin/Jxxxxx` 200 → `{"username":"admin","role":"admin"}`，HttpOnly cookie 只含 token
- `POST /login admin/wrongpass` 401（错密码拒绝）
- `POST /logout` 后 `/api/me` 401（会话失效）
### T3 认证后搜索
- `ubuntu` 查询：返回 50 条（3 个源全活：apibay 50, nyaa 5, solidtorrents 20 合并后 50 条）
- 时延 ~几十 ms（缓存+并发）
- 去重一致：50/50 唯一 info_hash
### T4 中文 / 非 ASCII 查询跳过 apibay
- `甄嬛传` 查询：atisfies `skipped: apibay 跳过：TPB 对非 ASCII 关键词无匹配能力`
- 返回 19 条（solidtorrents 索引） + nyaa 1 条，`API 数据` 真实数据
### T5 严格去重（多源合并）
- 直接调用 `dedup_merge`：原始条目 70 → 去重后 68（唯一 info_hash 68）
- 多源合一：2 个 hash 出现两个来源（`232cd67eb3...` / `d540fc48...`），各自独立记录 seeders
  （apibay=27 / solidtorrents=78；apibay=6 / solidtorrents=28）
### T6 权重变化改变排序
- before（w_relevance=0.4, w_seed=0.2）Top5 包含 The Matrix 2160p AI Ups
- after  (w_relevance=0.02, w_seed=0.75) top5 顺序改变（第 2 位 The Matrix (1999) 2160p YT 替代）
  → `RESULT:权重生效-顺序改变`
### T7 source 单点故障容错
- `POST /api/admin/sources/solidtorrents/toggle` 后 count=50 (apibay/nyaa 全活)
- 再切换回来 count=50 (3 源)，`ok=True`
### T8 角色硬隔离
- admin 创建 `paul`（user）→ 登录
- `/api/admin/sources` → 403；`/api/admin/settings` POST → 403；`/api/admin/users` 无法改
- `/api/search?q=ubuntu` → 200（可搜索）
### T9 数据库无明文密码
- `users` 表 `pw_hash` = 64 位 hex（scrypt 32B）、`salt` = 32 位 hex；DB 文件不含 admin 原始密码与 `FT1k25UBlRnd`
### T10 L2 全局单任务
- 同时两个 L2 请求 → 第二个马上 429 `正在检测，请稍候（全局仅允许 1 个深度检测任务）`
- L2 结果：`dht_nodes=369, peers_seen=36, num_peers=33, connect_candidates=688, metadata=false, elapsed=60.0S`（真实值）
### T11 L1 实时 UDP scrape
- `hash=d160b8d8ea35a5b4e52837468fc8f03d55cef1f7` → `🟢 活跃, 3/6 tracker 返回 peers, max seeders=18`
### T12 磁盘 / L2 临时目录
- 服务变量体积仅 `torrent.db = 1MB`；`ls /tmp/tt-*` = 0（L2 子进程退出已清理）
- 磁盘占用 `/` 仅 3.7G / 9.9G 可用
### T13 登录失败锁定 / 日志脱敏
- 连续 5 次错密码 → 第 5-/6 次 429 触发（15 分钟内限制）
- `logs` 表 189 条（admin/pass/token/head 无一出现；只记 username 与 IP，无任何密码 / Authorization / token）

## 四、其他容错与行为
- **X-Forwarded-For**：user 会走您自己配的反代；per-IP rate-limit / 登录失败锁定改认首个 XFF ip。
- **Privacy**：管理后台所有敏感数据（用户列表除外）均不暴露在日志；`logs` 表只存事件。
- **DHT/L2 短时**：子进程始终挂起或正常结束（无强制 kill），`runtimes (libtorrent)` 未持久数据，`/tmp` 目录 auto cleanup。
- **`torrent.db`**（现 1.0 MB）不保存 torrent 资料，只存 users/sessions/sources 状态/logs/搜索 TTL 缓存。

## 五、测试中发现并修复的问题
1. `sources/` 文件夹无 `__init__.py` → pkgutil 什么都不加载（空源）。已添加。
2. `apibay.py` 残留 `_db_cfg` / 旧 `_db` 属性名 → 换到新 `getattr(self,'_runtime_cfg',{...})`
3. `ranking.py` 缺 `precision_filter` server 使用 → 已补（strict/balanced/broad）。
4. `server.py` 内 `source_quality_value` 名字不一致 → 统一 `source_quality`。
5. `_gs` helper 名字与 sed 误删的 default fallback 拼接错 → `tdb.get_setting(key, default)` → `gs(key, default)` 统一 helper。
6. `admin_settings_get` dict 引用 `l2_timeout` 等扩展 key 不在 DEFAULTS → 增加 `EXT_DEFAULTS` 合并，并提供 `gs()` 默认值 fallback。
7. `static/index.html` 6 处 JSON 模板字符串有 NaN-like `??""` 硬值 `?? "["}""` 语法垃圾 → 修为规范模板：`value="${s[key]??""}"`。settings 面板渲染 18 个输入框。
8. **HTML 里 `<script>` 缺失 `</script>` 结尾标签 → 整个 script 块不自动执行 → 首页按钮无响应。**（这次关键修复）
9. `torrent_core/__init__.py` 引用错误名（`rank_results` / `.base` / `.sources.dedup`） 改为当前实际接口。
10. 逻辑功能回归测试确保 previous features 依然可用（并指出：`server.py` 现在不再监听 0.0.0.0）。

## 六、遗留 / 限制
- （无）；确认使用流程：
  1. 管理员登录 → 管理面板调整（搜索精度 / 权重 / 优先级 / Source 开关 / 用户）
  2. 普通用户 login → 搜索 → 一键 L1 实时检测 / L2 深度检测 / 复制 magnet 到本地 qBittorrent
- **下一阶段：4C（MCP 服务器）** —— *等待您的明确指令才开始*。4B 内没有任何 MCP 帧代码。

## 七、值得注意的数字 / 现实约束
- **同一服务端进程里启用的插件**：3 个；`source_concurrency` 3 单线程并行；L1 `BoundedSemaphore(2)`；L2 全局 1 个（thread Lock）。
- **搜索缓存 TTL**：600s（可调）；**L1** 300s；**L2** 900s。
- **session ttl**：7 天（604800s，可通过设置）；**HttpOnly / SameSite=Lax**；安全 cookie 由 `cookie_secure` 可控（部署反代 HTTPS 后可开）。
- **rate-limit**（token bucket）：search 30/0.5·s；l1 12/0.2·s；l2 6/0.05·s。
- **单实例 L2 lock**：429 会立即拒绝第二个并发请求，不排队、不竞争。
- **admin 默认密码只打印一次**：`[boot] admin created; ONE-TIME password in journal；密码 24 位随机；"sha256 hash of token only" in DB`，用户首次登录后可改为自己的密码。

## 八、需要在您部署反代后补做的
1. `cookie_secure=true`（如果您以后用 HTTPS 反代）→ 在 管理面板 - 设置 中改 1 处。
2. 反代需保留 X-Forwarded-For（已支持）。
