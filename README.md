# BR Seat Matrix

A small Flask app that shows **station-to-station seat availability** for Bangladesh Railway intercity trains. You pick a train (or a From/To route), and it builds a grid of every boarding × alighting pair with live seat counts, fares, and a **Buy** link straight into the official e-ticket site.

The official site only answers one from/to question at a time. This app asks all of them for you, within the API's rate limit, so you can spot options like "Dhaka → Natore has seats even though Dhaka → Rajshahi is sold out".

> Unofficial project. Not affiliated with Bangladesh Railway or Shohoz. It uses your own e-ticket login and the same public web API the official site uses. Use it for personal lookups and respect their terms.

## Features

- **Search by Train**: train + date, full matrix for every seat class.
- **Search by Route**: From, To, date and one seat class; one colour-coded card per train running that day, each with its own matrix and an expandable route timeline.
- **Full-train grid**: every stop of the train on both axes, in route order. Each cell shows seats (`8`), sold out (`0`), no seat quota (`–`), not checked (`·`), or is greyed out (same station / backwards).
- **Minimize search** (route mode, on by default): only checks stations near your From and To (default 3 before/after From, 5 before/after To) plus, optionally, the **split legs** (From → every stop in between, every stop in between → To). The rest of the grid shows `·`. If the window would cover the whole train anyway, it runs a full check.
- **Split options**: per train, the cheapest two-ticket journeys From → X → To where both legs have seats, with Buy links for each leg.
- **Allocation learning**: after a train is scanned once, station pairs with no seat quota are remembered and skipped next time.
- **Rate-limit safe**: paces requests to 45/min, survives restarts, and if Shohoz still returns 429 it waits out the ~10 minute lockout and resumes.
- Background jobs with a progress bar, ETA and a **Cancel** button.
- All 120 intercity trains as browsable cards; searchable comboboxes for trains and 225 stations.
- Visitor counter (today / week / month / year) kept in a plain text file.
- Light and dark mode, works on mobile.

## How it works

```
 Browser                        Flask app (app.py)                       Shohoz API
 ───────                        ──────────────────                       ──────────
 POST /  (train or route) ───►  validate, load route  ─────────────────► POST /train-routes      (no auth)
                                start background job  ── thread ──┐
 page shows route + progress                                      │
 GET /job/<id>  (every 1.5s) ◄─ progress / finished HTML          ├───► GET /bookings/search-trips-v2
 POST /job/<id>/cancel ───────► sets cancel flag                  │      (auth, one call per segment,
                                                                  │       45/min shared limiter)
                                data/allocations.json  ◄──────────┘
```

### The core problem

**`search-trips-v2`** takes exactly one `from_city`, `to_city`, `date_of_journey` and `seat_class`. There's no batch endpoint (I went through the official site's JS bundle looking for one). A 24-stop train has 276 station pairs, so a full matrix is 276 requests. Everything in `shohoz.py` exists to make that tolerable:

1. **One search answers every train on that segment.** The response lists all trains running from→to that day, with every seat class. Route searches key results by `(from, to, date)` and share them across trains, so overlapping lines cost one call, not one per train.
2. **Learn where quota exists.** An empty matrix cell means the train didn't appear in that pair's results, i.e. no allocation. A `0` means it appeared but is sold out. Empty pairs are stored as `false` in `data/allocations.json` and skipped next time; `0` pairs are still queried since cancellations free seats.
3. **Minimize search** restricts route mode to a window around your stations, counted along each train's own stops, plus the split legs `From → x` and `x → To` for every stop `x` in between (about 2 queries per stop, versus N² for the full grid). Split options are computed from those cells, so they cost no extra calls.
4. **Pace, don't burst into a lockout.** A process-wide sliding-window limiter (45 requests / 60s) gates every search request across all jobs. Its history and any active 429 pause are persisted to `data/ratelimit.json`, so a restart doesn't reset the budget.

### Dates on overnight trains

The search date is the date the train leaves *your boarding station*, not its origin. `get_route()` walks the timetable and bumps the date whenever the clock goes backwards, so a Dhaka 23:30 departure correctly searches Natore on the next day.

### Code layout

