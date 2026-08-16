# V4.4 Production Queue Guide

## What the queue does
Every actual download action is now converted to a persistent Queue Job. Link analysis stays fast and interactive; the heavy file generation/download step goes through the worker pool.

Default worker count: 3 simultaneous jobs.

## User experience
After choosing a quality/format the user sees:
- queue position while waiting
- running state when a worker starts
- Cancel button
- Queue Status button

`Home -> صف من` lists the user's active jobs.

## Deduplication
If two or more users ask for the exact same resource/format while it is waiting/running, V4.4 creates only one physical queue job. Other users subscribe to that job. When it finishes, Telegram `file_id` is reused for the subscribers.

This reduces FastSaver credits, bandwidth, CPU and duplicated work.

## Smart Cache
Successful queue jobs save reusable Telegram artifacts in `smart_cache`.

A later identical request is served directly with Telegram `file_id` and does not call FastSaver or download the media again.

Default cache retention is 90 days (`SMART_CACHE_TTL_DAYS`).

## Retry
Temporary errors such as transport failures, timeout and HTTP 429 are retried automatically up to `QUEUE_MAX_RETRIES` additional retries. Existing FastSaver API Pool failover still runs inside every FastSaver request, so a job can switch API keys before the queue-level retry is used.

## Anti-abuse
- `MAX_QUEUE_SIZE=100`: physical waiting+running jobs allowed
- `MAX_ACTIVE_JOBS_PER_USER=3`: active subscriptions per normal user
- `USER_JOB_COOLDOWN=3`: seconds between queue submissions
- Admins bypass user cooldown/cap and get higher queue priority

## Admin Queue
Admin Mode -> `📥 صف دانلود`

Shows:
- waiting/running/done/failed
- worker count and capacity
- Smart Cache entries and hit rate
- dedup joins
- retries
- active jobs with Cancel controls

## Health manager
Every `FASTSAVER_HEALTH_INTERVAL` seconds the bot checks enabled FastSaver keys using the balance endpoint. Keys with positive balance become active again; zero-credit keys become exhausted. Admins are alerted when the pool reaches zero active keys and again when it recovers.

## Render recommendation
For Render Free start with:
```env
MAX_CONCURRENT_JOBS=2
```
If memory/CPU remains stable, increase to 3. The code defaults to 3, but lower concurrency is safer for resource-heavy direct Instagram/X/SoundCloud downloads.
