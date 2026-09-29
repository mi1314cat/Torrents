#!/usr/bin/env python3
"""torrent MCP Server（Phase 4C）

薄适配层：调用现有 torrent_core（search/dedup/rank/L1/L2 的唯一实现），
不新增任何搜索/健康检测逻辑。

- Transport: Streamable HTTP（FastMCP 4.0.3 http_app，挂载在现有 FastAPI 进程内）
- 认证：Authorization: Bearer <mcp_token>（sha256 hash 只存 DB，明文不落盘）
- 4 个工具：search_torrents / check_torrent / deep_check_torrent / get_torrent
"""
import time, json, sys, re
sys.path.insert(0, "/root/deepseek/torrent-tool")
from fastmcp import FastMCP

mcp = FastMCP(name="torrent-core", instructions="""
私有 Torrent 搜索与健康检测。工具提供：结构化搜索结果（来源 API 报告值）与实时健康检测证据（L1 tracker / L2 libtorrent）。
策略建议：先 search_torrents 拿候选 → 根据相关度与 api_seeders 筛选少量候选 → check_torrent 获取实时证据 →
仅在证据不足时 deep_check_torrent。请勿对全部结果批量检测。
搜索结果的 api_seeders 是来源 API 的报告值，与实时检测证据相互独立，二者不可相加。
""")

HEX40 = re.compile(r"^[a-fA-F0-9]{40}$")

def _ok(payload):
    return {"error": None, "payload": payload}

def _err(code, message, extra=None):
    e = {"code": code, "message": message}
    if extra:
        e["detail"] = extra
    return {"error": e, "payload": None}

# 在 server.py 启动时注入依赖（避免循环 import）
_ctx = {}

def bind(search_core_fn, l1_fn, l2_fn, get_torrent_fn, log_fn):
    _ctx["search"] = search_core_fn
    _ctx["l1"] = l1_fn
    _ctx["l2"] = l2_fn
    _ctx["get"] = get_torrent_fn
    _ctx["log"] = log_fn

@mcp.tool(
    description=(
        "搜索多个 Torrent 来源并返回按相关度排序的候选结果（含 dedup 与 ranking）。\n"
        "api_seeders 是来源 API 报告值，不代表实时在线种子；需要实时可用性证据时，请再调用 check_torrent。\n"
        "filters 支持：year, quality, language, min_size(bytes), max_size(bytes), category, source。\n"
        "工作建议：先搜索→筛选少量高相关候选→对最有希望的候选调用 check_torrent（只查少量，不要批量）。"
    ),
)
def search_torrents(query: str, limit: int = 20, precision: str = "balanced",
                    sort: str = "relevance", filters: dict | None = None) -> dict:
    if not _ctx:
        return _err("INTERNAL_ERROR", "MCP not bound to server core")
    q = (query or "").strip()
    if not q or len(q) > 256:
        return _err("INVALID_ARGUMENT", "query 需要 1~256 字符")
    if precision not in ("strict", "balanced", "broad"):
        return _err("INVALID_ARGUMENT", "precision 必须是 strict/balanced/broad")
    if sort not in ("relevance", "seeders", "freshness", "health"):
        return _err("INVALID_ARGUMENT", "sort 必须是 relevance/seeders/freshness/health")
    limit = max(1, min(int(limit), 50))
    try:
        r = _ctx["search"](q, precision=precision, limit=limit)
    except Exception as e:
        return _err("SOURCE_ERROR", "search core 异常", str(e)[:200])
    if r.get("error"):
        return _err("SOURCE_ERROR", r["error"])
    rows = r.get("results", [])
    rows = _filter(rows, filters)
    if sort == "seeders":
        rows = sorted(rows, key=lambda x: -max([s.get("seeders") or 0 for s in (x.get("sources") or [])] or [0]))
    elif sort == "freshness":
        rows = sorted(rows, key=lambda x: -(int(x.get("published_at") or 0) if str(x.get("published_at") or "0").isdigit() else 0))
    # relevance = ranking 默认顺序；health 证据默认与 relevance 相同（当前权重里 health_evidence 常为 0）
    t0 = time.time()
    
    # source_status（含失败源，便于 Agent 判断搜索范围）
    statuses = []
    for s in r.get("sources_status", []):
        statuses.append({"name": s["name"], "status": "ok" if s.get("ok") else "error",
                         "count": s.get("count", 0), "latency": s.get("latency"),
                         "detail": s.get("error") if s.get("error") else None})
    kind = "mcp_search"
    _ctx["log"](kind, "dur_ms=%d" % int((time.time() - t0) * 1000))
    return {"error": None, "payload": {
        "query": q, "precision": precision, "sort": sort,
        "total_candidates": len(rows),
        "returned": len(rows[:limit]),
        "results": [_row(x, i + 1) for i, x in enumerate(rows[:limit])],
        "source_status": statuses,
        "skipped": r.get("skipped"),
        "note": ("api_seeders/api_leechers 为来源 API 报告值，与实时检测结果独立。"
                 "health_status=not_checked 表示未做过实时检测。"
                 "需要实时可用性证据请调用 check_torrent。")}}

