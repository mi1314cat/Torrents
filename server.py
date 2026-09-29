#!/usr/bin/env python3
"""Torrent 搜索 + 可用性检测 + 管理服务（Phase 4B）

- sources/ 插件自动注册（apibay/nyaa/solidtorrents）
- Recall→Dedup→Ranking 分离；Ranking 权重存 SQLite，管理员可调
- L1 UDP tracker scrape（复用 Phase 3 已验证实现）
- L2 libtorrent 子进程（全局并发=1，超时清理）
- 用户/角色/Session/管理员（scrypt、HttpOnly Cookie、登录失败锁定）
- 不后台抓取、不做种、不保存 Torrent 资源库
"""
import os, sys, json, time, socket, struct, random, threading, subprocess, ipaddress, hashlib, re, glob
import urllib.parse
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Depends, Request, Response, Query
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn

BASE = Path(__file__).resolve().parent

sys.path.insert(0, str(BASE))
from torrent_core import db as tdb
from torrent_core.sources.base import SearchFilters
from torrent_core.parser import parse_query, title_relevance
from torrent_core.dedup import dedup_merge
from torrent_core.ranking import apply_ranking, precision_filter
from mcp_server import mcp as mcp_server_instance, bind as mcp_bind
import mcp_server as _mcp_server
import torrent_core.db as _tdb_mod

# MCP Streamable HTTP 子应用（stateless_http=True 不需要会话）
_mcp_app = mcp_server_instance.http_app(path="/mcp", stateless_http=True)
from contextlib import asynccontextmanager as _ac

@_ac
async def _combined_lifespan(app):
    async with _mcp_app.router.lifespan_context(_mcp_app):
        yield

APP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 10011
DB = BASE / "torrent.db"

EXT_DEFAULTS = {"l1_timeout": 5, "l2_timeout": 90, "l1_trackers": 6,
                "max_per_source": 50, "cookie_secure": False}

def gs(key, default=None):
    v = tdb.get_setting(key)
    if v is not None:
        return v
    if default is not None:
        return default
    return EXT_DEFAULTS.get(key, DEFAULT_or(key))

def DEFAULT_or(key):
    return tdb.DEFAULTS.get(key)

tdb.init(DB)
if tdb.ensure_admin():
    print("[boot] admin created; ONE-TIME password in journal (do not store elsewhere)", flush=True)

# ---------------- source registry ----------------
import torrent_core as tcore
SOURCES = {s.name: s for s in tcore.load_sources()}
tdb.ensure_sources(list(SOURCES.keys()))

def inject_cfg():
    for row in tdb.source_list():
        s = SOURCES.get(row[0])
        if s:
            s._runtime_cfg = {"enabled": bool(row[1]), "priority": row[2], "timeout": row[3]}
            s.enabled = bool(row[1])
            s.priority = row[2]

def source_quality(names):
    rows = {r[0]: r for r in tdb.source_list()}
    vals = []
    for n in (names if isinstance(names, list) else [names]):
        r = rows.get(n)
        if not r:
            vals.append(0.5); continue
        bins = (r[4] or 0) + (r[5] or 0)
        succ = (r[4] or 0) / bins if bins else 0.5
        avg = r[6]
        lat = 1.0 if avg is None else (1.0 if avg < 400 else 0.6 if avg < 1500 else 0.35)
        prio = min(1.0, max(0.2, (r[2] or 50) / 100.0))
        vals.append(0.55 * succ + 0.30 * lat + 0.15 * prio)
    return sum(vals) / len(vals) if vals else 0.5

# ---------------- app + middleware ----------------
app = FastAPI(title="torrent-core", docs_url=None, redoc_url=None,
              lifespan=_combined_lifespan)
_buckets = {}
_login_fail = {}
SESSION_COOKIE = "tt_session"
HEX40 = re.compile(r"^[a-fA-F0-9]{40}$")
_L1_SEM = threading.BoundedSemaphore(2)
_L2_LOCK = threading.Lock()

