# BlueGate Downloader

Telegram downloader service with a web-hook based backend, persistent database support, user/admin modes, download queue, caching, and FastSaver API pooling.

## Supported sources

- Instagram
- YouTube
- X / Twitter
- SoundCloud
- Spotify

## Main features

- Separate User and Admin modes
- Smart download queue with retry and cancellation
- Duplicate-job detection and Telegram `file_id` cache
- Multiple FastSaver API keys with automatic fallback
- Persistent PostgreSQL storage via Neon
- Per-user preferences and quick-download mode
- Favorites and recent-download history
- Batch link processing
- Daily limits, cooldowns, bans and per-user overrides
- Maintenance mode, broadcast and service toggles
- Persian / English user interface

## Files

```text
main.py            Application entry point
requirements.txt   Python dependencies
Dockerfile         Container build configuration
start.sh           Application start command
render.yaml        Render deployment configuration
.env.example       Environment variable template
.gitignore         Git ignore rules
README.md          Project documentation
```

## Required environment variables

Create these in Render under **Environment**:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=CHANGE_TO_A_RANDOM_SECRET
ADMIN_IDS=YOUR_TELEGRAM_USER_ID
DATABASE_URL=YOUR_NEON_POSTGRES_CONNECTION_STRING
```

`ADMIN_IDS` can contain multiple Telegram user IDs separated by commas if supported by your deployment configuration.

### FastSaver

A bootstrap key can optionally be set with:

```env
FASTSAVER_API_KEY=fs_sk_xxxxxxxxx
```

After the bot is running, additional FastSaver API keys can be added directly from:

```text
/start → Admin Mode → FastSaver APIs → Add API
```

The bot stores API keys in the persistent database and automatically falls back to another active key when a key is rate-limited, exhausted or unavailable.

## Neon database setup

1. Create a project in Neon.
2. Open the project and choose **Connect**.
3. Copy the pooled PostgreSQL connection string.
4. Add it to Render as:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Do not commit the connection string to GitHub.

No manual table creation is required. The application creates or migrates its required tables during startup.

If `DATABASE_URL` is not defined, the application falls back to the local SQLite path configured by `DB_PATH`. For Render deployments, PostgreSQL is recommended for persistent data.

## Render deployment

1. Create a GitHub repository.
2. Upload all project files to the repository root.
3. Create a new Render Web Service connected to the repository.
4. Select Docker deployment if Render does not detect it automatically.
5. Add the required environment variables.
6. Deploy the service.

The repository root should look like:

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

The health endpoint is:

```text
/health
```

## Optional settings

Defaults are already provided, but these can be overridden in Render:

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
BATCH_MAX_LINKS=10
```

For low-memory hosting, reducing concurrent jobs is recommended:

```env
MAX_CONCURRENT_JOBS=2
```

## Admin access

Administrators listed in `ADMIN_IDS` receive the admin interface when using `/start` and can switch between Admin Mode and User Mode.

Admin tools include:

- Dashboard and usage statistics
- User management and bans
- FastSaver API pool management
- Broadcast messages
- Service enable/disable controls
- Daily limits and per-user overrides
- Force Join configuration
- Maintenance mode
- Queue monitoring and cancellation
- Error reports and system status

## Updating

To update the bot:

1. Replace the project files in the GitHub repository with the new version.
2. Keep secrets only in Render Environment variables.
3. Commit and push the changes.
4. Render will redeploy automatically when Auto Deploy is enabled.

Neon data is kept independently from the application deployment, so normal updates do not remove users, settings or API-pool entries.

## Security notes

- Never commit `BOT_TOKEN`, FastSaver API keys or `DATABASE_URL`.
- Revoke and replace a Telegram bot token immediately if it becomes public.
- Keep `.env` files outside version control.
- Use a dedicated database and service credentials for production deployments.
