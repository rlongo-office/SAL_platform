import os
import sys
import requests
import json
from datetime import datetime, date, timedelta
from dotenv import load_dotenv, find_dotenv
from process_json import process_saved_json, process_saved_json_debug

# ─── Configuration ─────────────────────────────────────────────────────────────

# Only MLB
SPORT_LABEL = "mlb"
SPORT_KEY   = "baseball_mlb"

# Times (local) for your 3 daily snapshots
SNAPSHOT_TIMES = ["11:00:00", "17:30:00", "23:30:00"]

# output directory (project-root/output/mlb)
# __file__ is src/main.py, so go up one level to the project root:
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
OUT_DIR  = os.path.join(BASE_DIR, "output", SPORT_LABEL)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def ensure_output_dir():
    os.makedirs(OUT_DIR, exist_ok=True)

def daterange(start_date: date, end_date: date):
    d = start_date
    while d <= end_date:
        yield d
        d += timedelta(days=1)

def fetch_historical_odds(sport_key: str, snapshot_time: str):
    api_key = os.getenv("ODDS_API_KEY")
    url     = f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds"
    params  = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "date": snapshot_time
    }
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"✗ Error @ {snapshot_time}: {resp.text}")
        return None
    return resp.json()

def fetch_and_save_for_date(date_obj: date):
    date_str = date_obj.isoformat()
    print(f"\n=== Fetching {SPORT_LABEL.upper()} odds for {date_str} ===")

    # build list of full ISO timestamps
    snapshots = {}
    for t in SNAPSHOT_TIMES:
        iso_ts = f"{date_str}T{t}Z"
        print(f" → snapshot @ {iso_ts}")
        data = fetch_historical_odds(SPORT_KEY, iso_ts)
        snapshots[iso_ts] = data or {"error": "API call failed"}

    # write out one file per date
    ensure_output_dir()
    out_path = os.path.join(OUT_DIR, f"historical_{SPORT_LABEL}_{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, indent=2)
    print(f"✔ Saved to {out_path}")

# ─── CLI Entrypoint ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: python main.py 1 <start_date> <end_date>")
        print(" e.g. python main.py 1 2024-06-01 2024-06-30")
        sys.exit(1)

    option     = sys.argv[1]
    start_date = sys.argv[2]
    end_date   = sys.argv[3]

    if option != "1":
        print("Invalid option. Only ‘1’ (fetch odds) is supported in this script.")
        sys.exit(1)

    # load .env
    env_file = find_dotenv()
    load_dotenv(env_file)

    # parse dates
    sd = datetime.fromisoformat(start_date).date()
    ed = datetime.fromisoformat(end_date).date()

    # loop days
    for single_date in daterange(sd, ed):
        fetch_and_save_for_date(single_date)

if __name__ == "__main__":
    main()
