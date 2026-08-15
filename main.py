import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

import httpx
import instaloader
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
# Avoid httpx logging full Telegram Bot API URLs (which contain the bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bluegate-downloader-v4.2")
STARTED_AT = int(time.time())

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me").strip()
COOKIE_FILE = os.getenv("COOKIE_FILE", "").strip()
YOUTUBE_COOKIE_FILE = os.getenv("YOUTUBE_COOKIE_FILE", "").strip()
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
MAX_SEND_MB = int(os.getenv("MAX_SEND_MB", "49"))
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "10"))
DB_PATH = os.getenv("DB_PATH", "/tmp/bluegate_downloader.db").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "BlueGate Downloader").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "BlueGateSupport").strip().lstrip("@")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}
JOB_TTL_HOURS = int(os.getenv("JOB_TTL_HOURS", "12"))
SPOTIFY_ENABLED = os.getenv("SPOTIFY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SPOTIFY_BITRATE = os.getenv("SPOTIFY_BITRATE", "128k").strip()
YOUTUBE_POT_ENABLED = os.getenv("YOUTUBE_POT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
YOUTUBE_POT_BASE_URL = os.getenv("YOUTUBE_POT_BASE_URL", "http://127.0.0.1:4416").strip()
YOUTUBE_PLAYER_CLIENT = os.getenv("YOUTUBE_PLAYER_CLIENT", "mweb").strip() or "mweb"
FASTSAVER_API_KEY = os.getenv("FASTSAVER_API_KEY", "").strip()
FASTSAVER_BASE_URL = os.getenv("FASTSAVER_BASE_URL", "https://api.fastsaver.io/v1").strip().rstrip("/")
FASTSAVER_BOT_USERNAME = os.getenv("FASTSAVER_BOT_USERNAME", "").strip()
FASTSAVER_TIMEOUT = int(os.getenv("FASTSAVER_TIMEOUT", "300"))

def prepare_runtime_cookies() -> None:
    """Materialize base64-encoded Netscape cookies from Render secrets."""
    global YOUTUBE_COOKIE_FILE
    if YOUTUBE_COOKIES_B64:
        target = Path("/tmp/youtube_cookies.txt")
        try:
            raw = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
            target.write_bytes(raw)
            os.chmod(target, 0o600)
            YOUTUBE_COOKIE_FILE = str(target)
            log.info("YouTube cookie file loaded from YOUTUBE_COOKIES_B64")
        except Exception as exc:
            log.error("Could not decode YOUTUBE_COOKIES_B64: %s", exc)

prepare_runtime_cookies()

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI(title="BlueGate Multi Downloader V4.2 Hybrid Admin/API Pool")
loader = instaloader.Instaloader(download_pictures=False, download_videos=False, save_metadata=False, quiet=True)

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
POST_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"https?://(?:www\.)?instagram\.com/stories/[^\s?#]+(?:/\d+)?/?", re.I)
HIGHLIGHT_RE = re.compile(r"https?://(?:www\.)?instagram\.com/stories/highlights/\d+/?", re.I)
SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)", re.I)

PLATFORM_LABELS = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "twitter": "X / Twitter",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
    "generic": "Media",
}
PLATFORM_ICONS = {"instagram":"📸", "youtube":"▶️", "twitter":"𝕏", "soundcloud":"☁️", "spotify":"🟢", "generic":"🌐"}


def now_ts() -> int:
    return int(time.time())


class CompatRow(dict):
    """Mapping row that also supports row[0] like sqlite3.Row."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class CompatCursor:
    def __init__(self, cursor, postgres: bool):
        self.cursor = cursor
        self.postgres = postgres
    def _row(self, row):
        if row is None or not self.postgres:
            return row
        return CompatRow(row)
    def fetchone(self):
        return self._row(self.cursor.fetchone())
    def fetchall(self):
        return [self._row(r) for r in self.cursor.fetchall()]


class CompatConn:
    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres
    def execute(self, sql: str, params=()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return CompatCursor(self.raw.execute(sql, params), self.postgres)
    def executescript(self, script: str):
        # Our schema statements contain no semicolons inside strings/procedures.
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)


@contextmanager
def db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL تنظیم شده ولی psycopg نصب نیست.")
        # Disable server-side prepared statements for PgBouncer/pooled Neon URLs.
        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=12, prepare_threshold=None)
        conn = CompatConn(raw, True)
    else:
        path = Path(DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        conn = CompatConn(raw, False)
    try:
        yield conn
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def db_backend() -> str:
    return "Neon/PostgreSQL" if DATABASE_URL else "SQLite (ephemeral on Render)"


def init_db():
    with db() as conn:
        if DATABASE_URL:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                joined_at BIGINT NOT NULL, last_seen BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,
                source_url TEXT NOT NULL, result_json TEXT NOT NULL, created_at BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS downloads (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, job_id TEXT,
                media_type TEXT, quality TEXT, bytes BIGINT DEFAULT 0,
                platform TEXT DEFAULT 'unknown', created_at BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_cache (
                cache_key TEXT PRIMARY KEY, file_id TEXT NOT NULL, title TEXT, platform TEXT, updated_at BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bans (user_id BIGINT PRIMARY KEY, reason TEXT, created_at BIGINT NOT NULL);
            CREATE TABLE IF NOT EXISTS error_logs (id BIGSERIAL PRIMARY KEY, user_id BIGINT, platform TEXT, error TEXT, created_at BIGINT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_state (admin_id BIGINT PRIMARY KEY, action TEXT NOT NULL, payload TEXT, updated_at BIGINT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_modes (admin_id BIGINT PRIMARY KEY, mode TEXT NOT NULL, updated_at BIGINT NOT NULL);
            CREATE TABLE IF NOT EXISTS fastsaver_keys (
                key_id TEXT PRIMARY KEY, key_secret TEXT UNIQUE NOT NULL, label TEXT,
                priority INTEGER NOT NULL DEFAULT 100, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active', cooldown_until BIGINT NOT NULL DEFAULT 0,
                last_error TEXT, last_used BIGINT NOT NULL DEFAULT 0, balance_json TEXT,
                created_at BIGINT NOT NULL
            );
            """)
            conn.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'unknown'")
        else:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                joined_at INTEGER NOT NULL, last_seen INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                source_url TEXT NOT NULL, result_json TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, job_id TEXT,
                media_type TEXT, quality TEXT, bytes INTEGER DEFAULT 0,
                platform TEXT DEFAULT 'unknown', created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_cache (
                cache_key TEXT PRIMARY KEY, file_id TEXT NOT NULL, title TEXT, platform TEXT, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY, reason TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS error_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, platform TEXT, error TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_state (admin_id INTEGER PRIMARY KEY, action TEXT NOT NULL, payload TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_modes (admin_id INTEGER PRIMARY KEY, mode TEXT NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fastsaver_keys (
                key_id TEXT PRIMARY KEY, key_secret TEXT UNIQUE NOT NULL, label TEXT,
                priority INTEGER NOT NULL DEFAULT 100, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active', cooldown_until INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, last_used INTEGER NOT NULL DEFAULT 0, balance_json TEXT,
                created_at INTEGER NOT NULL
            );
            """)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
            if "platform" not in cols:
                conn.execute("ALTER TABLE downloads ADD COLUMN platform TEXT DEFAULT 'unknown'")
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (now_ts() - JOB_TTL_HOURS * 3600,))

    # Backwards compatibility: the old single Render key becomes pool key #1 once.
    if FASTSAVER_API_KEY:
        try:
            add_fastsaver_key(FASTSAVER_API_KEY, "Render ENV", validate=False)
        except Exception as exc:
            log.warning("Could not bootstrap FASTSAVER_API_KEY into pool: %s", exc)


def upsert_user(user: dict[str, Any]):
    uid = user.get("id")
    if not uid:
        return
    with db() as conn:
        conn.execute("""
            INSERT INTO users(user_id,username,first_name,joined_at,last_seen) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen
        """, (uid, user.get("username", ""), user.get("first_name", ""), now_ts(), now_ts()))


def save_job(user_id: int, chat_id: int, source_url: str, result: dict[str, Any]) -> str:
    job_id = secrets.token_urlsafe(7).replace("-", "").replace("_", "")[:9]
    with db() as conn:
        conn.execute("INSERT INTO jobs(job_id,user_id,chat_id,source_url,result_json,created_at) VALUES(?,?,?,?,?,?)",
                     (job_id, user_id, chat_id, source_url, json.dumps(result, ensure_ascii=False), now_ts()))
    return job_id


def load_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row or row["created_at"] < now_ts() - JOB_TTL_HOURS * 3600:
        return None
    return {"job_id": row["job_id"], "user_id": row["user_id"], "chat_id": row["chat_id"],
            "source_url": row["source_url"], "result": json.loads(row["result_json"])}


def record_download(user_id: int, job_id: str, media_type: str, quality: str, size: int, platform: str):
    with db() as conn:
        conn.execute("INSERT INTO downloads(user_id,job_id,media_type,quality,bytes,platform,created_at) VALUES(?,?,?,?,?,?,?)",
                     (user_id, job_id, media_type, quality, size, platform, now_ts()))


def get_cached_file_id(cache_key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT file_id FROM media_cache WHERE cache_key=?", (cache_key,)).fetchone()
    return row["file_id"] if row else None


def set_cached_file_id(cache_key: str, file_id: str, title: str = "", platform: str = "") -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO media_cache(cache_key,file_id,title,platform,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET file_id=excluded.file_id,title=excluded.title,platform=excluded.platform,updated_at=excluded.updated_at
        """, (cache_key, file_id, title, platform, now_ts()))



def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))


def bool_setting(key: str, default: bool=True) -> bool:
    v=get_setting(key, "1" if default else "0").lower()
    return v in {"1","true","yes","on"}


def service_enabled(platform: str) -> bool:
    return bool_setting(f"service_{platform}", True)


def is_banned(user_id: int) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM bans WHERE user_id=?",(user_id,)).fetchone() is not None


def set_ban(user_id: int, banned: bool, reason: str="Admin") -> None:
    with db() as conn:
        if banned: conn.execute("INSERT INTO bans(user_id,reason,created_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at",(user_id,reason,now_ts()))
        else: conn.execute("DELETE FROM bans WHERE user_id=?",(user_id,))


def log_error(user_id: int|None, platform: str, exc: Exception|str) -> None:
    try:
        text=str(exc)[:1800]
        with db() as conn: conn.execute("INSERT INTO error_logs(user_id,platform,error,created_at) VALUES(?,?,?,?)",(user_id,platform,text,now_ts()))
    except Exception: pass


def daily_limit() -> int:
    try: return max(0,int(get_setting("daily_limit","20")))
    except Exception: return 20


def user_downloads_today(user_id:int) -> int:
    start=now_ts()-86400
    with db() as conn:
        return conn.execute("SELECT COUNT(*) c FROM downloads WHERE user_id=? AND created_at>=?",(user_id,start)).fetchone()["c"]