def client_ip(request: Request):
    """反代场景：信任本机反代注入的 X-Forwarded-For 第一个值；直连时用 socket 地址。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
        if len(ip) < 64:
            return ip
    return request.client.host if request.client else "?"

@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    path = request.url.path
    kind = "l1" if path.startswith("/api/l1") else ("l2" if path.startswith("/api/l2") else "search" if path.startswith("/api/search") else None)
    ip = client_ip(request)
    # MCP bearer auth（仅 /mcp*）
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        tok = tdb.mcp_token_auth(token)
        if not tok:
            return JSONResponse({"error": {"code": "AUTH_REQUIRED", "message": "需要有效的 MCP Bearer Token"}},
                                status_code=401)
        request.state.mcp_token = tok
    rkind = kind or ("mcp" if request.url.path.startswith("/mcp") else None)
    if rkind:
        cap = int(tdb.get_setting("rate_%s_capacity" % rkind) or 0) or int(tdb.get_setting("rate_search_capacity"))
        refill = float(tdb.get_setting("rate_%s_refill" % rkind) or 0) or float(tdb.get_setting("rate_search_refill"))
        rk = rkind
        tok = getattr(request.state, "mcp_token", None)
        bk = _buckets.setdefault(("mcp:" + str(tok["id"]) if request.url.path.startswith("/mcp") else ip, rk), {"t": time.time(), "n": float(cap)})
        now = time.time()
        bk["n"] = min(float(cap), bk["n"] + (now - bk["t"]) * refill)
        bk["t"] = now
        if bk["n"] < 1:
            return JSONResponse(
                {"error": {"code": "TIMEOUT", "message": "请求过于频繁，请稍候"}} if request.url.path.startswith("/mcp") else {"error": "请求过于频繁，请稍候"},
                status_code=429)
        bk["n"] -= 1
    if path == "/login":
        fb = _login_fail.setdefault(ip, {"n": 0, "t": 0.0})
        if time.time() - fb["t"] < 900 and fb["n"] >= 5:
            tdb.log("login_rate_blocked", ip)
            return JSONResponse({"error": "失败次数过多，请 15 分钟后再试"}, status_code=429)
    return await call_next(request)

# ---------------- auth ----------------
def require_user(request: Request):
    u = tdb.session_user(request.cookies.get(SESSION_COOKIE))
    if not u:
        raise HTTPException(401, "请登录")
    return u

def require_admin(u=Depends(require_user)):
    if u["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return u

class LoginReq(BaseModel):
    username: str
    password: str

@app.post("/login")
def api_login(req: LoginReq, request: Request, response: Response):
    urow = tdb.get_user(req.username.strip())
    if not (urow and urow[5] and tdb.verify_password(req.password, urow[2], urow[3])):
        ip = client_ip(request)
        fb = _login_fail.setdefault(ip, {"n": 0, "t": 0.0})
        fb["n"] += 1
        fb["t"] = time.time()
        tdb.log("login_failed", ip)
        raise HTTPException(401, "用户名或密码错误")
    response.set_cookie(SESSION_COOKIE,
                        tdb.create_session(urow[0], int(tdb.get_setting("session_ttl"))),
                        httponly=True, samesite="lax",
                        max_age=int(tdb.get_setting("session_ttl")),
                        secure=bool(tdb.get_setting("cookie_secure")))
    tdb.touch_last_login(urow[0])
    tdb.log("login_ok", urow[1])
    return {"username": urow[1], "role": urow[4]}

@app.post("/logout")
def api_logout(request: Request, response: Response):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        tdb.logout(tok)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}

@app.get("/api/me")
def api_me(u=Depends(require_user)):
    return u

# ---------------- search core（Web/MCP 同一套） ----------------
def search_core(q: str, precision: str | None = None, limit: int | None = None):
    s = tdb.all_settings()
    precision = precision or s.get("precision", "balanced")
    limit = min(int(limit or s.get("max_results", 50)), 200)
    q = (q or "").strip()
    if not q or len(q) > 256:
        raise HTTPException(400, "关键词为空或过长")
    skey = "s:" + hashlib.sha1(json.dumps([q, precision, s.get("w_relevance"), s.get("w_seed")],
                                          sort_keys=True).encode()).hexdigest()[:24]
    cached = tdb.cache_get(skey)
    if cached:
        cached["cached"] = True
        return cached

    inject_cfg()
    parsed = parse_query(q)
    enabled = [x for x in SOURCES.values() if getattr(x, "enabled", True)]
    skipped_note = None
    if any(ord(ch) > 127 for ch in q):
        enabled = [x for x in enabled if getattr(x, "supports_unicode", True)]
        skipped_note = "apibay 跳过：TPB 对非 ASCII 关键词无匹配能力（会返回无关热门）"

    raw, statuses = [], []
    def run(src):
        try:
            items, meta = src.search(q, SearchFilters(precision=precision,
                                                      max_per_source=int(s.get("max_per_source", 50))))
            return src.name, items, meta
        except Exception as e:
            return src.name, [], {"ok": False, "error": str(e)[:180]}

    with ThreadPoolExecutor(max_workers=int(s.get("source_concurrency", 3))) as ex:
        for name, items, meta in ex.map(run, enabled):
            statuses.append({"name": name, "ok": bool(meta.get("ok")), "http": meta.get("http"),
                             "count": len(items), "latency": meta.get("latency"),
                             "error": meta.get("error")})
            raw.extend(items)
            tdb.source_record(name, bool(meta.get("ok")), meta.get("latency"), meta.get("error"))

    unified = dedup_merge(raw)
    # unified -> recs（统一 ranking 输入）
    recs = [{"title": r.get("title"), "info_hash": r["info_hash"], "magnet": r["magnet"],
             "size": r.get("size"), "files": r.get("files"), "category": r.get("category"),
             "published_at": r.get("published_at"), "sources": r["sources"],
             "max_api_seeders": r.get("max_api_seeders")}
            for r in unified]
    for t in recs:
        t["_rel"] = title_relevance(parsed, t["title"] or "")
        t["_source_quality"] = source_quality([x["name"] for x in t["sources"]])
        t["_weights"] = dict(w_relevance=s.get("w_relevance"), w_seed=s.get("w_seed"),
                             w_fresh=s.get("w_fresh"), w_source_quality=s.get("w_source_quality"),
                             w_size=s.get("w_size"), w_health=s.get("w_health"))
    recs = precision_filter(parsed, recs, precision)
    apply_ranking(recs, s)
    top = recs[:limit]
    result = []
    for t in top:
        d = {k: t[k] for k in ("title", "info_hash", "magnet", "size", "files",
                               "category", "published_at", "sources",
                               "max_api_seeders", "score_breakdown")}
        result.append(d)
    resp = {"query": q, "precision": precision, "count": len(result), "results": result,
            "sources_status": statuses, "skipped": skipped_note}
    tdb.cache_put(skey, resp, int(gs("ttl_search", 600)))
    return resp

@app.get("/api/search")
def api_search(q: str, precision: str = Query(default=None), limit: int | None = None,
               u=Depends(require_user)):
    tdb.log("search", "%s q=%s" % (u["username"], (q or "")[:80]))
    return search_core(q, precision, limit)

# ---------------- L1 ----------------
TRACKER_CACHE = BASE / ".trackers.txt"
FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce", "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce", "udp://explodie.org:6969/announce"]

def best_trackers():
    n = int(gs("l1_trackers", 6))
    if not TRACKER_CACHE.exists() or time.time() - TRACKER_CACHE.stat().st_mtime > 28800:
        from torrent_core.sources.base import _http_get
        ok, _, _, text = _http_get("https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt", 15)
        if ok:
            lines = [l for l in text.splitlines() if l.startswith("udp")][:40]
            random.shuffle(lines)
            TRACKER_CACHE.write_text("\n".join(lines))
    lines = TRACKER_CACHE.read_text().splitlines()
    udps = [l for l in lines if l.startswith("udp")] or FALLBACK_TRACKERS
    return udps[:n]

_connids = {}
_conn_lk = threading.Lock()

def _resolve_host(hp):
    host, _, port = hp.rpartition(":")
    try:
        int(port)
    except ValueError:
        return None
    try:
        ipaddress.ip_address(host)
        ip = host
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, int(port), socket.AF_INET)
            ip = infos[0][4][0]
        except Exception:
            return None
    return ip, int(port)

def udp_scrape_one(tracker, ih_bytes, timeout=5):
    r = _resolve_host(tracker.replace("udp://", "").split("/")[0])
    if not r:
        return None
    ip, port = r
    key = "%s:%s" % (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        with _conn_lk:
            cid = _connids.get(key)
        if cid is None:
            tid = random.randint(0, 2 ** 31)
            sock.sendto(struct.pack(">QII", 0x41727101980, 0, tid), (ip, port))
            d, _ = sock.recvfrom(16)
            action, rtid, got = struct.unpack(">IIQ", d)
            if action != 0 or rtid != tid:
                return None
            cid = got
            with _conn_lk:
                _connids[key] = cid
        tid2 = random.randint(0, 2 ** 31)
        sock.sendto(cid.to_bytes(8, "big") + struct.pack(">II", 2, tid2) + ih_bytes, (ip, port))
        d, _ = sock.recvfrom(64)
        a, rtid2 = struct.unpack(">II", d[:8])
        if a != 2 or rtid2 != tid2:
            return None
        se, cm, le = struct.unpack(">III", d[8:20])
        return {"tracker": tracker[:48], "seeders": se, "leechers": le}
    except Exception:
        return None
    finally:
        sock.close()

def l1_core(info_hash: str) -> dict:
    ih = info_hash.lower()
    if not HEX40.match(ih):
        raise HTTPException(400, "hash 必须为 40 位十六进制")
    key = "l1:" + ih
    cached = tdb.cache_get(key)
    if cached:
        cached["cached"] = True
        return cached
    with _L1_SEM:
        trackers = best_trackers()
        ib = bytes.fromhex(ih)
        tried, hits = [], []
        def probe(tr):
            r = udp_scrape_one(tr, ib, int(gs("l1_timeout", 5)))
            x = {"tracker": tr, "responded": r is not None}
            if r:
                x.update(seeders=r["seeders"], leechers=r["leechers"])
                if r["seeders"] or r["leechers"]:
                    hits.append(r)
            tried.append(x)
        ths = [threading.Thread(target=probe, args=(tr,)) for tr in trackers]
        for t in ths: t.start()
        for t in ths: t.join(7)
        max_seed = max(h["seeders"] for h in hits) if hits else 0
        responded = any(x["responded"] for x in tried)
        zero = sum(1 for x in tried if x.get("responded") and x.get("seeders") == 0)
        if max_seed > 0:
            status, explain = "🟢 活跃", "%d/%d 个 tracker scrape 返回 peers，最大 seeders=%d" % (len(hits), len(tried), max_seed)
        elif zero >= 3:
            status, explain = "🔴 无种子", "%d/%d 个 tracker scrape 明确返回 seeders=0" % (zero, len(tried))
        elif hits:
            status, explain = "🔴 疑似无种子", "scrape 有响应但全部 seeders=0"
        elif responded:
            status, explain = "🟡 信息不足", "tracker 响应但未返回 peers；建议深度检测"
        else:
            status, explain = "🟡 信息不足", "全部 tracker scrape 无响应；不能判死，建议深度检测"
        out = {"info_hash": ih, "status": status, "explain": explain,
               "trackers_tried": tried, "max_seeders": max_seed,
               "detection_time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "note": "实时 UDP tracker scrape（非 API 字段）。Server-side Health ≠ 本机网速"}
        tdb.cache_put(key, out, int(gs("ttl_l1", 300)))
        tdb.log("l1", ih[:12])
        return out

def _get_torrent_cached(ih):
    from torrent_core.db import find_cached_result
    rec, key = find_cached_result(ih)
    return rec, key

def _mcp_deep_check(ih, timeout=60):
    """MCP 版 L2（非阻塞：忙时返回 busy 结构，不抛 HTTP 异常）；timeout 为本次调用可允许的最长等待。"""
    if not _L2_LOCK.acquire(False):
        return {"status": "busy", "message": "另一个深度检测任务正在运行，请稍后再试"}
    try:
        import os as _os
        proc = subprocess.Popen([sys.executable, str(L2_CHECK), "magnet:?xt=urn:btih:%s" % ih.lower()],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t0 = time.time()
        try:
            out, err = proc.communicate(timeout=int(timeout) + 10)  # 秒 buffer（默认60 仍需给子进程自身60s运行周期）
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
            for d in glob.glob("/tmp/tt-*"):
                shutil.rmtree(d, ignore_errors=True)
            return {"status": "timeout", "message": "deep_check_torrent %d 秒内未完成，子进程已清理" % int(timeout)}
        el = round(time.time() - t0, 1)
        try:
            r = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
        except Exception:
            return {"status": "error", "message": (err or b"").decode(errors="replace")[-200:] or "子进程无有效输出"}
        r.setdefault("status", "observed" if (r.get("num_peers") or r.get("connect_candidates") or r.get("metadata")) else "no_evidence")
        seen = r.get("num_peers") or r.get("connect_candidates") or r.get("metadata")
        r["status"] = "observed" if seen else "no_evidence"
        r["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        r["elapsed_sec"] = el
        tdb.cache_put("l2:" + ih.lower(), r, int(gs("ttl_l2", 900)))
        tdb.log("mcp_l2", (ih or "")[:12])
        return r
    finally:
        _L2_LOCK.release()

@app.get("/api/l1")
def api_l1(hash: str = Query(..., min_length=40, max_length=40), u=Depends(require_user)):
    tdb.log("l1", "%s %s" % (u["username"], hash.lower()[:12]))
    return l1_core(hash)

# ---------------- L2 ----------------
_L2_LOCK = threading.Lock()
L2_CHECK = BASE / "l2_check.py"

def run_l2(target):
    if not target.startswith("magnet:"):
        target = "magnet:?xt=urn:btih:%s" % target.lower()
    ih = target.lower().split("urn:btih:")[1][:40] if "urn:btih:" in target.lower() else target.lower()
    key = "l2:" + ih
    cached = tdb.cache_get(key)
    if cached:
        cached["cached"] = True
        return cached
    timeout = int(gs("l2_timeout", 90))
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, str(L2_CHECK), target],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.communicate()
        for d in glob.glob("/tmp/tt-*"):
            shutil.rmtree(d, ignore_errors=True)
        return {"error": "深度检测超时，子进程已清理", "elapsed": timeout}
    el = round(time.time() - t0, 1)
    try:
        last = out.decode(errors="replace").strip().splitlines()[-1]
        r = json.loads(last)
    except Exception:
        return {"error": (err or b"").decode(errors="replace")[-400:] or "子进程无有效输出"}
    r["wall_elapsed"] = r.get("elapsed_sec", el)
    r["detection_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    r["note"] = "L2 libtorrent 短会话，子进程已退出。Server-side Health ≠ 本机网速"
    tdb.cache_put(key, r, int(gs("ttl_l2", 900)))
    tdb.log("l2", ih[:12])
    return r

@app.get("/api/l2")
def api_l2(hash: str = Query(default="", max_length=40), magnet: str = Query(default=""),
           u=Depends(require_user)):
    if not _L2_LOCK.acquire(False):
        raise HTTPException(429, "正在检测，请稍候（全局仅允许 1 个深度检测任务）")
    try:
        target = magnet if magnet else hash
        if not target:
            raise HTTPException(400, "缺少 hash 或 magnet")
        if hash and not HEX40.match(hash):
            raise HTTPException(400, "hash 必须为 40 位十六进制")
        tdb.log("l2", "%s %s" % (u["username"], hash.lower()[:12] if hash else "m"))
        return run_l2(target)
    finally:
        _L2_LOCK.release()

# ---------------- admin ----------------
@app.get("/api/admin/sources")
def admin_sources(u=Depends(require_admin)):
    rows = tdb.source_list()
    return [{"name": r[0], "enabled": bool(r[1]), "priority": r[2], "timeout": r[3],
             "success_count": r[4], "fail_count": r[5],
             "avg_latency": round(r[6], 1) if r[6] else None,
             "last_success": time.strftime("%m-%d %H:%M", time.localtime(r[7])) if r[7] else None,
             "last_error": r[8]} for r in rows]

@app.post("/api/admin/sources/{name}/toggle")
def admin_source_toggle(name: str, u=Depends(require_admin)):
    cur = next((r for r in tdb.source_list() if r[0] == name), None)
    if not cur:
        raise HTTPException(404, "Source 不存在")
    tdb.source_update(name, "enabled", 0 if cur[1] else 1)
    inject_cfg()
    tdb.log("source_toggle", "%s -> %s" % (name, not cur[1]))
    return {"name": name, "enabled": not cur[1]}

@app.post("/api/admin/sources/{name}/test")
def admin_source_test(name: str, u=Depends(require_admin)):
    s = SOURCES.get(name)
    if not s:
        raise HTTPException(404, "Source 不存在")
    try:
        r = s.test()
        ok = bool(r.get("status"))
        tdb.source_record(name, ok, r.get("latency"), None if ok else "test_fail")
        tdb.log("source_test", name)
        return r
    except Exception as e:
        tdb.source_record(name, False, None, str(e))
        return {"status": False, "error": str(e)}

@app.post("/api/admin/sources/{name}/set")
def admin_source_set(name: str, field: str = Query(...), value: float = Query(...),
                     u=Depends(require_admin)):
    if field not in ("priority", "timeout"):
        raise HTTPException(400, "field 仅支持 priority/timeout")
    if field == "priority" and not (0 <= int(value) <= 100):
        raise HTTPException(400, "priority 需 0~100")
    if field == "timeout" and not (2 <= float(value) <= 30):
        raise HTTPException(400, "timeout 需 2~30 秒")
    tdb.source_update(name, field, int(value) if field == "priority" else float(value))
    inject_cfg()
    tdb.log("source_set", "%s %s=%s" % (name, field, value))
    return {"ok": True}

ALLOWED_SETTINGS = ("precision", "sort", "max_results", "source_concurrency",
                    "l1_concurrency", "l2_concurrency", "l1_auto",
                    "session_ttl", "ttl_search", "ttl_l1", "ttl_l2", "l2_timeout",
                    "w_relevance", "w_seed", "w_fresh", "w_source_quality",
                    "w_size", "w_health", "cookie_secure", "l1_trackers")

@app.get("/api/admin/settings")
def admin_settings_get(u=Depends(require_admin)):
    s = tdb.all_settings()
    return {k: (s[k] if k in s else gs(k, {"l1_timeout":5,"l2_timeout":90,"l1_trackers":6,"max_per_source":50,"cookie_secure":False}.get(k))) for k in ALLOWED_SETTINGS}

def _typed(k, v):
    int_keys = {"max_results", "source_concurrency", "l1_concurrency", "l2_concurrency",
                "session_ttl", "ttl_search", "ttl_l1", "ttl_l2", "l2_timeout", "l1_trackers"}
    float_keys = {"w_relevance", "w_seed", "w_fresh", "w_source_quality", "w_size", "w_health"}
    if k in int_keys:
        return int(v)
    if k in float_keys:
        return float(v)
    if k == "precision":
        assert v in ("strict", "balanced", "broad"), "precision 必须是 strict/balanced/broad"
        return v
    return v

@app.post("/api/admin/settings")
def admin_settings_set(body: dict, u=Depends(require_admin)):
    applied = {}
    for k, v in body.items():
        if k not in ALLOWED_SETTINGS:
            continue
        tdb.set_setting(k, _typed(k, v))
        applied[k] = _typed(k, v)
    inject_cfg()
    tdb.log("settings", ",".join(list(applied))[:120])
    return {"ok": True, "applied": applied}

@app.get("/api/admin/users")
def admin_users(u=Depends(require_admin)):
    rows = tdb.user_list()
    return [{"username": r[0], "role": r[1], "enabled": bool(r[2]),
             "created": time.strftime("%Y-%m-%d", time.localtime(r[3])),
             "last_login": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[4])) if r[4] else None}
            for r in rows]

class UserReq(BaseModel):
    username: str
    password: str
    role: str | None = None

@app.post("/api/admin/users")
def admin_user_create(req: UserReq, u=Depends(require_admin)):
    if len(req.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if not tdb.user_create(req.username, req.password, "admin" if req.role == "admin" else "user"):
        raise HTTPException(400, "用户名已存在或非法")
    tdb.log("user_create", req.username)
    return {"ok": True}

class UserActReq(BaseModel):
    action: str
    password: str | None = None

@app.post("/api/admin/users/{username}/action")
def admin_user_action(username: str, req: UserActReq, u=Depends(require_admin)):
    a = req.action
    if a == "disable":
        tdb.user_set_enabled(username, False); ev = "user_disable"
    elif a == "enable":
        tdb.user_set_enabled(username, True); ev = "user_enable"
    elif a == "reset_password":
        if len(req.password or "") < 8:
            raise HTTPException(400, "密码至少 8 位")
        tdb.user_set_password(username, req.password); ev = "user_reset_pw"
    elif a == "delete":
        r = tdb.user_delete(username); ev = "user_delete"
        if not r:
            raise HTTPException(400, "不能删除 admin 或用户不存在")
    else:
        raise HTTPException(400, "非法 action")
    tdb.log(ev, username)
    return {"ok": True}

@app.get("/api/admin/mcp_tokens")
def admin_mcp_tokens(u=Depends(require_admin)):
    rows = tdb.mcp_token_list()
    return [{"id": r[0], "name": r[1],
             "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[2])),
             "last_used": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3])) if r[3] else None,
             "enabled": bool(r[4]),
             "expires": time.strftime("%Y-%m-%d", time.localtime(r[5])) if r[5] else "永不过期"}
            for r in rows]

class McpTokenCreateReq(BaseModel):
    name: str
    ttl_days: int | None = None

@app.post("/api/admin/mcp_tokens")
def admin_mcp_token_create(req: McpTokenCreateReq, u=Depends(require_admin)):
    name = req.name.strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "name 非法（1~40 字符）")
    ttl = req.ttl_days and int(req.ttl_days) * 86400 or None
    tok, tid = tdb.mcp_token_create(name, ttl)
    if not tok:
        raise HTTPException(400, "name 已存在")
    tdb.log("mcp_token_create", name)
    return {"ok": True, "id": tid, "name": name,
            "token": tok,   # 明文仅此次返回，数据库只存 sha256
            "warning": "该 token 仅本次显示，请立即复制保存；数据库不可恢复。"}


@app.post("/api/admin/mcp_tokens/{id_or_name}/action")
def admin_mcp_token_action(id_or_name: str, body: dict, u=Depends(require_admin)):
    a = body.get("action")
    ok = tdb.mcp_token_action(id_or_name, a)
    if not ok:
        raise HTTPException(400, "invalid action" if a not in ("disable","enable","delete") else "找不到该 token")
    tdb.log("mcp_token_" + a, str(id_or_name)[:32])
    return {"ok": True}


@app.get("/api/admin/system")
def admin_system(u=Depends(require_admin)):
    return {"db_path": str(tdb.DB_PATH),
            "db_size_kb": os.path.getsize(tdb.DB_PATH) // 1024,
            "sources": [r[0] for r in tdb.source_list()],
            "limits": {"l1_concurrency": tdb.get_setting("l1_concurrency"),
                       "l2_concurrency": 1,
                       "source_concurrency": tdb.get_setting("source_concurrency")},
            "note": "Server-side Health ≠ 本机网速。服务端不保存 Torrent 资源库。"}

@app.get("/api/admin/logs")
def admin_logs(u=Depends(require_admin)):
    return [{"event": r[0], "detail": r[1], "at": r[2]} for r in tdb.log_tail(80)]

# ---------------- UI ----------------
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")

mcp_bind(search_core, l1_core, _mcp_deep_check,
         _get_torrent_cached, lambda ev, d: tdb.log(ev, d))

# 注意：get_torrent 还需 l1/l2 缓存回调
import mcp_server as _mcp_server
_mcp_server._ctx["l1c"] = lambda ih: tdb.cache_get("l1:" + ih.lower())
_mcp_server._ctx["l2c"] = lambda ih: tdb.cache_get("l2:" + ih.lower())

app.mount("/", _mcp_app)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("TT_LISTEN", "127.0.0.1"), port=APP_PORT, log_level="warning")
