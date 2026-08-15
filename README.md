# BlueGate Instagram Downloader Bot — V2

Telegram bot for downloading Instagram Post/Reel/Carousel and best-effort Story/Highlight media.

## V2 features

- Post / Reel / Carousel analysis
- Separate image/video buttons
- Video quality buttons when multiple heights are exposed
- `Download All` for multi-media posts/highlights
- Story / Highlight URL support through yt-dlp (login cookies are often required)
- Optional Force Join channel gate
- Telegram-native admin panel (`/admin`)
- User and download statistics
- Recent-user view and job cleanup
- Short-lived Job IDs stored in SQLite instead of placing URLs in callback data
- Per-user ownership checks on callback buttons
- Temporary file cleanup after every download
- Configurable Telegram upload cap
- BlueGate branding/support variables
- Webhook mode for Render and similar hosts

## Commands

- `/start` — start/help
- `/admin` — admin panel (only IDs in `ADMIN_IDS`)
- `/stats` — same admin panel shortcut

## BotFather

1. Open `@BotFather`.
2. Run `/newbot`.
3. Copy the token.
4. Never commit the token or cookies to GitHub.

## Environment variables

Required:

```env
BOT_TOKEN=123456789:YOUR_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=a-long-random-secret
```

Recommended:

```env
MAX_SEND_MB=49
BRAND_NAME=BlueGate Downloader
SUPPORT_USERNAME=BlueGateSupport
ADMIN_IDS=123456789
DB_PATH=/tmp/instabot.db
JOB_TTL_HOURS=12
```

Optional Force Join:

```env
FORCE_JOIN_CHANNEL=@YourChannel
FORCE_JOIN_URL=https://t.me/YourChannel
```

For reliable membership checks, add the bot to the channel as an administrator.

Optional Instagram cookies:

```env
COOKIE_FILE=/app/cookies.txt
```

Use a separate Instagram account instead of a sensitive/main account. Cookies can expire and Instagram can rate-limit server IPs.

## Run locally

```bash
docker build -t bluegate-insta-v2 .
docker run --rm -p 10000:10000 \
  -e BOT_TOKEN='TOKEN' \
  -e WEBHOOK_URL='https://YOUR-PUBLIC-HTTPS-URL' \
  -e WEBHOOK_SECRET='LONG_RANDOM_SECRET' \
  -e ADMIN_IDS='YOUR_TELEGRAM_NUMERIC_ID' \
  bluegate-insta-v2
```

A public HTTPS URL is required for Telegram webhooks.

## Deploy on Render

1. Upload this folder to a GitHub repository.
2. Create a new **Web Service** in Render from the repo.
3. Render will detect the `Dockerfile`.
4. Choose the free plan if it is available for your account/region.
5. Add the required environment variables.
6. Deploy.
7. Copy the final Render URL to `WEBHOOK_URL`, then redeploy/restart once.
8. Send `/start` to the bot.

### Important: database persistence on free hosting

The default `DB_PATH=/tmp/instabot.db` is intentionally simple. On hosts with ephemeral filesystems, statistics and users can reset after a restart/redeploy. Downloads themselves do not depend on long-term DB persistence; Job IDs only need to survive for `JOB_TTL_HOURS`.

If you later want permanent analytics/subscriptions/quotas, move the three small tables to a managed Postgres database (for example Supabase/Neon) or use a host with a persistent disk.

## Story / Highlight caveat

The bot recognizes Story and Highlight URLs and sends them through yt-dlp's Instagram Story extractor. Instagram frequently requires a logged-in session for these endpoints, so `cookies.txt` is strongly recommended. Photo-only stories depend on what metadata Instagram exposes to the extractor at that moment.

## Telegram file-size behavior

`MAX_SEND_MB=49` is deliberately conservative for the cloud Bot API. If a selected video is larger, the bot asks the user to choose a lower quality.

## Project files

- `main.py` — webhook, Instagram extraction, downloads, SQLite, admin/Force Join
- `Dockerfile` — Python + ffmpeg runtime
- `requirements.txt` — Python dependencies
- `render.yaml` — optional Render Blueprint
- `.env.example` — configuration template

## Legal / usage note

Use this bot for media you are allowed to download. Private/login-only access should only use an account/session you control and should respect Instagram's terms and creators' rights.