def _row(resfe, rank=None):
    sb = resfe.get("score_breakdown") or {}
    ranking_score = round(sum(v for v in sb.values() if isinstance(v, (int, float))), 4)
    srcs = resfe.get("sources") or []
    api_seeders = max([s.get("seeders") or 0 for s in srcs] or [0])
    leechers = max([s.get("leechers") or 0 for s in srcs] or [0])
    return {"rank": rank, "title": resfe.get("title"), "info_hash": resfe.get("info_hash"),
            "magnet": resfe.get("magnet"), "size": resfe.get("size"),
            "files": resfe.get("files"), "category": resfe.get("category"),
            "published_at": resfe.get("published_at"),
            "api_seeders": api_seeders, "api_leechers": leechers,
            "ranking_score": round(ranking_score, 4),
            "relevance_score": round(sb.get("relevance", 0) / 0.40 if sb.get("relevance") else 0, 3),
            "score_breakdown": sb,
            "sources": [{"name": s.get("name"), "seeders": s.get("seeders") or 0,
                         "leechers": s.get("leechers") or 0} for s in srcs],
            "health_status": "not_checked"}

def _filter(rows, filters):
    if not filters:
        return rows
    out = []
    for r in rows:
        size = r.get("size") or 0
        title = (r.get("title") or "")
        keep = True
        if "min_size" in filters and size and size < (filters["min_size"] or 0):
            keep = False
        if "max_size" in filters and size and filters.get("max_size") and size > filters["max_size"]:
            keep = False
        if "year" in filters and filters.get("year"):
            if str(filters["year"]) not in (title.split("20")):
                if str(filters["year"]) not in title:
                    keep = False
        if "quality" in filters and filters.get("quality"):
            if str(filters["quality"]).lower() not in title.lower():
                keep = False
        if "language" in filters and filters.get("language"):
            if str(filters["language"]).lower() not in title.lower():
                keep = False
        if "category" in filters and filters.get("category"):
            if (r.get("category") or "") != str(filters["category"]):
                keep = False
        if "source" in filters and filters.get("source"):
            names = [x.get("name") for x in (r.get("sources") or [])]
            if str(filters["source"]) not in names:
                keep = False
        if keep:
            out.append(r)
    return out


@mcp.tool(
    description=(
        "使用实时 UDP Tracker scrape（复用现有 L1 实现）检查指定 InfoHash 的当前 Tracker 活动。\n"
        "返回实时检测证据（与来源 API 报告值独立，不可相加）；同一 InfoHash 300 秒内的结果直接返回缓存。"
    ),
)
def check_torrent(info_hash: str) -> dict:
    ih = (info_hash or "").strip().lower()
    if not HEX40.match(ih):
        return _err("INVALID_ARGUMENT", "info_hash 必须为 40 位十六进制")
    try:
        r = _ctx["l1"](ih)
    except Exception as e:
        return _err("SOURCE_ERROR", "L1 检测异常", str(e)[:200])
    trows = []
    for t in r.get("trackers_tried", []):
        trows.append({"tracker": t.get("tracker"), "responded": bool(t.get("responded")),
                      "seeders": t.get("seeders"), "leechers": t.get("leechers")})
    responded = sum(1 for t in trows if t["responded"])
    hits = [t for t in trows if t["responded"]]
    st = (r.get("status") or "").strip()
    if st.startswith("🟢"):
        machine_status = "active"
    elif st.startswith("🔴"):
        machine_status = "no_seed"
    else:
        machine_status = "insufficient"
    return {"error": None, "payload": {
        "info_hash": ih, "status": machine_status, "status_human": r.get("status"),
        "checked_at": r.get("detection_time"), "cached": bool(r.get("cached")),
        "tracker_results": trows,
        "summary": {"responding_trackers": responded, "total_trackers": len(trows),
                    "max_seeders": r.get("max_seeders") or 0,
                    "max_leechers": max([t.get("leechers") or 0 for t in trows] or [0])},
        "explain": r.get("explain"),
        "note": ("实时 UDP Tracker scrape（非来源 API 数据）；"
                 "Server-side Health ≠ 本机网速。")}}


