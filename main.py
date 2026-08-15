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

import httpx
import instaloader
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
# Avoid httpx logging full Telegram Bot API URLs (which contain the bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bluegate-downloader-v4.1")
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
app = FastAPI(title="BlueGate Multi Downloader V4.1 API Edition")
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


@contextmanager
def db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
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
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
        if "platform" not in cols:
            conn.execute("ALTER TABLE downloads ADD COLUMN platform TEXT DEFAULT 'unknown'")
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (now_ts() - JOB_TTL_HOURS * 3600,))


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
        if banned: conn.execute("INSERT OR REPLACE INTO bans(user_id,reason,created_at) VALUES(?,?,?)",(user_id,reason,now_ts()))
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
    with db() as conn: conn.execute("INSERT OR REPLACE INTO admin_state(admin_id,action,payload,updated_at) VALUES(?,?,?,?)",(admin_id,action,payload,now_ts()))


def pop_admin_state(admin_id:int):
    with db() as conn:
        row=conn.execute("SELECT action,payload FROM admin_state WHERE admin_id=?",(admin_id,)).fetchone()
        if row: conn.execute("DELETE FROM admin_state WHERE admin_id=?",(admin_id,))
    return (row["action"],row["payload"]) if row else None


def force_join_channel() -> str:
    return get_setting("force_join_channel", FORCE_JOIN_CHANNEL)


def force_join_url() -> str:
    return get_setting("force_join_url", FORCE_JOIN_URL)

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


def fastsaver_headers() -> dict[str, str]:
    if not FASTSAVER_API_KEY:
        raise RuntimeError("FASTSAVER_API_KEY روی Render تنظیم نشده.")
    return {"X-Api-Key": FASTSAVER_API_KEY, "Accept": "application/json"}


def fastsaver_error(response: httpx.Response, data: Any = None) -> RuntimeError:
    detail = None
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("error") or data.get("message")
    if not detail:
        detail = (response.text or f"HTTP {response.status_code}")[:500]
    return RuntimeError(f"FastSaver HTTP {response.status_code}: {detail}")


def fastsaver_youtube_info_sync(url: str) -> dict[str, Any]:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(f"{FASTSAVER_BASE_URL}/youtube/info", params={"url": url}, headers=fastsaver_headers())
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
            raise fastsaver_error(r, data)
        return data


def analyze_youtube_fastsaver(url: str) -> dict[str, Any]:
    info = fastsaver_youtube_info_sync(url)
    formats = info.get("formats") or []
    qualities: list[int] = []
    size_map: dict[str, int] = {}
    for f in formats:
        if f.get("type") != "video":
            continue
        fmt = str(f.get("format") or "")
        m = re.fullmatch(r"(\d+)p", fmt)
        if not m:
            continue
        q = int(m.group(1))
        qualities.append(q)
        try:
            size_map[str(q)] = int(f.get("filesize") or 0)
        except Exception:
            pass
    qualities = sorted(set(qualities), reverse=True)
    safe_limit = MAX_SEND_MB * 1024 * 1024
    safe_qualities = [q for q in qualities if not size_map.get(str(q)) or size_map.get(str(q), 0) <= safe_limit]
    if safe_qualities:
        qualities = safe_qualities
    return {
        "platform":"youtube", "kind":"video", "url":url,
        "title":str(info.get("title") or "YouTube video")[:180],
        "owner":info.get("author") or "YouTube",
        "thumbnail":info.get("thumbnail") or (info.get("thumbnails") or {}).get("max"),
        "media":[{
            "type":"video", "qualities":qualities[:8], "display_url":info.get("thumbnail"),
            "playlist_index":1, "title":str(info.get("title") or "YouTube video")[:180],
            "has_audio":True, "duration":info.get("duration"), "id":info.get("video_id"),
            "filesizes":size_map,
        }],
        "provider":"fastsaver",
    }


