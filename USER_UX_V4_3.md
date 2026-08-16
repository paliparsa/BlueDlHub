# User UX — V4.3

## Home
Users see:
- Download Link
- Music Search
- Recent Downloads
- My Account
- Service Status
- Help / Support

The Home screen also shows service health and the user's rolling 24-hour usage.

## Link flow
1. User sends a supported URL.
2. One progress message is created and edited as the request advances.
3. A preview card is shown when a thumbnail is available.
4. Buttons are generated according to the platform/media type.
5. After delivery, the progress message becomes a success screen with:
   - Change quality/format
   - Recent Downloads
   - Home

## Music Search
Tap `🎵 جستجوی موزیک`, then type a song or artist name.
The bot performs one FastSaver Music Search request and shows up to five results.
Selecting a result performs Music Download and returns Telegram Audio.

## Recent Downloads
Files sent by Telegram are saved using `file_id` in Neon/PostgreSQL.
Selecting an item from Recent Downloads resends the existing Telegram file and does not call FastSaver again.
The bot retains the most recent 30 cached items per user and displays the latest 8.

## Error UX
Users do not see long Python/FastSaver stack errors. They get a short explanation plus:
- Retry
- Report Problem
- Home

Technical errors remain in `error_logs`. One-tap reports are stored in `user_reports` and also surfaced to the existing Error Center via an error-log entry.

## Database migration
No manual migration is required. On startup, V4.3 creates missing UX tables with `CREATE TABLE IF NOT EXISTS`.
