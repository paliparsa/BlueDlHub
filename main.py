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
log = logging.getLogger("bluegate-downloader-v3")

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
app = FastAPI(title="BlueGate Multi Downloader V3")
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


def ydl_options(skip_download: bool = True, platform: str = "generic") -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "skip_download": skip_download,
        "noplaylist": False, "socket_timeout": 30,
        "extract_flat": False,
    }
    cookie = YOUTUBE_COOKIE_FILE if platform == "youtube" and YOUTUBE_COOKIE_FILE else COOKIE_FILE
    if cookie and Path(cookie).exists():
        opts["cookiefile"] = cookie
    return opts


def extract_yt_info(url: str, platform: str = "generic") -> dict[str, Any] | None:
    try:
        with yt_dlp.YoutubeDL(ydl_options(True, platform)) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("yt-dlp metadata failed %s: %s", url, exc)
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


def analyze_spotify(url: str) -> dict[str, Any]:
    if not SPOTIFY_ENABLED:
        raise RuntimeError("Spotify downloader غیرفعال است.")
    m = SPOTIFY_RE.search(url)
    if not m:
        raise ValueError("فعلاً لینک‌های open.spotify.com برای Track / Album / Playlist پشتیبانی می‌شوند.")
    kind = m.group(1).lower()
    title = {"track":"Spotify Track","album":"Spotify Album","playlist":"Spotify Playlist"}[kind]
    return {"platform":"spotify","kind":kind,"url":url,"title":title,"owner":"Spotify metadata",
            "media":[{"type":"spotify","title":title,"playlist_index":1,"qualities":[]}]}


def analyze_sync(url: str) -> dict[str, Any]:
    platform = detect_platform(url)
    if platform == "spotify": return analyze_spotify(url)
    if platform == "instagram":
        kind = instagram_kind(url)
        if kind == "post": return analyze_instagram_post(url)
        if kind in {"story","highlight"}: return analyze_generic_ydl(url, "instagram", kind)
        return analyze_generic_ydl(url, "instagram", "media")
    if platform in {"youtube","twitter","soundcloud"}:
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
    if not FORCE_JOIN_CHANNEL: return True
    try:
        member = await tg("getChatMember", {"chat_id":FORCE_JOIN_CHANNEL,"user_id":str(user_id)})
        return member.get("status") in {"creator","administrator","member","restricted"}
    except Exception as exc:
        log.warning("force join check failed: %s", exc)
        return False


def join_keyboard() -> dict:
    rows = []
    if FORCE_JOIN_URL: rows.append([{"text":"📢 عضویت در کانال","url":FORCE_JOIN_URL}])
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
        lines += [f"📦 نوع: <b>{kind}</b>","",f"🎵 خروجی: MP3 · هدف {html.escape(SPOTIFY_BITRATE)}","انتخاب کن 👇"]
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
    cmd=[sys.executable,"-m","spotdl","download",source_url,"--output",str(outdir/"{artist} - {title}.{output-ext}"),
         "--format","mp3","--bitrate",SPOTIFY_BITRATE]
    cookie = YOUTUBE_COOKIE_FILE or COOKIE_FILE
    if cookie and Path(cookie).exists():
        cmd.extend(["--cookie-file", cookie])
    proc=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
    output = (proc.stdout or "").strip()
    if proc.returncode!=0:
        raise RuntimeError("spotDL/YouTube failed:\n" + output[-1600:])
    files=sorted([p for p in outdir.rglob("*.mp3") if p.is_file()], key=lambda p:p.stat().st_mtime)
    if not files:
        hint = output[-1600:] if output else "No output from spotDL."
        raise RuntimeError("spotDL خروجی صوتی نساخت. علت احتمالی خطای YouTube/YouTube Music است:\n" + hint)
    return files[:MAX_PLAYLIST_ITEMS]


