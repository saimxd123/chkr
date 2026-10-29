import sqlite3, os, time, threading, json
from contextlib import contextmanager
from config import DB_PATH, ADMIN_ID, DEFAULT_PIN

_lock = threading.Lock()

def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS users (
            chat_id   INTEGER PRIMARY KEY,
            username  TEXT,
            is_admin  INTEGER DEFAULT 0,
            authed_at INTEGER,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS proxies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER,
            url        TEXT,
            live       INTEGER DEFAULT 0,
            latency_ms INTEGER,
            checked_at INTEGER,
            UNIQUE(chat_id, url)
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER,
            kind       TEXT,
            status     TEXT DEFAULT 'queued',
            created_at INTEGER,
            total      INTEGER DEFAULT 0,
            done       INTEGER DEFAULT 0,
            hits       INTEGER DEFAULT 0,
            meta       TEXT
        );
        CREATE TABLE IF NOT EXISTS urls (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id         INTEGER,
            url            TEXT,
            status         TEXT DEFAULT 'pending',
            live           INTEGER DEFAULT 0,
            shopify        INTEGER DEFAULT 0,
            has_cart       INTEGER DEFAULT 0,
            has_checkout   INTEGER DEFAULT 0,
            has_captcha    INTEGER DEFAULT 0,
            lowest_price   REAL,
            lowest_product TEXT,
            note           TEXT,
            checked_at     INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_urls_job ON urls(job_id);
        CREATE INDEX IF NOT EXISTS ix_proxies_chat ON proxies(chat_id);
        """)
        c.execute("INSERT OR IGNORE INTO kv (k,v) VALUES ('pin', ?)", (DEFAULT_PIN,))

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

# ── kv ────────────────────────────────────────────────────────────
def kv_get(k: str, d=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d

def kv_set(k: str, v: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)", (k, v))

# ── pin ───────────────────────────────────────────────────────────
def get_pin() -> str:
    return kv_get("pin", DEFAULT_PIN)

def set_pin(new: str):
    kv_set("pin", new)

# ── users ─────────────────────────────────────────────────────────
def upsert_user(chat_id: int, username: str):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (chat_id, username, is_admin, created_at) VALUES (?,?,?,?)",
            (chat_id, username, 1 if chat_id == ADMIN_ID else 0, int(time.time())),
        )
        c.execute("UPDATE users SET username=? WHERE chat_id=?", (username, chat_id))

def is_authed(chat_id: int) -> bool:
    if chat_id == ADMIN_ID:
        return True
    with db() as c:
        r = c.execute("SELECT authed_at FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r["authed_at"])

def mark_authed(chat_id: int):
    with db() as c:
        c.execute("UPDATE users SET authed_at=? WHERE chat_id=?", (int(time.time()), chat_id))

def list_users():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT 500"
        )]

def revoke_user(chat_id: int) -> bool:
    with db() as c:
        cur = c.execute("UPDATE users SET authed_at=NULL WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

# ── proxies ───────────────────────────────────────────────────────
def add_proxies(chat_id: int, urls: list[str]) -> int:
    added = 0
    with db() as c:
        for u in urls:
            u = u.strip()
            if not u:
                continue
            if "://" not in u:
                u = "http://" + u
            try:
                c.execute(
                    "INSERT OR IGNORE INTO proxies (chat_id, url) VALUES (?,?)",
                    (chat_id, u),
                )
                added += c.total_changes
            except Exception:
                pass
    return added

def set_proxy_result(proxy_id: int, live: bool, latency_ms: int | None):
    with db() as c:
        c.execute(
            "UPDATE proxies SET live=?, latency_ms=?, checked_at=? WHERE id=?",
            (1 if live else 0, latency_ms, int(time.time()), proxy_id),
        )

def list_proxies(chat_id: int, live_only: bool = False):
    with db() as c:
        q = "SELECT * FROM proxies WHERE chat_id=?"
        if live_only:
            q += " AND live=1"
        return [dict(r) for r in c.execute(q, (chat_id,))]

def clear_proxies(chat_id: int):
    with db() as c:
        c.execute("DELETE FROM proxies WHERE chat_id=?", (chat_id,))

def pick_live_proxies(chat_id: int) -> list[str]:
    return [p["url"] for p in list_proxies(chat_id, live_only=True)]

# ── jobs ──────────────────────────────────────────────────────────
def create_job(chat_id: int, kind: str, meta: dict, total: int) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO jobs (chat_id, kind, created_at, total, meta) VALUES (?,?,?,?,?)",
            (chat_id, kind, int(time.time()), total, json.dumps(meta)),
        )
        return cur.lastrowid

def get_job(jid: int):
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None

def set_job_status(jid: int, status: str):
    with db() as c:
        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))

def add_urls(jid: int, urls: list[str]):
    with db() as c:
        c.executemany(
            "INSERT INTO urls (job_id, url) VALUES (?,?)",
            [(jid, u) for u in urls],
        )

def save_url_result(jid: int, url: str, data: dict):
    with db() as c:
        c.execute("""UPDATE urls SET status='checked', live=?, shopify=?, has_cart=?,
                     has_checkout=?, has_captcha=?, lowest_price=?, lowest_product=?,
                     note=?, checked_at=? WHERE job_id=? AND url=?""",
                  (data.get("live"),
                   data.get("shopify"),
                   data.get("has_cart"),
                   data.get("has_checkout"),
                   data.get("has_captcha"),
                   data.get("lowest_price"),
                   data.get("lowest_product"),
                   data.get("note", ""),
                   int(time.time()),
                   jid, url))
        c.execute(
            "UPDATE jobs SET done=(SELECT COUNT(*) FROM urls WHERE job_id=? AND status='checked') WHERE id=?",
            (jid, jid),
        )

def save_scrape_hit(jid: int, site: str, price: float, title: str):
    with db() as c:
        c.execute(
            "INSERT INTO urls (job_id, url, status, live, shopify, has_cart, lowest_price, lowest_product, checked_at) "
            "VALUES (?,?, 'checked', 1, 1, 1, ?, ?, ?)",
            (jid, site, price, title, int(time.time())),
        )
        c.execute("UPDATE jobs SET done=done+1, hits=hits+1 WHERE id=?", (jid,))

def job_urls(jid: int):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM urls WHERE job_id=?", (jid,))]

def pending_urls(jid: int):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM urls WHERE job_id=? AND status='pending'", (jid,)
        )]

def list_jobs(chat_id: int, limit: int = 15):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM jobs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )]

def delete_job(jid: int, chat_id: int):
    with db() as c:
        c.execute("DELETE FROM urls WHERE job_id=?", (jid,))
        c.execute("DELETE FROM jobs WHERE id=? AND chat_id=?", (jid, chat_id))

def admin_stats():
    with db() as c:
        users   = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        authed  = c.execute("SELECT COUNT(*) c FROM users WHERE authed_at IS NOT NULL OR chat_id=?", (ADMIN_ID,)).fetchone()["c"]
        jobs    = c.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        urls    = c.execute("SELECT COUNT(*) c FROM urls").fetchone()["c"]
        live_pr = c.execute("SELECT COUNT(*) c FROM proxies WHERE live=1").fetchone()["c"]
        return {"users": users, "authed": authed, "jobs": jobs, "urls": urls, "live_proxies": live_pr}

init()
