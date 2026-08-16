# BlueGate Downloader

BlueGate Downloader is a Telegram media downloader with a persistent PostgreSQL backend, user/admin modes, download queue, caching, FastSaver API pooling, and membership controls.

## Supported sources

- Instagram
- YouTube
- X / Twitter
- SoundCloud
- Spotify

## V5 features

### Membership

The bot includes three default plans:

| Plan | Daily downloads | Active jobs | Cooldown | Batch | Queue priority | YouTube | Spotify/day |
|---|---:|---:|---:|---:|---:|---:|---:|
| Free | 20 | 2 | 5s | 3 | 100 | 720p | 5 |
| VIP | 100 | 5 | 1s | 10 | 50 | 1080p | 30 |
| Premium | Unlimited | 10 | 0s | 25 | 10 | Best | Unlimited |

All values can be edited from the admin panel without redeploying the service.

V5 also includes:

- Redeem codes with plan, duration, use count and expiry
- Referral links and automatic rewards
- Subscription expiry reminders and automatic return to Free
- Per-plan YouTube and Spotify limits
- Plan-based queue priority
- Global campaigns that temporarily add daily download allowance
- Growth analytics
- Admin audit log
- Direct subscription management from each user's admin profile

Existing V4 data is kept. New V5 tables are created automatically on startup.

## Existing platform features

- Separate User and Admin modes
- Smart queue with retry and cancellation
- Duplicate-job detection
- Telegram `file_id` cache and recent downloads
- Multiple FastSaver API keys with automatic fallback
- FastSaver health monitoring
- Persistent PostgreSQL storage via Neon
- Quick Download preferences
- Favorites and history search
- Batch link processing
- Per-user overrides, bans and cooldowns
- Broadcast, Force Join and Maintenance mode
- Persian / English user interface

## Files

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

## Required environment variables

Add these in Render under **Environment**:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=CHANGE_TO_A_RANDOM_SECRET
ADMIN_IDS=YOUR_TELEGRAM_USER_ID
DATABASE_URL=YOUR_NEON_POSTGRES_CONNECTION_STRING
```

Multiple admin IDs can be separated with commas.

## FastSaver

An initial key can optionally be provided through Render:

```env
FASTSAVER_API_KEY=fs_sk_xxxxxxxxx
```

After the bot is running, manage all additional keys from:

```text
/start → Admin Mode → FastSaver APIs
```

The API pool supports automatic fallback when a key is rate-limited, exhausted, disabled or temporarily unavailable.

## Neon setup

1. Create a project in Neon.
2. Open **Connect** in the project dashboard.
3. Copy the pooled PostgreSQL connection string.
4. Add it to Render as `DATABASE_URL`.

Example format:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Do not commit this value to GitHub.

No SQL setup is required. Tables and migrations are handled automatically during application startup.

If `DATABASE_URL` is missing, SQLite is used as a local fallback. SQLite on a free Render service should not be treated as persistent storage.

## Deploy on Render

1. Upload all project files to the root of a GitHub repository.
2. Create a Render Web Service connected to that repository.
3. Use the included Docker configuration.
4. Add the required environment variables.
5. Deploy.

The repository root should contain:

```text
main.py
Dockerfile
requirements.txt
start.sh
render.yaml
.env.example
.gitignore
README.md
```

Health endpoint:

```text
/health
```

## Admin control center

Administrators listed in `ADMIN_IDS` enter Admin Mode from `/start` and can switch back to User Mode at any time.

Main sections include:

- Dashboard
- Plans & permissions
- Redeem codes
- Referral center
- Campaigns
- Users and subscription management
- FastSaver API pool
- Services
- Broadcast
- Queue
- Analytics
- Error center
- System status
- Audit log

### Editing plans

Open:

```text
Admin → Plans → select a plan
```

Editable values include:

- Daily limit
- Active jobs
- Cooldown
- Batch size
- Queue priority
- Maximum YouTube height
- YouTube daily limit
- Spotify daily limit

A value of `0` means unlimited where applicable. For maximum YouTube height, `0` means Best/unrestricted.

### Creating a redeem code

Open:

```text
Admin → Codes → Create Code
```

Send:

```text
vip | 7 | 1 | 30
```

Meaning:

```text
plan | subscription days | maximum uses | code expiry days
```

Users can redeem codes from **My Plan → Redeem Code** or with:

```text
/redeem CODE
```

### Referral rewards

Open:

```text
Admin → Referral → Reward Settings
```

Example:

```text
3 | vip | 1
```

This means every 3 successful referrals grants 1 day of VIP.

Each user receives a personal Telegram deep link from **Invite Friends**. Self-referrals and multiple referrers for the same user are blocked.

### Campaigns

A campaign temporarily adds extra daily downloads to non-unlimited plans.

Open:

```text
Admin → Campaigns → New Campaign
```

Example:

```text
Weekend Boost | 48 | 20
```

This creates a 48-hour campaign with +20 downloads per day.

### Direct subscription management

Open a user from the Admin Users page. You can:

- Grant VIP
- Grant Premium
- Return the account to Free
- Change per-user daily limits
- Change active-job limits
- Change cooldown
- Ban / Unban
- Send a direct message

## User membership interface

Users can open **My Plan** to see:

- Current plan
- Remaining subscription time
- Downloads used today
- Batch limit
- Queue priority
- YouTube quality allowance
- Spotify daily allowance

They can also open **Invite Friends** to get their referral link.

## Subscription expiry

The bot checks active subscriptions in the background. It can send reminders near expiry and automatically returns expired subscriptions to Free. Subscription events are stored in the database.

## Optional environment settings

```env
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

`BATCH_MAX_LINKS` is the absolute system ceiling. Each membership plan can have a smaller batch limit.

For low-memory hosting, use:

```env
MAX_CONCURRENT_JOBS=2
```

## Updating

1. Replace the files in the GitHub repository with the new version.
2. Keep all secrets in Render Environment variables.
3. Commit and push.
4. Let Render redeploy.

Neon is independent from the Render container, so normal deployments keep users, API keys, subscriptions, codes, referrals and settings.

## Security

- Never commit Telegram bot tokens, FastSaver keys or `DATABASE_URL`.
- Revoke a Telegram bot token immediately if it is exposed.
- Do not commit `.env` files.
- Use dedicated production credentials for the bot and database.
