# Spotify Music-only path — V4.2.1

Spotify Track downloads deliberately do **not** use FastSaver Video Download.

Normal uncached successful flow:

1. Spotify public metadata (no FastSaver credit)
2. `GET /youtube/search` — **Music Search: 2 credits**
3. `POST /youtube/audio/tg-bot` — **Music Download: 7 credits**
4. Telegram sends the returned `file_id`

Total FastSaver cost: **9 credits per uncached Spotify track**.

If the same matched audio already exists in the bot's local `file_id` cache, step 3 is skipped, so the request can cost only the Music Search call. API-key pool fallback remains enabled for rate-limit / exhausted / invalid keys.

Spotify never calls `/youtube/download` in this version.
