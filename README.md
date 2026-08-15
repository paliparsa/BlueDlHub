# BlueGate Multi Downloader Bot — V3

A Telegram webhook bot that auto-detects supported links and offers the appropriate media/audio download options.

## Supported platforms

- Instagram — Post, Reel, Carousel, Story, Highlight
- YouTube — Videos, Shorts, playlists; video quality + MP3 extraction
- X / Twitter — video/GIF/media supported by yt-dlp
- SoundCloud — tracks/sets supported by yt-dlp; MP3 output
- Spotify — Track / Album / Playlist through Spotify Lite resolver (Spotify metadata + matched audio source)

## V3 highlights

- Automatic platform detection
- Video quality buttons (when yt-dlp exposes multiple heights)
- MP3 128 / 192 / 320 target encoding for sources that contain audio
- Download All for multi-item yt-dlp results
- Spotify flow separated from normal yt-dlp jobs
- Playlist cap via `MAX_PLAYLIST_ITEMS`
- Platform-aware admin statistics
- Force Join, Job IDs, SQLite analytics, cleanup and upload-size guard retained from V2
- Webhook-first design for Render and similar hosts

## Important Spotify note

Spotify itself is not being decrypted or ripped. Spotify Lite resolver uses Spotify links/metadata and finds a matching audio source (normally YouTube Music), then writes Spotify metadata/artwork to the output. Actual source quality can be lower than an MP3 target bitrate. Spotify Lite resolver's current docs state the normal source ceiling is around 128 kbps unless an eligible YouTube Music Premium setup is used.

## Commands

- `/start` or `/help`
- `/admin`
- `/stats`

## Required environment variables

```env
BOT_TOKEN=123456789:YOUR_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=a-long-random-secret
```

Recommended:

```env
MAX_SEND_MB=49
MAX_PLAYLIST_ITEMS=10
BRAND_NAME=BlueGate Downloader
SUPPORT_USERNAME=BlueGateSupport
ADMIN_IDS=123456789
DB_PATH=/tmp/bluegate_downloader.db
JOB_TTL_HOURS=12
SPOTIFY_ENABLED=true
SPOTIFY_BITRATE=128k
```

Optional cookies:

```env
COOKIE_FILE=/app/cookies.txt
YOUTUBE_COOKIE_FILE=/app/youtube_cookies.txt
```

Never commit live cookies or bot tokens to GitHub.

## Deploy / update from V2 on Render

1. Replace the V2 repository files with the V3 files.
2. Commit/push to the branch connected to Render.
3. Add new env vars `MAX_PLAYLIST_ITEMS`, `SPOTIFY_ENABLED`, `SPOTIFY_BITRATE` if desired.
4. Render rebuilds the Docker image automatically when Auto-Deploy is enabled.
5. Existing `BOT_TOKEN`, webhook URL, admins and Force Join settings can remain unchanged.
6. Test `/health`; it should report version `3`.
7. Send `/start`, then test one link from each platform.

`spotdl` is installed from `requirements.txt`; ffmpeg is supplied by the Dockerfile.

## Resource warning on free hosting

YouTube video merging, MP3 transcoding and Spotify matching can use much more CPU/RAM/network than the Instagram-only bot. Keep `MAX_PLAYLIST_ITEMS` conservative on a free instance. Very large downloads are rejected before Telegram upload based on `MAX_SEND_MB`.

## Legal / usage

Use the bot only for media you have the right or permission to download and reuse. Some services may require account cookies for content you are authorized to access. Platform changes can temporarily break extractors; keeping yt-dlp/Spotify Lite resolver current is important.
