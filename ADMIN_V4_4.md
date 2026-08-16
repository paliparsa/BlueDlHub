# Admin — V4.4

All V4.3 admin features remain available. New production controls:

## Queue Dashboard
Admin Mode -> `📥 صف دانلود`

Displays worker concurrency, queue capacity, waiting/running jobs, historical success/failure counts, cache statistics, dedup joins and retry count.

Admins can cancel an active physical queue job from this page.

## Smart Cache
Admin panel -> `♻️ پاکسازی Cache` clears the V4.4 smart artifact cache. Use this if Telegram file IDs become stale or you intentionally want resources rebuilt.

## API health alerts
The background health manager notifies all configured `ADMIN_IDS` if no active FastSaver API keys remain. It also sends a recovery message once a key becomes usable again.

## Priority
Admin download requests use a higher queue priority than normal user requests.
