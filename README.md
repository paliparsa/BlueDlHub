# BlueGate Downloader V4.2

Multi-platform Telegram downloader with a two-mode admin/user UI, persistent PostgreSQL support and a multi-key FastSaver failover pool.

## Providers
- Instagram: direct extractor path
- X / Twitter: direct extractor path
- SoundCloud: direct extractor path
- YouTube: FastSaver API Pool
- Spotify Track: Spotify metadata -> FastSaver YouTube search/download

## V4.2 highlights
- Admin `/start` defaults to Admin Mode
- One-tap Admin Mode <-> User Mode switch
- Button-based User Home and account/service status
- Unlimited FastSaver API keys managed from Telegram
- Automatic fallback on rate limit, exhausted credits, invalid keys and temporary provider errors
- Pool strategies: Sequential, Round Robin, Most Credits
- Per-key balance/status/priority/enable/delete controls
- Neon/PostgreSQL persistence for users, downloads, settings, bans, admin mode and API pool
- Legacy `FASTSAVER_API_KEY` can bootstrap the new pool

## Required Render environment variables
- `BOT_TOKEN`
- `WEBHOOK_URL`
- `WEBHOOK_SECRET`
- `ADMIN_IDS`
- `DATABASE_URL` (recommended; Neon pooled connection string)

Optional:
- `FASTSAVER_API_KEY` (legacy/bootstrap only)
- `FASTSAVER_BASE_URL=https://api.fastsaver.io/v1`
- `FASTSAVER_TIMEOUT=300`
- `MAX_SEND_MB=49`
- `MAX_PLAYLIST_ITEMS=10`
- `BRAND_NAME=BlueGate Downloader`
- `SUPPORT_USERNAME=BlueGateSupport`

## Admin commands
- `/start` -> currently selected admin/user mode
- `/admin` -> Admin Mode
- `/user` -> User Mode

Read `NEON_SETUP.md` and `ADMIN_V4_2.md`.