async def fastsaver_json(method: str, path: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout or FASTSAVER_TIMEOUT, follow_redirects=True) as client:
        r = await client.request(method, f"{FASTSAVER_BASE_URL}{path}", params=params, json=payload, headers=fastsaver_headers())
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
            raise fastsaver_error(r, data)
        return data


async def fastsaver_youtube_download(url: str, quality: str, outdir: Path) -> Path:
    fmt = f"{quality}p" if quality.isdigit() else "720p"
    data = await fastsaver_json("POST", "/youtube/download", payload={"url":url, "format":fmt}, timeout=FASTSAVER_TIMEOUT)
    dl_url = data.get("download_url")
    if not dl_url:
        raise RuntimeError("FastSaver لینک دانلود برنگرداند.")
    filename = str(data.get("filename") or f"youtube_{data.get('video_id','video')}_{fmt}.mp4")
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")[:180] or "youtube.mp4"
    if not Path(filename).suffix:
        filename += ".mp4"
    dest = outdir / filename
    await download_url(str(dl_url), dest)
    return dest


async def fastsaver_search_music(query: str) -> dict[str, Any]:
    data = await fastsaver_json("GET", "/youtube/search", params={"query":query, "page":1}, timeout=90)
    results = [x for x in (data.get("results") or []) if isinstance(x, dict) and x.get("video_id")]
    if not results:
        raise RuntimeError("FastSaver/YouTube Music نتیجه‌ای برای این آهنگ پیدا نکرد.")
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


def admin_keyboard() -> dict:
    return {"inline_keyboard":[
        [{"text":"📊 داشبورد","callback_data":"adm|stats"},{"text":"👥 کاربران","callback_data":"adm|users"}],
        [{"text":"📢 Broadcast","callback_data":"adm|broadcast"},{"text":"🚫 Ban/Unban","callback_data":"adm|userfind"}],
        [{"text":"🔌 سرویس‌ها","callback_data":"adm|services"},{"text":"🚦 محدودیت","callback_data":"adm|limit"}],
        [{"text":"📢 Force Join","callback_data":"adm|forcejoin"},{"text":"🛠 Maintenance","callback_data":"adm|maintenance"}],
        [{"text":"❌ خطاها","callback_data":"adm|errors"},{"text":"🩺 سیستم","callback_data":"adm|system"}],
        [{"text":"🧹 پاکسازی Jobها","callback_data":"adm|clean"}]
    ]}


def services_keyboard() -> dict:
    rows=[]
    for p in ("instagram","youtube","twitter","soundcloud","spotify"):
        on=service_enabled(p); rows.append([{"text":f"{'✅' if on else '❌'} {PLATFORM_LABELS[p]}","callback_data":f"admtoggle|{p}"}])
    rows.append([{"text":"⬅️ برگشت","callback_data":"adm|stats"}])
    return {"inline_keyboard":rows}


async def send_admin_panel(chat_id:int):
    s=stats(); gb=s["bytes"]/(1024**3)
    with db() as conn:
        banned=conn.execute("SELECT COUNT(*) c FROM bans").fetchone()["c"]
        errors24=conn.execute("SELECT COUNT(*) c FROM error_logs WHERE created_at>=?",(now_ts()-86400,)).fetchone()["c"]
    lines=["🛡 <b>BlueGate Admin · V4.1</b>","",f"👥 کاربران: <b>{s['users']}</b> · 🚫 بن: <b>{banned}</b>",f"🟢 فعال ۲۴h: <b>{s['active24']}</b>",f"📥 دانلود کل: <b>{s['downloads']}</b> · امروز: <b>{s['downloads24']}</b>",f"❌ خطای ۲۴h: <b>{errors24}</b>",f"💾 حجم: <b>{gb:.2f} GB</b>",f"🚦 سقف روزانه: <b>{daily_limit() or 'نامحدود'}</b>",f"🛠 Maintenance: <b>{'ON' if bool_setting('maintenance',False) else 'OFF'}</b>","","📊 <b>پلتفرم‌ها</b>"]
    for p,c in s["platforms"][:8]: lines.append(f"• {PLATFORM_LABELS.get(p,p)}: <b>{c}</b>")
    await send_text(chat_id,"\n".join(lines),admin_keyboard())