@mcp.tool(
    description=(
        "使用短生命周期 libtorrent 会话进行 DHT/Peer 观察（复用现有 L2 实现）。\n"
        "资源成本高于普通搜索和 L1；同一时刻全局仅允许 1 个任务，若并发将返回 busy。\n"
        "metadata=false 不代表 torrent 无效，请结合实际 peers/connect_candidates 解释。\n"
        "建议仅在 L1 证据不足时调用。"
    ),
)
def deep_check_torrent(info_hash: str, timeout: int = 60) -> dict:
    ih = (info_hash or "").strip().lower()
    if not HEX40.match(ih):
        return _err("INVALID_ARGUMENT", "info_hash 必须为 40 位十六进制")
    try:
        r = _ctx["l2"](ih, timeout=int(timeout))
    except Exception as e:
        return _err("SOURCE_ERROR", "L2 检测异常", str(e)[:200])
    r = dict(r)
    if r.get("status"):
        # 直接是 busy/error 结构
        return {"error": None, "payload": r}
    # 有 peers / metadata → observed；否则 no_evidence（不判死）
    seen = r.get("num_peers") or r.get("connect_candidates") or r.get("metadata") or r.get("peers_seen")
    status = "observed" if seen else "no_evidence"
    return {"error": None, "payload": {
        "info_hash": ih, "status": status, "checked_at": r.get("detection_time"),
        "elapsed": r.get("elapsed_sec") or r.get("wall_elapsed"),
        "dht_nodes": r.get("dht_nodes"), "peers_seen": r.get("peers_seen"),
        "num_peers": r.get("num_peers"), "num_seeds": r.get("num_seeds"),
        "connect_candidates": r.get("connect_candidates"), "metadata": bool(r.get("metadata")),
        "metadata_files": r.get("metadata_files"), "cached": bool(r.get("cached")),
        "note": ("深度检测为服务器环境观察结果，不等同于本机 qBittorrent 的下载速度。"
                 "metadata=false 不代表 torrent 无效。")}}


@mcp.tool(
    description=(
        "获取已搜索/已缓存的指定 InfoHash 信息（title/magnet/size/api_* /最近健康缓存）。\n"
        "不进行大规模重新搜索；若从未缓存则返回 not_found。"
    ),
)
def get_torrent(info_hash: str) -> dict:
    ih = (info_hash or "").strip().lower()
    if not HEX40.match(ih):
        return _err("INVALID_ARGUMENT", "info_hash 必须为 40 位十六进制")
    rec, key = _ctx["get"](ih)
    if not rec:
        return _err("NOT_FOUND", "info_hash 不在当前缓存结果中")
    l1c = _ctx["l1c"](ih) if _ctx.get("l1c") else None
    l2c = _ctx["l2c"](ih) if _ctx.get("l2c") else None
    return {"error": None, "payload": {**_row(rec), "health_status": None,
        "_cached_key": key, "_cache_live": rec.get("_cache_live", True),
        "_l1_cache": {"status": ("active" if (l1c.get("status") or "").startswith("🟢")
                                 else "no_seed" if (l1c.get("status") or "").startswith("🔴")
                                 else "insufficient"), "checked_at": l1c.get("_checked_at") or l1c.get("detection_time"),
                      "max_seeders": l1c.get("max_seeders")} if l1c else None,
        "_l2_cache": {k: l2c.get(k) for k in (
            "status", "detection_time", "dht_nodes", "peers_seen", "num_peers",
            "connect_candidates", "metadata")} if l2c else {}}}

# 工具 4（get_torrent）注册见上
