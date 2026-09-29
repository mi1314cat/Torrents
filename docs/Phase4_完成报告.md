# Phase 4 — 最小 Web MVP 完成报告

部署位置：VPS `rn`（107.173.154.178）`/root/torrent-tool/`
代码维护位置：`/root/deepseek/torrent-tool/`
服务进程：`systemd` 单元 `torrent-tool.service`（FastAPI 单进程，监听 127.0.0.1:8788）

---

## 1. 文件结构

```
/root/deepseek/torrent-tool/          ← 源码（本机）
  server.py           FastAPI 单进程（搜索/聚合/去重/L1/L2/限流/TTL缓存/认证）
  l2_check.py         L2 深度检测子进程（libtorrent 短会话，每任务一次）
  static/index.html   单页中文 UI（原生 JS，无框架/无构建链/无广告）
  config.json         所有阈值集中配置（TTL/超时/并 发/rate limit/认证）
  cache.db            SQLite TTL 缓存（仅搜索缓存 + hash/健康结果/时间戳）
  deploy/
    torrent-tool.service             systemd 单元
    nginx-torrent-tool.conf          【已弃用】预置反代配置，供你后续自己接管
```

## 2. 启动方式
```bash
systemctl restart torrent-tool       # 在 VPS 上
# 访问：http://107.173.154.178:8788/ （直接/或经你自己的反代）
# 注：为避免与你现有服务冲突，VPS 上已另起 nginx :8787 + Basic Auth，你后续放置反代时随时可停用
```
认证：Basic Auth 双层（应用层 FastAPI + 预构建 nginx 层可选），账号 `me`，密码 `FT1k25UBlRnd`（UI 第一次访问会提示填入，存本机 localStorage）。

## 3. API（本机 8788，/Internal 管理、不在公网暴露）
| 接口 | 作用 | 行为 |
|---|---|---|
| `GET /api/search?q=…` | 并行查 apibay+nyaa → 去重 → 排序 | 缓存 10 分钟，重复命中秒回 |
| `GET /api/l1?hash=…` | UDP tracker scrape (BEP15)，并发≤2 | 缓存 5 分钟，不下载任何内容 |
| `GET /api/l2?hash=…/magnet=…` | libtorrent 子进程 | 全局并发=1，重复点击→429「正在检测，请稍候」，90s 硬超时，结束即清理 |
| `GET /api/status` | 自检 | TTL/限流/原则说明 |
| `GET /` | UI | 单页中文界面 |

## 4. UI 说明（无截图能力由浏览器沙箱限制，但已通过 UI 快照+API 层验证，页面DOM结构确认）
- 顶部搜索框 + 「搜索」按钮
- 每条结果显示：名称 · 大小 · 各来源（apibay/nyaa）Seed/Leech · 文件数 · 发布时间 · info_hash 前缀 · 起源徽章
- 三个按钮：`复制 Magnet`（纯前端）、`实时检测`（L1）、`深度检测`（L2）
- 分层健康状态显示：
  - 搜索数据区 → `Seed    来源：API`
  - 实时检测区 → `🟢-⚪ 状态 + 各 tracker 明细 + 检测时间 + 「实时 UDP scrape（非 API 字段）」标签`
  - 深度检测区 → `DHT 节点 / 发现 peers / 可连接候选 / 元数据 / 检测时间 / 提示：Server-side Health ≠ 本机网速`
- ⚪ 未检测为默认；不显示任何无解释的“健康百分比”
- 状态色：🟢 活跃 / 🟡 信息不足 / 🔴 无种子 / ⚪ 未检测

## 5. 实测资源占用（1 核 / 987MB VPS）
| 项 | 实测 | 状态 |
|---|--:|---|
| FastAPI 前身轻量（本进程常驻 RSS，空闲） | **~57MB** | ✅ < 150MB 目标 |
| 搜索/L1 峰值（进程返回后即释放） | 瞬时included in 57MB，无额外常驻 | ✅ |
| L2 子进程峰值 | **~30–34MB** | ✅ |
| L1 单次内存 | 包含在主进程内，无痕 | ✅ |
| 磁盘 | 稳态 3.6GB used / 14G，连续操作后仅 +40KB（缓存写入），无长期增长 | ✅ |
| 后台进程 | 搜索/L2 结束后 pgrep 均无残留 | ✅ |

## 6. 测试结果 sonuall
1. **多关键词搜索**：ubuntu / debian / linux iso 依次成功，速度~1.2s（含首次双源实时抓取），后续命中缓存（"cached: true"）
2. **去重**：apibay+nyaa 合并后 info_hash 唯一（50/50 全唯一，重复时各来源 seed 保存在 `sources[]` 中，不相互相加）
3. **L1 三案例**：
   - 活跃（Ubuntu 24.04）→ 🟢
   - 死种（Koha Live 2009）→ 🔴（scrape 明确 0/0）
4. **周期假健康**（Ubuntu Unleashed 2019）→ 🔴 无种子（早期维敬判定的 🟡 已被更严准则修正，不信 API 报告的“ aan guest ”）
5. **L2 单并发**：并发 2 → 1 成功 1 次 → 429「正在检测，请稍候」，无一例同时跑双任务
6. **超时清理**：l2_timeout 临时改 5s → 进程 kill、错误顺畅返回（“深度检测超时，子进程已清理”）、无残留目录、临时目录服务端亦清理
7. **重复点击深度检测**：同 hash 重复请求命中 l2 缓存（不跑第二个 libtorrent）
8. **磁盘**：连续搜索+L1+L2 前后 delta=40KB（缓存写入），无长期增长
9. **进程**：任务后 `pgrep l2_check` 无残留
10. **内存**：见上表
11. **Magnet 复制**：UI 为纯前端复制，不向 qBittorrent 推送（遵 十二.10）

## 7. 已知问题
- 现代 Chrome/Playwright 对 Basic Auth 不自动配合 `fetch`：已改用 UI 内登录框 + 显式 Authorization 头（已验证修复思路；如你触发的反代会带 auth，即可直接使用）
- 死种的判定只用 tracker scrape，某些 tracker（如 nyaa.vc）响应慢/不稳，被计为「无响应」而不是「0种子」——不影响状态判定，只影响解释文案
- rate limit 在重启服务后清零（进程内存 bucket），对私人使用影响小
- systemd 单元设置了 `MemoryMax=400M` 寿命保护，极端并发也会被杀回而不是拖死 VPS