async def admin_users(chat_id:int):
    with db() as conn: rows=conn.execute("SELECT user_id,username,first_name,last_seen FROM users ORDER BY last_seen DESC LIMIT 20").fetchall()
    lines=["👥 <b>۲۰ کاربر اخیر</b>",""]+[f"• @{html.escape(r['username']) if r['username'] else html.escape(r['first_name'] or '-') } · <code>{r['user_id']}</code>" for r in rows]
    lines += ["","برای مدیریت یک نفر، دکمه Ban/Unban رو بزن و ID یا username رو بفرست."]
    await send_text(chat_id,"\n".join(lines))


async def admin_errors(chat_id:int):
    with db() as conn: rows=conn.execute("SELECT id,user_id,platform,error,created_at FROM error_logs ORDER BY id DESC LIMIT 10").fetchall()
    if not rows: return await send_text(chat_id,"✅ هنوز خطایی ثبت نشده.")
    lines=["❌ <b>۱۰ خطای اخیر</b>",""]
    for r in rows: lines.append(f"#{r['id']} · {PLATFORM_LABELS.get(r['platform'],r['platform'])} · <code>{r['user_id'] or '-'}</code>\n<code>{html.escape(r['error'])[:220]}</code>")
    await send_text(chat_id,"\n\n".join(lines))


async def admin_system(chat_id:int):
    du=shutil.disk_usage('/tmp'); up=now_ts()-STARTED_AT
    text=(f"🩺 <b>System Status</b>\n\n✅ Bot: Online\n⏱ Uptime: <b>{up//3600}h {(up%3600)//60}m</b>\n"
          f"💽 /tmp free: <b>{du.free/(1024**3):.2f} GB</b>\n🗃 DB: <code>{html.escape(DB_PATH)}</code>\n"
          f"⚡ FastSaver: <b>{'Configured' if FASTSAVER_API_KEY else 'Missing API key'}</b>\n📦 Version: <b>4.1</b>")
    await send_text(chat_id,text)


async def handle_admin_input(user_id:int,chat_id:int,text:str,state:tuple[str,str]) -> bool:
    action,payload=state
    if action=="broadcast":
        with db() as conn: ids=[r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]
        ok=fail=0; status=await send_text(chat_id,f"📢 ارسال برای {len(ids)} کاربر شروع شد…")
        for uid in ids:
            try: await send_text(uid,text); ok+=1
            except Exception: fail+=1
            await asyncio.sleep(.04)
        await send_text(chat_id,f"✅ Broadcast تمام شد.\nموفق: <b>{ok}</b> · ناموفق: <b>{fail}</b>")
        return True
    if action=="userfind":
        q=text.strip().lstrip('@')
        with db() as conn:
            row=conn.execute("SELECT * FROM users WHERE user_id=?",(int(q) if q.lstrip('-').isdigit() else -999999,)).fetchone() if q.lstrip('-').isdigit() else conn.execute("SELECT * FROM users WHERE lower(username)=lower(?)",(q,)).fetchone()
        if not row: await send_text(chat_id,"❌ کاربر پیدا نشد."); return True
        uid=row['user_id']; banned=is_banned(uid); cnt=user_downloads_today(uid)
        kb={"inline_keyboard":[[{"text":"✅ Unban" if banned else "🚫 Ban","callback_data":f"adminban|{uid}|{0 if banned else 1}"}]]}
        await send_text(chat_id,f"👤 <b>{html.escape(row['first_name'] or '')}</b> @{html.escape(row['username'] or '-')}\nID: <code>{uid}</code>\nدانلود ۲۴h: <b>{cnt}</b>\nوضعیت: <b>{'BANNED' if banned else 'ACTIVE'}</b>",kb); return True
    if action=="limit":
        try: n=max(0,int(text.strip())); set_setting('daily_limit',str(n)); await send_text(chat_id,f"✅ سقف روزانه شد <b>{n or 'نامحدود'}</b>.")
        except: await send_text(chat_id,"❌ فقط عدد بفرست؛ 0 یعنی نامحدود.")
        return True
    if action=="forcejoin":
        parts=[x.strip() for x in text.split('|',1)]; channel=parts[0]
        url=parts[1] if len(parts)>1 else ''
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
    "🟢 <b>Spotify</b> — Track → YouTube Music match + Telegram audio\n\n"
    f"📦 حداکثر آیتم Playlist در هر درخواست: <b>{MAX_PLAYLIST_ITEMS}</b>\n"
    "فقط محتوایی رو دانلود کن که اجازه ذخیره/استفاده ازش رو داری."
)


