# FastSaver Setup — V4.2 API Pool

V4.2 does not require a single `FASTSAVER_API_KEY` environment variable.

Recommended flow:
1. Configure `DATABASE_URL` (Neon) first.
2. In Telegram, as an admin: `/admin` -> **FastSaver APIs** -> **Add API**.
3. Send a FastSaver API key.
4. The bot checks the free `/balance` endpoint and saves the key to the database.
5. Repeat for as many keys as you want.

Pool strategies:
- Sequential: use priority order until a key is limited/exhausted.
- Round Robin: distribute calls across keys.
- Most Credits: prefer the key with the largest last-known balance.

Fallback states:
- HTTP 429 -> temporary rate limit/cooldown, then next key
- HTTP 401 -> invalid key, then next key
- exhausted quota/credits -> exhausted, then next key
- 5xx/network error -> temporary cooldown, then next key

`FASTSAVER_API_KEY` in Render is supported only as a legacy bootstrap key and is automatically imported into the pool.
