# Neon PostgreSQL Setup for BlueGate V4.2

V4.2 can use Neon PostgreSQL so users, stats, settings, bans, admin mode and FastSaver API keys survive Render restarts/redeploys.

## 1. Create Neon project
1. Sign in to Neon.
2. Open Projects and create a New Project.
3. Give it a name such as `bluegate-downloader`.
4. The default database/branch is enough; you do not need to create tables manually.

## 2. Copy the pooled connection string
1. Open the project dashboard.
2. Click **Connect**.
3. Enable **Connection pooling** / choose the pooled connection string.
4. Copy the full Postgres URL. It looks roughly like:

   `postgresql://USER:PASSWORD@ep-xxxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require`

Do not put this URL in GitHub.

## 3. Add it to Render
Render > your bot service > Environment > Add Environment Variable:

- Key: `DATABASE_URL`
- Value: paste the Neon pooled connection string

Save changes and redeploy.

## 4. Verify
After startup, open `/admin` or press Start as an admin.
System Status should show:

`DB: Neon/PostgreSQL`

The app creates/migrates its tables automatically on startup.

## 5. Add FastSaver APIs
From Telegram:

`/admin` -> `FastSaver APIs` -> `Add API`

Send a FastSaver key. V4.2 checks `/balance`, stores the key in Neon, masks it in the UI, and tries to delete the Telegram message containing the key.

You can add as many keys as you want.

## Notes
- If `DATABASE_URL` is missing, V4.2 falls back to SQLite at `DB_PATH`. On Render Free that local database should be treated as temporary.
- `FASTSAVER_API_KEY` is now optional. If it is still present in Render, V4.2 imports it into the API Pool as a bootstrap key.
- Existing old SQLite statistics are not automatically imported into Neon.