def set_admin_state(admin_id:int, action:str, payload:str="") -> None:
    with db() as conn: conn.execute("INSERT INTO admin_state(admin_id,action,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(admin_id) DO UPDATE SET action=excluded.action,payload=excluded.payload,updated_at=excluded.updated_at",(admin_id,action,payload,now_ts()))


def pop_admin_state(admin_id:int):
    with db() as conn:
        row=conn.execute("SELECT action,payload FROM admin_state WHERE admin_id=?",(admin_id,)).fetchone()
        if row: conn.execute("DELETE FROM admin_state WHERE admin_id=?",(admin_id,))
    return (row["action"],row["payload"]) if row else None


def force_join_channel() -> str:
    return get_setting("force_join_channel", FORCE_JOIN_CHANNEL)


def force_join_url() -> str:
    return get_setting("force_join_url", FORCE_JOIN_URL)

def get_admin_mode(admin_id: int) -> str:
    if admin_id not in ADMIN_IDS:
        return "user"
    with db() as conn:
        row = conn.execute("SELECT mode FROM admin_modes WHERE admin_id=?", (admin_id,)).fetchone()
    return row["mode"] if row and row["mode"] in {"admin", "user"} else "admin"


def set_admin_mode(admin_id: int, mode: str) -> None:
    if admin_id not in ADMIN_IDS:
        return
    mode = "user" if mode == "user" else "admin"
    with db() as conn:
        conn.execute("INSERT INTO admin_modes(admin_id,mode,updated_at) VALUES(?,?,?) ON CONFLICT(admin_id) DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at", (admin_id, mode, now_ts()))


def mask_api_key(secret: str) -> str:
    if len(secret) <= 12:
        return secret[:3] + "••••"
    return secret[:7] + "••••••" + secret[-4:]


def add_fastsaver_key(secret: str, label: str = "", validate: bool = False) -> str:
    secret = secret.strip()
    if not secret:
        raise ValueError("API key خالی است.")
    key_id = secrets.token_hex(4)
    with db() as conn:
        existing = conn.execute("SELECT key_id FROM fastsaver_keys WHERE key_secret=?", (secret,)).fetchone()
        if existing:
            return existing["key_id"]
        row = conn.execute("SELECT COALESCE(MAX(priority),0) p FROM fastsaver_keys").fetchone()
        priority = int(row["p"] or 0) + 10
        conn.execute("INSERT INTO fastsaver_keys(key_id,key_secret,label,priority,enabled,status,cooldown_until,last_error,last_used,balance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (key_id, secret, label or f"API {priority//10}", priority, 1, "active", 0, "", 0, "", now_ts()))
    return key_id


def list_fastsaver_keys(include_disabled: bool = True):
    with db() as conn:
        sql = "SELECT * FROM fastsaver_keys"
        if not include_disabled:
            sql += " WHERE enabled=1 AND status NOT IN ('invalid','exhausted')"
        sql += " ORDER BY priority ASC, created_at ASC"
        return conn.execute(sql).fetchall()


def get_fastsaver_key(key_id: str):
    with db() as conn:
        return conn.execute("SELECT * FROM fastsaver_keys WHERE key_id=?", (key_id,)).fetchone()


def update_fastsaver_key(key_id: str, **fields) -> None:
    allowed = {"label","priority","enabled","status","cooldown_until","last_error","last_used","balance_json"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE fastsaver_keys SET {sets} WHERE key_id=?", tuple(fields.values()) + (key_id,))


def delete_fastsaver_key(key_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM fastsaver_keys WHERE key_id=?", (key_id,))


def toggle_fastsaver_key(key_id: str) -> None:
    row = get_fastsaver_key(key_id)
    if row:
        update_fastsaver_key(key_id, enabled=0 if int(row["enabled"] or 0) else 1,
                             status="active" if not int(row["enabled"] or 0) else row["status"])


def move_fastsaver_key(key_id: str, direction: int) -> None:
    rows = list_fastsaver_keys(True)
    ids = [r["key_id"] for r in rows]
    if key_id not in ids:
        return
    i = ids.index(key_id); j = i + direction
    if j < 0 or j >= len(rows):
        return
    a,b=rows[i],rows[j]
    update_fastsaver_key(a["key_id"], priority=int(b["priority"]))
    update_fastsaver_key(b["key_id"], priority=int(a["priority"]))


def _balance_number(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    preferred = ("credits_remaining","remaining_credits","remaining","credits","balance")
    for k in preferred:
        if k in data and isinstance(data[k], (int,float)):
            return float(data[k])
    for v in data.values():
        if isinstance(v, dict):
            x = _balance_number(v)
            if x is not None: return x
    return None


def fastsaver_strategy() -> str:
    v = get_setting("fastsaver_strategy", "sequential")
    return v if v in {"sequential","round_robin","most_credits"} else "sequential"


def fastsaver_candidates():
    rows = list_fastsaver_keys(False)
    now = now_ts()
    # Expired cooldowns automatically become active again.
    for r in rows:
        if r["status"] == "rate_limited" and int(r["cooldown_until"] or 0) <= now:
            update_fastsaver_key(r["key_id"], status="active", cooldown_until=0)
    rows = [r for r in list_fastsaver_keys(False) if not (r["status"] == "rate_limited" and int(r["cooldown_until"] or 0) > now)]
    strategy = fastsaver_strategy()
    if strategy == "round_robin":
        rows.sort(key=lambda r: (int(r["last_used"] or 0), int(r["priority"] or 0)))
    elif strategy == "most_credits":
        def credit(r):
            try: data=json.loads(r["balance_json"] or "{}")
            except Exception: data={}
            x=_balance_number(data)
            return -1 if x is None else x
        rows.sort(key=lambda r: (-credit(r), int(r["priority"] or 0)))
    return rows


class FastSaverHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str, key_id: str = ""):
        self.status=status; self.detail=detail; self.key_id=key_id
        super().__init__(f"FastSaver HTTP {status}: {detail}")


def _fastsaver_account_failure(status: int, detail: str) -> tuple[str | None, int]:
    d=detail.lower()
    if status == 401:
        return "invalid", 0
    if status == 429:
        return "rate_limited", 90
    if status in {402,403} and any(x in d for x in ("credit","quota","subscription","limit")):
        return "exhausted", 0
    if status == 400 and any(x in d for x in ("insufficient credit","not enough credit","credits exhausted","out of credit","quota exceeded")):
        return "exhausted", 0
    if status >= 500:
        return "temporary", 30
    return None, 0


def fastsaver_pool_summary() -> dict[str,int]:
    rows=list_fastsaver_keys(True); now=now_ts()
    out={"total":len(rows),"active":0,"rate_limited":0,"exhausted":0,"invalid":0,"disabled":0}
    for r in rows:
        if not int(r["enabled"] or 0): out["disabled"]+=1
        elif r["status"]=="rate_limited" and int(r["cooldown_until"] or 0)>now: out["rate_limited"]+=1
        elif r["status"] in out: out[r["status"]]+=1
        else: out["active"]+=1
    return out


def stats() -> dict[str, Any]:
    day = now_ts() - 86400
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active24 = conn.execute("SELECT COUNT(*) c FROM users WHERE last_seen>=?", (day,)).fetchone()["c"]
        dl = conn.execute("SELECT COUNT(*) c FROM downloads").fetchone()["c"]
        dl24 = conn.execute("SELECT COUNT(*) c FROM downloads WHERE created_at>=?", (day,)).fetchone()["c"]
        total_bytes = conn.execute("SELECT COALESCE(SUM(bytes),0) c FROM downloads").fetchone()["c"]
        platforms = conn.execute("SELECT platform,COUNT(*) c FROM downloads GROUP BY platform ORDER BY c DESC").fetchall()
    return {"users":users,"active24":active24,"downloads":dl,"downloads24":dl24,"bytes":total_bytes,
            "platforms": [(r["platform"], r["c"]) for r in platforms]}


def clean_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,)>]}\"'؟")