| Path | What's in it |
|---|---|
| `app.py` | Flask routes, background job runner (in-memory `JOBS`), form handling, minimize window, visitor counter |
| `shohoz.py` | API client, credential parsing, route/date logic, rate limiter, allocation store, matrix building (`build_matrix`, `search_route`) |
| `templates/index.html` | Page shell: search panel, train list, results area, cancel dialog |
| `templates/_macros.html` | `route_timeline()` and `matrix_table()` macros |
| `templates/_matrices.html`, `_train_cards.html` | Result fragments returned by `/job/<id>` |
| `static/app.js` | Mode accordion, comboboxes, date picker, job polling, cancel |
| `static/style.css` | Theme tokens, layout, per-train accent colours |
| `tools/build_trains.py` | Regenerates `data/trains.json` |
| `data/trains.json` | 120 intercity trains with stops (committed) |
| `data/allocations.json` | Learned seat quotas per train (committed, grows with use) |
| `data/ratelimit.json`, `data/visits.txt` | Runtime state (gitignored) |

### HTTP routes

| Route | Purpose |
|---|---|
| `GET /` | Home page with the train list |
| `POST /` | Run a search. `mode=train` (`train`, `date`) or `mode=route` (`from`, `to`, `rdate`, `seat`, optional `minimize`, `fb`, `fa`, `tb`, `ta`, `split`). Optional `full=on` forces a full re-check; optional `token` carries a pasted cURL. |
| `GET /job/<id>` | Progress JSON: `done`, `total`, `waiting` (epoch seconds of resume time during a 429 pause), and `html` + `error` once finished |
| `POST /job/<id>/cancel` | Stop a job before its next API request |

## Setup

Developed on **Python 3.14**; anything recent with `requests` and Flask 3 should be fine.

```bash
git clone https://github.com/AsifAbu/BR_Seat.git
cd BR_Seat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional, see below
.venv/bin/python app.py       # http://127.0.0.1:5000
```

Debug mode (auto-reload on code edits, Werkzeug debugger) is off unless you set `FLASK_DEBUG=1`. Note that auto-reload restarts the server on every `.py` edit, which kills running searches.

### Getting your login credentials

Seat search needs three headers from a logged-in browser session. The Bearer token alone gets a **401**; it must come with the matching device headers.

1. Log in at **eticket.railway.gov.bd** and run any search.
2. DevTools → Network → find the **`search-trips-v2`** request.
3. Right click → **Copy → Copy as cURL**.
4. Paste it into the **Login** box in the app, or put the three values in `.env`. The box keeps it until the tab closes; tick **Remember login on this browser** to keep it longer (not on shared computers).

```env
SHOHOZ_TOKEN=your-bearer-token
SHOHOZ_DEVICE_ID=your-x-device-id
SHOHOZ_DEVICE_KEY=your-x-device-key
```

The token expires about **12 hours** after login, and logging in somewhere else (phone app, another browser) revokes it. When the app says "Login rejected", grab a fresh cURL.

`.env` holds your token, phone number and NID inside the JWT. It's gitignored; keep it that way.

## Configuration

Most knobs are constants at the top of the files:

| Setting | Where | Default | Notes |
|---|---|---|---|
| `RATE_LIMIT`, `RATE_WINDOW` | `shohoz.py` | 45 / 60s | Shohoz locks out at roughly 50-60/min |
| `COOLDOWN` | `shohoz.py` | 600s | Wait after a 429 before retrying |
| `MAX_429_RETRIES` | `shohoz.py` | 2 | Then the job fails with a message |
| `MAX_WORKERS` | `shohoz.py` | 3 | More won't help; the limiter is the bottleneck |
| `ALLOC_MAX_AGE` | `shohoz.py` | 14 days | After this a train's learned quota is re-scanned in full |
| `WINDOW_DEFAULTS` | `app.py` | 3/3/5/5 | Minimize search: From before/after, To before/after |
| `CACHE_TTL` | `app.py` | 600s | How long a finished job is reused for an identical search |
| `PORT` | env | 5000 | Dev server port |
| `APP_PASSWORD` | env | unset | When set, the whole site asks for this password (HTTP Basic, any username) |
| `FLASK_DEBUG` | env | unset | `1` enables auto-reload and the Werkzeug debugger (local only) |
| `MAX_RUNNING_JOBS` | `app.py` | 5 | Concurrent searches; extra ones get a "server busy" message |

## Refreshing the train list

