#!/usr/bin/env python3
"""torrent_core.db — SQLite: users/sessions/sources/settings/logs + TTL cache.
Python 标准库实现，无外部依赖。

设计时序：先启动时建成 schema；admin 账号只创建一次；密保不写配置不带前端。"},
"""
import sqlite3, time, json, hashlib, secrets, threading
from pathlib import Path

DB_PATH = None
_conn = None
_lk = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,          -- scrypt hex
  salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
  name TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 50,
  timeout REAL NOT NULL DEFAULT 10,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  success_count INTEGER DEFAULT 0,
  fail_count INTEGER DEFAULT 0,
  avg_latency REAL DEFAULT NULL,
  last_success REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS search_settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS cache(          -- search_cache / health_cache 共用表
  key TEXT PRIMARY KEY,
  value TEXT,
  created_at REAL,
  expires_at REAL
);
CREATE TABLE IF NOT EXISTS mcp_tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,   -- sha256 hex；绝不存明文
  created_at REAL NOT NULL,
  last_used_at REAL,
  enabled INTEGER NOT NULL DEFAULT 1,
  expires_at REAL                    -- NULL = 永不过期
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT, detail TEXT, created_at REAL
);
"""


def init(db_path):
    global DB_PATH, _conn
    DB_PATH = str(db_path)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.executescript(SCHEMA)
    _conn.commit()


def _q(sql, args=()):
    with _lk:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.fetchall()


# ---------- TTL cache ----------
def cache_get(key):
    with _lk:
        row = _conn.execute("SELECT value FROM cache WHERE key=? AND expires_at>?",
                            (key, time.time())).fetchone()
    return json.loads(row[0]) if row else None

def cache_put(key, value, ttl):
    with _lk:
        _conn.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                      (key, json.dumps(value, ensure_ascii=False),
                       time.time(), time.time() + ttl))
        if secrets.randbelow(10) == 0:
            _conn.execute("DELETE FROM cache WHERE expires_at<?", (time.time(),))
        _conn.commit()

# ---------- scrypt 密码 ----------
SCRYPT_N, SCRYPT_r, SCRYPT_p = 1 << 14, 8, 1

def hash_password(pw: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=SCRYPT_N, r=SCRYPT_r, p=SCRYPT_p, dklen=32)
    return h.hex(), salt

def verify_password(pw: str, pw_hash: str, salt: str) -> bool:
    try:
        h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=SCRYPT_N, r=SCRYPT_r, p=SCRYPT_p, dklen=32)
        return secrets.compare_digest(h.hex(), pw_hash)
    except Exception:
        return False

# ---------- users ----------
def ensure_admin():
    """无用户时创建 admin（密码优先环境变量，否则随机写 stdout journal）。"""
    if _conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return None
    import os
    pw = os.environ.get("TT_ADMIN_PASS") or secrets.token_urlsafe(12)
    h, salt = hash_password(pw)
    _conn.execute("INSERT INTO users(username,pw_hash,salt,role,enabled,created_at) VALUES(?,?,?,?,1,?)",
                  ("admin", h, salt, "admin", time.time()))
    _conn.commit()
    print("ADMIN_CREATED user=admin password=%s (仅本次显示，请勿记录到日志)" % pw, flush=True)
    return "admin"

def get_user(username):
    with _lk:
        return _conn.execute("SELECT id,username,pw_hash,salt,role,enabled,created_at,last_login FROM users WHERE username=?",
                             (username,)).fetchone()

def login_user(username, password):
    row = get_user(username)
    if not row or not row[5]:
        return None
    if not verify_password(password, row[2], row[3]):
        return None
    return row

def session_token():
    return secrets.token_urlsafe(32)

def create_session(user_id, ttl):
    tok = session_token()
    th = hashlib.sha256(tok.encode()).hexdigest()
    with _lk:
        _conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                      (th, user_id, time.time(), time.time() + ttl))
        # keep sessions small
        _conn.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        _conn.commit()
    return tok

def session_user(token):
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    with _lk:
        row = _conn.execute(
            "SELECT u.id,u.username,u.role FROM users u JOIN sessions s ON s.user_id=u.id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1", (th, time.time())).fetchone()
    return {"id": row[0], "username": row[1], "role": row[2]} if row else None

def logout(token):
    if token:
        with _lk:
            _conn.execute("DELETE FROM sessions WHERE token_hash=?",
                          (hashlib.sha256(token.encode()).hexdigest(),))
            _conn.commit()

def touch_last_login(user_id):
    with _lk:
        _conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))
        _conn.commit()

# ---------- admin: users ----------
def user_list():
    with _lk:
        return _conn.execute("SELECT username,role,enabled,created_at,last_login FROM users ORDER BY id").fetchall()

def user_create(username, password, role='user'):
    h, salt = hash_password(password)
    with _lk:
        try:
            _conn.execute("INSERT INTO users(username,pw_hash,salt,role,enabled,created_at) VALUES(?,?,?,?,1,?)",
                          (username, h, salt, role, time.time()))
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def user_delete(username):
    with _lk:
        n = _conn.execute("DELETE FROM users WHERE username=? AND role!='admin'", (username,)).rowcount
        _conn.commit()
        return n > 0

def user_set_enabled(username, enabled):
    with _lk:
        _conn.execute("UPDATE users SET enabled=? WHERE username=?", (1 if enabled else 0, username))
        _conn.commit()

def user_set_password(username, password):
    h, salt = hash_password(password)
    with _lk:
        _conn.execute("UPDATE users SET pw_hash=?,salt=? WHERE username=?", (h, salt, username))
        _conn.commit()

# ---------- sources ----------
def ensure_sources(defaults):
    with _lk:
        now = time.time()
        for s in defaults:
            _conn.execute("INSERT OR IGNORE INTO sources(name,created_at,updated_at) VALUES(?,?,?)", (s, now, now))
        _conn.commit()

def source_list():
    with _lk:
        return _conn.execute("SELECT name,enabled,priority,timeout,success_count,fail_count,avg_latency,last_success,last_error FROM sources ORDER BY name").fetchall()

def source_update(name, field, value):
    assert field in ("enabled", "priority", "timeout")
    with _lk:
        _conn.execute(f"UPDATE sources SET {field}=?, updated_at=? WHERE name=?", (value, time.time(), name))
        _conn.commit()

def source_record(name, ok, latency=None, error=None):
    with _lk:
        now = time.time()
        cur = _conn.execute("SELECT success_count, fail_count, avg_latency FROM sources WHERE name=?", (name,)).fetchone()
        if not cur:
            _conn.execute("INSERT OR IGNORE INTO sources(name,created_at,updated_at) VALUES(?,?,?)", (name, now, now))
            cur = (0, 0, None)
        s, f, a = cur[0] or 0, cur[1] or 0, cur[2]
        if ok:
            s += 1
            a = a * 0.7 + latency * 0.3 if a is not None else latency
            _conn.execute("UPDATE sources SET success_count=?, avg_latency=?, last_success=? WHERE name=?",
                          (s, a, now, name))
        else:
            f += 1
            _conn.execute("UPDATE sources SET fail_count=?, last_error=?, updated_at=? WHERE name=?",
                          (f, (error or "")[:160], now, name))
        _conn.commit()

# ---------- settings ----------
DEFAULTS = {
    "precision": "balanced", "sort": "relevance", "max_results": 50,
    "source_concurrency": 3, "l1_concurrency": 2, "l2_concurrency": 1,
    "l1_auto": False, "session_ttl": 7 * 86400,
    "ttl_search": 600, "ttl_l1": 300, "ttl_l2": 900,
    "w_relevance": 0.40, "w_seed": 0.20, "w_fresh": 0.15,
    "w_source_quality": 0.10, "w_size": 0.10, "w_health": 0.05,
    "rate_search_capacity": 30, "rate_search_refill": 0.5,
    "rate_l1_capacity": 12, "rate_l1_refill": 0.2,
    "rate_l2_capacity": 6, "rate_l2_refill": 0.05,
}

def get_setting(key):
    with _lk:
        row = _conn.execute("SELECT value FROM search_settings WHERE key=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    return DEFAULTS.get(key)

def set_setting(key, value):
    with _lk:
        _conn.execute("INSERT OR REPLACE INTO search_settings VALUES(?,?)", (key, json.dumps(value)))
        _conn.commit()

def all_settings():
    out = dict(DEFAULTS)
    with _lk:
        for k, v in _conn.execute("SELECT key,value FROM search_settings").fetchall():
            out[k] = json.loads(v)
    return out

# ---------- logs (短期，自动截断) ----------
def log(event, detail=""):
    with _lk:
        _conn.execute("INSERT INTO logs(event,detail,created_at) VALUES(?,?,?)",
                      (event, detail[:300], time.time()))
        _conn.execute("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs) - 800")
        _conn.commit()

def log_tail(n=80):
    with _lk:
        return _conn.execute("SELECT event, detail, datetime(created_at,'unixepoch','localtime') FROM logs ORDER BY id DESC LIMIT ?", (n,)).fetchall()

# ---------- MCP tokens ----------
def mcp_token_create(name, ttl=None):
    """创建 token；返回 (token明文, id)。明文只在创建时返回一次。"""
    tok = secrets.token_urlsafe(32)
    th = hashlib.sha256(tok.encode()).hexdigest()
    exp = time.time() + ttl if ttl else None
    with _lk:
        try:
            _conn.execute("INSERT INTO mcp_tokens(name,token_hash,created_at,enabled,expires_at) VALUES(?,?,?,1,?)",
                          (name, th, time.time(), exp))
            _conn.commit()
        except sqlite3.IntegrityError:
            return None, None
    return tok, _conn.execute("SELECT id FROM mcp_tokens WHERE name=?", (name,)).fetchone()[0]

def mcp_token_auth(token):
    """校验 Bearer token，返回 (token_id, name) 或 None。成功时更新 last_used_at。"""
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    with _lk:
        row = _conn.execute(
            "SELECT id,name,enabled,expires_at FROM mcp_tokens WHERE token_hash=?", (th,)).fetchone()
        if not row or not row[2]:
            return None
        if row[3] and row[3] < time.time():
            return None
        _conn.execute("UPDATE mcp_tokens SET last_used_at=? WHERE id=?", (time.time(), row[0]))
        _conn.commit()
    return {"id": row[0], "name": row[1]}

def mcp_token_list():
    with _lk:
        return _conn.execute(
            "SELECT id,name,created_at,last_used_at,enabled,expires_at FROM mcp_tokens ORDER BY id").fetchall()

def mcp_token_action(id_or_name, action):
    """action: disable|enable|delete。"""
    with _lk:
        row = _conn.execute("SELECT id FROM mcp_tokens WHERE id=? OR name=?", (id_or_name, id_or_name)).fetchone()
        if not row:
            return False
        tid = row[0]
        if action == "delete":
            _conn.execute("DELETE FROM mcp_tokens WHERE id=?", (tid,))
        elif action == "disable":
            _conn.execute("UPDATE mcp_tokens SET enabled=0 WHERE id=?", (tid,))
        elif action == "enable":
            _conn.execute("UPDATE mcp_tokens SET enabled=1 WHERE id=?", (tid,))
        else:
            return False
        _conn.commit()
        return True

# ---------- get_torrent 辅助：从 TTL 缓存的搜索结果里找 info_hash ----------
def find_cached_result(info_hash):
    """从 cache 表的 s:* 搜索缓存里找 info_hash。返回 (rec, key) 或 None。
    注意：只读已缓存内容，不做重新搜索（避免 L2/全网扫描）。"""
    ih = info_hash.lower()
    with _lk:
        rows = _conn.execute("SELECT key,value,(expires_at>?) FROM cache WHERE key LIKE 's:%' ORDER BY created_at DESC",
                             (time.time(),)).fetchall()
    import json as _json
    for k, v, live in rows:
        try:
            d = _json.loads(v)
        except Exception:
            continue
        for rec in d.get("results", []):
            if rec.get("info_hash") == ih:
                rec = dict(rec)
                rec["_cache_live"] = bool(live)   # False = 记录已过 TTL 仍在（视为历史记录）
                return rec, k
    return None, None

def cached_l1(info_hash):
    cached = cache_get("l1:" + info_hash.lower())
    if cached:
        cached["_checked_at"] = cached.get("detection_time")
    return cached

def cached_l2(info_hash):
    return cache_get("l2:" + info_hash.lower())