async def prepare_media(job: dict[str,Any], idx: int, quality: str, mode: str, tmp: Path) -> tuple[Path,str,str]:
    result=job["result"]; item=result["media"][idx]; platform=result.get("platform","generic")
    if item["type"]=="image":
        if not item.get("display_url"): raise RuntimeError("URL عکس پیدا نشد.")
        path=tmp/f"image_{idx+1}.jpg"; await download_url(item["display_url"],path); return path,"image","HQ"
    playlist_index=int(item.get("playlist_index",idx+1))
    if mode=="audio" or item["type"]=="audio":
        path=await asyncio.to_thread(download_audio_sync,job["source_url"],playlist_index,quality,tmp,platform)
        return path,"audio",f"MP3 {quality}k"
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
    tmp=Path(tempfile.mkdtemp(prefix="bluegate_v3_"))
    try:
        await send_text(chat_id,f"⬇️ آیتم <b>{idx+1}</b> در حال آماده‌سازی…")
        path,kind,qlabel=await prepare_media(job,idx,quality,mode,tmp)
        platform=job["result"].get("platform","generic")
        ok=await send_file(chat_id,path,kind,f"{html.escape(BRAND_NAME)} · {PLATFORM_LABELS.get(platform,platform)} · {qlabel}")
        if ok: record_download(user_id,job["job_id"],kind,qlabel,path.stat().st_size,platform)
    finally: shutil.rmtree(tmp,ignore_errors=True)


async def send_spotify(job:dict[str,Any],user_id:int,chat_id:int):
    tmp=Path(tempfile.mkdtemp(prefix="bluegate_spotify_"))
    try:
        await send_text(chat_id,"🟢 Spotify شناسایی شد؛ دارم ترک متناظر + متادیتا رو آماده می‌کنم…")
        files=await asyncio.to_thread(download_spotify_sync,job["source_url"],tmp)
        sent=0
        for p in files:
            ok=await send_file(chat_id,p,"audio",f"{html.escape(BRAND_NAME)} · Spotify · MP3")
            if ok:
                sent+=1; record_download(user_id,job["job_id"],"audio",f"MP3 {SPOTIFY_BITRATE}",p.stat().st_size,"spotify")
        if len(files)>1: await send_text(chat_id,f"✅ <b>{sent}</b> فایل Spotify ارسال شد.")
    finally: shutil.rmtree(tmp,ignore_errors=True)


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
    return {"inline_keyboard":[[{"text":"📊 آمار","callback_data":"adm|stats"},{"text":"👥 کاربران","callback_data":"adm|users"}],
                               [{"text":"🧹 پاکسازی Jobها","callback_data":"adm|clean"}]]}


async def send_admin_panel(chat_id:int):
    s=stats(); gb=s["bytes"]/(1024**3)
    lines=["🛠 <b>BlueGate Downloader V3</b>","",f"👥 کاربران: <b>{s['users']}</b>",f"🟢 فعال ۲۴ ساعت: <b>{s['active24']}</b>",
           f"📥 کل دانلودها: <b>{s['downloads']}</b>",f"⚡ دانلود ۲۴ ساعت: <b>{s['downloads24']}</b>",f"💾 حجم ارسال‌شده: <b>{gb:.2f} GB</b>","","📊 <b>پلتفرم‌ها</b>"]
    for p,c in s["platforms"][:8]: lines.append(f"• {PLATFORM_LABELS.get(p,p)}: <b>{c}</b>")
    await send_text(chat_id,"\n".join(lines),admin_keyboard())


HELP_TEXT=(
    "لینک یکی از این سرویس‌ها رو بفرست 👇\n\n"
    "📸 <b>Instagram</b> — Post / Reel / Carousel / Story / Highlight\n"
    "▶️ <b>YouTube</b> — Video / Shorts / Playlist + MP3\n"
    "𝕏 <b>X / Twitter</b> — Video / GIF / media\n"
    "☁️ <b>SoundCloud</b> — Track / set + MP3\n"
    "🟢 <b>Spotify</b> — Track / Album / Playlist → matched audio + metadata\n\n"
    f"📦 حداکثر آیتم Playlist در هر درخواست: <b>{MAX_PLAYLIST_ITEMS}</b>\n"
    "فقط محتوایی رو دانلود کن که اجازه ذخیره/استفاده ازش رو داری."
)


