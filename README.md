# BlueGate Downloader V3.3 — YouTube PO Token Edition

V3.3 keeps the V3.2 Telegram downloader and adds an automatic YouTube PO-token provider for Render/datacenter IPs.

## Supported platforms
- Instagram
- YouTube / YouTube Music
- X / Twitter
- SoundCloud
- Spotify Track (resolved through the hardened YouTube engine)

## What changed in V3.3
- `bgutil-ytdlp-pot-provider` 1.3.1 installed as a yt-dlp plugin.
- A local BgUtils provider starts inside the same Render container on `127.0.0.1:4416`.
- YouTube extraction defaults to the `mweb` client with automatic PO tokens.
- YouTube tries three strategies: `mweb+POT+cookies`, `mweb+POT guest`, then legacy cookies.
- Spotify Lite uses the same hardened YouTube engine.
- `httpx` request logging is reduced so Telegram bot tokens are not printed in ordinary INFO logs.
- Render health-check `HEAD /` remains supported.

## Render deployment
Replace the files in the existing GitHub repository with the contents of this folder and deploy the latest commit. Keep your existing environment variables.

Recommended environment variables:

```env
BOT_TOKEN=...
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=...
ADMIN_IDS=...
YOUTUBE_COOKIES_B64=...
YOUTUBE_POT_ENABLED=true
YOUTUBE_POT_BASE_URL=http://127.0.0.1:4416
YOUTUBE_PLAYER_CLIENT=mweb
SPOTIFY_ENABLED=true
SPOTIFY_BITRATE=128k
```

Do not upload `cookies.txt`, bot tokens, or secrets to GitHub.

## Expected startup clues
The Render log should show the BgUtils provider starting before Uvicorn. During a YouTube/Spotify request, application logs use markers such as:

- `youtube-engine: metadata attempt=mweb+pot+cookies`
- `spotify-lite: YouTube attempt=mweb+pot+cookies`
- fallback to `mweb+pot+guest` if necessary

Providing PO tokens improves compatibility with YouTube challenges but cannot guarantee every datacenter IP will be accepted by YouTube.