There's no "list trains" endpoint the app uses, so `tools/build_trains.py` probes codes 700-999 against the unauthenticated `train-routes` endpoint and keeps the ones that return a route. Takes a few minutes and needs no login:

```bash
.venv/bin/python tools/build_trains.py
```

(The site also has an unauthenticated `/all-trains/info` endpoint that returns 134 trains, including commuter/mail trains, in one call. Not wired in yet.)

## Deploying

It must run as a **web service**, not a static site: it needs a server to call Shohoz and run jobs. On Render:

- Build: `pip install -r requirements.txt gunicorn`
- Start: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
- Region: Singapore

**Exactly one worker.** Jobs, the limiter and the caches live in process memory. Two workers means the progress poll can land on a process that doesn't know the job, and each worker allows its own 45/min.

Don't put your `SHOHOZ_*` values in the host's env vars on a public URL: anyone who finds it would be searching with your account. Let each user paste their own cURL, or set **`APP_PASSWORD`** so only you can use it.

On free hosting expect: sleep after ~15 min idle (kills running jobs), and an ephemeral disk, so `allocations.json`, `ratelimit.json` and `visits.txt` reset on every deploy/restart. Shohoz may also treat non-Bangladesh datacenter IPs differently; untested.

## Security

- **Secrets stay local.** `.env` (token, device headers, and the phone/NID inside the JWT) is gitignored, as are the runtime files in `data/`.
- **CSRF**: `POST` requests carrying a cross-site `Origin` or `Sec-Fetch-Site: cross-site` are rejected with **403**, so another website can't make your browser start or cancel searches.
- **Per-login jobs**: running/cached searches are keyed by a SHA-256 fingerprint of the login headers, so two people never share (or cancel) each other's jobs.
- **Flood guard**: at most `MAX_RUNNING_JOBS` (5) searches run at once.
- **Optional password**: set `APP_PASSWORD` to put the whole site behind HTTP Basic auth.
- **Cookies**: the visitor cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` when served over HTTPS (proxy-aware via `ProxyFix`).
- **Debugger off by default**: the Werkzeug debugger only runs with `FLASK_DEBUG=1`.

## Shohoz API notes

What I've observed, not documented anywhere official:

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /v1.0/web/train-routes` `{"model": "705", "departure_date_time": "2026-10-09"}` | No | Stops, times, running days. Unknown codes return an empty `train_name`. `halt` can be `"---"`. |
| `GET /v1.0/web/bookings/search-trips-v2?from_city&to_city&date_of_journey=09-Oct-2026&seat_class` | Bearer + `X-Device-Id` + `X-Device-Key` | `seat_class` is required (422 without) but the response lists every class. `seat_counts.online` is what you can buy. |
| `GET /v1.0/web/all-trains/info` | No | Train numbers, origin, destination, sales opening time (08:00 WEST / 14:00 EAST zone). |

Station names use underscores (`Biman_Bandar`, `Thakurgaon_Road`); the app displays them with spaces and normalises for matching.

**Rate limit**: roughly 50-60 searches per rolling minute per login. Exceeding it returns **429** and locks the login for about 10 minutes; probing during the lockout seems to extend it. Other use of the same login (official site, phone app) shares this budget.

## Trade-offs & known issues

- **Bursty pacing.** The limiter caps the count per 60s but not the spacing, so requests go out in clumps (e.g. 21 in 5s, then idle). Around window boundaries Shohoz can see more than its limit and return 429 even though the app counted 45. Fix: space requests evenly (~1.33s apart). Not applied yet.
- **Single process by design.** In-memory jobs mean a restart (including Flask's debug auto-reloader on any `.py` edit) kills running searches. Learned allocations are flushed at the end of a job, so a killed job learns nothing. Moving jobs to a small queue (RQ + Redis) would fix both, but it's overkill for a personal tool.
- **First scan of a train is always full.** Allocation learning only pays off from the second search. A pre-scan of all trains would cost hours of API time.
- **Some calls bypass the limiter.** `train-routes` lookups and HTTP-level connection retries aren't counted. They're a handful per search, but they eat into the same budget if Shohoz counts them.
- **Allocation can change.** A pair learned as "no quota" is skipped for 14 days. Tick **Re-check every station pair** to force a full scan when quotas shift (e.g. around Eid).
