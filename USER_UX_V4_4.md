# User UX — V4.4

V4.4 keeps the V4.3 Smart Home and adds queue visibility.

## Home
- 📥 دانلود لینک
- 🎵 جستجوی موزیک
- 🕘 دانلودهای اخیر
- 📥 صف من
- 📊 حساب من
- 🟢 وضعیت سرویس‌ها
- ❓ راهنما
- 🆘 پشتیبانی

## When a user chooses a download
1. The request enters the queue.
2. The user sees their position.
3. When a worker starts, the status changes to processing.
4. The existing editable download progress continues.
5. The final file is sent and reusable in Recent Downloads.

## Duplicate request
When the same media/quality is already being processed, the user sees that the bot attached them to the existing job instead of downloading it twice.

## Smart Cache
If the exact resource has already completed, it is sent immediately from Telegram cache. The user sees `Smart Cache Hit`; this path uses no FastSaver API call.

## Cancel
A user can cancel their own queue subscription. If other users share the same deduplicated job, their download continues; if nobody remains subscribed, the physical job is cancelled.
