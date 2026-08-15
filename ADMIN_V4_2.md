# BlueGate V4.2 Admin / User Modes

## Start behavior
- Normal user `/start` -> User Home.
- Admin `/start` -> last selected mode; default is Admin Mode.
- `/admin` -> switch to Admin Mode.
- `/user` -> switch to User Mode.

## User Home
- Download from link
- Music
- My account / daily usage
- Service status
- Help / support

## Admin Home
- Dashboard
- FastSaver API Pool
- Users + Ban/Unban
- Broadcast
- Service toggles
- Daily limit
- Force Join
- Maintenance
- Error Center
- System status

## FastSaver API Pool
Admin > FastSaver APIs:
- Add unlimited keys
- Balance check
- Enable / Disable
- Delete
- Change priority
- Refresh all
- Strategy: Sequential / Round Robin / Most Credits

Fallback behavior:
- 429 -> temporary Rate Limited -> next API
- 401 -> Invalid -> next API
- credit/quota exhausted -> Exhausted -> next API
- temporary 5xx/network -> short cooldown -> next API
- ordinary content errors such as fetch.error do not permanently burn a key

API keys are masked in Telegram. The bot attempts to delete the message containing a newly submitted key.