def detect_platform(url: str) -> str:
    host = re.sub(r"^www\.", "", httpx.URL(url).host or "").lower()
    if host.endswith("instagram.com"):
        return "instagram"
    if host in {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"} or host.endswith(".youtube.com"):
        return "youtube"
    if host in {"twitter.com", "x.com", "mobile.twitter.com"} or host.endswith(".twitter.com") or host.endswith(".x.com"):
        return "twitter"
    if host.endswith("soundcloud.com") or host == "on.soundcloud.com":
        return "soundcloud"
    if host.endswith("spotify.com") or host == "spotify.link":
        return "spotify"
    return "generic"


def instagram_kind(url: str) -> str:
    if HIGHLIGHT_RE.match(url): return "highlight"
    if STORY_RE.match(url): return "story"
    if POST_RE.match(url): return "post"
    return "unknown"


def ydl_options(skip_download: bool = True, platform: str = "generic", *, youtube_no_cookie: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "skip_download": skip_download,
        "noplaylist": False, "socket_timeout": 45,
        "extract_flat": False,
    }
    cookie = YOUTUBE_COOKIE_FILE if platform == "youtube" and YOUTUBE_COOKIE_FILE else COOKIE_FILE
    if cookie and Path(cookie).exists() and not (platform == "youtube" and youtube_no_cookie):
        opts["cookiefile"] = cookie

    # Current yt-dlp recommendation for challenged YouTube IPs: mweb + automatic PO-token provider.
    # bgutil runs locally in this same Render container and generates a fresh token per video.
    if platform == "youtube" and YOUTUBE_POT_ENABLED:
        opts["extractor_args"] = {
            "youtube": {"player_client": [YOUTUBE_PLAYER_CLIENT]},
            "youtubepot-bgutilhttp": {"base_url": [YOUTUBE_POT_BASE_URL]},
        }
    return opts


def youtube_attempt_options(skip_download: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Ordered YouTube strategies: POT with cookies, POT guest, then legacy cookie mode."""
    attempts: list[tuple[str, dict[str, Any]]] = []
    if YOUTUBE_POT_ENABLED:
        attempts.append(("mweb+pot+cookies", ydl_options(skip_download, "youtube")))
        attempts.append(("mweb+pot+guest", ydl_options(skip_download, "youtube", youtube_no_cookie=True)))
    legacy = ydl_options(skip_download, "youtube")
    legacy.pop("extractor_args", None)
    attempts.append(("legacy+cookies", legacy))
    return attempts


def extract_yt_info(url: str, platform: str = "generic") -> dict[str, Any] | None:
    if platform != "youtube":
        try:
            with yt_dlp.YoutubeDL(ydl_options(True, platform)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            log.warning("yt-dlp metadata failed %s: %s", url, exc)
            return None

    last_exc: Exception | None = None
    for name, opts in youtube_attempt_options(True):
        try:
            log.info("youtube-engine: metadata attempt=%s", name)
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            last_exc = exc
            log.warning("youtube-engine: metadata attempt=%s failed: %s", name, exc)
    if last_exc:
        log.warning("yt-dlp metadata failed after all YouTube strategies: %s", last_exc)
    return None


def flatten_entries(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not info: return []
    entries = info.get("entries")
    if entries:
        return [e for e in entries if e][:MAX_PLAYLIST_ITEMS]
    return [info]


def quality_list(entry: dict[str, Any] | None) -> list[int]:
    heights = set()
    for f in (entry or {}).get("formats") or []:
        h, vcodec = f.get("height"), f.get("vcodec")
        if h and vcodec not in (None, "none"):
            heights.add(int(h))
    preferred = [2160, 1440, 1080, 720, 480, 360, 240]
    out = [q for q in preferred if q in heights]
    if not out:
        out = sorted(heights, reverse=True)
    return out[:6]


def best_thumbnail(entry: dict[str, Any]) -> str | None:
    thumbs = entry.get("thumbnails") or []
    for t in reversed(thumbs):
        if t.get("url"): return t["url"]
    return entry.get("thumbnail")


def entry_has_video(entry: dict[str, Any]) -> bool:
    if entry.get("vcodec") not in (None, "none"): return True
    return any(f.get("vcodec") not in (None, "none") for f in entry.get("formats") or [])


def entry_has_audio(entry: dict[str, Any]) -> bool:
    if entry.get("acodec") not in (None, "none"): return True
    return any(f.get("acodec") not in (None, "none") for f in entry.get("formats") or [])


def shortcode_from_url(url: str) -> str:
    m = POST_RE.search(url)
    if not m: raise ValueError("این لینک Instagram Post/Reel معتبر نیست.")
    return m.group(1)


def analyze_instagram_post(url: str) -> dict[str, Any]:
    post = instaloader.Post.from_shortcode(loader.context, shortcode_from_url(url))
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())
        media = [{"type":"video" if n.is_video else "image", "display_url":n.display_url,
                  "video_url":n.video_url if n.is_video else None, "qualities":[], "playlist_index":i+1}
                 for i,n in enumerate(nodes)]
    else:
        media = [{"type":"video" if post.is_video else "image", "display_url":post.url,
                  "video_url":post.video_url if post.is_video else None, "qualities":[], "playlist_index":1}]
    yt_entries = flatten_entries(extract_yt_info(url, "instagram"))
    for i,item in enumerate(media):
        if item["type"] == "video":
            entry = yt_entries[i] if i < len(yt_entries) else (yt_entries[0] if yt_entries else None)
            item["qualities"] = quality_list(entry)
            item["has_audio"] = entry_has_audio(entry or {})
    return {"platform":"instagram","kind":"post","url":url,"title":(post.caption or "Instagram media")[:180],
            "owner":getattr(post.owner_profile,"username","instagram"),"media":media}


def analyze_generic_ydl(url: str, platform: str, kind: str | None = None) -> dict[str, Any]:
    info = extract_yt_info(url, platform)
    if not info:
        raise RuntimeError("اطلاعات این لینک از extractor دریافت نشد.")
    entries = flatten_entries(info)
    media = []
    for i,e in enumerate(entries):
        has_v = entry_has_video(e)
        has_a = entry_has_audio(e)
        ext = (e.get("ext") or "").lower()
        if has_v:
            mtype = "video"
        elif has_a or platform == "soundcloud" or ext in {"mp3","m4a","aac","opus","ogg","flac","wav"}:
            mtype = "audio"
        else:
            mtype = "image" if best_thumbnail(e) else "unknown"
        media.append({
            "type":mtype, "qualities":quality_list(e) if has_v else [], "display_url":best_thumbnail(e),
            "playlist_index":i+1, "title":(e.get("title") or f"Item {i+1}")[:180], "has_audio":has_a,
            "duration":e.get("duration"), "id":e.get("id"),
        })
    if not media:
        raise RuntimeError("هیچ فایل قابل دانلودی پیدا نشد.")
    return {"platform":platform,"kind":kind or (info.get("_type") or "media"),"url":url,
            "title":(info.get("title") or media[0].get("title") or "Media")[:180],
            "owner":info.get("uploader") or info.get("channel") or info.get("creator") or platform,
            "media":media}


def fastsaver_headers(secret: str) -> dict[str, str]:
    return {"X-Api-Key": secret, "Accept": "application/json"}


def _extract_detail(response: httpx.Response, data: Any = None) -> str:
    detail = None
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("error") or data.get("message")
    return str(detail or (response.text or f"HTTP {response.status_code}"))[:500]


def _handle_key_failure(row, status: int, detail: str, retry_after: int | None = None) -> bool:
    state,cooldown=_fastsaver_account_failure(status,detail)
    if not state:
        return False
    if state == "rate_limited":
        cooldown=max(cooldown, retry_after or 0)
        update_fastsaver_key(row["key_id"], status="rate_limited", cooldown_until=now_ts()+cooldown, last_error=detail)
    elif state == "temporary":
        update_fastsaver_key(row["key_id"], status="rate_limited", cooldown_until=now_ts()+cooldown, last_error=detail)
    else:
        update_fastsaver_key(row["key_id"], status=state, cooldown_until=0, last_error=detail)
    return True


def fastsaver_request_sync(method: str, path: str, *, params=None, payload=None, timeout: int = 60) -> dict[str,Any]:
    candidates=fastsaver_candidates()
    if not candidates:
        raise RuntimeError("هیچ FastSaver API فعالی در Pool وجود ندارد. ادمین از پنل API اضافه کند.")
    failures=[]
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for row in candidates:
            try:
                update_fastsaver_key(row["key_id"], last_used=now_ts())
                r=client.request(method,f"{FASTSAVER_BASE_URL}{path}",params=params,json=payload,headers=fastsaver_headers(row["key_secret"]))
                try: data=r.json()
                except Exception: data=None
                detail=_extract_detail(r,data)
                if r.status_code >= 400:
                    retry=int(r.headers.get("Retry-After","0") or 0) if str(r.headers.get("Retry-After","0")).isdigit() else 0
                    if _handle_key_failure(row,r.status_code,detail,retry):
                        failures.append(f"{mask_api_key(row['key_secret'])}: {r.status_code} {detail}"); continue
                    raise FastSaverHTTPError(r.status_code,detail,row["key_id"])
                if not isinstance(data,dict) or ("ok" in data and not data.get("ok")):
                    raise RuntimeError(detail)
                update_fastsaver_key(row["key_id"], status="active", cooldown_until=0, last_error="")
                return data
            except FastSaverHTTPError:
                raise
            except (httpx.TransportError,httpx.TimeoutException) as exc:
                update_fastsaver_key(row["key_id"],status="rate_limited",cooldown_until=now_ts()+30,last_error=str(exc)[:400])
                failures.append(f"{mask_api_key(row['key_secret'])}: network {exc}")
    raise RuntimeError("FastSaver همه APIها را امتحان کرد و هیچ‌کدام آماده نبودند. " + " | ".join(failures[-4:]))


def fastsaver_youtube_info_sync(url: str) -> dict[str, Any]:
    return fastsaver_request_sync("GET","/youtube/info",params={"url":url},timeout=60)


def analyze_youtube_fastsaver(url: str) -> dict[str, Any]:
    info = fastsaver_youtube_info_sync(url)
    formats = info.get("formats") or []
    qualities: list[int] = []
    size_map: dict[str, int] = {}
    for f in formats:
        if f.get("type") != "video": continue
        fmt = str(f.get("format") or "")
        m = re.fullmatch(r"(\d+)p", fmt)
        if not m: continue
        q = int(m.group(1)); qualities.append(q)
        try: size_map[str(q)] = int(f.get("filesize") or 0)
        except Exception: pass
    qualities = sorted(set(qualities), reverse=True)
    safe_limit = MAX_SEND_MB * 1024 * 1024
    safe_qualities = [q for q in qualities if not size_map.get(str(q)) or size_map.get(str(q), 0) <= safe_limit]
    if safe_qualities: qualities = safe_qualities
    return {"platform":"youtube","kind":"video","url":url,
            "title":str(info.get("title") or "YouTube video")[:180],"owner":info.get("author") or "YouTube",
            "thumbnail":info.get("thumbnail") or (info.get("thumbnails") or {}).get("max"),
            "media":[{"type":"video","qualities":qualities[:8],"display_url":info.get("thumbnail"),"playlist_index":1,
                      "title":str(info.get("title") or "YouTube video")[:180],"has_audio":True,"duration":info.get("duration"),
                      "id":info.get("video_id"),"filesizes":size_map}],"provider":"fastsaver-pool"}


async def fastsaver_json(method: str, path: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
    candidates=fastsaver_candidates()
    if not candidates:
        raise RuntimeError("هیچ FastSaver API فعالی در Pool وجود ندارد. ادمین از پنل API اضافه کند.")
    failures=[]
    async with httpx.AsyncClient(timeout=timeout or FASTSAVER_TIMEOUT,follow_redirects=True) as client:
        for row in candidates:
            try:
                update_fastsaver_key(row["key_id"],last_used=now_ts())
                r=await client.request(method,f"{FASTSAVER_BASE_URL}{path}",params=params,json=payload,headers=fastsaver_headers(row["key_secret"]))
                try: data=r.json()
                except Exception: data=None
                detail=_extract_detail(r,data)
                if r.status_code >= 400:
                    retry=int(r.headers.get("Retry-After","0") or 0) if str(r.headers.get("Retry-After","0")).isdigit() else 0
                    if _handle_key_failure(row,r.status_code,detail,retry):
                        failures.append(f"{mask_api_key(row['key_secret'])}: {r.status_code} {detail}"); continue
                    raise FastSaverHTTPError(r.status_code,detail,row["key_id"])
                if not isinstance(data,dict) or ("ok" in data and not data.get("ok")):
                    raise RuntimeError(detail)
                update_fastsaver_key(row["key_id"],status="active",cooldown_until=0,last_error="")
                return data
            except FastSaverHTTPError:
                raise
            except (httpx.TransportError,httpx.TimeoutException) as exc:
                update_fastsaver_key(row["key_id"],status="rate_limited",cooldown_until=now_ts()+30,last_error=str(exc)[:400])
                failures.append(f"{mask_api_key(row['key_secret'])}: network {exc}")
    raise RuntimeError("FastSaver همه APIها را امتحان کرد و هیچ‌کدام آماده نبودند. " + " | ".join(failures[-4:]))


async def fastsaver_probe_key(secret: str) -> dict[str,Any]:
    async with httpx.AsyncClient(timeout=30,follow_redirects=True) as client:
        r=await client.get(f"{FASTSAVER_BASE_URL}/balance",headers=fastsaver_headers(secret))
        try: data=r.json()
        except Exception: data=None
        if r.status_code >= 400 or not isinstance(data,dict):
            raise FastSaverHTTPError(r.status_code,_extract_detail(r,data))
        return data


async def refresh_fastsaver_key(key_id: str) -> dict[str,Any]:
    row=get_fastsaver_key(key_id)
    if not row: raise RuntimeError("API پیدا نشد.")
    data=await fastsaver_probe_key(row["key_secret"])
    update_fastsaver_key(key_id,balance_json=json.dumps(data,ensure_ascii=False),status="active",cooldown_until=0,last_error="")
    return data


async def refresh_all_fastsaver_keys() -> tuple[int,int]:
    ok=fail=0
    for row in list_fastsaver_keys(True):
        if not int(row["enabled"] or 0): continue
        try: await refresh_fastsaver_key(row["key_id"]); ok+=1
        except Exception as exc:
            update_fastsaver_key(row["key_id"],last_error=str(exc)[:400]); fail+=1
    return ok,fail


async def fastsaver_youtube_download(url: str, quality: str, outdir: Path) -> Path:
    fmt = f"{quality}p" if quality.isdigit() else "720p"
    data = await fastsaver_json("POST", "/youtube/download", payload={"url":url, "format":fmt}, timeout=FASTSAVER_TIMEOUT)
    dl_url = data.get("download_url")
    if not dl_url: raise RuntimeError("FastSaver لینک دانلود برنگرداند.")
    filename = str(data.get("filename") or f"youtube_{data.get('video_id','video')}_{fmt}.mp4")
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")[:180] or "youtube.mp4"
    if not Path(filename).suffix: filename += ".mp4"
    dest = outdir / filename; await download_url(str(dl_url), dest); return dest


async def fastsaver_search_music(query: str) -> dict[str, Any]:
    data = await fastsaver_json("GET", "/youtube/search", params={"query":query, "page":1}, timeout=90)
    results = [x for x in (data.get("results") or []) if isinstance(x, dict) and x.get("video_id")]
    if not results: raise RuntimeError("FastSaver/YouTube Music نتیجه‌ای برای این آهنگ پیدا نکرد.")
    return results[0]


async def ensure_bot_username() -> str:
    global FASTSAVER_BOT_USERNAME
    if FASTSAVER_BOT_USERNAME:
        return FASTSAVER_BOT_USERNAME if FASTSAVER_BOT_USERNAME.startswith("@") else "@" + FASTSAVER_BOT_USERNAME
    me = await tg("getMe", {})
    username = (me or {}).get("username")
    if not username:
        raise RuntimeError("Username بات از Telegram دریافت نشد؛ FASTSAVER_BOT_USERNAME را تنظیم کن.")
    FASTSAVER_BOT_USERNAME = "@" + username
    return FASTSAVER_BOT_USERNAME


async def fastsaver_audio_file_id(video_id: str, cache_key: str, title: str, platform: str) -> str:
    cached = get_cached_file_id(cache_key)
    if cached:
        log.info("fastsaver: Telegram file_id cache hit key=%s", cache_key)
        return cached
    bot_username = await ensure_bot_username()
    data = await fastsaver_json("POST", "/youtube/audio/tg-bot", payload={"video_id":video_id, "bot_username":bot_username}, timeout=FASTSAVER_TIMEOUT)
    file_id = data.get("file_id")
    if not file_id:
        raise RuntimeError("FastSaver Telegram file_id برنگرداند.")
    set_cached_file_id(cache_key, str(file_id), title, platform)
    return str(file_id)


async def send_audio_file_id(chat_id: int, file_id: str, caption: str) -> None:
    await tg("sendAudio", {"chat_id":str(chat_id), "audio":file_id, "caption":caption, "parse_mode":"HTML"})


def spotify_oembed_sync(url: str) -> dict[str, Any]:
    """Fetch lightweight public metadata using Spotify's oEmbed endpoint."""
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"}) as client:
            r = client.get("https://open.spotify.com/oembed", params={"url": url})
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("spotify oEmbed failed: %s", exc)
        return {}


def analyze_spotify(url: str) -> dict[str, Any]:
    if not SPOTIFY_ENABLED:
        raise RuntimeError("Spotify downloader غیرفعال است.")
    m = SPOTIFY_RE.search(url)
    if not m:
        raise ValueError("فعلاً لینک‌های open.spotify.com برای Track / Album / Playlist پشتیبانی می‌شوند.")
    kind = m.group(1).lower()
    meta = spotify_oembed_sync(url)
    title = (meta.get("title") or {"track":"Spotify Track","album":"Spotify Album","playlist":"Spotify Playlist"}[kind])[:180]
    thumb = meta.get("thumbnail_url")
    return {"platform":"spotify","kind":kind,"url":url,"title":title,"owner":"Spotify",
            "thumbnail":thumb,
            "media":[{"type":"spotify","title":title,"playlist_index":1,"qualities":[]}]}


def analyze_sync(url: str) -> dict[str, Any]:
    platform = detect_platform(url)
    if platform == "spotify": return analyze_spotify(url)
    if platform == "instagram":
        kind = instagram_kind(url)
        if kind == "post": return analyze_instagram_post(url)
        if kind in {"story","highlight"}: return analyze_generic_ydl(url, "instagram", kind)
        return analyze_generic_ydl(url, "instagram", "media")
    if platform == "youtube":
        return analyze_youtube_fastsaver(url)
    if platform in {"twitter","soundcloud"}:
        return analyze_generic_ydl(url, platform)
    raise ValueError("لینک باید از Instagram، YouTube، X/Twitter، SoundCloud یا Spotify باشد.")


async def analyze(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(analyze_sync, url)


async def tg(method: str, data: dict[str, Any] | None = None, files=None):
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{API}/{method}", data=data, files=files)
        try: payload = r.json()
        except Exception: raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:500]}")
        if not payload.get("ok"): raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload.get("result")


async def safe_answer_callback(callback_query_id: str, text: str | None = None, show_alert: bool = False):
    """Best-effort callback acknowledgement. Never abort the actual download flow."""
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text[:180]
    if show_alert:
        data["show_alert"] = "true"
    try:
        await tg("answerCallbackQuery", data)
    except Exception as exc:
        log.warning("answerCallbackQuery ignored: %s", exc)


async def send_text(chat_id: int, text: str, reply_markup: dict | None = None):
    data = {"chat_id":str(chat_id),"text":text,"parse_mode":"HTML",
            "link_preview_options":json.dumps({"is_disabled":True})}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await tg("sendMessage", data)


async def edit_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    data = {"chat_id":str(chat_id),"message_id":str(message_id),"text":text,"parse_mode":"HTML",
            "link_preview_options":json.dumps({"is_disabled":True})}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await tg("editMessageText", data)


async def is_joined(user_id: int) -> bool:
    channel=force_join_channel()
    if not channel: return True
    try:
        member = await tg("getChatMember", {"chat_id":channel,"user_id":str(user_id)})
        return member.get("status") in {"creator","administrator","member","restricted"}
    except Exception as exc:
        log.warning("force join check failed: %s", exc)
        return False


def join_keyboard() -> dict:
    rows = []
    url=force_join_url()
    if url: rows.append([{"text":"📢 عضویت در کانال","url":url}])
    rows.append([{"text":"✅ عضو شدم · بررسی","callback_data":"joincheck"}])
    return {"inline_keyboard":rows}


async def ensure_joined(user_id: int, chat_id: int) -> bool:
    if await is_joined(user_id): return True
    await send_text(chat_id, "🔒 برای استفاده از بات اول عضو کانال شو، بعد «عضو شدم» رو بزن.", join_keyboard())
    return False


def build_keyboard(result: dict[str, Any], job_id: str) -> dict:
    rows: list[list[dict[str,str]]] = []
    platform = result.get("platform", "generic")
    if platform == "spotify":
        rows.append([{"text":"🎵 دانلود MP3","callback_data":f"sp|{job_id}"}])
        return {"inline_keyboard":rows}
    for idx,item in enumerate(result["media"]):
        title_idx = f" {idx+1}" if len(result["media"]) > 1 else ""
        if item["type"] == "image":
            rows.append([{"text":f"🖼 عکس{title_idx} · HQ","callback_data":f"d|{job_id}|i|{idx}|b"}])
        elif item["type"] == "audio":
            rows.append([
                {"text":f"🎵 MP3 128{title_idx}","callback_data":f"a|{job_id}|{idx}|128"},
                {"text":"🎵 MP3 192","callback_data":f"a|{job_id}|{idx}|192"},
                {"text":"🎵 MP3 320","callback_data":f"a|{job_id}|{idx}|320"},
            ])
        elif item["type"] == "video":
            qs = item.get("qualities") or []
            if qs:
                chunk=[]
                for q in qs[:5]:
                    chunk.append({"text":f"🎬 {q}p","callback_data":f"d|{job_id}|v|{idx}|{q}"})
                    if len(chunk)==3:
                        rows.append(chunk); chunk=[]
                if chunk: rows.append(chunk)
            else:
                rows.append([{"text":f"🎬 ویدیو{title_idx} · Best","callback_data":f"d|{job_id}|v|{idx}|b"}])
            if item.get("has_audio"):
                if platform == "youtube":
                    rows.append([{"text":f"🎧 دانلود صوت{title_idx}","callback_data":f"a|{job_id}|{idx}|128"}])
                else:
                    rows.append([
                        {"text":f"🎧 MP3 128{title_idx}","callback_data":f"a|{job_id}|{idx}|128"},
                        {"text":"🎧 MP3 320","callback_data":f"a|{job_id}|{idx}|320"},
                    ])
    if len(result["media"]) > 1:
        rows.append([{"text":"📥 دانلود همه · Best","callback_data":f"all|{job_id}"}])
    return {"inline_keyboard":rows}


def human_duration(sec: Any) -> str:
    try:
        sec=int(sec or 0)
        if not sec: return ""
        return f"{sec//60}:{sec%60:02d}"
    except Exception: return ""


def result_text(result: dict[str, Any]) -> str:
    platform=result.get("platform","generic")
    icon=PLATFORM_ICONS.get(platform,"🌐")
    label=PLATFORM_LABELS.get(platform,platform)
    media=result.get("media",[])
    counts={t:sum(1 for x in media if x.get("type")==t) for t in ("image","video","audio")}
    lines=[f"{icon} <b>{html.escape(label)} · آماده دانلود</b>",
           f"📝 {html.escape(str(result.get('title') or 'Media'))[:180]}",
           f"👤 {html.escape(str(result.get('owner') or label))}"]
    if platform=="spotify":
        kind=result.get("kind","track").title()
        lines += [f"📦 نوع: <b>{kind}</b>","","🎵 خروجی: Telegram Audio · FastSaver","انتخاب کن 👇"]
        return "\n".join(lines)
    lines.append(f"📦 {len(media)} آیتم · 🖼 {counts['image']} · 🎬 {counts['video']} · 🎵 {counts['audio']}")
    lines.append("")
    for i,item in enumerate(media[:12],1):
        typ=item.get("type")
        if typ=="video":
            q=", ".join(f"{x}p" for x in item.get("qualities") or []) or "Best"
            desc=f"🎬 ویدیو — {q}"
        elif typ=="audio": desc="🎵 صوت — MP3"
        elif typ=="image": desc="🖼 عکس — HQ"
        else: desc="📄 Media"
        dur=human_duration(item.get("duration"))
        title=(item.get("title") or "")[:55]
        suffix=(f" · {html.escape(title)}" if title and len(media)>1 else "") + (f" · {dur}" if dur else "")
        lines.append(f"{i}. {desc}{suffix}")
    if len(media)>12: lines.append(f"… و {len(media)-12} آیتم دیگر")
    lines += ["","کیفیت/فرمت رو انتخاب کن 👇"]
    return "\n".join(lines)


async def download_url(url: str, dest: Path):
    async with httpx.AsyncClient(timeout=180, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"}) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(256*1024): f.write(chunk)


def find_new_file(outdir: Path, before: set[Path], exts: set[str]) -> Path:
    after=[p for p in outdir.rglob("*") if p.is_file() and p not in before and p.suffix.lower() in exts]
    if not after: after=[p for p in outdir.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    if not after: raise RuntimeError("فایل خروجی ساخته نشد.")
    return max(after, key=lambda p:p.stat().st_mtime)


def download_video_sync(source_url: str, playlist_index: int, quality: str, outdir: Path, platform: str) -> Path:
    opts=ydl_options(False, platform)
    opts.update({"outtmpl":str(outdir/"%(playlist_index|0)03d_%(id)s.%(ext)s"),"merge_output_format":"mp4",
                 "restrictfilenames":True,"playlist_items":str(playlist_index)})
    if quality=="b": opts["format"]="bestvideo*+bestaudio/best"
    else:
        h=int(quality); opts["format"]=f"bestvideo*[height<={h}]+bestaudio/best[height<={h}]/best"
    before=set(outdir.rglob("*"))
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.extract_info(source_url, download=True)
    return find_new_file(outdir,before,{".mp4",".mkv",".webm",".mov",".m4v"})


def download_audio_sync(source_url: str, playlist_index: int, bitrate: str, outdir: Path, platform: str) -> Path:
    opts=ydl_options(False, platform)
    opts.update({"outtmpl":str(outdir/"%(playlist_index|0)03d_%(title).120B.%(ext)s"),"restrictfilenames":True,
                 "playlist_items":str(playlist_index),"format":"bestaudio/best",
                 "postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":bitrate}]})
    before=set(outdir.rglob("*"))
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.extract_info(source_url, download=True)
    return find_new_file(outdir,before,{".mp3"})


def download_spotify_sync(source_url: str, outdir: Path) -> list[Path]:
    """Resolve one Spotify track and download matched audio using the hardened YouTube engine."""
    m = SPOTIFY_RE.search(source_url)
    if not m:
        raise RuntimeError("لینک Spotify معتبر نیست.")
    kind = m.group(1).lower()
    if kind != "track":
        raise RuntimeError("فعلاً Spotify Track تکی پشتیبانی می‌شود؛ Album/Playlist هنوز فعال نیست.")

    log.info("spotify-lite: fetching oEmbed metadata")
    meta = spotify_oembed_sync(source_url)
    title = (meta.get("title") or "").strip()
    if not title:
        raise RuntimeError("Spotify metadata پیدا نشد؛ لینک را دوباره بررسی کن.")

    query = f"ytsearch3:{title} official audio"
    last_exc: Exception | None = None
    for attempt_name, base_opts in youtube_attempt_options(False):
        opts = dict(base_opts)
        opts.update({
            "outtmpl": str(outdir/"%(title).140B.%(ext)s"),
            "restrictfilenames": True,
            "format": "bestaudio/best",
            "noplaylist": True,
            "playlistend": 3,
            "postprocessors": [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":SPOTIFY_BITRATE.rstrip("k")}],
        })
        before = set(outdir.rglob("*"))
        try:
            log.info("spotify-lite: YouTube attempt=%s query=%r", attempt_name, title)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
                entries = [e for e in (info or {}).get("entries", []) if e]
                if not entries:
                    raise RuntimeError("هیچ نتیجه‌ای در YouTube برای این ترک پیدا نشد.")
                chosen = entries[0]
                chosen_url = chosen.get("webpage_url") or chosen.get("url")
                if not chosen_url:
                    raise RuntimeError("نتیجه YouTube URL قابل دانلود نداشت.")
                log.info("spotify-lite: attempt=%s matched id=%s title=%r", attempt_name, chosen.get("id"), chosen.get("title"))
                ydl.extract_info(chosen_url, download=True)
            files=[p for p in outdir.rglob("*.mp3") if p.is_file() and p not in before]
            if not files:
                files=[p for p in outdir.rglob("*.mp3") if p.is_file()]
            if files:
                chosen_file=max(files, key=lambda p:p.stat().st_mtime)
                log.info("spotify-lite: mp3 ready via %s: %s", attempt_name, chosen_file.name)
                return [chosen_file]
            raise RuntimeError("YouTube اجرا شد ولی FFmpeg خروجی MP3 نساخت.")
        except Exception as exc:
            last_exc = exc
            log.warning("spotify-lite: attempt=%s failed: %s", attempt_name, exc)

    raise RuntimeError(f"Spotify Lite / YouTube failed after all strategies: {last_exc}")


async def prepare_media(job: dict[str,Any], idx: int, quality: str, mode: str, tmp: Path) -> tuple[Path,str,str]:
    result=job["result"]; item=result["media"][idx]; platform=result.get("platform","generic")
    if item["type"]=="image":
        if not item.get("display_url"): raise RuntimeError("URL عکس پیدا نشد.")
        path=tmp/f"image_{idx+1}.jpg"; await download_url(item["display_url"],path); return path,"image","HQ"
    playlist_index=int(item.get("playlist_index",idx+1))
    if mode=="audio" or item["type"]=="audio":
        path=await asyncio.to_thread(download_audio_sync,job["source_url"],playlist_index,quality,tmp,platform)
        return path,"audio",f"MP3 {quality}k"
    if platform=="youtube":
        path=await fastsaver_youtube_download(job["source_url"],quality,tmp)
        return path,"video",(f"{quality}p" if quality.isdigit() else "720p")
    path=await asyncio.to_thread(download_video_sync,job["source_url"],playlist_index,quality,tmp,platform)
    return path,"video",(f"{quality}p" if quality.isdigit() else "Best")


async def send_file(chat_id:int,path:Path,kind:str,caption:str) -> bool:
    size_mb=path.stat().st_size/(1024*1024)
    if size_mb>MAX_SEND_MB:
        await send_text(chat_id,f"⚠️ فایل <b>{size_mb:.1f} MB</b> است و از سقف {MAX_SEND_MB} MB بیشتره. کیفیت پایین‌تر رو انتخاب کن.")
        return False
    if kind=="image": field,method,mime="photo","sendPhoto","image/jpeg"
    elif kind=="audio": field,method,mime="audio","sendAudio","audio/mpeg"
    else: field,method,mime="video","sendVideo","video/mp4"
    with path.open("rb") as f:
        data={"chat_id":str(chat_id),"caption":caption,"parse_mode":"HTML"}
        if kind=="video": data["supports_streaming"]="true"
        await tg(method,data,{field:(path.name,f,mime)})
    return True


async def send_one(job:dict[str,Any],user_id:int,chat_id:int,idx:int,quality:str,mode:str):
    platform=job["result"].get("platform","generic")
    if platform=="youtube" and mode=="audio":
        try:
            item=job["result"]["media"][idx]
            video_id=str(item.get("id") or "")
            if not video_id:
                raise RuntimeError("YouTube video_id پیدا نشد.")
            await send_text(chat_id,"🎧 دارم نسخه صوتی رو از FastSaver آماده می‌کنم…")
            bot_username=(await ensure_bot_username()).lower()
            cache_key=f"youtube:{video_id}:{bot_username}"
            file_id=await fastsaver_audio_file_id(video_id,cache_key,job["result"].get("title","") or "", "youtube")
            await send_audio_file_id(chat_id,file_id,f"{html.escape(BRAND_NAME)} · YouTube · Audio")
            record_download(user_id,job["job_id"],"audio","FastSaver TG file_id",0,"youtube")
        except Exception as exc:
            log.exception("youtube FastSaver audio failed")
            await send_text(chat_id,"❌ دانلود صوت YouTube ناموفق بود.\n\n<code>"+html.escape(str(exc))[:900]+"</code>")
        return
    tmp=Path(tempfile.mkdtemp(prefix="bluegate_v4_"))
    try:
        await send_text(chat_id,f"⬇️ آیتم <b>{idx+1}</b> در حال آماده‌سازی…")
        path,kind,qlabel=await prepare_media(job,idx,quality,mode,tmp)
        ok=await send_file(chat_id,path,kind,f"{html.escape(BRAND_NAME)} · {PLATFORM_LABELS.get(platform,platform)} · {qlabel}")
        if ok: record_download(user_id,job["job_id"],kind,qlabel,path.stat().st_size,platform)
    except Exception as exc:
        log.exception("send_one failed")
        await send_text(chat_id,"❌ دانلود ناموفق بود.\n\n<code>"+html.escape(str(exc))[:900]+"</code>")
    finally: shutil.rmtree(tmp,ignore_errors=True)


async def send_spotify(job:dict[str,Any],user_id:int,chat_id:int):
    tmp=Path(tempfile.mkdtemp(prefix="bluegate_spotify_"))
    try:
        m=SPOTIFY_RE.search(job["source_url"])
        if not m or m.group(1).lower() != "track":
            raise RuntimeError("فعلاً فقط Spotify Track تکی پشتیبانی می‌شود.")
        await send_text(chat_id,"🟢 Spotify شناسایی شد؛ دارم نسخه متناظر رو پیدا می‌کنم…")
        meta=await asyncio.to_thread(spotify_oembed_sync,job["source_url"])
        title=str(meta.get("title") or job["result"].get("title") or "").strip()
        artist=str(meta.get("author_name") or "").strip()
        if not title: raise RuntimeError("عنوان ترک Spotify پیدا نشد.")
        queries=[]
        base=f"{artist} - {title}" if artist and artist.lower() not in title.lower() else title
        for q in (base+" official audio",base+" topic",base):
            if q not in queries: queries.append(q)
        last=None
        for q in queries:
            try:
                log.info("spotify-resolver: search=%r",q)
                result=await fastsaver_search_music(q)
                vid=str(result["video_id"])
                yt_url=f"https://www.youtube.com/watch?v={vid}"
                # Use the same proven /youtube/download path as normal YouTube, not tg-bot/fetch.
                data=await fastsaver_json("POST","/youtube/download",payload={"url":yt_url,"format":"mp3"},timeout=FASTSAVER_TIMEOUT)
                dl=data.get("download_url")
                if not dl: raise RuntimeError("FastSaver MP3 download_url نداد.")
                fn=re.sub(r"[^A-Za-z0-9._ -]+","_",str(data.get("filename") or f"{title}.mp3"))[:180]
                if not fn.lower().endswith(".mp3"): fn += ".mp3"
                path=tmp/fn
                await download_url(str(dl),path)
                ok=await send_file(chat_id,path,"audio",f"{html.escape(BRAND_NAME)} · Spotify · {html.escape(title)[:120]}")
                if not ok: raise RuntimeError("Telegram فایل Spotify را ارسال نکرد.")
                record_download(user_id,job["job_id"],"audio","FastSaver MP3",path.stat().st_size,"spotify")
                await send_text(chat_id,"✅ Spotify Track ارسال شد.")
                return
            except Exception as exc:
                last=exc; log.warning("spotify-resolver query failed: %s",exc)
        raise RuntimeError(f"Spotify resolver failed: {last}")
    except Exception as exc:
        log_error(user_id,"spotify",exc); log.exception("spotify-fastsaver failed")
        await send_text(chat_id,"❌ دانلود Spotify ناموفق بود.\n\n<code>"+html.escape(str(exc))[:900]+"</code>")
    finally:
        shutil.rmtree(tmp,ignore_errors=True)


async def send_all(job:dict[str,Any],user_id:int,chat_id:int):
    if job["result"].get("platform")=="spotify":
        return await send_spotify(job,user_id,chat_id)
    total=len(job["result"]["media"]); status=await send_text(chat_id,f"📥 دانلود همه شروع شد · <b>{total}</b> آیتم")
    ok_count=0
    for idx,item in enumerate(job["result"]["media"]):
        tmp=Path(tempfile.mkdtemp(prefix="bluegate_all_"))
        try:
            mode="audio" if item.get("type")=="audio" else "video"
            q="128" if mode=="audio" else "b"
            path,kind,qlabel=await prepare_media(job,idx,q,mode,tmp)
            ok=await send_file(chat_id,path,kind,f"{html.escape(BRAND_NAME)} · {idx+1}/{total} · {qlabel}")
            if ok:
                ok_count+=1; record_download(user_id,job["job_id"],kind,qlabel,path.stat().st_size,job["result"].get("platform","generic"))
        except Exception as exc:
            log.warning("download-all item %s failed: %s",idx,exc)
            await send_text(chat_id,f"⚠️ آیتم {idx+1} دانلود نشد: <code>{html.escape(str(exc))[:180]}</code>")
        finally: shutil.rmtree(tmp,ignore_errors=True)
    try: await edit_text(chat_id,status["message_id"],f"✅ دانلود همه تمام شد · <b>{ok_count}/{total}</b> فایل ارسال شد.")
    except Exception: pass


def user_home_keyboard(user_id:int) -> dict:
    rows=[
        [{"text":"📥 دانلود از لینک","callback_data":"home|download"},{"text":"🎵 موزیک","callback_data":"home|music"}],
        [{"text":"📊 حساب من","callback_data":"home|account"},{"text":"🟢 وضعیت سرویس‌ها","callback_data":"home|services"}],
        [{"text":"❓ راهنما","callback_data":"home|help"}],
    ]
    if SUPPORT_USERNAME:
        rows.append([{"text":"🆘 پشتیبانی","url":f"https://t.me/{SUPPORT_USERNAME}"}])
    if user_id in ADMIN_IDS:
        rows.append([{"text":"🛡 ورود به حالت ادمین","callback_data":"mode|admin"}])
    return {"inline_keyboard":rows}


async def send_user_home(chat_id:int,user_id:int,first_name:str=""):
    used=user_downloads_today(user_id); lim=daily_limit(); remaining="نامحدود" if not lim else max(0,lim-used)
    text=(f"👋 {html.escape(first_name) if first_name else ''}\n<b>{html.escape(BRAND_NAME)}</b>\n\n"
          f"📥 دانلود امروز: <b>{used}</b>\n🎟 باقی‌مانده: <b>{remaining}</b>\n\n"
          "لینک رو مستقیم بفرست یا از منوی پایین استفاده کن.")
    await send_text(chat_id,text,user_home_keyboard(user_id))


def admin_keyboard() -> dict:
    return {"inline_keyboard":[
        [{"text":"📊 داشبورد","callback_data":"adm|stats"},{"text":"⚡ FastSaver APIs","callback_data":"adm|apis"}],
        [{"text":"👥 کاربران","callback_data":"adm|users"},{"text":"🚫 Ban/Unban","callback_data":"adm|userfind"}],
        [{"text":"📢 Broadcast","callback_data":"adm|broadcast"},{"text":"🔌 سرویس‌ها","callback_data":"adm|services"}],
        [{"text":"🚦 محدودیت","callback_data":"adm|limit"},{"text":"📢 Force Join","callback_data":"adm|forcejoin"}],
        [{"text":"🛠 Maintenance","callback_data":"adm|maintenance"},{"text":"❌ خطاها","callback_data":"adm|errors"}],
        [{"text":"🩺 سیستم","callback_data":"adm|system"},{"text":"🧹 پاکسازی Jobها","callback_data":"adm|clean"}],
        [{"text":"👤 سوییچ به حالت کاربر","callback_data":"mode|user"}]
    ]}


def services_keyboard() -> dict:
    rows=[]
    for p in ("instagram","youtube","twitter","soundcloud","spotify"):
        on=service_enabled(p); rows.append([{"text":f"{'✅' if on else '❌'} {PLATFORM_LABELS[p]}","callback_data":f"admtoggle|{p}"}])
    rows.append([{"text":"⬅️ برگشت","callback_data":"adm|stats"}]); return {"inline_keyboard":rows}


def api_status_icon(row) -> str:
    if not int(row["enabled"] or 0): return "⏸"
    return {"active":"🟢","rate_limited":"🟠","exhausted":"🔴","invalid":"⛔️"}.get(row["status"],"⚪️")


def api_balance_label(row) -> str:
    try: data=json.loads(row["balance_json"] or "{}")
    except Exception: data={}
    n=_balance_number(data)
    return "?" if n is None else (str(int(n)) if float(n).is_integer() else f"{n:.1f}")


async def send_api_manager(chat_id:int,page:int=0):
    rows=list_fastsaver_keys(True); per=6; pages=max(1,(len(rows)+per-1)//per); page=max(0,min(page,pages-1))
    pool=fastsaver_pool_summary(); strategy=fastsaver_strategy()
    text=("⚡ <b>FastSaver API Pool</b>\n\n"
          f"کل: <b>{pool['total']}</b> · 🟢 {pool['active']} · 🟠 {pool['rate_limited']} · 🔴 {pool['exhausted']} · ⛔️ {pool['invalid']} · ⏸ {pool['disabled']}\n"
          f"🔀 Strategy: <b>{strategy}</b>\n\n"
          "اگر یک Key به Rate Limit/اعتبار برسد، درخواست خودکار با Key بعدی ادامه پیدا می‌کند.")
    kb=[]
    for i,row in enumerate(rows[page*per:(page+1)*per],start=page*per+1):
        kb.append([{"text":f"{api_status_icon(row)} #{i} {mask_api_key(row['key_secret'])} · 💳 {api_balance_label(row)}","callback_data":f"apiinfo|{row['key_id']}"}])
    kb.append([{"text":"➕ Add API","callback_data":"apiadd"},{"text":"🔄 Refresh All","callback_data":"apirefresh"}])
    kb.append([{"text":"🔀 تغییر Strategy","callback_data":"apistrategy"}])
    nav=[]
    if page>0: nav.append({"text":"⬅️","callback_data":f"apipage|{page-1}"})
    nav.append({"text":f"{page+1}/{pages}","callback_data":"noop"})
    if page<pages-1: nav.append({"text":"➡️","callback_data":f"apipage|{page+1}"})
    kb.append(nav); kb.append([{"text":"⬅️ پنل ادمین","callback_data":"adm|stats"}])
    await send_text(chat_id,text,{"inline_keyboard":kb})


async def send_api_info(chat_id:int,key_id:str):
    row=get_fastsaver_key(key_id)
    if not row: return await send_text(chat_id,"❌ API پیدا نشد.")
    cool=max(0,int(row["cooldown_until"] or 0)-now_ts())
    text=(f"⚡ <b>{html.escape(row['label'] or 'FastSaver API')}</b>\n\n"
          f"Key: <code>{html.escape(mask_api_key(row['key_secret']))}</code>\n"
          f"Status: <b>{html.escape(row['status'])}</b> · Enabled: <b>{'YES' if int(row['enabled']) else 'NO'}</b>\n"
          f"Priority: <b>{row['priority']}</b> · Credits: <b>{api_balance_label(row)}</b>\n"
          f"Cooldown: <b>{cool}s</b>\n"
          f"Last error: <code>{html.escape(row['last_error'] or '-')[:350]}</code>")
    kb={"inline_keyboard":[
        [{"text":"💳 Check Balance","callback_data":f"apicheck|{key_id}"},{"text":"⏯ Enable/Disable","callback_data":f"apitoggle|{key_id}"}],
        [{"text":"⬆️ Priority","callback_data":f"apiup|{key_id}"},{"text":"⬇️ Priority","callback_data":f"apidown|{key_id}"}],
        [{"text":"🗑 حذف API","callback_data":f"apidel|{key_id}"}],
        [{"text":"⬅️ API Pool","callback_data":"adm|apis"}]
    ]}
    await send_text(chat_id,text,kb)


async def send_admin_panel(chat_id:int):
    s=stats(); gb=s["bytes"]/(1024**3); pool=fastsaver_pool_summary()
    with db() as conn:
        banned=conn.execute("SELECT COUNT(*) c FROM bans").fetchone()["c"]
        errors24=conn.execute("SELECT COUNT(*) c FROM error_logs WHERE created_at>=?",(now_ts()-86400,)).fetchone()["c"]
    lines=["🛡 <b>BlueGate Admin · V4.2</b>","",f"👥 کاربران: <b>{s['users']}</b> · 🚫 بن: <b>{banned}</b>",f"🟢 فعال ۲۴h: <b>{s['active24']}</b>",
           f"📥 دانلود کل: <b>{s['downloads']}</b> · امروز: <b>{s['downloads24']}</b>",f"❌ خطای ۲۴h: <b>{errors24}</b>",f"💾 حجم: <b>{gb:.2f} GB</b>",
           f"🚦 سقف روزانه: <b>{daily_limit() or 'نامحدود'}</b>",f"🛠 Maintenance: <b>{'ON' if bool_setting('maintenance',False) else 'OFF'}</b>",
           f"⚡ API Pool: <b>{pool['total']}</b> · Active <b>{pool['active']}</b> · Limited <b>{pool['rate_limited']}</b>",f"🗃 DB: <b>{db_backend()}</b>","","📊 <b>پلتفرم‌ها</b>"]
    for p,c in s["platforms"][:8]: lines.append(f"• {PLATFORM_LABELS.get(p,p)}: <b>{c}</b>")
    await send_text(chat_id,"\n".join(lines),admin_keyboard())


async def admin_users(chat_id:int):
    with db() as conn: rows=conn.execute("SELECT user_id,username,first_name,last_seen FROM users ORDER BY last_seen DESC LIMIT 20").fetchall()
    lines=["👥 <b>۲۰ کاربر اخیر</b>",""]+[f"• @{html.escape(r['username']) if r['username'] else html.escape(r['first_name'] or '-') } · <code>{r['user_id']}</code>" for r in rows]
    lines += ["","برای مدیریت یک نفر، Ban/Unban رو بزن و ID یا username رو بفرست."]; await send_text(chat_id,"\n".join(lines))


async def admin_errors(chat_id:int):
    with db() as conn: rows=conn.execute("SELECT id,user_id,platform,error,created_at FROM error_logs ORDER BY id DESC LIMIT 10").fetchall()
    if not rows: return await send_text(chat_id,"✅ هنوز خطایی ثبت نشده.")
    lines=["❌ <b>۱۰ خطای اخیر</b>",""]
    for r in rows: lines.append(f"#{r['id']} · {PLATFORM_LABELS.get(r['platform'],r['platform'])} · <code>{r['user_id'] or '-'}</code>\n<code>{html.escape(r['error'])[:220]}</code>")
    await send_text(chat_id,"\n\n".join(lines))


async def admin_system(chat_id:int):
    du=shutil.disk_usage('/tmp'); up=now_ts()-STARTED_AT; pool=fastsaver_pool_summary()
    text=(f"🩺 <b>System Status</b>\n\n✅ Bot: Online\n⏱ Uptime: <b>{up//3600}h {(up%3600)//60}m</b>\n"
          f"💽 /tmp free: <b>{du.free/(1024**3):.2f} GB</b>\n🗃 DB: <b>{db_backend()}</b>\n"
          f"⚡ FastSaver Pool: <b>{pool['total']}</b> keys / <b>{pool['active']}</b> active\n📦 Version: <b>4.2</b>")
    await send_text(chat_id,text)


async def handle_admin_input(user_id:int,chat_id:int,text:str,state:tuple[str,str],message_id:int|None=None) -> bool:
    action,payload=state
    if action=="apiadd":
        secret=text.strip()
        if message_id:
            try: await tg("deleteMessage",{"chat_id":chat_id,"message_id":message_id})
            except Exception: pass
        status=await send_text(chat_id,"⚡ دارم API Key رو بررسی می‌کنم…")
        try:
            data=await fastsaver_probe_key(secret)
            kid=add_fastsaver_key(secret,f"API {len(list_fastsaver_keys(True))+1}")
            update_fastsaver_key(kid,balance_json=json.dumps(data,ensure_ascii=False),status="active",last_error="")
            await edit_text(chat_id,status["message_id"],f"✅ API اضافه شد.\n<code>{html.escape(mask_api_key(secret))}</code>\n💳 Credits: <b>{_balance_number(data) if _balance_number(data) is not None else '?'}</b>")
        except Exception as exc:
            await edit_text(chat_id,status["message_id"],"❌ API Key تأیید نشد.\n<code>"+html.escape(str(exc))[:600]+"</code>")
        return True
    if action=="broadcast":
        with db() as conn: ids=[r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
        ok=fail=0; await send_text(chat_id,f"📢 ارسال برای {len(ids)} کاربر شروع شد…")
        for uid in ids:
            try: await send_text(uid,text); ok+=1
            except Exception: fail+=1
            await asyncio.sleep(.04)
        await send_text(chat_id,f"✅ Broadcast تمام شد.\nموفق: <b>{ok}</b> · ناموفق: <b>{fail}</b>"); return True
    if action=="userfind":
        q=text.strip().lstrip('@')
        with db() as conn:
            row=conn.execute("SELECT * FROM users WHERE user_id=?",(int(q),)).fetchone() if q.lstrip('-').isdigit() else conn.execute("SELECT * FROM users WHERE lower(username)=lower(?)",(q,)).fetchone()
        if not row: await send_text(chat_id,"❌ کاربر پیدا نشد."); return True
        uid=row['user_id']; banned=is_banned(uid); cnt=user_downloads_today(uid)
        kb={"inline_keyboard":[[{"text":"✅ Unban" if banned else "🚫 Ban","callback_data":f"adminban|{uid}|{0 if banned else 1}"}]]}
        await send_text(chat_id,f"👤 <b>{html.escape(row['first_name'] or '')}</b> @{html.escape(row['username'] or '-')}\nID: <code>{uid}</code>\nدانلود ۲۴h: <b>{cnt}</b>\nوضعیت: <b>{'BANNED' if banned else 'ACTIVE'}</b>",kb); return True
    if action=="limit":
        try: n=max(0,int(text.strip())); set_setting('daily_limit',str(n)); await send_text(chat_id,f"✅ سقف روزانه شد <b>{n or 'نامحدود'}</b>.")
        except: await send_text(chat_id,"❌ فقط عدد بفرست؛ 0 یعنی نامحدود.")
        return True
    if action=="forcejoin":
        parts=[x.strip() for x in text.split('|',1)]; channel=parts[0]; url=parts[1] if len(parts)>1 else ''
        if channel.lower() in {'off','0','خاموش'}: channel=''; url=''
        set_setting('force_join_channel',channel); set_setting('force_join_url',url)
        await send_text(chat_id,f"✅ Force Join {'خاموش شد' if not channel else 'تنظیم شد: <code>'+html.escape(channel)+'</code>'}"); return True
    return False

HELP_TEXT=(
    "لینک یکی از این سرویس‌ها رو بفرست 👇\n\n"
    "📸 <b>Instagram</b> — Post / Reel / Carousel / Story / Highlight\n"
    "▶️ <b>YouTube</b> — Video / Shorts + Audio (FastSaver API)\n"
    "𝕏 <b>X / Twitter</b> — Video / GIF / media\n"
    "☁️ <b>SoundCloud</b> — Track / set + MP3\n"
    "🟢 <b>Spotify</b> — Track → YouTube Music match via FastSaver API Pool\n\n"
    f"📦 حداکثر آیتم Playlist در هر درخواست: <b>{MAX_PLAYLIST_ITEMS}</b>\n"
    "فقط محتوایی رو دانلود کن که اجازه ذخیره/استفاده ازش رو داری."
)


async def handle_message(message:dict[str,Any]):
    chat_id=message["chat"]["id"]; user=message.get("from",{}); user_id=user.get("id",chat_id); upsert_user(user)
    text=message.get("text") or message.get("caption") or ""; msg_id=message.get("message_id")
    if user_id in ADMIN_IDS:
        state=pop_admin_state(user_id)
        if state and not text.startswith("/"):
            if await handle_admin_input(user_id,chat_id,text,state,msg_id): return
    if user_id not in ADMIN_IDS and is_banned(user_id):
        await send_text(chat_id,"⛔️ دسترسی شما به بات مسدود شده."); return
    if user_id not in ADMIN_IDS and bool_setting("maintenance",False):
        await send_text(chat_id,"🛠 بات موقتاً در حال بروزرسانیه. کمی بعد دوباره امتحان کن."); return
    if text.startswith("/admin"):
        if user_id in ADMIN_IDS:
            set_admin_mode(user_id,"admin"); await send_admin_panel(chat_id)
        else: await send_text(chat_id,"⛔️ دسترسی ادمین نداری.")
        return
    if text.startswith("/user"):
        if user_id in ADMIN_IDS: set_admin_mode(user_id,"user")
        if not await ensure_joined(user_id,chat_id): return
        await send_user_home(chat_id,user_id,user.get("first_name","")); return
    if text.startswith("/start"):
        if user_id in ADMIN_IDS and get_admin_mode(user_id)=="admin":
            await send_admin_panel(chat_id); return
        if not await ensure_joined(user_id,chat_id): return
        await send_user_home(chat_id,user_id,user.get("first_name","")); return
    if text.startswith("/help"):
        if not await ensure_joined(user_id,chat_id): return
        await send_text(chat_id,HELP_TEXT,user_home_keyboard(user_id)); return
    if not await ensure_joined(user_id,chat_id): return
    url=clean_url(text)
    if not url:
        await send_user_home(chat_id,user_id,user.get("first_name","")); return
    platform=detect_platform(url)
    if not service_enabled(platform):
        await send_text(chat_id,f"⛔️ سرویس {PLATFORM_LABELS.get(platform,platform)} فعلاً توسط ادمین غیرفعاله."); return
    lim=daily_limit()
    if user_id not in ADMIN_IDS and lim and user_downloads_today(user_id)>=lim:
        await send_text(chat_id,f"🚦 سقف دانلود روزانه‌ات ({lim}) پر شده. فردا دوباره امتحان کن."); return
    if platform=="generic":
        await send_text(chat_id,"❌ این دامنه فعلاً فعال نیست. Instagram / YouTube / X / SoundCloud / Spotify رو بفرست."); return
    status=await send_text(chat_id,f"{PLATFORM_ICONS.get(platform,'🌐')} دارم لینک {PLATFORM_LABELS.get(platform,platform)} رو آنالیز می‌کنم…")
    try:
        result=await analyze(url); job_id=save_job(user_id,chat_id,url,result)
        await edit_text(chat_id,status["message_id"],result_text(result),build_keyboard(result,job_id))
    except Exception as exc:
        log_error(user_id,platform,exc); log.exception("analyze failed")
        hints={"instagram":"اگر محتوا Private/Story باشه ممکنه Cookie لازم باشه.","youtube":"YouTube از FastSaver API Pool استفاده می‌کند؛ وضعیت APIها را در پنل ادمین ببین.",
               "soundcloud":"SoundCloud گاهی extractor را موقتاً محدود می‌کند.","spotify":"Spotify از FastSaver API Pool + YouTube Music استفاده می‌کند؛ فعلاً Track تکی پشتیبانی می‌شود."}
        await edit_text(chat_id,status["message_id"],f"❌ نتونستم لینک رو بخونم.\n\n💡 {hints.get(platform,'')}\n\n<code>{html.escape(str(exc))[:500]}</code>")


async def handle_callback(cb:dict[str,Any]):
    cb_id=cb["id"]; message=cb.get("message") or {}; chat_id=message.get("chat",{}).get("id")
    user=cb.get("from",{}); user_id=user.get("id"); data=cb.get("data",""); upsert_user(user)
    if data=="noop": await safe_answer_callback(cb_id); return
    if data=="joincheck":
        if await is_joined(user_id):
            await safe_answer_callback(cb_id,"عضویت تأیید شد ✅")
            if chat_id: await send_user_home(chat_id,user_id,user.get("first_name",""))
        else: await safe_answer_callback(cb_id,"هنوز عضویت تأیید نشده.",True)
        return
    if data.startswith("mode|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        mode=data.split("|",1)[1]; set_admin_mode(user_id,mode); await safe_answer_callback(cb_id,"حالت تغییر کرد ✅")
        if mode=="admin": await send_admin_panel(chat_id)
        else: await send_user_home(chat_id,user_id,user.get("first_name",""))
        return
    if data.startswith("home|"):
        action=data.split("|",1)[1]; await safe_answer_callback(cb_id)
        if action=="download": await send_text(chat_id,"📥 لینک Instagram / YouTube / X / SoundCloud رو همینجا بفرست. بات خودش نوع لینک رو تشخیص می‌ده.")
        elif action=="music": await send_text(chat_id,"🎵 لینک Spotify Track، SoundCloud یا YouTube رو بفرست. برای YouTube می‌تونی Audio/Video انتخاب کنی.")
        elif action=="account":
            used=user_downloads_today(user_id); lim=daily_limit(); rem="نامحدود" if not lim else max(0,lim-used)
            await send_text(chat_id,f"📊 <b>حساب من</b>\n\n📥 دانلود امروز: <b>{used}</b>\n🎟 باقی‌مانده: <b>{rem}</b>\n🚦 سقف روزانه: <b>{lim or 'نامحدود'}</b>")
        elif action=="services":
            lines=["🟢 <b>وضعیت سرویس‌ها</b>",""]
            for p in ("instagram","youtube","twitter","soundcloud","spotify"): lines.append(f"{'✅' if service_enabled(p) else '❌'} {PLATFORM_LABELS[p]}")
            await send_text(chat_id,"\n".join(lines))
        else: await send_text(chat_id,HELP_TEXT,user_home_keyboard(user_id))
        return
    if data.startswith("adm|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        action=data.split("|",1)[1]; await safe_answer_callback(cb_id)
        if action=="clean":
            with db() as conn: count=conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]; conn.execute("DELETE FROM jobs")
            await send_text(chat_id,f"🧹 <b>{count}</b> Job پاک شد.")
        elif action=="users": await admin_users(chat_id)
        elif action=="apis": await send_api_manager(chat_id)
        elif action=="services": await send_text(chat_id,"🔌 <b>سرویس‌ها</b>\nروی هر سرویس بزن تا روشن/خاموش بشه.",services_keyboard())
        elif action=="broadcast": set_admin_state(user_id,"broadcast"); await send_text(chat_id,"📢 پیام Broadcast رو همین الان بفرست. HTML تلگرام هم قابل استفاده‌ست.")
        elif action=="userfind": set_admin_state(user_id,"userfind"); await send_text(chat_id,"👤 Telegram ID یا username کاربر رو بفرست.")
        elif action=="limit": set_admin_state(user_id,"limit"); await send_text(chat_id,f"🚦 سقف فعلی: <b>{daily_limit() or 'نامحدود'}</b>\nعدد جدید رو بفرست؛ 0 یعنی نامحدود.")
        elif action=="forcejoin": set_admin_state(user_id,"forcejoin"); await send_text(chat_id,"📢 به این فرمت بفرست:\n<code>@Channel | https://t.me/Channel</code>\nبرای خاموش کردن: <code>off</code>")
        elif action=="maintenance":
            new=not bool_setting('maintenance',False); set_setting('maintenance','1' if new else '0'); await send_text(chat_id,f"🛠 Maintenance: <b>{'ON' if new else 'OFF'}</b>")
        elif action=="errors": await admin_errors(chat_id)
        elif action=="system": await admin_system(chat_id)
        else: await send_admin_panel(chat_id)
        return
    if data=="apiadd":
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        await safe_answer_callback(cb_id); set_admin_state(user_id,"apiadd")
        await send_text(chat_id,"➕ FastSaver API Key رو بفرست.\nپیام حاوی Key بعد از دریافت تا حد ممکن از چت پاک می‌شه و Key کامل در پنل نمایش داده نمی‌شه."); return
    if data=="apirefresh":
        if user_id not in ADMIN_IDS: return
        await safe_answer_callback(cb_id,"در حال بررسی…"); ok,fail=await refresh_all_fastsaver_keys(); await send_text(chat_id,f"🔄 Refresh تمام شد · ✅ {ok} · ❌ {fail}"); return
    if data=="apistrategy":
        if user_id not in ADMIN_IDS: return
        order=["sequential","round_robin","most_credits"]; cur=fastsaver_strategy(); new=order[(order.index(cur)+1)%len(order)]; set_setting("fastsaver_strategy",new)
        await safe_answer_callback(cb_id,f"Strategy: {new}"); await send_api_manager(chat_id); return
    if data.startswith("apipage|"):
        if user_id not in ADMIN_IDS: return
        await safe_answer_callback(cb_id); await send_api_manager(chat_id,int(data.split('|')[1])); return
    for prefix in ("apiinfo","apicheck","apitoggle","apidel","apiup","apidown"):
        if data.startswith(prefix+"|"):
            if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
            kid=data.split("|",1)[1]; await safe_answer_callback(cb_id)
            if prefix=="apiinfo": await send_api_info(chat_id,kid)
            elif prefix=="apicheck":
                try:
                    d=await refresh_fastsaver_key(kid); await send_text(chat_id,f"✅ Balance refresh شد.\n💳 Credits: <b>{_balance_number(d) if _balance_number(d) is not None else '?'}</b>")
                except Exception as exc: await send_text(chat_id,"❌ Balance check: <code>"+html.escape(str(exc))[:500]+"</code>")
            elif prefix=="apitoggle": toggle_fastsaver_key(kid); await send_api_info(chat_id,kid)
            elif prefix=="apidel": delete_fastsaver_key(kid); await send_text(chat_id,"🗑 API حذف شد."); await send_api_manager(chat_id)
            elif prefix=="apiup": move_fastsaver_key(kid,-1); await send_api_info(chat_id,kid)
            elif prefix=="apidown": move_fastsaver_key(kid,1); await send_api_info(chat_id,kid)
            return
    if data.startswith("admtoggle|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        p=data.split("|",1)[1]; set_setting(f"service_{p}",'0' if service_enabled(p) else '1'); await safe_answer_callback(cb_id,"تغییر کرد ✅")
        await edit_text(chat_id,message['message_id'],"🔌 <b>سرویس‌ها</b>\nروی هر سرویس بزن تا روشن/خاموش بشه.",services_keyboard()); return
    if data.startswith("adminban|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        _,uid,b=data.split('|'); set_ban(int(uid),b=='1'); await safe_answer_callback(cb_id,"انجام شد ✅"); await send_text(chat_id,f"{'🚫 Ban' if b=='1' else '✅ Unban'}: <code>{uid}</code>"); return
    if user_id not in ADMIN_IDS and is_banned(user_id): await safe_answer_callback(cb_id,"دسترسی مسدود است.",True); return
    if user_id not in ADMIN_IDS and bool_setting("maintenance",False): await safe_answer_callback(cb_id,"بات در حال بروزرسانی است.",True); return
    if not await is_joined(user_id):
        await safe_answer_callback(cb_id,"اول عضو کانال شو.",True)
        if chat_id: await send_text(chat_id,"🔒 اول عضویتت رو تأیید کن.",join_keyboard())
        return
    parts=data.split("|"); await safe_answer_callback(cb_id,"در حال آماده‌سازی…")
    if not chat_id: return
    if parts[0]=="sp" and len(parts)==2:
        job=load_job(parts[1]);
        if not job or job["user_id"]!=user_id: await send_text(chat_id,"⌛️ درخواست منقضی شده؛ لینک رو دوباره بفرست."); return
        await send_spotify(job,user_id,chat_id); return
    if parts[0]=="all" and len(parts)==2:
        job=load_job(parts[1]);
        if not job or job["user_id"]!=user_id: await send_text(chat_id,"⌛️ درخواست منقضی شده؛ لینک رو دوباره بفرست."); return
        await send_all(job,user_id,chat_id); return
    if parts[0]=="a" and len(parts)==4:
        _,job_id,idx_s,bitrate=parts; job=load_job(job_id)
        if not job or job["user_id"]!=user_id: await send_text(chat_id,"⌛️ درخواست منقضی شده؛ لینک رو دوباره بفرست."); return
        if bitrate not in {"128","192","320"}: await send_text(chat_id,"❌ Bitrate نامعتبره."); return
        await send_one(job,user_id,chat_id,int(idx_s),bitrate,"audio"); return
    if parts[0]=="d" and len(parts)==5:
        _,job_id,media_kind,idx_s,quality=parts; job=load_job(job_id)
        if not job or job["user_id"]!=user_id: await send_text(chat_id,"⌛️ درخواست منقضی شده؛ لینک رو دوباره بفرست."); return
        idx=int(idx_s); item=job["result"]["media"][idx]
        if media_kind=="i" and item["type"]!="image": await send_text(chat_id,"❌ انتخاب نامعتبره."); return
        if media_kind=="v" and item["type"]!="video": await send_text(chat_id,"❌ انتخاب نامعتبره."); return
        await send_one(job,user_id,chat_id,idx,quality,"video"); return


@app.on_event("startup")
async def startup():
    init_db()
    if BOT_TOKEN and WEBHOOK_URL:
        try:
            await tg("setWebhook",{"url":f"{WEBHOOK_URL}/telegram/{WEBHOOK_SECRET}","secret_token":WEBHOOK_SECRET,
                                   "allowed_updates":json.dumps(["message","callback_query"]),"drop_pending_updates":"false"})
            log.info("Webhook configured")
            try:
                await ensure_bot_username()
                log.info("FastSaver bot username resolved: %s", FASTSAVER_BOT_USERNAME)
            except Exception as exc:
                log.warning("Could not resolve bot username at startup: %s", exc)
        except Exception: log.exception("Webhook setup failed")


@app.api_route("/", methods=["GET", "HEAD"])
async def root(): return PlainTextResponse(f"{BRAND_NAME} V4.2 is running ✅")


@app.get("/health")
async def health(): return JSONResponse({"ok":True,"version":"4.2","platforms":["instagram","youtube","twitter","soundcloud","spotify"],"youtube_provider":"FastSaverAPI","spotify_provider":"FastSaverAPI"})


@app.post("/telegram/{secret}")
async def telegram_webhook(secret:str,request:Request):
    if secret!=WEBHOOK_SECRET: return JSONResponse({"ok":False},status_code=403)
    header=request.headers.get("x-telegram-bot-api-secret-token")
    if header and header!=WEBHOOK_SECRET: return JSONResponse({"ok":False},status_code=403)
    update=await request.json()
    try:
        if "message" in update: await handle_message(update["message"])
        elif "callback_query" in update: await handle_callback(update["callback_query"])
    except Exception: log.exception("update handling failed")
    return JSONResponse({"ok":True})