async def handle_message(message:dict[str,Any]):
    chat_id=message["chat"]["id"]; user=message.get("from",{}); user_id=user.get("id",chat_id); upsert_user(user)
    text=message.get("text") or message.get("caption") or ""
    if text.startswith("/start") or text.startswith("/help"):
        if not await ensure_joined(user_id,chat_id): return
        await send_text(chat_id,f"سلام 👋\nبه <b>{html.escape(BRAND_NAME)} V3</b> خوش اومدی.\n\n{HELP_TEXT}"+(f"\n\n🆘 @{html.escape(SUPPORT_USERNAME)}" if SUPPORT_USERNAME else ""))
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
    if platform=="generic":
        await send_text(chat_id,"❌ این دامنه فعلاً در V3 فعال نیست. Instagram / YouTube / X / SoundCloud / Spotify رو بفرست."); return
    status=await send_text(chat_id,f"{PLATFORM_ICONS.get(platform,'🌐')} دارم لینک {PLATFORM_LABELS.get(platform,platform)} رو آنالیز می‌کنم…")
    try:
        result=await analyze(url); job_id=save_job(user_id,chat_id,url,result)
        await edit_text(chat_id,status["message_id"],result_text(result),build_keyboard(result,job_id))
    except Exception as exc:
        log.exception("analyze failed")
        hints={"instagram":"اگر محتوا Private/Story باشه ممکنه Cookie لازم باشه.","youtube":"بعضی ویدیوها Login/Cookie یا محدودیت منطقه‌ای دارند.",
               "soundcloud":"SoundCloud گاهی extractor را موقتاً محدود می‌کند.","spotify":"Spotify/spotDL ممکنه به تغییرات API یا محدودیت منبع صوتی بخوره."}
        await edit_text(chat_id,status["message_id"],f"❌ نتونستم لینک رو بخونم.\n\n💡 {hints.get(platform,'')}\n\n<code>{html.escape(str(exc))[:500]}</code>")


async def handle_callback(cb:dict[str,Any]):
    cb_id=cb["id"]; message=cb.get("message") or {}; chat_id=message.get("chat",{}).get("id")
    user=cb.get("from",{}); user_id=user.get("id"); data=cb.get("data",""); upsert_user(user)
    if data=="joincheck":
        if await is_joined(user_id):
            await tg("answerCallbackQuery",{"callback_query_id":cb_id,"text":"عضویت تأیید شد ✅"})
            if chat_id: await send_text(chat_id,"✅ عضویت تأیید شد. حالا لینک رو بفرست.")
        else: await tg("answerCallbackQuery",{"callback_query_id":cb_id,"text":"هنوز عضویت تأیید نشده.","show_alert":"true"})
        return
    if data.startswith("adm|"):
        if user_id not in ADMIN_IDS:
            await tg("answerCallbackQuery",{"callback_query_id":cb_id,"text":"Access denied","show_alert":"true"}); return
        action=data.split("|",1)[1]; await tg("answerCallbackQuery",{"callback_query_id":cb_id})
        if action=="clean":
            with db() as conn:
                count=conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]; conn.execute("DELETE FROM jobs")
            await send_text(chat_id,f"🧹 <b>{count}</b> Job پاک شد.")
        elif action=="users":
            with db() as conn: rows=conn.execute("SELECT user_id,username,first_name FROM users ORDER BY last_seen DESC LIMIT 15").fetchall()
            lines=["👥 <b>۱۵ کاربر اخیر</b>",""]+[f"• {html.escape(r['username'] or r['first_name'] or str(r['user_id']))} · <code>{r['user_id']}</code>" for r in rows]
            await send_text(chat_id,"\n".join(lines))
        else: await send_admin_panel(chat_id)
        return
    if not await is_joined(user_id):
        await tg("answerCallbackQuery",{"callback_query_id":cb_id,"text":"اول عضو کانال شو.","show_alert":"true"})
        if chat_id: await send_text(chat_id,"🔒 اول عضویتت رو تأیید کن.",join_keyboard())
        return
    parts=data.split("|"); await tg("answerCallbackQuery",{"callback_query_id":cb_id,"text":"در حال آماده‌سازی…"})
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
        except Exception: log.exception("Webhook setup failed")


@app.get("/")
async def root(): return PlainTextResponse(f"{BRAND_NAME} V3 is running ✅")


@app.get("/health")
async def health(): return JSONResponse({"ok":True,"version":3,"platforms":["instagram","youtube","twitter","soundcloud","spotify"]})


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
