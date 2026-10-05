"""Thin client for the Shohoz railway API plus the seat-matrix builder."""

import json
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

BASE = "https://railspaapi.shohoz.com/v1.0/web"
BUY_URL = "https://eticket.railway.gov.bd/booking/train/search"
WEEK = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]
MAX_WORKERS = 3
# Shohoz returns 429 after ~50 searches in a rolling minute and then blocks for
# several minutes, so stay well under that across the whole process.
RATE_LIMIT, RATE_WINDOW = 45, 60.0
# A 429 locks the login out for ~10 minutes, and probing early seems to restart
# the clock, so wait the whole lockout before retrying.
COOLDOWN = 10 * 60
MAX_429_RETRIES = 2
ALLOC_PATH = Path(__file__).resolve().parent / "data" / "allocations.json"
ALLOC_MAX_AGE = 14  # days before a train's learned allocation is re-scanned in full

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://eticket.railway.gov.bd",
    "Referer": "https://eticket.railway.gov.bd/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
}


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class Cancelled(Exception):
    """The user stopped the job; raised at the next request boundary."""


def parse_credentials(text):
    """Pull auth headers out of a pasted 'Copy as cURL' (or a bare token).

    The search API rejects the Bearer token unless it comes with the
    X-Device-Id / X-Device-Key pair from the same browser session.
    """
    creds = {}
    for name in ("Authorization", "X-Device-Id", "X-Device-Key"):
        m = re.search(rf"{name}:\s*([^'\"\n]+)", text, re.I)
        if m:
            creds[name] = m.group(1).strip()
    if not creds and text.strip():
        creds["Authorization"] = text.strip()
    if "Authorization" in creds and not creds["Authorization"].lower().startswith("bearer "):
        creds["Authorization"] = f"Bearer {creds['Authorization']}"
    return creds


