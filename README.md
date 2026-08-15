# BlueGate Downloader V4.1

Hybrid Telegram downloader.

## Providers
- Instagram: direct extractor
- X/Twitter: direct extractor
- SoundCloud: direct extractor
- YouTube: FastSaverAPI
- Spotify Track: Spotify metadata -> FastSaver YouTube search -> `/youtube/download` MP3

## V4.1 Admin Panel
Use `/admin` as an ID listed in `ADMIN_IDS`.

Features:
- Dashboard and per-platform stats
- Recent users
- Search user by Telegram ID / username
- Ban / Unban
- Broadcast
- Enable / disable each platform
- Daily per-user download limit (`0` = unlimited)
- Force Join channel and URL from Telegram
- Maintenance mode
- Recent error center
- System status
- Job cleanup

Persistent admin settings live in SQLite (`DB_PATH`). On Render free instances `/tmp` may not be persistent across service replacement/restart, so use a persistent disk/database if you need settings and stats to survive redeploys.

## Required environment
```env
BOT_TOKEN=
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=change-this
ADMIN_IDS=123456789
FASTSAVER_API_KEY=fs_sk_...
```

Optional:
```env
DB_PATH=/tmp/bluegate_downloader.db
BRAND_NAME=BlueGate Downloader
SUPPORT_USERNAME=BlueGateSupport
MAX_SEND_MB=49
MAX_PLAYLIST_ITEMS=10
FASTSAVER_BASE_URL=https://api.fastsaver.io/v1
FASTSAVER_TIMEOUT=300
FORCE_JOIN_CHANNEL=
FORCE_JOIN_URL=
```

## Spotify V4.1 change
V4 used `/youtube/audio/tg-bot` for Spotify. V4.1 deliberately does not use that path for Spotify. It searches for a matching YouTube track and then requests MP3 through the same FastSaver `/youtube/download` engine used by YouTube.

Only Spotify Track URLs are enabled for now.
