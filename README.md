# Hotel Metasearch & Aviasales Hot Tickets

Python application combining a multi-source hotel search MVP with an Aviasales fare collector and Telegram bot. Hotel search merges SravniOtel API results and Trip.com browser results through normalization, hotel matching, ranking, and SQLite persistence. FastAPI serves the local search interface. The fare collector reads the public Hot Tickets page through Chromium and the rendered DOM; it does not call Aviasales Explore API endpoints.

## Hotel metasearch MVP

The local hotel search page is available at [http://127.0.0.1:8766/hotels](http://127.0.0.1:8766/hotels). Run `hotel-search` (or `python -m aviasales_collector.hotel_cli`) to start it. It shares the existing SQLite database but uses separate `hotel_*` tables. City suggestions and live hotel search call the verified SravniOtel JSON endpoints. Trip.com has an independent ordinary Chromium adapter that uses the persistent `.tripcom-browser` profile for manual sign-in and searches; its profile is anchored beside the project, local, and git-ignored. To sign in manually, run `tripcom-login` (or `python -m aviasales_collector.tripcom_login`) and close that Chromium window after signing in. If Trip.com returns an access challenge or block, that source stops and reports its status while SravniOtel results remain available.

SravniOtel requests return source HTTP status and latency, normalized hotel and provider offers, and an opaque cursor for the next page. Trip.com searches through its ordinary result page in a separate headed Chromium profile. If that profile needs a sign-in, run `tripcom-login`, sign in manually, then close the Chromium window; the browser profile is git-ignored. The adapter reports `login_required`, `blocked`, or `unavailable` independently and stops on access challenges or HTTP 403/429/430 without retrying or bypassing them.

Hotel results are normalized and matched across sources using name, address, coordinates, city/country, stars, chain, phone, website, image URLs, and nearby-place context when those facts exist. High-confidence pairs become one canonical hotel with all source offers; uncertain pairs remain separate and include match-candidate evidence. Provider offers stay grouped under the hotel. Review coverage is shown as a confidence signal: unrated properties remain available when the provider has a current offer, are ranked after rated properties, and do not receive an imputed rating. Ranking exposes source-backed rating, review-count confidence, center distance, and price features; its current weights are initial product heuristics, not calibrated probabilities.

The result page shows one primary offer/CTA and collapses alternative offers. It compares the primary price in the selected currency (RUB by default when available), with source freshness shown separately. Local filters cover rating, stars, review count, price, center radius, meal, cancellation, and handling of unknown values. Trip.com review themes are derived only from exact short phrases visible on its result cards; exact phrases remain beside any detected theme. Deep review-page analysis is not implemented, and no overall positive/negative sentiment is inferred from those snippets. Coordinates use provider facts first, then identity-verified provider detail or exact geocoding; there is no city-center fallback. Beach distances use the hotel's coordinates and verified OpenStreetMap beach geometry, and remain hidden when no local beach feature is confirmed. The map uses CARTO Voyager tiles with OpenStreetMap and CARTO attribution. SravniOtel cursor pagination is supported by Load more; Trip.com currently returns the stable cards observed on its result page, with no next-page behavior confirmed.

## Install

```powershell
python -m pip install -e ".[test]"
python -m playwright install chromium
```

## Run

```powershell
python -m aviasales_collector --origin MOW --trip-type OW
python -m aviasales_collector --origin LED --trip-type RT --duration-min 3 --duration-max 12
```

The command writes one JSON document to stdout and exits with status 0 only when `complete` is true. Diagnostics go in `warnings`; a browser-side failure also attempts a screenshot in `artifacts/`. RT duration defaults to 1..30 days. The default run reloads once and reapplies the requested settings because the site resets the trip mode on refresh.

Dates without a year are normalized only when the year is defensible from the current date and, for return dates, the normalized departure date and visible trip duration. Recent/potentially stale past dates remain null with their raw value and a warning; a date well before the current calendar occurrence can roll into next year, but that inference is explicitly warned. City/country/airport mappings are intentionally not inferred from route tokens.

Run unit tests with `python -m pytest`.

## SQLite persistence

The standard-library repository stores per-user/chat settings, scan history, destinations, and one offer snapshot per collected card and scan. `scan_and_persist` reads saved settings, runs Playwright in a worker thread, and stores the result while leaving the calling event loop available:

```python
import asyncio

from aviasales_collector.database import MonitorSettings, SQLiteStore
from aviasales_collector.scan_service import scan_and_persist

with SQLiteStore("monitor.sqlite3") as store:
    store.save_settings(MonitorSettings(telegram_user_id, chat_id, "MOW", "OW", 5))
    outcome = asyncio.run(scan_and_persist(store, telegram_user_id, chat_id))
```

Inside an existing async application, call `await scan_and_persist(...)` directly.

Only complete scans update destinations and their current price snapshots. Incomplete regular scans remain in scan history but do not retain offers. Deep scans retain every successfully collected card, including partial passes; only destinations with collected results update their price snapshots, while failed or skipped destinations keep their previous values. A deep pass with no usable results does not change the catalog. Destination identity uses the origin plus the normalized visible destination name; the route token is kept as observed metadata, not treated as a stable city ID. New destinations start inactive. The repository does not send notifications or run scans on a schedule.

## Telegram bot

When the bot starts, it also serves a local results page at [http://127.0.0.1:8765](http://127.0.0.1:8765). The origin tabs show one city or all origins, with search, regular/deep mode, an active-destinations filter, price/date/trip-length sorting, automatic refresh, and paginated history. Current destination lists in Telegram and on the website include only destinations present in the latest successful complete regular scan for that origin; older destination and offer records remain stored for history and return automatically if found again. Failed or partial scans do not hide destinations. A card shows its previous-check price crossed out when the current price is at least 6.5% lower. The heart toggles the same per-destination monitoring state as Telegram. The web service only binds to loopback, reads scan data in read-only mode, and filters scans to `TELEGRAM_ALLOWED_USER_IDS`; its local archive and activation controls are owner-scoped. Its startup failure does not stop the Telegram bot. The website is not started by the standalone collector CLI.

## Deep scan and price tracking

The `/deep` command (also available from the home menu) visits every active destination present in the latest successful complete regular scan through the visible **More tickets** control. The browser follows the resulting `/hottickets/<origin>/<destination>` page and parses its rendered ticket links; the application never calls Aviasales Explore endpoints directly. Destination pages can virtualize their DOM, so completion is based on stable scrolling and the accumulated unique links rather than the number of nodes left mounted at the end.

Deep scans are intentionally manual at this stage because one destination page can be considerably more expensive than a normal Hot Tickets scan. For active destinations, SQLite records the previous and current price. A price decrease must meet the configured `PRICE_DROP_THRESHOLD_PERCENT` (default 12%) on the historical-reference path, plus the existing deal-score and absolute-saving conditions. The strong single-drop path uses the same configured percentage plus its absolute-saving guard. Other changes remain in scan history. If some destination pages fail, successfully checked destinations are persisted and the Telegram status reports a partial check; failed destinations keep their previous price baseline. If none of the destinations can be checked, the pass is incomplete and does not update the catalog. The bot can cap the number of destinations per deep scan and retry an incomplete pass with backoff; skipped destinations are reported in the scan warnings and are not treated as newly observed data.

The bot also exposes `/history` (and a home-menu button) with the latest scan summaries. Optional deep-scan controls are read from the environment:

```text
PRICE_DROP_THRESHOLD_PERCENT=12
DEEP_SCAN_MAX_DESTINATIONS=      # empty means all active destinations
DEEP_SCAN_RETRIES=0
DEEP_SCAN_RETRY_DELAY_SECONDS=3
DEEP_SCAN_INTERVAL_MINUTES=360   # default: every six hours; blank also uses 360
AVIASALES_BROWSER_DATA_DIR=.aviasales-browser
HOT_TICKETS_LOCK_PATH=.hot-tickets-bot.lock
```

Monitor settings are stored per `(Telegram user, chat, departure origin)`, so MOW and LED can run simultaneously with different trip modes and intervals. The home screen renders one block per departure origin with its active current-destination count. The `/destinations` buttons show each currently found destination's saved price when available, and manual scans report whether new destinations appeared; deep scans edit one status message with the current destination and result count.

The deep scheduler runs every six hours by default and checks active destinations for each enabled origin. It restores the next due time from the last completed deep scan in SQLite, so restarting the bot does not trigger an extra immediate scan. Set `DEEP_SCAN_INTERVAL_MINUTES` to override the cadence. Application logs use local `HH:MM:SS` timestamps and include the origin and the next scheduled run.

On Windows, `run-bot-with-logs.cmd` starts the bot in the current console and appends the same output to `bot-runtime.log`.

The bot uses Telethon's native MTProxy connection class and connects to the local `tg-ws-proxy` listener. `tg-ws-proxy` is an MTProto-over-TCP proxy on the client side; it is not a SOCKS or HTTP proxy. The upstream project bridges traffic to Telegram DCs over WebSocket and may use configured fallback transports if a WebSocket route is unavailable. This app does not start or bundle the proxy: it must already be running on the bot host.

For a headless local proxy, follow the upstream [build-from-source instructions](https://github.com/Flowseal/tg-ws-proxy/blob/main/docs/RU/BuildFromSource.md): install the proxy project and run, in that project environment:

```powershell
tg-ws-proxy --host 127.0.0.1 --port 1443 --secret <32-hex-secret>
```

The proxy prints a `tg://proxy` link. Its `secret` parameter is `dd` followed by the 32 hex characters. The Windows tray release also has **Copy Link** and uses the same local MTProto protocol. Keep that proxy process running while the bot runs.

Configure the bot in PowerShell (get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from `my.telegram.org`; create `TELEGRAM_BOT_TOKEN` with BotFather; use the secret from the local proxy link):

```powershell
$env:TELEGRAM_API_ID = "123456"
$env:TELEGRAM_API_HASH = "your-api-hash"
$env:TELEGRAM_BOT_TOKEN = "your-bot-token"
$env:TELEGRAM_ALLOWED_USER_IDS = "123456789"
# Optional: explicitly allow group chat IDs
# $env:TELEGRAM_ALLOWED_CHAT_IDS = "-1001234567890"
$env:TG_WS_PROXY_HOST = "127.0.0.1"
$env:TG_WS_PROXY_PORT = "1443"
$env:TG_WS_PROXY_SECRET = "dd<32-hex-secret>"
hot-tickets-bot
```

The bot refuses to start without a valid `dd` secret and an allowed Telegram user ID, so it cannot silently fall back to a direct Telethon connection or accept users outside the allowlist. Set multiple user IDs as a comma-separated list. By default, commands are accepted only in the authorized user's private chat; add explicit group/channel chat IDs to `TELEGRAM_ALLOWED_CHAT_IDS` to enable those chats. It offers `/start`, `/settings`, `/destinations`, `/details`, `/scan`, `/deep`, `/history`, and `/cancel`; settings are saved per Telegram user, chat, and departure origin. Destination buttons are owner-scoped and idempotent. After connecting, one in-process scheduler polls enabled settings, runs scans per origin, and uses each saved interval for the next run; transient failures use a short exponential retry. A monitor can be paused or resumed from the home menu; changing settings takes effect without restarting the bot. Complete scans that discover new destinations trigger one compact Telegram notification; the full list and activation controls remain in `/destinations`. `/details` shows a paginated compact view of the latest offers with price, dates, airline, stops, transfer city, transfer airports, and flight duration. Incomplete scans are logged and never produce a false empty-result notification. The deep scheduler checks active destinations for each enabled origin every six hours by default, restoring its due time from the latest completed scan after restart. The bot creates a singleton lock to prevent duplicate processes and uses an origin-specific persistent browser profile. Telethon's `.session`, browser profiles, and the SQLite DB contain sensitive state and are git-ignored.
