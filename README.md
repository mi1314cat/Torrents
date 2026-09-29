# Torrent Tool

私有 Torrent **搜索 + 可用性检测** 服务。对外只提供搜索、聚合、去重、排序和健康检测；
**不下载、不做种、不建 DHT、不存种子文件**。下载由你自己本地的 qBittorrent 负责。

自建在 1 核 / 1GB 内存的小机器上也可以跑。

---

## 快速开始

### 一键部署（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/mi1314cat/Torrents/main/deploy.sh | sudo bash
```

脚本会：拉代码 → 建 venv 装依赖 → 写 systemd 单元 → 启动 → 健康检查，
结束后在终端打印 **admin 初始密码（只显示这一次）**。

已安装过的机器上再跑一次就是**更新**（幂等）：

```bash
sudo bash /opt/torrent-tool/deploy.sh
```

可选参数：

```bash
sudo bash deploy.sh --dir /opt/torrent-tool --port 10011
```

### 部署完之后

1. 配反代（公网访问需要 nginx / caddy 转发到 `127.0.0.1:10011`），
   参考 [`deploy/nginx-torrent-tool.conf`](deploy/nginx-torrent-tool.conf)。
2. 浏览器打开反代地址，用 `admin` 登录。
3. 到后台「MCP」页签创建 Token。

常用命令：

```bash
systemctl status torrent-tool          # 状态
journalctl -u torrent-tool -f          # 实时日志
systemctl restart torrent-tool         # 重启
```

---

## 功能

| 能力 | 说明 |
|---|---|
| 多源聚合搜索 | 8 个数据源并行召回，按 info_hash 严格去重 |
| 排序与过滤 | 相关度 / 做种 / 新鲜度 / 大小 / 质量加权排序；strict / balanced / broad 三档过滤 |
| L1 实时检测 | UDP Tracker（BEP15）scrape，得到**当前**的 seeders/leechers 证据 |
| L2 深度检测 | 短生命周期 libtorrent 会话，观察 DHT 节点与 peers；全局并发 1 |
| Web 后台 | 数据源管理、权重设置、用户管理、MCP Token 管理、系统状态 |
| MCP 服务端 | 4 个工具，供 Claude 等 AI 客户端直接调用 |

### L1 状态含义

| 状态 | 含义 |
|---|---|
| 🟢 active | 有 tracker 响应且 seeders > 0 |
| 🔴 no_seed | ≥3 个 tracker 响应但全部 0 seeders |
| 🟡 insufficient | 响应不全，证据不足以判断 |
| ⚪ not_checked | 没做过实时检测（搜索结果的默认状态） |

> **重要**：`api_seeders`（来源 API 报告的做种数）和实时检测结果是**两个独立字段**，
> 不可相加。搜索结果里的 `api_seeders` 只是来源站自报值，不代表当下真的能下。

---

## 数据源

| 源 | 类型 | 带 seeders | 备注 |
|---|---|---|---|
| apibay | JSON API（ThePirateBay） | ✅ | 不支持中文查询 |
| nyaa | RSS | ✅ | 动漫/日文 |
| solidtorrents | HTML 解析 | ✅ | |
| torrents-csv | JSON API | ✅ | 开源社区聚合库 |
| therarbg | JSON API | ✅ | RARBG 后继社区，影视质量高 |
| yts | JSON API | ✅ | 仅电影 |
| dmhy 動漫花園 | RSS | ❌ | 中文/簡中字幕，**无 seeders 字段** |
| tokyotosho | RSS | ❌ | 动漫，**无 seeders 字段** |

无 seeders 的源排序权重自动靠后，但它们对中文/动漫召回不可替代。
后台「数据源」页可以逐个启用/禁用、调优先级、单独测试连通性。

---

## MCP 用法

**端点**：`https://你的域名/mcp`（Streamable HTTP）
**鉴权**：`Authorization: Bearer <token>`（在后台「MCP」页签创建）

Token 在数据库里只存 **sha256 哈希**，明文仅创建时显示一次，无法找回；
支持禁用 / 删除 / 设置过期。**MCP Token 与 Web 账号完全独立，不能用来登录后台。**

四个工具：

| 工具 | 作用 |
|---|---|
| `search_torrents` | 搜索 + 去重 + 排序，返回 `api_*` 数值和 `source_status` |
| `check_torrent` | L1 实时 tracker 检测（轻量，可对少量候选调用） |
| `deep_check_torrent` | L2 libtorrent 深度检测（**全局并发 1**，忙时返回 `busy`） |
| `get_torrent` | 取已缓存的种子信息（不重新搜索） |

错误统一结构：

```json
{"error": {"code": "AUTH_REQUIRED", "message": "..."}, "payload": null}
```

错误码：`AUTH_REQUIRED` / `INVALID_ARGUMENT` / `NOT_FOUND` / `BUSY` / `TIMEOUT` / `SOURCE_ERROR` / `INTERNAL_ERROR`

**MCP 和 Web 用的是同一套 `torrent_core`**，不存在两套搜索逻辑。

---

## 目录结构

```
.
├── server.py              FastAPI 主服务：Web 后台 + API + 挂载 /mcp
├── mcp_server.py          FastMCP 适配层：4 个工具（薄封装，不重复实现核心逻辑）
├── l2_check.py            L2 深度检测子进程（libtorrent，短生命周期）
├── torrent_core/          核心：解析 / 召回 / 去重 / 排序 / 数据源插件
│   └── sources/           8 个数据源插件，新增源只需丢一个 .py 进来
├── static/index.html      Web 后台单页
├── deploy/                systemd 单元模板 + nginx 参考配置
├── docs/                  各阶段完成报告
├── requirements.txt       已实测通过的依赖版本
└── deploy.sh              一键部署 / 更新
```

### 新增数据源

在 `torrent_core/sources/` 下加一个继承 `BaseSource` 的文件，实现 `search()` 返回
`TorrentResult` 列表即可，去重/排序/过滤自动生效，**核心代码零修改**。
模板参考 `sources/apibay.py`（JSON）与 `sources/nyaa.py`（RSS）。

---

## 安全设计

- 后端**只监听 `127.0.0.1`**，公网必须经反代进入（不要改成 `0.0.0.0`）
- 密码用 scrypt 加盐存储；Session / MCP Token 只存哈希
- 内存令牌桶限流：search 30/0.5s、L1 12/0.2s、L2 6/0.05s，**MCP 与 Web 共用同一套限额**
- L1 并发上限 2，L2 全局并发 1
- 日志**不记录** Authorization 头、Token、密码
- systemd `MemoryMax=400M`，防止 L2 子进程拖垮小机器
- L2 子进程被强杀后自动清理 `/tmp/tt-*`

---

## 已知限制

- **不做下载、不做种、不持久化 DHT** —— 检测是短生命周期的「观察」，不是长期在线状态
- L2 观察到的速度是**服务器侧**的，和你本机的实际下载速度无关
- `metadata=false` 不代表种子无效，只代表这次没拿到元数据
- RSS 源（dmhy / tokyotosho）无 seeders，排序权重较低
- 部分站点会限流或改结构，源失效时搜索会跳过该源并继续返回其他结果（`source_status` 里可见）

---

## 环境要求

- Linux（systemd），Debian 13 / Ubuntu 22.04+ 实测
- Python **≥ 3.11**（推荐 3.13）
- 内存 ≥ 1GB
- 约 300MB 磁盘（venv + 依赖）

---

## 许可

[MIT](LICENSE) © 2026 mi1314cat
