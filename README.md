# BlueGate Downloader V4.4 — Production Queue

Multi-platform Telegram downloader with Smart UX, Neon/PostgreSQL persistence, an unlimited FastSaver API pool, and a production-style download queue.

## Providers
- Instagram: direct extractor path
- X / Twitter: direct extractor path
- SoundCloud: direct extractor path
- YouTube: FastSaver API Pool
- Spotify Track: Music Search (2cr) -> Music Download / Telegram file_id (7cr)

## New in V4.4
- Persistent download queue backed by the same SQLite/Neon database
- Configurable concurrent workers (`MAX_CONCURRENT_JOBS`, default 3)
- User queue view + queue position + cancel button
- Admin live queue dashboard and admin cancel
- Admin requests get higher queue priority
- Deduplication / single-flight: identical active requests share one physical download
- Smart Cache: completed Telegram `file_id` artifacts are reused with zero FastSaver calls
- Intelligent retry for temporary network / timeout / 429 failures
- Anti-abuse controls: queue capacity, active jobs per user, and submit cooldown
- Background FastSaver health checks with automatic recovery of recharged/cooldown keys
- Admin alerts if the FastSaver pool loses all active keys and when it recovers
- Queue/cache metrics in the admin dashboard and `/health`

## V4.3 UX retained
- Smart Home, preview cards, contextual download buttons
- Music Search, Recent Downloads, Account and service status
- Editable progress, friendly errors, Retry and Report Problem
- Admin Mode <-> User Mode switch

## V4.2 API/DB retained
- Unlimited FastSaver keys from Telegram admin panel
- Sequential / Round Robin / Most Credits strategies
- Failover across API keys
- Neon/PostgreSQL persistence
- Ban, Broadcast, Force Join, service toggles, Maintenance and rate limit controls

## Persistent V4.4 tables
Created automatically; no SQL migration is needed:
- `queue_jobs`
- `queue_subscribers`
- `smart_cache`
- `runtime_metrics`

Existing tables and data are preserved.

## Required Render environment variables
- `BOT_TOKEN`
- `WEBHOOK_URL`
- `WEBHOOK_SECRET`
- `ADMIN_IDS`
- `DATABASE_URL` (strongly recommended, using the pooled Neon connection string)

## Optional queue tuning
```env
MAX_CONCURRENT_JOBS=3
MAX_QUEUE_SIZE=100
MAX_ACTIVE_JOBS_PER_USER=3
USER_JOB_COOLDOWN=3
QUEUE_MAX_RETRIES=2
SMART_CACHE_TTL_DAYS=90
FASTSAVER_HEALTH_INTERVAL=600
```

Other optional variables remain compatible with V4.3/V4.2, including `FASTSAVER_API_KEY`, `FASTSAVER_BASE_URL`, `FASTSAVER_TIMEOUT`, `MAX_SEND_MB`, `MAX_PLAYLIST_ITEMS`, `BRAND_NAME`, and `SUPPORT_USERNAME`.

## Admin commands
- `/start` -> selected Admin/User mode
- `/admin` -> Admin Mode
- `/user` -> User Mode

Read `PRODUCTION_QUEUE_V4_4.md`, `USER_UX_V4_4.md`, `NEON_SETUP.md`, and `FASTSAVER_SETUP.md`.