def _session(creds=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers.update(creds or {})
    adapter = HTTPAdapter(pool_maxsize=MAX_WORKERS, max_retries=2)
    s.mount("https://", adapter)
    return s


def _error_message(resp):
    try:
        msgs = resp.json().get("error", {}).get("messages")
        if isinstance(msgs, list):
            return " ".join(msgs)
        if isinstance(msgs, dict):
            return " ".join(str(v) for v in msgs.values())
    except ValueError:
        pass
    return f"HTTP {resp.status_code}"


def parse_model(train_input):
    """'EKOTA EXPRESS (705)' or '705' -> '705'."""
    m = re.search(r"\((\d+)\)", train_input) or re.search(r"(\d+)", train_input)
    return m.group(1) if m else None


# ---------------------------------------------------------------- route

def _clock(t):
    """'10:38 am BST' -> datetime.time, or None."""
    if not t:
        return None
    return datetime.strptime(t.replace("BST", "").strip().upper(), "%I:%M %p").time()


def _fmt_hm(value, minutes_only=False):
    """'04:12' -> '4 h 12 min', '05' -> '5 min'."""
    if not value or not re.fullmatch(r"\d+(:\d+)?", value.strip()):
        return None  # API sometimes sends '---'
    if minutes_only:
        return f"{int(value.split(':')[-1])} min"
    h, m = (int(x) for x in (["0"] + value.split(":"))[-2:])
    parts = ([f"{h} h"] if h else []) + ([f"{m} min"] if m else [])
    return " ".join(parts) or "0 min"


def get_route(model, journey_date):
    """Fetch the train's stops and work out the calendar date at each stop."""
    resp = _session().post(
        f"{BASE}/train-routes",
        json={"model": model, "departure_date_time": journey_date.strftime("%Y-%m-%d")},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ApiError(f"Could not load the train route: {_error_message(resp)}")
    data = resp.json().get("data") or {}
    if not data.get("routes"):
        raise ApiError(f"No route found for train {model}.")

    stops, day, prev = [], journey_date, None
    for i, r in enumerate(data["routes"]):
        arr, dep = _clock(r.get("arrival_time")), _clock(r.get("departure_time"))
        # Walk the timetable; any clock going backwards means we crossed midnight.
        arr_date = None
        if arr:
            if prev and arr < prev:
                day += timedelta(days=1)
            arr_date, prev = day, arr
        if dep:
            if prev and dep < prev:
                day += timedelta(days=1)
            prev = dep
        stops.append({
            "city": r["city"],
            "arrival": r.get("arrival_time", "").replace(" BST", "") if arr else None,
            "departure": r.get("departure_time", "").replace(" BST", "") if dep else None,
            "halt": _fmt_hm(r.get("halt"), minutes_only=True),
            "duration": _fmt_hm(r.get("duration")),
            "date": day,  # date the train leaves (or, at the terminus, reaches) this stop
            "date_changed": arr_date is not None and arr_date != journey_date,
            "is_origin": i == 0,
            "is_destination": i == len(data["routes"]) - 1,
        })

    running = set(data.get("days") or [])
    return {
        "train_name": data.get("train_name") or f"Train {model}",
        "days": [{"day": d, "off": d not in running} for d in WEEK],
        "stops": stops,
        "total_duration": _fmt_hm(data.get("total_duration")),
    }


# ---------------------------------------------------------------- seats

class _RateLimiter:
    """Process-wide sliding-window limiter shared by every running job.

    Remembers when each request in the last `window` seconds started, across
    searches: a new search right after a finished one still waits for the old
    requests to age out. The history (and any 429 lockout pause) is saved to
    disk, so restarting the app (incl. the debug auto-reloader) doesn't reset it.
    Uses wall-clock time so saved timestamps stay valid in a new process.
    """

    def __init__(self, limit, window, path=None):
        self.limit, self.window, self.path = limit, window, path
        self.calls = deque()
        self.paused_until = 0.0
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text())
        except (AttributeError, OSError, ValueError):
            return
        now = time.time()
        self.calls.extend(t for t in sorted(state.get("calls", [])) if now - t <= self.window)
        self.paused_until = max(0.0, float(state.get("paused_until", 0)))

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"calls": list(self.calls), "paused_until": self.paused_until}))
            tmp.replace(self.path)
        except OSError:
            pass  # limiting still works in memory

    def wait(self, cancel=None):
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] > self.window:
                    self.calls.popleft()
                delay = max(self.paused_until - now,
                            self.calls[0] + self.window - now if len(self.calls) >= self.limit else 0)
                if delay <= 0:
                    self.calls.append(now)
                    self._save()
                    return
            time.sleep(min(delay, 1.0))

    def pause(self, seconds):
        """Hold every request for `seconds`; returns the wall-clock time we resume."""
        with self.lock:
            self.paused_until = max(self.paused_until, time.time() + seconds)
            self._save()
            return self.paused_until


limiter = _RateLimiter(RATE_LIMIT, RATE_WINDOW, Path(__file__).resolve().parent / "data" / "ratelimit.json")


def norm_station(name):
    """'Biman_Bandar' / 'biman bandar' -> 'biman bandar' for comparisons."""
    return name.replace("_", " ").strip().lower()


def _seg_key(origin, dest, date):
    return norm_station(origin), norm_station(dest), date


def _find_train(trains, model):
    for train in trains:
        if str(train.get("train_model")) == model or f"({model})" in train.get("trip_number", ""):
            return train
    return None


def search_trips(session, origin, dest, date, seat_class="S_CHAIR", on_wait=None, cancel=None):
    """All trains (with seat counts) on one from->to segment; [] when nothing is sold."""
    params = {
        "from_city": origin, "to_city": dest,
        "date_of_journey": date.strftime("%d-%b-%Y"),
        "seat_class": seat_class,  # required by the API; the response still lists every class
    }
    for attempt in range(MAX_429_RETRIES + 1):
        limiter.wait(cancel)
        resp = session.get(f"{BASE}/bookings/search-trips-v2", params=params, timeout=20)
        if resp.status_code != 429:
            break
        # Probing too soon seems to extend Shohoz's lockout, so back off exponentially.
        resume_at = limiter.pause(COOLDOWN)
        if on_wait:
            on_wait(resume_at)
    else:
        raise ApiError("Shohoz kept rate-limiting this login. Close other tabs/apps using it and try again in ~10 minutes.")
    if resp.status_code == 401:
        raise AuthError(_error_message(resp))
    if resp.status_code != 200:
        return None  # error / not on sale: blank cell, and don't learn from it
    return (resp.json().get("data") or {}).get("trains", [])


