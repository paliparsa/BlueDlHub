# BlueGate Downloader V4.3 — Smart UX

Multi-platform Telegram downloader with a user-first interface, two-mode admin UI, persistent Neon/PostgreSQL storage, and a multi-key FastSaver failover pool.

## Providers
- Instagram: direct extractor path
- X / Twitter: direct extractor path
- SoundCloud: direct extractor path
- YouTube: FastSaver API Pool
- Spotify Track: Music Search (2cr) -> Music Download / Telegram file_id (7cr)

## V4.3 user UX
- Smart Home with service health indicators and daily usage
- Platform-aware preview cards with thumbnail, title, owner, duration and available formats
- Contextual buttons for each platform instead of one generic download menu
- Editable progress message during analysis/download/upload; less chat spam
- Recent Downloads: resend Telegram-cached files without using FastSaver credits
- Friendly user-facing errors; technical details stay in admin error logs
- Retry and one-tap Report Problem buttons
- Text Music Search: user can type a song/artist without sending a link
- Account page with today/total downloads and most-used platform
- Home navigation available across user submenus and download screens
- First-use mini onboarding

## Existing V4.2 features retained
- Admin `/start` defaults to Admin Mode
- One-tap Admin Mode <-> User Mode switch
- Unlimited FastSaver API keys managed from Telegram
- Automatic failover on rate limit, exhausted credits, invalid keys and network/provider failures
- Pool strategies: Sequential, Round Robin, Most Credits
- Per-key balance/status/priority/enable/delete controls
- Neon/PostgreSQL persistence
- Legacy `FASTSAVER_API_KEY` can bootstrap the pool

## Persistent V4.3 tables
V4.3 automatically creates these on the existing Neon database; no manual SQL is needed:
- `user_state` — short-lived conversational state, such as Music Search input
- `recent_downloads` — Telegram file_id history for zero-credit resends
- `user_reports` — one-tap problem reports

Existing users/downloads/API keys/settings are not removed.

## Required Render environment variables
- `BOT_TOKEN`
- `WEBHOOK_URL`
- `WEBHOOK_SECRET`
- `ADMIN_IDS`
- `DATABASE_URL` (strongly recommended; Neon pooled connection string)

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

Read `USER_UX_V4_3.md`, `NEON_SETUP.md`, `ADMIN_V4_3.md`, and `FASTSAVER_SETUP.md`.
