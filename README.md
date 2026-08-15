# BlueGate Downloader V4 — Hybrid API Edition

V4 keeps the working direct extractors for Instagram, X/Twitter and SoundCloud, and moves only YouTube + Spotify to FastSaverAPI.

## Routing

- Instagram -> Instaloader / yt-dlp (direct)
- X / Twitter -> yt-dlp (direct)
- SoundCloud -> yt-dlp (direct)
- YouTube -> FastSaverAPI (`/youtube/info`, `/youtube/download`, `/youtube/audio/tg-bot`)
- Spotify Track -> Spotify oEmbed metadata -> FastSaver YouTube Music search -> FastSaver Telegram `file_id`

Spotify Album/Playlist are intentionally not enabled in V4.0; only individual Track links are supported.

## Render setup

Required Environment variables:

```text
BOT_TOKEN=...
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=some-long-random-value
FASTSAVER_API_KEY=fs_sk_...
```

Optional:

```text
ADMIN_IDS=123456789
BRAND_NAME=BlueGate Downloader
SUPPORT_USERNAME=BlueGateSupport
FASTSAVER_BOT_USERNAME=@YourBotUsername
MAX_SEND_MB=49
```

`FASTSAVER_BOT_USERNAME` is optional. If omitted, the bot calls Telegram `getMe` on startup and resolves it automatically.

## Important V4 behavior

YouTube metadata/quality selection no longer uses yt-dlp, cookies or PO tokens. The bot asks FastSaver `/youtube/info` for the actual formats and then `/youtube/download` for the selected resolution.

YouTube audio and Spotify Track delivery use FastSaver `/youtube/audio/tg-bot`, which returns a Telegram `file_id` scoped to your bot. V4 caches that file_id in SQLite, so repeated sends of the same resolved track do not consume another FastSaver call while the Render database survives.

On Render Free, `/tmp` is ephemeral; cache and statistics reset when the instance is recreated/redeployed.

## Deploy

Put these files directly in the GitHub repository root, commit, and let Render redeploy the Docker service.
