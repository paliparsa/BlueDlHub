import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
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
log = logging.getLogger("insta-bot-v2")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me").strip()
COOKIE_FILE = os.getenv("COOKIE_FILE", "").strip()
MAX_SEND_MB = int(os.getenv("MAX_SEND_MB", "49"))
DB_PATH = os.getenv("DB_PATH", "/tmp/instabot.db").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "BlueGate Downloader").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "BlueGateSupport").strip().lstrip("@")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()  # @channel or numeric chat id
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
JOB_TTL_HOURS = int(os.getenv("JOB_TTL_HOURS", "12"))

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI(title="Instagram Telegram Downloader V2")
loader = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    save_metadata=False,
    quiet=True,
)

POST_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"https?://(?:www\.)?instagram\.com/stories/[^\s?#]+(?:/\d+)?/?", re.I)
HIGHLIGHT_RE = re.compile(r"https?://(?:www\.)?instagram\.com/stories/highlights/\d+/?", re.I)
INSTAGRAM_ANY_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.I)


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
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                job_id TEXT,
                media_type TEXT,
                quality TEXT,
                bytes INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """
        )
        cutoff = now_ts() - JOB_TTL_HOURS * 3600
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))


def upsert_user(user: dict[str, Any]):
    uid = user.get("id")
    if not uid:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
            """,
            (uid, user.get("username", ""), user.get("first_name", ""), now_ts(), now_ts()),
        )


def save_job(user_id: int, chat_id: int, source_url: str, result: dict[str, Any]) -> str:
    job_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,user_id,chat_id,source_url,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (job_id, user_id, chat_id, source_url, json.dumps(result, ensure_ascii=False), now_ts()),
        )
    return job_id


def load_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    if row["created_at"] < now_ts() - JOB_TTL_HOURS * 3600:
        return None
    return {
        "job_id": row["job_id"],
        "user_id": row["user_id"],
        "chat_id": row["chat_id"],
        "source_url": row["source_url"],
        "result": json.loads(row["result_json"]),
    }


def record_download(user_id: int, job_id: str, media_type: str, quality: str, size: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO downloads(user_id,job_id,media_type,quality,bytes,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, job_id, media_type, quality, size, now_ts()),
        )


def stats() -> dict[str, int]:
    day = now_ts() - 86400
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active24 = conn.execute("SELECT COUNT(*) c FROM users WHERE last_seen>=?", (day,)).fetchone()["c"]
        dl = conn.execute("SELECT COUNT(*) c FROM downloads").fetchone()["c"]
        dl24 = conn.execute("SELECT COUNT(*) c FROM downloads WHERE created_at>=?", (day,)).fetchone()["c"]
        total_bytes = conn.execute("SELECT COALESCE(SUM(bytes),0) c FROM downloads").fetchone()["c"]
    return {"users": users, "active24": active24, "downloads": dl, "downloads24": dl24, "bytes": total_bytes}


def clean_instagram_url(text: str) -> str | None:
    m = INSTAGRAM_ANY_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,)>]\u061f")
    return url.split("?")[0].rstrip("/") + "/"


def source_kind(url: str) -> str:
    if HIGHLIGHT_RE.match(url):
        return "highlight"
    if STORY_RE.match(url):
        return "story"
    if POST_RE.match(url):
        return "post"
    return "unknown"


def shortcode_from_url(url: str) -> str:
    m = POST_RE.search(url)
    if not m:
        raise ValueError("این URL پست یا ریل معتبر اینستاگرام نیست.")
    return m.group(1)


def ydl_options(skip_download: bool = True) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": skip_download,
        "noplaylist": False,
        "socket_timeout": 25,
    }
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        opts["cookiefile"] = COOKIE_FILE
    return opts


