# BlueGate Downloader

Telegram media downloader with persistent PostgreSQL storage, queueing, cache, memberships, and a multi-key FastSaver pool.

## Supported sources

- Instagram — posts, Reels, photo posts, carousels, Stories and Highlights
- TikTok — videos, photo posts/slideshows, plus the post audio when available
- YouTube — video and audio
- X / Twitter — direct extractor first, FastSaver fallback if direct extraction fails
- SoundCloud — direct audio extraction
- Spotify — music search + audio download

Only publicly reachable media is supported for Instagram, TikTok and X.

## Social download routing

- Instagram: FastSaver `/fetch`
- TikTok: FastSaver `/fetch`
- X / Twitter: direct `yt-dlp` first; FastSaver `/fetch` only as fallback
- SoundCloud: direct `yt-dlp`
- YouTube: FastSaver YouTube endpoints
- Spotify: FastSaver Music Search + Music Download

Instagram no longer needs an Instagram login, session file, cookie export or Instaloader.

FastSaver `/fetch` currently costs 1.5 credits for normal Instagram posts/Reels/carousels, TikTok posts and X posts. Instagram Stories/Highlights cost 5 credits. Signed media URLs are downloaded immediately; if one expires while waiting in queue, the bot resolves it once more.

## Core features

- Separate User/Admin modes
- Free / VIP / Premium plans
- Redeem codes and referral rewards
- Priority download queue
- Duplicate-job detection and shared delivery
- Telegram `file_id` smart cache
- Recent downloads, favorites and history search
- Batch link processing
- Quick Download preferences
- Multiple FastSaver API keys with automatic fallback
- FastSaver health monitoring
- Per-user limits, bans and overrides
- Campaigns, analytics and audit log
- Force Join, broadcast and maintenance mode
- Persian / English user interface
- Persistent PostgreSQL storage via Neon

## Required Render environment variables

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=CHANGE_TO_A_RANDOM_SECRET
ADMIN_IDS=YOUR_TELEGRAM_USER_ID
DATABASE_URL=YOUR_NEON_POSTGRES_CONNECTION_STRING
```

Optional bootstrap FastSaver key:

```env
FASTSAVER_API_KEY=fs_sk_xxxxxxxxx
```

Additional FastSaver keys are managed from the Telegram admin panel:

```text
/start → Admin Mode → FastSaver APIs
```

## Optional settings

```env
FASTSAVER_BASE_URL=https://api.fastsaver.io/v1
FASTSAVER_TIMEOUT=300
BRAND_NAME=BlueGate Downloader
SUPPORT_USERNAME=BlueGateSupport
MAX_SEND_MB=49
MAX_PLAYLIST_ITEMS=10
FORCE_JOIN_CHANNEL=
FORCE_JOIN_URL=
SPOTIFY_ENABLED=true
MAX_CONCURRENT_JOBS=3
MAX_QUEUE_SIZE=100
MAX_ACTIVE_JOBS_PER_USER=3
USER_JOB_COOLDOWN=3
QUEUE_MAX_RETRIES=2
SMART_CACHE_TTL_DAYS=90
FASTSAVER_HEALTH_INTERVAL=600
BATCH_MAX_LINKS=25
```

## Neon

Create a Neon PostgreSQL project, copy its pooled connection string, and save it in Render as `DATABASE_URL`. Tables and migrations are created automatically at startup; no manual SQL setup is required.

If `DATABASE_URL` is missing, the app falls back to local SQLite. Local SQLite on a free Render instance should not be treated as persistent storage.

## Deploy on Render

1. Put all files from this project in the root of the GitHub repository.
2. Connect the repository to a Render Web Service.
3. Use the included Dockerfile.
4. Add the required environment variables.
5. Deploy.
6. Open Telegram and use `/start`.

Repository root:

```text
main.py
requirements.txt
Dockerfile
start.sh
render.yaml
.env.example
.gitignore
README.md
```

## Updating

Replace the project files in the repository root and commit. Render can automatically deploy the new commit. Keep secrets only in Render/Neon, never in GitHub.