def fetch_segments(session, segments, seat_class="S_CHAIR", progress=None, done_offset=0, total=None,
                   cancel=None):
    """Search every (origin, dest, date) segment once, in parallel under the rate limit.

    One search returns every train on that segment, so callers share the result
    across trains instead of querying per train. Returns {_seg_key(...): trains}.
    progress(done, total, waiting) is called as segments complete; waiting is
    False, or the epoch time requests resume after a rate-limit lockout.
    """
    segments = list(segments)
    total = total or len(segments) + done_offset
    done, lock, abort = done_offset, threading.Lock(), threading.Event()
    first_error = []

    def report(waiting=False):
        if progress:
            progress(done, total, waiting)

    def job(seg):
        nonlocal done
        if abort.is_set():  # don't burn rate-limit quota once the login is known bad
            raise AuthError("aborted")
        try:
            trains = search_trips(session, *seg, seat_class=seat_class,
                                  on_wait=lambda resume_at: report(waiting=resume_at), cancel=cancel)
        except Exception as e:
            with lock:
                first_error.append(e)
            abort.set()
            raise
        with lock:
            done += 1
            report()
        return _seg_key(*seg), trains

    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        try:
            return dict(pool.map(job, segments))
        except Exception:
            if first_error:
                raise first_error[0]
            raise


def _buy_url(stops, i, j, seat_type):
    return requests.Request("GET", BUY_URL, params={
        "fromcity": stops[i]["city"], "tocity": stops[j]["city"],
        "doj": stops[i]["date"].strftime("%d-%b-%Y"), "class": seat_type,
    }).prepare().url


def _grid(stops, model, results, considered, only_type=None):
    """Full-train grid(s): every stop on both axes, one grid per seat class.

    results     {seg_key: trains list, or None if that request failed}
    considered  pairs this search meant to cover; those missing from results
                were skipped because they're learned as having no quota.

    Each cell is a dict with "state":
      na    same station or travelling backwards
      seats train sells this class here: "seats" (0 = sold out), "fare", "buy"
      none  no seat quota (train not listed, class absent, or learned no-quota)
      skip  not checked (outside a minimized window, or the request failed)
    """
    n = len(stops)
    info = {}  # (i, j) -> list of seat_types the train has there ([] = no quota)
    for i in range(n):
        for j in range(i + 1, n):
            key = _seg_key(stops[i]["city"], stops[j]["city"], stops[i]["date"])
            if results.get(key) is not None:
                train = _find_train(results[key], model)
                info[(i, j)] = train.get("seat_types", []) if train else []
            elif (i, j) in considered and key not in results:
                info[(i, j)] = []
    classes = [only_type] if only_type else sorted({st["type"] for v in info.values() for st in v})
    matrices = []
    for cls in classes:
        rows, total, any_quota = [], 0, False
        for i in range(n):
            row = []
            for j in range(n):
                if j <= i:
                    row.append({"state": "na"})
                    continue
                seat_types = info.get((i, j))
                st = next((x for x in seat_types or [] if x["type"] == cls), None)
                if seat_types is None:
                    row.append({"state": "skip"})
                elif st is None:
                    row.append({"state": "none"})
                else:
                    seats = (st.get("seat_counts") or {}).get("online", 0)
                    fare = round(float(st.get("fare") or 0) + float(st.get("vat_amount") or 0))
                    row.append({"state": "seats", "seats": seats, "fare": fare, "buy": _buy_url(stops, i, j, cls)})
                    total += seats
                    any_quota = True
            rows.append(row)
        if any_quota:
            matrices.append({"seat_type": cls, "stations": [x["city"] for x in stops],
                             "rows": rows, "total_seats": total})
    matrices.sort(key=lambda m: -m["total_seats"])
    return matrices


