# FastSaver setup for BlueGate V4

1. Create/get a FastSaver API key from the FastSaver dashboard.
2. Open Render -> your BlueGate service -> Environment.
3. Add:

   FASTSAVER_API_KEY=fs_sk_...

4. Keep your existing BOT_TOKEN, WEBHOOK_URL and WEBHOOK_SECRET.
5. `YOUTUBE_COOKIES_B64`, PO-token variables and bgutil are not required by V4 for YouTube/Spotify.
6. Deploy the latest GitHub commit.

Expected /health response includes:

- version: 4.0
- youtube_provider: FastSaverAPI
- spotify_provider: FastSaverAPI

Spotify V4.0 supports individual Spotify Track URLs. YouTube video/Shorts and audio are supported. YouTube playlists are not expanded by FastSaver in this V4 build.
