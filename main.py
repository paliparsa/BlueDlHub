import asyncio
import html
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
import instaloader
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("insta-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me").strip()
COOKIE_FILE = os.getenv("COOKIE_FILE", "").strip()
MAX_SEND_MB = int(os.getenv("MAX_SEND_MB", "49"))

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI(title="Instagram Telegram Downloader")
loader = instaloader.Instaloader(download_pictures=False, download_videos=False, save_metadata=False, quiet=True)

INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")


def clean_instagram_url(text: str) -> str | None:
    m = INSTAGRAM_RE.search(text or "")
    return m.group(0).split("?")[0] if m else None


def shortcode_from_url(url: str) -> str:
    m = INSTAGRAM_RE.search(url)
    if not m:
        raise ValueError("لینک معتبر پست/ریل اینستاگرام نیست.")
    return m.group(1)


def ydl_options() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "socket_timeout": 20,
    }
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        opts["cookiefile"] = COOKIE_FILE
    return opts


def extract_yt_info(url: str) -> dict[str, Any] | None:
    try:
        with yt_dlp.YoutubeDL(ydl_options()) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("yt-dlp metadata failed: %s", exc)
        return None


def video_entries(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not info:
        return []
    entries = info.get("entries")
    if entries:
        return [e for e in entries if e and (e.get("formats") or e.get("url"))]
    return [info] if (info.get("formats") or info.get("url")) else []


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


def analyze_sync(url: str) -> dict[str, Any]:
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
    yt_videos = video_entries(yt_info)
    vpos = 0
    for item in media:
        if item["type"] == "video":
            entry = yt_videos[vpos] if vpos < len(yt_videos) else None
            item["qualities"] = quality_list(entry)
            item["yt_entry_index"] = vpos
            vpos += 1

    return {
        "url": url,
        "owner": getattr(post.owner_profile, "username", "unknown"),
        "caption": (post.caption or "")[:300],
        "media": media,
    }


async def analyze(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(analyze_sync, url)


async def tg(method: str, data: dict[str, Any] | None = None, files=None):
    async with httpx.AsyncClient(timeout=90) as client:
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
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await tg("sendMessage", data)


def build_keyboard(result: dict[str, Any]) -> dict:
    rows = []
    for idx, item in enumerate(result["media"]):
        if item["type"] == "image":
            rows.append([{"text": f"🖼 عکس {idx+1} · بهترین کیفیت", "callback_data": f"img:{idx}"}])
        else:
            qs = item.get("qualities") or []
            if qs:
                buttons = [{"text": f"🎬 {q}p", "callback_data": f"vid:{idx}:{q}"} for q in qs[:3]]
                rows.append(buttons)
                if len(qs) > 3:
                    rows.append([{"text": f"🎬 {q}p", "callback_data": f"vid:{idx}:{q}"} for q in qs[3:]])
            else:
                rows.append([{"text": f"🎬 ویدیو {idx+1} · بهترین کیفیت", "callback_data": f"vid:{idx}:best"}])
    return {"inline_keyboard": rows}


def result_text(result: dict[str, Any]) -> str:
    image_count = sum(1 for x in result["media"] if x["type"] == "image")
    video_count = sum(1 for x in result["media"] if x["type"] == "video")
    lines = [
        "✅ <b>لینک بررسی شد</b>",
        f"👤 @{html.escape(result['owner'])}",
        f"📦 تعداد مدیا: <b>{len(result['media'])}</b>",
        f"🖼 عکس: <b>{image_count}</b>  |  🎬 ویدیو: <b>{video_count}</b>",
        "",
    ]
    for i, item in enumerate(result["media"], 1):
        if item["type"] == "image":
            lines.append(f"{i}. 🖼 عکس — بهترین کیفیت موجود")
        else:
            qs = item.get("qualities") or []
            qtxt = ", ".join(f"{q}p" for q in qs) if qs else "بهترین کیفیت موجود"
            lines.append(f"{i}. 🎬 ویدیو — {qtxt}")
    lines += ["", "کیفیت/مدیای موردنظرت رو انتخاب کن 👇", "", f"🔗 {html.escape(result['url'])}"]
    return "\n".join(lines)


def url_from_bot_message(message: dict[str, Any]) -> str | None:
    return clean_instagram_url(message.get("text") or message.get("caption") or "")


async def download_image(url: str, dest: Path):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=90, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    f.write(chunk)


def download_video_sync(post_url: str, video_entry_index: int, quality: str, outdir: Path) -> Path:
    opts = ydl_options()
    opts.pop("skip_download", None)
    opts.update({
        "outtmpl": str(outdir / "video.%(ext)s"),
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "playlist_items": str(video_entry_index + 1),
    })
    if quality == "best":
        opts["format"] = "bestvideo+bestaudio/best"
    else:
        h = int(quality)
        opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(post_url, download=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if entries:
            downloaded = [p for p in outdir.glob("video*") if p.is_file()]
            if downloaded:
                return max(downloaded, key=lambda p: p.stat().st_size)
        requested = info.get("requested_downloads") if isinstance(info, dict) else None
        if requested:
            fp = requested[0].get("filepath")
            if fp and Path(fp).exists():
                return Path(fp)

    candidates = [p for p in outdir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not candidates:
        raise RuntimeError("فایل ویدیو ساخته نشد.")
    return max(candidates, key=lambda p: p.stat().st_size)


async def send_file(chat_id: int, path: Path, kind: str, caption: str):
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_SEND_MB:
        await send_text(chat_id, f"⚠️ فایل حدود <b>{size_mb:.1f} MB</b> است و از سقف تنظیم‌شده این بات ({MAX_SEND_MB} MB) بزرگ‌تره. یک کیفیت پایین‌تر انتخاب کن.")
        return

    field = "photo" if kind == "image" else "video"
    method = "sendPhoto" if kind == "image" else "sendVideo"
    mime = "image/jpeg" if kind == "image" else "video/mp4"
    with path.open("rb") as f:
        files = {field: (path.name, f, mime)}
        data = {"chat_id": str(chat_id), "caption": caption}
        await tg(method, data, files)


async def handle_message(message: dict[str, Any]):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    if text.startswith("/start"):
        await send_text(chat_id, "سلام 👋\nلینک یک <b>Post / Reel</b> اینستاگرام رو بفرست. من عکس‌ها و ویدیوها رو جدا می‌کنم و برای ویدیو کیفیت‌های موجود رو نشون می‌دم.")
        return

    url = clean_instagram_url(text)
    if not url:
        await send_text(chat_id, "لطفاً لینک مستقیم پست یا ریل اینستاگرام بفرست؛ مثل:\n<code>https://www.instagram.com/reel/...</code>")
        return

    status = await send_text(chat_id, "⏳ دارم لینک رو بررسی می‌کنم...")
    try:
        result = await analyze(url)
        await tg("editMessageText", {
            "chat_id": str(chat_id),
            "message_id": str(status["message_id"]),
            "text": result_text(result),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(build_keyboard(result), ensure_ascii=False),
        })
    except Exception as exc:
        log.exception("analyze failed")
        await tg("editMessageText", {
            "chat_id": str(chat_id),
            "message_id": str(status["message_id"]),
            "text": "❌ نتونستم این لینک رو بخونم. اگر پست Private/Login-only باشه باید cookies تنظیم بشه؛ بعضی وقت‌ها هم Instagram موقتاً درخواست‌های سرور رو محدود می‌کنه.\n\n" + html.escape(str(exc))[:500],
            "parse_mode": "HTML",
        })


async def handle_callback(cb: dict[str, Any]):
    cb_id = cb["id"]
    message = cb.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    data = cb.get("data", "")
    url = url_from_bot_message(message)
    await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "در حال آماده‌سازی…"})
    if not chat_id or not url:
        return

    parts = data.split(":")
    kind = parts[0]
    idx = int(parts[1])
    quality = parts[2] if len(parts) > 2 else "best"

    tmp = Path(tempfile.mkdtemp(prefix="instabot_"))
    try:
        result = await analyze(url)
        if idx >= len(result["media"]):
            raise RuntimeError("شماره مدیا معتبر نیست.")
        item = result["media"][idx]
        await send_text(chat_id, f"⬇️ دارم مدیای {idx+1} رو آماده می‌کنم...")

        if kind == "img" and item["type"] == "image":
            path = tmp / f"instagram_{idx+1}.jpg"
            await download_image(item["display_url"], path)
            await send_file(chat_id, path, "image", f"Instagram · عکس {idx+1}")
        elif kind == "vid" and item["type"] == "video":
            try:
                path = await asyncio.to_thread(download_video_sync, url, item.get("yt_entry_index", 0), quality, tmp)
            except Exception:
                # Fallback: direct best video URL from Instaloader
                if not item.get("video_url"):
                    raise
                path = tmp / f"instagram_{idx+1}.mp4"
                await download_image(item["video_url"], path)
            qlabel = f"{quality}p" if quality.isdigit() else "Best"
            await send_file(chat_id, path, "video", f"Instagram · ویدیو {idx+1} · {qlabel}")
        else:
            raise RuntimeError("نوع مدیا با انتخاب شما هم‌خوانی ندارد.")
    except Exception as exc:
        log.exception("download failed")
        await send_text(chat_id, "❌ دانلود انجام نشد. ممکنه لینک منقضی شده باشه، Instagram سرور رو محدود کرده باشه یا این محتوا نیاز به Login داشته باشه.\n\n<code>" + html.escape(str(exc))[:500] + "</code>")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.on_event("startup")
async def startup():
    if BOT_TOKEN and WEBHOOK_URL:
        try:
            await tg("setWebhook", {
                "url": f"{WEBHOOK_URL}/telegram/{WEBHOOK_SECRET}",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
            log.info("Webhook configured")
        except Exception:
            log.exception("Webhook setup failed")


@app.get("/")
async def root():
    return PlainTextResponse("Instagram Telegram Bot is running ✅")


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
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
