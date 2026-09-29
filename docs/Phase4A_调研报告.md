# Phase 4A — 调研 + 架构设计 + 数据源验证（报告，未开发）

环境：VPS `rn`（Debian 13, Python 3.13），全部结论为实测，非猜测。**不出进入 4B/4C/4D。**

---

## A. 数据源验证（全部在你 VPS 上实测）

### ✅ 可用（推荐纳入插件列表）

| Source | 协议 | info_hash | seed/leech | size | files | 中文 | 延迟 | 判定 |
|---|---|---|---|---|---|---|---|---|
| apibay.org（TPB 官方前端 API） | JSON API | ✅ | ✅/✅ | ✅ | ✅ | ❌ 不匹配（会返回不相干热门） | 24–280ms | 默认启用，**仅 ASCII 查询** |
| nyaa.si | RSS | ✅ | ✅/✅ | ✅(GiB/MiB→bytes) | ❌ | ✅（中文字幕类覆盖好，实测“中文字幕”75 条） | ~480ms | 默认启用 |
| bitsearch.eu（SolidTorrents 后端 JSON API，`/api/v1/search`） | JSON API | ✅ | ✅/✅ | ✅ | ❌ | ✅✅（真中文索引，周杰伦 398、甄嬛传 209、流星花园 20） | 200–500ms | 默认启用，需浏览器 UA；秒级限流需重试 |

### ❌ 不可用（实测淘汰，不做猜测）

| Source | 实测结果 | 原因 |
|---|---|---|
| knaben.eu, scene .eu/.org | HTTP 000/不可达 | 接口已变/地域阻断 |
| torrentproject.co | 403 | Cloudflare 拦截 |
| torrentgalaxy.to, otx.so, 1337x.to | 403 | Cloudflare 拦截（不绕） |
| yts.mx/.rs | HTTP 000 / 500 | 地域限制 / 接口失效 |
| bt4g.org, btsow.com | 403 (cf) | 中文站全在 CF 后面（不绕） |
| Academic Torrents | 无搜索 API | 官方建议下全库 JSON/DB 到本地索引，~数百 MB，违反「不建立大型本地索引」原则 → 不采纳，报告备案 |

**结论：3 个活跃源（2 个 JSON API + 1 个 RSS），全为无需登录、无验证码、无需绕过限制。** 与“插件式架构”配合，以后加源只改 `sources/` 目录。

### 成本评估（Answer 必备 7 问，针对每个新源）
- RAM/CPU/磁盘：**0 常驻**（全部即时 HTTP 请求 + TTL 缓存）
- 网络：每次搜索期间 3 个并发 HTTPS 请求，结束即停
- 是否依赖重型服务：否

---

## B. 插件式搜索源架构（4B 落地规范）

```
sources/
    __init__.py        # 自动扫描注册，核心不改代码
    base.py            # BaseSource 抽象类 + 统一 TorrentResult(dataclass)
    apibay.py          # JSON API（ASCII-only）
    nyaa.py            # RSS
    solidtorrents.py   # JSON API（浏览器 UA + 1 次延迟重试）
```

接口契约（每个 source 必须实现）：
```python
class BaseSource:
    name: str
    def search(self, query: str, filters: SearchFilters) -> list[TorrentResult]: ...
    def test(self) -> dict: {...}     # 管理员后台「测试」按钮使用
```
统一返回：`title / info_hash / magnet / size / seeders / leechers / files / source / category / published_at`（缺失字段 → null，禁止伪造）。
去重逻辑（info_hash 合并 + sources[] 保留各家 seed 数据）保持在核心层，与源无关。

## C. 搜索精度/排序设计（轻量纯 Python，无 AI 无索引库）

**Query 解析（不擅改用户意图，只提取元信息）**
```text
ABC 2024 S02E03 1080p 中文 →
  base="ABC", year=2024, season=2, episode=3, quality="1080p", lang_hint="zh"
```
三种策略（SearchFilters.precision）：
- 严格：标题需包含 base 与全部已识别元数据
- 标准：base 必含，年份/分辨率加分不加惩罚
- 宽松：base 部分匹配即可