def extract_yt_info(url: str) -> dict[str, Any] | None:
    try:
        with yt_dlp.YoutubeDL(ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("yt-dlp metadata failed for %s: %s", url, exc)
        return None


def flatten_entries(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not info:
        return []
    entries = info.get("entries")
    if entries:
        return [e for e in entries if e]
    return [info]


def quality_list(entry: dict[str, Any] | None) -> list[int]:
    if not entry:
        return []
    heights = set()
    for f in entry.get("formats") or []:
        h = f.get("height")
        vcodec = f.get("vcodec")
        if h and vcodec != "none":
            heights.add(int(h))
    return sorted(heights, reverse=True)[:5]


def best_thumbnail(entry: dict[str, Any]) -> str | None:
    thumbs = entry.get("thumbnails") or []
    for t in reversed(thumbs):
        if t.get("url"):
            return t["url"]
    return entry.get("thumbnail")


def analyze_post_sync(url: str) -> dict[str, Any]:
    shortcode = shortcode_from_url(url)
    post = instaloader.Post.from_shortcode(loader.context, shortcode)

    if post.typename == "GraphSidecar":
        raw_nodes = list(post.get_sidecar_nodes())
        media = [
            {
                "type": "video" if n.is_video else "image",
                "display_url": n.display_url,
                "video_url": n.video_url if n.is_video else None,
            }
            for n in raw_nodes
        ]
    else:
        media = [{
            "type": "video" if post.is_video else "image",
            "display_url": post.url,
            "video_url": post.video_url if post.is_video else None,
        }]

    yt_info = extract_yt_info(url)
    yt_entries = flatten_entries(yt_info)
    # Match yt-dlp entries to full carousel positions when possible. Otherwise use video order fallback.
    video_only_entries = [e for e in yt_entries if e.get("formats") or e.get("url")]
    vpos = 0
    for idx, item in enumerate(media):
        if item["type"] != "video":
            continue
        entry = yt_entries[idx] if idx < len(yt_entries) and (yt_entries[idx].get("formats") or yt_entries[idx].get("url")) else None
        if not entry and vpos < len(video_only_entries):
            entry = video_only_entries[vpos]
        item["qualities"] = quality_list(entry)
        item["playlist_index"] = idx + 1
        item["yt_video_order"] = vpos
        vpos += 1

    return {
        "kind": "post",
        "url": url,
        "owner": getattr(post.owner_profile, "username", "unknown"),
        "caption": (post.caption or "")[:300],
        "media": media,
    }


def analyze_story_sync(url: str, kind: str) -> dict[str, Any]:
    info = extract_yt_info(url)
    if not info:
        raise RuntimeError("Story/Highlight قابل خواندن نبود. معمولاً برای این نوع لینک Cookie لاگین لازم است.")
    entries = flatten_entries(info)
    media: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        formats = entry.get("formats") or []
        has_video = any(f.get("vcodec") not in (None, "none") for f in formats)
        ext = (entry.get("ext") or "").lower()
        if formats or has_video or ext in {"mp4", "webm", "mov", "m4v"}:
            media.append({
                "type": "video",
                "display_url": best_thumbnail(entry),
                "video_url": entry.get("url"),
                "qualities": quality_list(entry),
                "playlist_index": i + 1,
                "yt_video_order": i,
            })
        else:
            thumb = best_thumbnail(entry) or entry.get("url")
            if thumb:
                media.append({"type": "image", "display_url": thumb, "video_url": None})

    if not media:
        raise RuntimeError("هیچ مدیای قابل دانلودی از Story/Highlight پیدا نشد.")

    return {
        "kind": kind,
        "url": url,
        "owner": info.get("uploader") or info.get("channel") or "instagram",
        "caption": (info.get("title") or "")[:300],
        "media": media,
    }


def analyze_sync(url: str) -> dict[str, Any]:
    kind = source_kind(url)
    if kind == "post":
        return analyze_post_sync(url)
    if kind in {"story", "highlight"}:
        return analyze_story_sync(url, kind)
    raise ValueError("فعلاً لینک Post / Reel / Story / Highlight اینستاگرام پشتیبانی می‌شود.")


async def analyze(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(analyze_sync, url)


async def tg(method: str, data: dict[str, Any] | None = None, files=None):
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{API}/{method}", data=data, files=files)
        try:
            payload = r.json()
        except Exception:
            raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:500]}")
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload.get("result")


async def send_text(chat_id: int, text: str, reply_markup: dict | None = None):
    data = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await tg("sendMessage", data)


async def edit_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await tg("editMessageText", data)


async def is_joined(user_id: int) -> bool:
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        member = await tg("getChatMember", {"chat_id": FORCE_JOIN_CHANNEL, "user_id": str(user_id)})
        return member.get("status") in {"creator", "administrator", "member", "restricted"}
    except Exception as exc:
        log.warning("force join check failed: %s", exc)
        return False


def join_keyboard() -> dict:
    rows = []
    if FORCE_JOIN_URL:
        rows.append([{"text": "📢 عضویت در کانال", "url": FORCE_JOIN_URL}])
    rows.append([{"text": "✅ عضو شدم · بررسی", "callback_data": "joincheck"}])
    return {"inline_keyboard": rows}


async def ensure_joined(user_id: int, chat_id: int) -> bool:
    if await is_joined(user_id):
        return True
    await send_text(chat_id, "🔒 برای استفاده از بات اول عضو کانال شو، بعد روی «عضو شدم» بزن.", join_keyboard())
    return False


def build_keyboard(result: dict[str, Any], job_id: str) -> dict:
    rows: list[list[dict[str, str]]] = []
    for idx, item in enumerate(result["media"]):
        if item["type"] == "image":
            rows.append([{"text": f"🖼 عکس {idx+1} · HQ", "callback_data": f"d|{job_id}|i|{idx}|b"}])
        else:
            qs = item.get("qualities") or []
            if qs:
                buttons = [{"text": f"🎬 {q}p", "callback_data": f"d|{job_id}|v|{idx}|{q}"} for q in qs[:3]]
                rows.append(buttons)
                if len(qs) > 3:
                    rows.append([{"text": f"🎬 {q}p", "callback_data": f"d|{job_id}|v|{idx}|{q}"} for q in qs[3:5]])
            else:
                rows.append([{"text": f"🎬 ویدیو {idx+1} · Best", "callback_data": f"d|{job_id}|v|{idx}|b"}])
    if len(result["media"]) > 1:
        rows.append([{"text": "📥 دانلود همه · بهترین کیفیت", "callback_data": f"all|{job_id}"}])
    return {"inline_keyboard": rows}


def kind_label(kind: str) -> str:
    return {"post": "Post / Reel", "story": "Story", "highlight": "Highlight"}.get(kind, "Instagram")


def result_text(result: dict[str, Any]) -> str:
    image_count = sum(1 for x in result["media"] if x["type"] == "image")
    video_count = sum(1 for x in result["media"] if x["type"] == "video")
    lines = [
        f"✅ <b>{kind_label(result.get('kind','post'))} بررسی شد</b>",
        f"👤 @{html.escape(str(result.get('owner') or 'instagram'))}",
        f"📦 مدیا: <b>{len(result['media'])}</b> · 🖼 {image_count} · 🎬 {video_count}",
        "",
    ]
    for i, item in enumerate(result["media"], 1):
        if item["type"] == "image":
            lines.append(f"{i}. 🖼 عکس — HQ")
        else:
            qs = item.get("qualities") or []
            qtxt = ", ".join(f"{q}p" for q in qs) if qs else "Best"
            lines.append(f"{i}. 🎬 ویدیو — {qtxt}")
    lines += ["", "انتخاب کن 👇"]
    return "\n".join(lines)


async def download_url(url: str, dest: Path):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    f.write(chunk)


def download_video_sync(source_url: str, playlist_index: int, quality: str, outdir: Path) -> Path:
    opts = ydl_options(skip_download=False)
    opts.update({
        "outtmpl": str(outdir / "media_%(playlist_index|0)03d_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "playlist_items": str(playlist_index),
    })
    if quality == "b":
        opts["format"] = "bestvideo+bestaudio/best"
    else:
        h = int(quality)
        opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"

    before = set(outdir.iterdir())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(source_url, download=True)
    after = [p for p in outdir.iterdir() if p not in before and p.is_file()]
    candidates = [p for p in after if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
    if not candidates:
        candidates = [p for p in outdir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
    if not candidates:
        raise RuntimeError("فایل ویدیو ساخته نشد.")
    return max(candidates, key=lambda p: p.stat().st_mtime)


async def prepare_media(job: dict[str, Any], idx: int, quality: str, tmp: Path) -> tuple[Path, str, str]:
    result = job["result"]
    if idx >= len(result["media"]):
        raise RuntimeError("شماره مدیا معتبر نیست.")
    item = result["media"][idx]
    if item["type"] == "image":
        if not item.get("display_url"):
            raise RuntimeError("URL عکس پیدا نشد.")
        path = tmp / f"instagram_{idx+1}.jpg"
        await download_url(item["display_url"], path)
        return path, "image", "HQ"

    try:
        path = await asyncio.to_thread(
            download_video_sync,
            job["source_url"],
            int(item.get("playlist_index", idx + 1)),
            quality,
            tmp,
        )
    except Exception:
        if not item.get("video_url"):
            raise
        path = tmp / f"instagram_{idx+1}.mp4"
        await download_url(item["video_url"], path)
    return path, "video", (f"{quality}p" if quality.isdigit() else "Best")


async def send_file(chat_id: int, path: Path, kind: str, caption: str) -> bool:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_SEND_MB:
        await send_text(chat_id, f"⚠️ فایل <b>{size_mb:.1f} MB</b> است و از سقف {MAX_SEND_MB} MB این بات بزرگ‌تره. کیفیت پایین‌تر رو انتخاب کن.")
        return False

    field = "photo" if kind == "image" else "video"
    method = "sendPhoto" if kind == "image" else "sendVideo"
    mime = "image/jpeg" if kind == "image" else "video/mp4"
    with path.open("rb") as f:
        files = {field: (path.name, f, mime)}
        data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if kind == "video":
            data["supports_streaming"] = "true"
        await tg(method, data, files)
    return True


async def send_one(job: dict[str, Any], user_id: int, chat_id: int, idx: int, quality: str):
    tmp = Path(tempfile.mkdtemp(prefix="instabot_v2_"))
    try:
        await send_text(chat_id, f"⬇️ مدیای <b>{idx+1}</b> در حال آماده‌سازی…")
        path, kind, qlabel = await prepare_media(job, idx, quality, tmp)
        ok = await send_file(chat_id, path, kind, f"{BRAND_NAME} · {idx+1} · {qlabel}")
        if ok:
            record_download(user_id, job["job_id"], kind, qlabel, path.stat().st_size)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def send_all(job: dict[str, Any], user_id: int, chat_id: int):
    total = len(job["result"]["media"])
    status = await send_text(chat_id, f"📥 دانلود همه شروع شد · <b>{total}</b> مدیا")
    ok_count = 0
    for idx in range(total):
        tmp = Path(tempfile.mkdtemp(prefix="instabot_all_"))
        try:
            path, kind, qlabel = await prepare_media(job, idx, "b", tmp)
            ok = await send_file(chat_id, path, kind, f"{BRAND_NAME} · {idx+1}/{total} · {qlabel}")
            if ok:
                ok_count += 1
                record_download(user_id, job["job_id"], kind, qlabel, path.stat().st_size)
        except Exception as exc:
            log.warning("download-all item %s failed: %s", idx, exc)
            await send_text(chat_id, f"⚠️ آیتم {idx+1} دانلود نشد: <code>{html.escape(str(exc))[:180]}</code>")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        await edit_text(chat_id, status["message_id"], f"✅ دانلود همه تمام شد · <b>{ok_count}/{total}</b> فایل ارسال شد.")
    except Exception:
        pass


def admin_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 آمار", "callback_data": "adm|stats"}, {"text": "👥 کاربران", "callback_data": "adm|users"}],
        [{"text": "🧹 پاکسازی Jobها", "callback_data": "adm|clean"}],
    ]}


async def send_admin_panel(chat_id: int):
    s = stats()
    gb = s["bytes"] / (1024 ** 3)
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 کاربران: <b>{s['users']}</b>\n"
        f"🟢 فعال ۲۴ ساعت: <b>{s['active24']}</b>\n"
        f"📥 کل دانلودها: <b>{s['downloads']}</b>\n"
        f"⚡ دانلود ۲۴ ساعت: <b>{s['downloads24']}</b>\n"
        f"💾 حجم ارسال‌شده: <b>{gb:.2f} GB</b>"
    )
    await send_text(chat_id, text, admin_keyboard())


async def handle_message(message: dict[str, Any]):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id", chat_id)
    upsert_user(user)
    text = message.get("text") or message.get("caption") or ""

    if text.startswith("/start"):
        if not await ensure_joined(user_id, chat_id):
            return
        await send_text(
            chat_id,
            f"سلام 👋\nبه <b>{html.escape(BRAND_NAME)}</b> خوش اومدی.\n\n"
            "لینک <b>Post / Reel / Story / Highlight</b> اینستاگرام رو بفرست. "
            "مدیاها رو جدا می‌کنم، کیفیت‌های ویدیو رو می‌دم و اگر چندتا باشه می‌تونی همه رو یکجا دانلود کنی."
            + (f"\n\n🆘 @{html.escape(SUPPORT_USERNAME)}" if SUPPORT_USERNAME else ""),
        )
        return

    if text.startswith("/admin") or text.startswith("/stats"):
        if user_id in ADMIN_IDS:
            await send_admin_panel(chat_id)
        else:
            await send_text(chat_id, "⛔️ دسترسی ادمین نداری.")
        return

    if not await ensure_joined(user_id, chat_id):
        return

    url = clean_instagram_url(text)
    if not url:
        await send_text(chat_id, "لینک Instagram بفرست 👇\n<code>Post / Reel / Story / Highlight</code>")
        return

    status_msg = await send_text(chat_id, "⏳ دارم لینک رو آنالیز می‌کنم…")
    try:
        result = await analyze(url)
        job_id = save_job(user_id, chat_id, url, result)
        await edit_text(chat_id, status_msg["message_id"], result_text(result), build_keyboard(result, job_id))
    except Exception as exc:
        log.exception("analyze failed")
        extra = "\n\n💡 Story/Highlight معمولاً به cookies.txt لاگین‌شده نیاز دارد." if source_kind(url) in {"story", "highlight"} else ""
        await edit_text(
            chat_id,
            status_msg["message_id"],
            "❌ نتونستم این لینک رو بخونم. ممکنه Instagram درخواست سرور رو محدود کرده باشه یا محتوا Login/Private باشه."
            + extra + "\n\n<code>" + html.escape(str(exc))[:400] + "</code>",
        )


async def handle_callback(cb: dict[str, Any]):
    cb_id = cb["id"]
    message = cb.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    user = cb.get("from", {})
    user_id = user.get("id")
    data = cb.get("data", "")
    upsert_user(user)

    if data == "joincheck":
        if await is_joined(user_id):
            await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "عضویت تأیید شد ✅"})
            if chat_id:
                await send_text(chat_id, "✅ عضویت تأیید شد. حالا لینک Instagram رو بفرست.")
        else:
            await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "هنوز عضویت تأیید نشده.", "show_alert": "true"})
        return

    if data.startswith("adm|"):
        if user_id not in ADMIN_IDS:
            await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Access denied", "show_alert": "true"})
            return
        action = data.split("|", 1)[1]
        await tg("answerCallbackQuery", {"callback_query_id": cb_id})
        if action == "clean":
            with db() as conn:
                count = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
                conn.execute("DELETE FROM jobs")
            await send_text(chat_id, f"🧹 <b>{count}</b> Job پاک شد.")
        elif action == "users":
            with db() as conn:
                rows = conn.execute("SELECT user_id,username,first_name,last_seen FROM users ORDER BY last_seen DESC LIMIT 15").fetchall()
            lines = ["👥 <b>۱۵ کاربر اخیر</b>", ""]
            for r in rows:
                name = html.escape(r["username"] or r["first_name"] or str(r["user_id"]))
                lines.append(f"• {name} · <code>{r['user_id']}</code>")
            await send_text(chat_id, "\n".join(lines))
        else:
            await send_admin_panel(chat_id)
        return

    if not await is_joined(user_id):
        await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "اول عضو کانال شو.", "show_alert": "true"})
        if chat_id:
            await send_text(chat_id, "🔒 اول عضویتت رو تأیید کن.", join_keyboard())
        return

    parts = data.split("|")
    await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "در حال آماده‌سازی…"})
    if not chat_id:
        return

    if parts[0] == "all" and len(parts) == 2:
        job = load_job(parts[1])
        if not job or job["user_id"] != user_id:
            await send_text(chat_id, "⌛️ این درخواست منقضی شده؛ لینک رو دوباره بفرست.")
            return
        await send_all(job, user_id, chat_id)
        return

    if parts[0] == "d" and len(parts) == 5:
        _, job_id, media_kind, idx_s, quality = parts
        job = load_job(job_id)
        if not job or job["user_id"] != user_id:
            await send_text(chat_id, "⌛️ این درخواست منقضی شده؛ لینک رو دوباره بفرست.")
            return
        idx = int(idx_s)
        item = job["result"]["media"][idx]
        if (media_kind == "i" and item["type"] != "image") or (media_kind == "v" and item["type"] != "video"):
            await send_text(chat_id, "❌ انتخاب نامعتبره.")
            return
        await send_one(job, user_id, chat_id, idx, quality)
        return


@app.on_event("startup")
async def startup():
    init_db()
    if BOT_TOKEN and WEBHOOK_URL:
        try:
            await tg("setWebhook", {
                "url": f"{WEBHOOK_URL}/telegram/{WEBHOOK_SECRET}",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": json.dumps(["message", "callback_query"]),
                "drop_pending_updates": "false",
            })
            log.info("Webhook configured")
        except Exception:
            log.exception("Webhook setup failed")


@app.get("/")
async def root():
    return PlainTextResponse(f"{BRAND_NAME} V2 is running ✅")


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "version": 2})


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    telegram_secret = request.headers.get("x-telegram-bot-api-secret-token")
    if telegram_secret and telegram_secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    try:
        if "message" in update:
            await handle_message(update["message"])
        elif "callback_query" in update:
            await handle_callback(update["callback_query"])
    except Exception:
        log.exception("update handling failed")
    return JSONResponse({"ok": True})