def split_options(matrix, a, b, max_tickets=4, limit=5):
    """Journeys a -> ... -> b on 2..max_tickets tickets, using only grid cells that
    have seats (> 0). Stations must run forwards; no extra API calls.

    Returns one group per ticket count, fewest tickets first:
      [{"tickets": 2, "found": n, "options": [{"legs": [...], "total": fare}, ...]}, ...]
    Options within a group are cheapest first (ties: more seats on the tightest leg).
    """
    rows, names = matrix["rows"], matrix["stations"]

    def ok(i, j):
        c = rows[i][j]
        return c["state"] == "seats" and c["seats"] > 0

    found = {k: [] for k in range(2, max_tickets + 1)}

    def walk(i, path):  # path = station indices so far, starting with a
        for j in range(i + 1, b + 1):
            if not ok(i, j):
                continue
            if j == b:
                if len(path) >= 2:  # at least 2 tickets; a single a->b leg is the direct ticket
                    found[len(path)].append(path + [b])
            elif len(path) < max_tickets:
                walk(j, path + [j])

    walk(a, [a])
    groups = []
    for k, paths in found.items():
        options = []
        for path in paths:
            legs = [dict(rows[x][y], **{"from": names[x], "to": names[y]}) for x, y in zip(path, path[1:])]
            options.append({"legs": legs, "total": sum(l["fare"] for l in legs)})
        options.sort(key=lambda o: (o["total"], -min(l["seats"] for l in o["legs"])))
        groups.append({"tickets": k, "found": len(options), "options": options[:limit]})
    return groups


class _Allocations:
    """Which from->to station pairs each train has a seat allocation on.

    Shohoz has no quota endpoint, but a pair with no allocation never lists the
    train in its search results (a sold-out pair still lists it, with 0 seats).
    Stored per train, one row per boarding station, in route order:

        "705": {
          "train": "EKOTA EXPRESS (705)",
          "updated": "2026-10-04",
          "allocation": {
            "Dhaka": {"Biman Bandar": true, "Tangail": false, ...},
            ...

    true = allocated (cell shows a number or 0), false = no allocation (empty
    cell, skipped next time). Pairs never checked are absent and get queried.
    Jobs update the in-memory copy and save once at the end (flush).
    """

    def __init__(self, path):
        self.path, self.lock, self.dirty = path, threading.Lock(), False
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        for model, entry in list(self.data.items()):
            if "checked" in entry:  # migrate the old {"checked": [...], "sold": [...]} format
                sold = {tuple(p) for p in entry["sold"]}
                table = {}
                for f, t in entry["checked"]:
                    table.setdefault(_label(f), {})[_label(t)] = (f, t) in sold
                self.data[model] = {"train": entry.get("train", ""), "updated": entry["updated"],
                                    "allocation": table}
                self.dirty = True
        self.flush()

    def skip(self, model):
        """{(from, to)} normalised pairs known to have no allocation (empty if unknown/stale)."""
        entry = self.data.get(model)
        if not entry or (date.today() - date.fromisoformat(entry["updated"])).days > ALLOC_MAX_AGE:
            return set()
        return {(norm_station(f), norm_station(t))
                for f, row in entry["allocation"].items() for t, ok in row.items() if not ok}

    def learn(self, model, train_name, stops, checked, sold, replace=False):
        """Merge one scan into the train's table (or replace it after a full re-check).

        checked/sold are (from, to) station-name pairs; the newest observation of
        a pair wins. A scan where nothing listed the train means sales aren't open
        for that date (or the login failed), so it teaches nothing.
        """
        if not sold:
            return
        sold = {(_label(f), _label(t)) for f, t in sold}
        with self.lock:
            entry = self.data.get(model)
            table = {} if replace or not entry else entry["allocation"]
            for f, t in checked:
                f, t = _label(f), _label(t)
                table.setdefault(f, {})[t] = (f, t) in sold
            # keep rows and columns in route order so the file reads like the matrix
            order = {_label(c): i for i, c in enumerate(stops)}
            rank = lambda name: (order.get(name, len(order)), name)
            table = {f: dict(sorted(table[f].items(), key=lambda kv: rank(kv[0])))
                     for f in sorted(table, key=rank)}
            self.data[model] = {"train": train_name or (entry or {}).get("train", ""),
                                "updated": date.today().isoformat(), "allocation": table}
            self.dirty = True

    def flush(self):
        """Write the file if anything changed: trains sorted by code, one line per station row."""
        with self.lock:
            if not self.dirty:
                return
            lines = ["{"]
            models = sorted(self.data, key=lambda m: (len(m), m))
            for n, model in enumerate(models):
                e = self.data[model]
                rows = list(e["allocation"].items())
                lines.append(f'  {json.dumps(model)}: {{')
                lines.append(f'    "train": {json.dumps(e["train"], ensure_ascii=False)},')
                lines.append(f'    "updated": {json.dumps(e["updated"])},')
                lines.append('    "allocation": {')
                for k, (f, row) in enumerate(rows):
                    comma = "," if k < len(rows) - 1 else ""
                    lines.append(f'      {json.dumps(f, ensure_ascii=False)}: '
                                 f'{json.dumps(row, ensure_ascii=False)}{comma}')
                lines.append("    }")
                lines.append("  }" + ("," if n < len(models) - 1 else ""))
            lines.append("}")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(self.path)
            self.dirty = False