**Relevance 计算（0~1）**：token 命中率 + 子串位置 + 全字段匹配度（`difflib` / 純集合运算，微秒级）

**Ranking Score 可配置权重（默认）**
```text
Relevance 40% · Seeders(log 缩放) 20% · Freshness 15% ·
SourceQuality 10% · Size filter match 10% · HealthEvidence 5%
```
权重存 SQLite `search_settings`，管理员后台修改即生效。**同规则同时服务 Web 和 MCP（共享同一个 `ranking.core`）。**

## D. 认证方案（选定）

| 项 | 选择 | 理由 |
|---|---|---|
| 密码存储 | `hashlib.scrypt`（标准库） | 免安装、抗 GPU、成熟 |
| 登录态 | 随机 32 字节 token 存 SQLite `sessions`（含 expiry），Cookie=HttpOnly+SameSite=Lax | 撤销可控、不依赖密码完整性 |
| 登录保护 | per-IP 失败计数（5 次/15 分钟内锁定） | 已有 token bucket 可复用 |
| MCP | 独立 `mcp_tokens` 表 + Bearer 校验依赖 | 与会话完全隔离，管理员后台可生成/撤销 |
| CSRF | SameSite=Lax 足够（POST 全走 /api 且带自定义 header） | 无需额外 token 契合

## E. MCP 集成（实测可行性，本阶段仅验证不在服务启用）

- 生态选定：**fastmcp 4.0.3**（PyPI 上活跃，官方 SDK 的上层实现），Python 3.13 venv 安装成功。
- 已验证：`mcp.http_app(path="/mcp")` 可挂载为 ASGI Streamable HTTP 端点，暴露 `tools/list`（HTTP JSON-RPC post）。
- Python tool 声明自动生成 JSON schema；结构化 dict 返回即 JSON。
- Bearer 认证 4B/4C 结合（FastMCP 支持自定义 auth provider；也可在其前置 FastAPI middleware 中校验 token 后放行，规划二选一，属 4C 实现细节）。
- 4 个工具签名（Web/MCP 共用同一 torrent_core）：
  - `search_torrents(query, limit=20, sort="relevance", filters={})`
  - `check_torrent(info_hash)` → L1 结构化
  - `deep_check_torrent(info_hash)` → L2 结构化（全局并发=1，同 Web 端同一把锁）
  - `get_torrent(info_hash)` → 最后一次搜索缓存内命中则返回合并数据

## F. 资源评估

| 项 | 现状 | 预计 4B/4C 后 |
|---|--:|--:|
| FastAPI 常驻 | 57MB | 75–95MB（加入 fastmcp + 静态管理页）|
| 搜索 3 源并发 | 26MB 瞬时 | 同（源插件无常驻） |
| L1/L2 | 不变（子进程 30–34MB 瞬时） | 同 |
| 磁盘 | cache only | +users/sessions/tokens/settings 表（KB 级） |
| 后台任务 | 0 | 0 |

1GB VPS 可承受（VPS 实测 available ~450MB 时部署 fastmcp venv 成功）。

## G. 已知风险 / 决策点（需要你确认）

1. 手机/浏览器自动化对 Basic Auth 静默 401 → 4B 改为「登录页 + session cookie」，会同时淘汰现有弹窗方案（你之前提过的那个问题自然消失）。
2. HTTPS：需要你提供 IP/域名 → 由你在反代处完成 TLS；应用层继续监听 127.0.0.1:10011。FastAPI 层不再裸暴露 0.0.0.0（等你的反代上线后切回 localhost-only？或保持现状二选一，如你反代在本机即可回 127.0.0.1）。
3. Academic Torrents：如后续确实要覆盖科学/公开数据集内容，需要一次性下 ~100–300MB 全库 SQLite 做本地索引 —— 默认不做，等明确需求。

**Phase 4A 到此为止，等你确认后进入 Phase 4B（插件式 sources 重构 + 用户系统 + 管理员后台）。**
