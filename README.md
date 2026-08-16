# BlueGate Downloader

Telegram media downloader with PostgreSQL persistence, queueing, smart cache, memberships, BlueCredits wallet, and a multi-key FastSaver pool.

## Supported sources

- Instagram — Post / Reel / Carousel / Story / Highlight
- TikTok — Video / Photo / Slideshow / post audio
- YouTube — Video and Audio
- X / Twitter — direct extractor first, FastSaver fallback
- SoundCloud — direct audio extraction
- Spotify — Music Search + Music Download

## BlueCredits wallet

V5.2 adds an internal credit wallet. BlueCredits are controlled entirely by the bot admin and are not tied 1:1 to FastSaver credits.

User menu:

```text
/start → Wallet
```

Users can see:

- Current BlueCredits balance
- Recent wallet transactions
- Active credit packages
- Package prices in USD and Toman
- Current download rates

Smart Cache delivery does not charge BlueCredits again. If a fresh queued download fails or is cancelled after charging, the charge is refunded automatically.

## Admin pricing

No pricing values need to be stored in Render environment variables.

Open:

```text
/start → Admin Mode → BlueCredits
```

The admin can manage:

- Credit packages
- Package name
- BlueCredits amount
- USD price
- Toman price
- Package active/inactive state
- Per-service download cost
- Welcome credits for new users
- Individual user balances
- Recent wallet transactions

To create a package:

```text
BlueCredits → Manage Packages → New Package
```

Open the package and use the buttons for Name, Credits, USD and Toman. Activate the package when it is ready to be shown to users.

After receiving payment manually, open:

```text
Users → User → Wallet → Apply Package
```

and select the purchased package. The package balance is added to the user and recorded in wallet history and the admin audit log.

## Default download rates

These are only initial values. Every value can be edited from the Telegram admin panel.

```text
Instagram Post/Reel/Carousel   15 BC
Instagram Story/Highlight      50 BC
TikTok                         15 BC
X / Twitter                    15 BC
YouTube                        150 BC
YouTube 2K/4K                  250 BC
YouTube Audio                  90 BC
Spotify Music                  90 BC
SoundCloud                     0 BC
```

Set any rate to `0` to make that route free.

## Membership monthly credits

Each plan now has a `Monthly BC` field in:

```text
Admin → Plans → Select Plan → Monthly BC
```

If set above zero, an active user receives that amount once per 30-day membership cycle. The default is zero for all plans until the admin changes it.

## FastSaver pool

An optional first FastSaver key can be supplied through `FASTSAVER_API_KEY`. Additional keys are managed from:

```text
Admin Mode → FastSaver APIs
```

When a key is rate-limited, exhausted, invalid, or temporarily unavailable, the pool can fall back to another configured key.

## Required Render environment variables

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=CHANGE_TO_A_RANDOM_SECRET
ADMIN_IDS=YOUR_TELEGRAM_USER_ID
DATABASE_URL=YOUR_NEON_POSTGRES_CONNECTION_STRING
```

Optional bootstrap API key:

```env
FASTSAVER_API_KEY=fs_sk_xxxxxxxxx
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

Create a Neon PostgreSQL project, copy its pooled connection string and save it in Render as `DATABASE_URL`.

No manual SQL migration is required. Wallets, packages, transactions, download rates and membership credit-grant tables are created automatically on startup. Existing users, API keys, plans, queue data and cache are preserved.

If `DATABASE_URL` is missing, the app falls back to SQLite. Local SQLite on a free Render instance should not be considered persistent storage.

## Deploy on Render

1. Put all project files directly in the root of the GitHub repository.
2. Connect the repository to a Render Web Service.
3. Use the included Dockerfile.
4. Add the required environment variables.
5. Deploy.
6. Open the Telegram bot and use `/start`.

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

Replace these project files in the repository root and commit. Render can deploy the new commit automatically. Keep API keys, bot tokens and database credentials out of GitHub.