def _label(name):
    """Display form used in the allocation file: 'biman_bandar' -> 'Biman Bandar'-style
    names are kept as the API spells them, minus underscores."""
    return name.replace("_", " ").strip()


allocations = _Allocations(ALLOC_PATH)


def _span_segments(stops, a, b):
    pairs = [(i, j) for i in range(a, b + 1) for j in range(i + 1, b + 1)]
    return pairs, [(stops[i]["city"], stops[j]["city"], stops[i]["date"]) for i, j in pairs]


def _name_pair(stops, pair):
    return norm_station(stops[pair[0]]["city"]), norm_station(stops[pair[1]]["city"])


def _allocated(model, stops, pairs, full):
    """The subset of pairs worth querying for this train (drops known no-quota pairs)."""
    skip = set() if full else allocations.skip(model)
    return [p for p in pairs if _name_pair(stops, p) not in skip]


def _collect(model, stops, pairs, segments, results):
    """Pull this train's seat types out of shared segment results; also returns
    every pair that was queried and listed the train (for learning)."""
    by_pair = {}
    for pair, seg in zip(pairs, segments):
        train = _find_train(results.get(_seg_key(*seg)) or [], model)
        if train:
            by_pair[pair] = train.get("seat_types", [])
    city = lambda p: (stops[p[0]]["city"], stops[p[1]]["city"])
    checked = [city(p) for p, seg in zip(pairs, segments) if results.get(_seg_key(*seg)) is not None]
    return by_pair, checked, [city(p) for p in by_pair]


def build_matrix(model, route, creds, progress=None, full=False, cancel=None):
    """Every pair on one train's route, one full grid per seat class.

    Only pairs with a known seat allocation are queried unless full=True or the
    train hasn't been scanned yet. Returns (matrices, stats).
    """
    stops = route["stops"]
    all_pairs, _ = _span_segments(stops, 0, len(stops) - 1)
    pairs = _allocated(model, stops, all_pairs, full)
    segments = [(stops[i]["city"], stops[j]["city"], stops[i]["date"]) for i, j in pairs]
    results = fetch_segments(_session(creds), segments, progress=progress, cancel=cancel)
    _, checked, sold = _collect(model, stops, pairs, segments, results)
    allocations.learn(model, route["train_name"], [x["city"] for x in stops], checked, sold, replace=full)
    allocations.flush()
    stats = {"queried": len(pairs), "considered": len(all_pairs), "total": len(all_pairs), "minimized": False}
    return _grid(stops, model, results, set(all_pairs)), stats


def _window_pairs(n, a, b, window):
    """Minimized search pairs, counted along this train's own stops.

    window = (from_before, from_after, to_before, to_after, split):
      - boarding stops around a x alighting stops around b
      - split=True adds a->x and x->b for every stop x in between, i.e. every
        two-ticket journey from your From to your To
      - your exact a->b is always included
    Pairs must run forwards (boarding before alighting).
    """
    fb, fa, tb, ta, split = window
    boards = range(max(0, a - fb), min(n, a + fa + 1))
    alights = range(max(0, b - tb), min(n, b + ta + 1))
    pairs = {(i, j) for i in boards for j in alights if i < j} | {(a, b)}
    if split:
        pairs |= {(a, x) for x in range(a + 1, b)} | {(x, b) for x in range(a + 1, b)}
    return sorted(pairs)