async def handle_message(message:dict[str,Any]):
    chat_id=message["chat"]["id"]; user=message.get("from",{}); user_id=user.get("id",chat_id); upsert_user(user)
    text=message.get("text") or message.get("caption") or ""
    if user_id in ADMIN_IDS:
        state=pop_admin_state(user_id)
        if state and not text.startswith("/"):
            if await handle_admin_input(user_id,chat_id,text,state): return
    if user_id not in ADMIN_IDS and is_banned(user_id):
        await send_text(chat_id,"⛔️ دسترسی شما به بات مسدود شده."); return
    if user_id not in ADMIN_IDS and bool_setting("maintenance",False):
        await send_text(chat_id,"🛠 بات موقتاً در حال بروزرسانیه. کمی بعد دوباره امتحان کن."); return
    if text.startswith("/start") or text.startswith("/help"):
        if not await ensure_joined(user_id,chat_id): return
        await send_text(chat_id,f"سلام 👋\nبه <b>{html.escape(BRAND_NAME)} V4</b> خوش اومدی.\n\n{HELP_TEXT}"+(f"\n\n🆘 @{html.escape(SUPPORT_USERNAME)}" if SUPPORT_USERNAME else ""))
        return
    if text.startswith("/admin") or text.startswith("/stats"):
        if user_id in ADMIN_IDS: await send_admin_panel(chat_id)
        else: await send_text(chat_id,"⛔️ دسترسی ادمین نداری.")
        return
    if not await ensure_joined(user_id,chat_id): return
    url=clean_url(text)
    if not url:
        await send_text(chat_id,HELP_TEXT); return
    platform=detect_platform(url)
    if not service_enabled(platform):
        await send_text(chat_id,f"⛔️ سرویس {PLATFORM_LABELS.get(platform,platform)} فعلاً توسط ادمین غیرفعاله."); return
    lim=daily_limit()
    if user_id not in ADMIN_IDS and lim and user_downloads_today(user_id)>=lim:
        await send_text(chat_id,f"🚦 سقف دانلود روزانه‌ات ({lim}) پر شده. فردا دوباره امتحان کن."); return
    if platform=="generic":
        await send_text(chat_id,"❌ این دامنه فعلاً در V4 فعال نیست. Instagram / YouTube / X / SoundCloud / Spotify رو بفرست."); return
    status=await send_text(chat_id,f"{PLATFORM_ICONS.get(platform,'🌐')} دارم لینک {PLATFORM_LABELS.get(platform,platform)} رو آنالیز می‌کنم…")
    try:
        result=await analyze(url); job_id=save_job(user_id,chat_id,url,result)
        await edit_text(chat_id,status["message_id"],result_text(result),build_keyboard(result,job_id))
    except Exception as exc:
        log_error(user_id,platform,exc)
        log.exception("analyze failed")
        hints={"instagram":"اگر محتوا Private/Story باشه ممکنه Cookie لازم باشه.","youtube":"YouTube در V4 از FastSaver API استفاده می‌کند؛ API key و اعتبار حساب را بررسی کن.",
               "soundcloud":"SoundCloud گاهی extractor را موقتاً محدود می‌کند.","spotify":"Spotify در V4 از FastSaver + YouTube Music استفاده می‌کند؛ فعلاً Track تکی پشتیبانی می‌شود."}
        await edit_text(chat_id,status["message_id"],f"❌ نتونستم لینک رو بخونم.\n\n💡 {hints.get(platform,'')}\n\n<code>{html.escape(str(exc))[:500]}</code>")


