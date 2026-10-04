import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request

import shohoz

load_dotenv()
app = Flask(__name__)

# Seat-matrix jobs run in background threads (a long route takes minutes under
# the API rate limit). Finished jobs double as a short-lived cache.
JOBS, JOBS_LOCK = {}, threading.Lock()
CACHE_TTL = 600

SEAT_TYPES = ["S_CHAIR", "SHOVAN", "SNIGDHA", "F_SEAT", "F_CHAIR", "F_BERTH",
              "AC_S", "AC_CHAIR", "AC_B", "SHULOV"]

# Built by tools/build_trains.py (the API has no train-list endpoint).
TRAINS = json.loads((Path(__file__).parent / "data" / "trains.json").read_text())
STATIONS = {}  # normalised name -> {"name": raw API name, "label": display name, "trains": n}
for t in TRAINS:
    t["short_name"] = re.sub(r"\s*\(\d+\)$", "", t["name"]).title()
    t["off"] = [d for d in shohoz.WEEK if d not in t["days"]]
    for city in t.get("stops") or [t["origin"], t["destination"]]:
        st = STATIONS.setdefault(shohoz.norm_station(city),
                                 {"name": city, "label": city.replace("_", " "), "trains": 0})
        st["trains"] += 1
STATION_LIST = sorted(STATIONS.values(), key=lambda s: s["label"])


def env_credentials():
    lines = [f"{h}: {os.getenv(v)}" for h, v in [
        ("Authorization", "SHOHOZ_TOKEN"),
        ("X-Device-Id", "SHOHOZ_DEVICE_ID"),
        ("X-Device-Key", "SHOHOZ_DEVICE_KEY"),
    ] if os.getenv(v)]
    return shohoz.parse_credentials("\n".join(lines))


def credentials_problem(creds):
    if "Authorization" not in creds:
        return "Seat availability needs your eticket.railway.gov.bd login. Paste 'Copy as cURL' of a search-trips-v2 request, or fill in .env."
    if "X-Device-Key" not in creds or "X-Device-Id" not in creds:
        return "Missing X-Device-Id / X-Device-Key; the API rejects the token without them. Paste the full 'Copy as cURL'."
    return None


def run_job(job, work):
    def progress(done, total, waiting):
        job.update(done=done, total=total, waiting=waiting)

    try:
        job["result"] = work(progress, job["cancel"])
    except shohoz.Cancelled:
        job["error"] = "Request cancelled. Wait a minute before searching again so the login isn't locked out."
    except shohoz.AuthError as e:
        job["error"] = f"Login rejected by Shohoz ({e}) Log in again and paste a fresh 'Copy as cURL'."
    except Exception as e:  # network errors etc. — show them instead of a stuck progress bar
        job["error"] = f"Error while fetching seats: {e}"
    job["finished"] = time.time()


def start_job(key, total, work, render):
    """Run work(progress) in the background; reuse a live/recent job with the same key."""
    with JOBS_LOCK:
        for jid, job in list(JOBS.items()):
            if job.get("finished") and time.time() - job["finished"] > CACHE_TTL:
                del JOBS[jid]
        for jid, job in JOBS.items():
            if job["key"] == key and not job.get("error"):
                return jid
        jid = uuid.uuid4().hex
        JOBS[jid] = {"key": key, "render": render, "done": 0, "total": total, "waiting": False,
                     "cancel": threading.Event()}
    threading.Thread(target=run_job, args=(JOBS[jid], work), daemon=True).start()
    return jid


WINDOW_DEFAULTS = {"fb": 3, "fa": 3, "tb": 5, "ta": 5}  # From before/after, To before/after


def parse_window(form):
    """Minimize-search window from the form, or None when the box is unticked."""
    if not form.get("minimize"):
        return None
    out = []
    for k, default in WINDOW_DEFAULTS.items():
        try:
            out.append(max(0, min(10, int(form.get(k, default)))))
        except ValueError:
            out.append(default)
    return tuple(out)


def scan_note(stats):
    if stats.get("full_span"):
        return (f"Minimized search: {stats['total']} station pairs near your stations instead of "
                f"{stats['full_span']} for the full route; queried {stats['queried']}, skipping known no-quota pairs.")
    skipped = stats["total"] - stats["queried"]
    if not skipped:
        return f"Checked all {stats['total']} station pairs; the next search for this train will skip pairs with no seat quota."
    return (f"Checked {stats['queried']} of {stats['total']} station pairs; skipped {skipped} "
            f"with no seat quota on earlier scans.")


