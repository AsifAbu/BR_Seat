"""Regenerate data/trains.json by probing the (unauthenticated) train-routes API.

There is no "list trains" endpoint, so we try every intercity code and keep
the ones that return a route. Run occasionally:  python tools/build_trains.py
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

URL = "https://railspaapi.shohoz.com/v1.0/web/train-routes"
CODES = range(700, 1000)
OUT = Path(__file__).resolve().parent.parent / "data" / "trains.json"


def fetch(code, day):
    for attempt in range(3):
        r = requests.post(URL, json={"model": str(code), "departure_date_time": day.isoformat()}, timeout=20)
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        return (r.json().get("data") or {}) if r.ok else {}
    return {}


def main():
    # Probe two consecutive days so a train that is off on one of them still shows up.
    days = [date.today() + timedelta(days=3), date.today() + timedelta(days=4)]
    trains = []
    for code in CODES:
        data = {}
        for d in days:
            data = fetch(code, d)
            if data.get("train_name"):
                break
            time.sleep(0.4)
        if data.get("train_name") and data.get("routes"):
            stops = data["routes"]
            trains.append({
                "code": str(code),
                "name": data["train_name"],
                "origin": stops[0]["city"].replace("_", " "),
                "destination": stops[-1]["city"].replace("_", " "),
                "departure": (stops[0].get("departure_time") or "").replace(" BST", ""),
                "days": data.get("days") or [],
                "stops": [st["city"] for st in stops],  # raw API names, e.g. "Biman_Bandar"
            })
            print(f"{code} {data['train_name']}: {trains[-1]['origin']} → {trains[-1]['destination']}", flush=True)
        time.sleep(0.4)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(trains, ensure_ascii=False, indent=1))
    print(f"saved {len(trains)} trains to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