async def handle_callback(cb:dict[str,Any]):
    cb_id=cb["id"]; message=cb.get("message") or {}; chat_id=message.get("chat",{}).get("id")
    user=cb.get("from",{}); user_id=user.get("id"); data=cb.get("data",""); upsert_user(user)
    if data=="joincheck":
        if await is_joined(user_id):
            await safe_answer_callback(cb_id,"عضویت تأیید شد ✅")
            if chat_id: await send_text(chat_id,"✅ عضویت تأیید شد. حالا لینک رو بفرست.")
        else: await safe_answer_callback(cb_id,"هنوز عضویت تأیید نشده.",True)
        return
    if data.startswith("adm|"):
        if user_id not in ADMIN_IDS:
            await safe_answer_callback(cb_id,"Access denied",True); return
        action=data.split("|",1)[1]; await safe_answer_callback(cb_id)
        if action=="clean":
            with db() as conn: count=conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]; conn.execute("DELETE FROM jobs")
            await send_text(chat_id,f"🧹 <b>{count}</b> Job پاک شد.")
        elif action=="users": await admin_users(chat_id)
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
    if data.startswith("admtoggle|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        p=data.split("|",1)[1]; set_setting(f"service_{p}",'0' if service_enabled(p) else '1'); await safe_answer_callback(cb_id,"تغییر کرد ✅")
        await edit_text(chat_id,message['message_id'],"🔌 <b>سرویس‌ها</b>\nروی هر سرویس بزن تا روشن/خاموش بشه.",services_keyboard()); return
    if data.startswith("adminban|"):
        if user_id not in ADMIN_IDS: await safe_answer_callback(cb_id,"Access denied",True); return
        _,uid,b=data.split('|'); set_ban(int(uid),b=='1'); await safe_answer_callback(cb_id,"انجام شد ✅"); await send_text(chat_id,f"{'🚫 Ban' if b=='1' else '✅ Unban'}: <code>{uid}</code>"); return
    if user_id not in ADMIN_IDS and is_banned(user_id):
        await safe_answer_callback(cb_id,"دسترسی مسدود است.",True); return
    if user_id not in ADMIN_IDS and bool_setting("maintenance",False):
        await safe_answer_callback(cb_id,"بات در حال بروزرسانی است.",True); return
    if not await is_joined(user_id):
        await safe_answer_callback(cb_id,"اول عضو کانال شو.",True)
        if chat_id: await send_text(chat_id,"🔒 اول عضویتت رو تأیید کن.",join_keyboard())
        return
    parts=data.split("|"); await safe_answer_callback(cb_id,"در حال آماده‌سازی…")
    if not chat_id: return
    if parts[0]=="sp" and len(parts)==2:
        job=load_job(parts[1])
        if not job or job["user_id"]!=user_id: await send_text(chat_id,"⌛️ درخواست منقضی شده؛ لینک رو دوباره بفرست."); return
        await send_spotify(job,user_id,chat_id); return
    if parts[0]=="all" and len(parts)==2:
        job=load_job(parts[1])
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
async def root(): return PlainTextResponse(f"{BRAND_NAME} V4 is running ✅")


@app.get("/health")
async def health(): return JSONResponse({"ok":True,"version":"4.1","platforms":["instagram","youtube","twitter","soundcloud","spotify"],"youtube_provider":"FastSaverAPI","spotify_provider":"FastSaverAPI"})


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
