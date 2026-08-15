# Instagram → Telegram Downloader Bot

بات تلگرام برای گرفتن لینک Post/Reel اینستاگرام، تشخیص عکس/ویدیو، نمایش کیفیت‌های موجود و ارسال فایل انتخابی در تلگرام.

## امکانات

- دریافت لینک‌های `instagram.com/p/...` و `instagram.com/reel/...`
- پشتیبانی از پست تک‌مدیا و Carousel
- تفکیک 🖼 عکس و 🎬 ویدیو
- نمایش کیفیت‌های ویدیو در صورت ارائه چند rendition توسط extractor
- ارسال عکس با بهترین کیفیت موجود
- دانلود و Merge ویدیو با `yt-dlp + ffmpeg`
- Webhook-based و مناسب Render Free
- بدون دیتابیس و Stateless در مرحله انتخاب کیفیت
- امکان اضافه کردن `cookies.txt` برای موارد Login-only

## ساخت بات در Telegram

1. در تلگرام `@BotFather` را باز کن.
2. دستور `/newbot` را بزن.
3. اسم و username بات را بساز.
4. Token را کپی کن و **هیچ‌وقت عمومی منتشرش نکن**.

## اجرای لوکال با Docker

```bash
docker build -t insta-tg-bot .
docker run --rm -p 10000:10000 \
  -e BOT_TOKEN='TOKEN' \
  -e WEBHOOK_URL='https://YOUR-PUBLIC-HTTPS-URL' \
  -e WEBHOOK_SECRET='a-long-random-secret' \
  insta-tg-bot
```

برای تست Webhook لوکال به یک URL عمومی HTTPS مثل tunnel نیاز داری.

## Deploy رایگان روی Render

### روش ساده

1. این پوشه را داخل یک GitHub repo قرار بده.
2. در Render یک **Web Service** جدید بساز و repo را وصل کن.
3. Render به خاطر وجود `Dockerfile` خودش Docker deploy را تشخیص می‌دهد.
4. Plan را روی **Free** بگذار.
5. Environment Variables:
   - `BOT_TOKEN` = توکن BotFather
   - `WEBHOOK_SECRET` = یک رشته تصادفی طولانی
   - `WEBHOOK_URL` = آدرس نهایی سرویس، مثل `https://insta-telegram-downloader.onrender.com`
   - `MAX_SEND_MB` = `49`
6. Deploy کن. بعد از بالا آمدن، برنامه در startup خودش Webhook را روی Telegram ست می‌کند.
7. داخل تلگرام `/start` بزن و لینک Instagram را بفرست.

> نکته: اگر بار اول `WEBHOOK_URL` را قبل از مشخص شدن آدرس Render تنظیم نکردی، بعد از دیدن URL سرویس مقدارش را وارد کن و یک Deploy/Restart انجام بده.

## Instagram Cookies (اختیاری)

Instagram ممکن است بعضی درخواست‌های بدون Login را محدود کند. برای محتوایی که اجازه دسترسی داری می‌توانی cookies خودت را به فرمت Netscape `cookies.txt` در سرویس قرار بدهی و `COOKIE_FILE=/app/cookies.txt` تنظیم کنی. Cookie حساب اصلی و حساس خودت را روی سرویس عمومی نگذار؛ یک حساب جدا بهتر است.

## محدودیت‌ها

- این ابزار برای محتوایی است که خودت مجاز به دانلودش هستی؛ حقوق سازنده و قوانین Instagram را رعایت کن.
- Private/Login-only بدون Cookie معتبر قابل دریافت نیست.
- Instagram مرتب روش‌های ضدبات را تغییر می‌دهد؛ `yt-dlp` و `Instaloader` باید به‌روز بمانند.
- در نسخه حاضر `MAX_SEND_MB=49` گذاشته شده تا ارسال از Bot API مطمئن‌تر باشد. اگر فایل بزرگ بود کیفیت پایین‌تر انتخاب کن.
- Render Free برای hobby/testing است و سرویس رایگان پس از بی‌کاری sleep می‌شود؛ Webhook باعث می‌شود با درخواست جدید دوباره بیدار شود، ولی اولین پاسخ بعد از sleep ممکن است کمی تأخیر داشته باشد.

## ساختار

- `main.py` — Telegram webhook + Instagram analysis/download
- `Dockerfile` — Python + ffmpeg
- `requirements.txt` — dependencies
- `render.yaml` — Blueprint اختیاری Render
- `.env.example` — نمونه متغیرهای محیطی