def search_route(origin, dest, journey_date, seat_class, creds, progress=None, full=False, cancel=None,
                 window=None):
    """Every train running origin->dest on journey_date, each with a full-train
    grid for seat_class. Without a window every pair on the train is checked;
    with one (see _window_pairs) only pairs near From/To (+ split legs), and the
    rest of the grid shows as not checked. Returns (trains, stats)."""
    session = _session(creds)
    if progress:
        progress(0, 1, False)
    trains = search_trips(session, origin, dest, journey_date, seat_class, cancel=cancel) or []
    known = {_seg_key(origin, dest, journey_date): trains}

    plans, minimized = [], False
    for t in trains:
        model = str(t.get("train_model") or parse_model(t.get("trip_number", "")) or "")
        parent = t.get("parent_route") or {}
        try:
            origin_date = datetime.strptime(parent["departure_date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            origin_date = journey_date
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        route = get_route(model, origin_date)
        stops = route["stops"]
        names = [norm_station(x["city"]) for x in stops]
        try:
            a, b = names.index(norm_station(origin)), names.index(norm_station(dest))
        except ValueError:
            continue
        if a >= b:
            continue
        all_pairs, _ = _span_segments(stops, 0, len(stops) - 1)
        pairs = all_pairs
        if window:
            win = _window_pairs(len(stops), a, b, window)
            if len(win) < len(all_pairs):  # a window bigger than the whole train isn't "minimized"
                pairs, minimized = win, True
        segments = [(stops[i]["city"], stops[j]["city"], stops[i]["date"]) for i, j in pairs]
        needed = set(_allocated(model, stops, pairs, full))
        all_keys = {_seg_key(stops[i]["city"], stops[j]["city"], stops[i]["date"]) for i, j in all_pairs}
        plans.append((t, model, route, a, b, pairs, segments, needed, all_keys))

    # Trains on the same line share most segments, so query each one only once,
    # and only where at least one train is known (or not yet known) to sell it.
    todo, considered_keys, every_key = {}, set(), set()
    for t, model, route, a, b, pairs, segments, needed, all_keys in plans:
        every_key |= all_keys
        for pair, seg in zip(pairs, segments):
            considered_keys.add(_seg_key(*seg))
            if pair in needed and _seg_key(*seg) not in known:
                todo.setdefault(_seg_key(*seg), seg)
    known.update(fetch_segments(session, todo.values(), seat_class, progress,
                                done_offset=1, total=len(todo) + 1, cancel=cancel))

    results = []
    for t, model, route, a, b, pairs, segments, needed, all_keys in plans:
        stops = route["stops"]
        _, checked, sold = _collect(model, stops, pairs, segments, known)
        # A route scan covers only part of the train's line, so merge rather than replace.
        allocations.learn(model, route["train_name"], [x["city"] for x in stops], checked, sold)
        matrix = next(iter(_grid(stops, model, known, set(pairs), only_type=seat_class)), None)
        direct = matrix and matrix["rows"][a][b]
        results.append({
            "trip_number": t.get("trip_number"),
            "model": model,
            "departure": t.get("departure_date_time"),
            "arrival": t.get("arrival_date_time"),
            "travel_time": t.get("travel_time"),
            "from": stops[a]["city"],
            "to": stops[b]["city"],
            "direct": direct if direct and direct["state"] == "seats" else None,
            "splits": split_options(matrix, a, b) if matrix else [],
            "minimized": len(pairs) < len(all_keys),  # middle-to-middle legs mostly unchecked
            "route": route,
            "board": a,
            "alight": b,
            # stops outside this range are dimmed in the route timeline
            "span": (min(i for i, _ in pairs), max(j for _, j in pairs)),
            "matrix": matrix,
        })
    allocations.flush()  # one write per job, after every train is merged
    direct_key = _seg_key(origin, dest, journey_date)
    # counts are distinct segments across all trains (shared segments are queried once)
    stats = {"queried": len(todo) + 1, "considered": len(considered_keys | {direct_key}),
             "total": len(every_key | {direct_key}), "minimized": minimized}
    return results, stats