def train_search(form, creds, ctx):
    model = shohoz.parse_model(form.get("train", ""))
    if not model:
        return render_template("index.html", error="Pick a train from the list (or type its code, e.g. 705).", **ctx)
    try:
        journey_date = datetime.strptime(form.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return render_template("index.html", error="Pick a date of journey.", **ctx)
    try:
        route = shohoz.get_route(model, journey_date)
    except (shohoz.ApiError, OSError) as e:
        return render_template("index.html", error=str(e), **ctx)

    job_id, matrix_error = None, credentials_problem(creds)
    if not matrix_error:
        def render(job):
            matrices, stats = job["result"]
            note = None if matrices else "No seats are on sale for this train and date yet."
            return note, render_template("_matrices.html", matrices=matrices, journey_date=journey_date,
                                         scan_note=scan_note(stats))

        full = bool(form.get("full"))
        n = len(route["stops"])
        job_id = start_job(("train", model, journey_date, full), n * (n - 1) // 2,
                           lambda p, c: shohoz.build_matrix(model, route, creds, p, full, c), render)
    return render_template("index.html", results="train", route=route, job_id=job_id,
                           matrix_error=matrix_error, **ctx)


def route_search(form, creds, ctx):
    src = STATIONS.get(shohoz.norm_station(form.get("from", "")))
    dst = STATIONS.get(shohoz.norm_station(form.get("to", "")))
    seat = form.get("seat", "")
    if not src or not dst:
        return render_template("index.html", error="Pick both stations from the list.", **ctx)
    if src is dst:
        return render_template("index.html", error="From and To must be different stations.", **ctx)
    if seat not in SEAT_TYPES:
        return render_template("index.html", error="Pick a seat type.", **ctx)
    try:
        journey_date = datetime.strptime(form.get("rdate", ""), "%Y-%m-%d").date()
    except ValueError:
        return render_template("index.html", error="Pick a date of journey.", **ctx)

    job_id, matrix_error = None, credentials_problem(creds)
    query = {"from": src["label"], "to": dst["label"], "date": journey_date, "seat": seat}
    if not matrix_error:
        def render(job):
            trains, stats = job["result"]
            shown = [t for t in trains if t["matrix"] or t["direct"]]
            note = None
            if not trains:
                note = f"No train runs {src['label']} → {dst['label']} on {journey_date:%d %b %Y}, or tickets aren't on sale yet."
            elif not shown:
                note = f"None of the {len(trains)} trains on this route sell {seat}."
            return note, render_template("_train_cards.html", trains=shown, query=query,
                                         hidden=len(trains) - len(shown), scan_note=scan_note(stats))

        full = bool(form.get("full"))
        window = parse_window(form)
        job_id = start_job(("route", src["name"], dst["name"], journey_date, seat, full, window), 1,
                           lambda p, c: shohoz.search_route(src["name"], dst["name"], journey_date, seat,
                                                            creds, p, full, c, window),
                           render)
    return render_template("index.html", results="route", query=query, job_id=job_id,
                           matrix_error=matrix_error, **ctx)


@app.route("/", methods=["GET", "POST"])
def index():
    form = request.form or request.args
    ctx = {
        "form": form,
        "mode": form.get("mode", "train"),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "trains": TRAINS,
        "stations": STATION_LIST,
        "seat_types": SEAT_TYPES,
        # ticked by default: only an actual route-search submit can untick it
        "minimize_on": form.get("mode") != "route" or bool(form.get("minimize")),
        "window": {k: form.get(k, v) for k, v in WINDOW_DEFAULTS.items()},
    }
    if request.method == "GET":
        return render_template("index.html", **ctx)

    creds = shohoz.parse_credentials(form.get("token", "")) or env_credentials()
    return (route_search if ctx["mode"] == "route" else train_search)(form, creds, ctx)


@app.route("/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id) or abort(404)
    out = {k: job[k] for k in ("done", "total", "waiting")}
    if job.get("finished"):
        out["error"] = job.get("error")
        if not out["error"]:
            out["error"], out["html"] = job["render"](job)
        out.setdefault("html", "")
    return jsonify(out)


@app.post("/job/<job_id>/cancel")
def job_cancel(job_id):
    job = JOBS.get(job_id) or abort(404)
    job["cancel"].set()  # the worker stops before its next API request
    return jsonify(ok=True)


if __name__ == "__main__":
    # threaded: the progress poll must be served while a job is running
    app.run(debug=True, threaded=True, port=int(os.getenv("PORT", 5000)))
