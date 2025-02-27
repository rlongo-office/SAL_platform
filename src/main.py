import os
import sys
import requests
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv, find_dotenv
from process_json import process_saved_json, process_saved_json_debug  # Import the new module

print("Current working directory:", os.getcwd())
env_file = find_dotenv()
load_dotenv(env_file)

def fetch_historical_odds(sport_key, snapshot_time):
    """
    Fetch the historical odds snapshot for a given sport_key at or before snapshot_time (ISO8601).
    For example, snapshot_time = '2024-10-04T12:00:00Z'.
    """
    api_key = os.getenv("ODDS_API_KEY")
    base_url = f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds"

    params = {
        "apiKey": api_key,
        "regions": "us",                      # or multiple regions, e.g. "us,uk"
        "markets": "h2h,spreads,totals",      # add more if needed (costly!)
        "oddsFormat": "american",
        "date": snapshot_time                 # The key: fetch snapshot at/near this time
    }

    resp = requests.get(base_url, params=params)
    if resp.status_code != 200:
        print(f"Error fetching historical odds for {sport_key} at {snapshot_time}: {resp.text}")
        return None
    
    return resp.json()

def fetch_and_save_odds(date_str):
    """ Fetch odds from the API and save them to a JSON file. """
    print(f"Fetching 4-hour window of historical odds for date: {date_str}")

    start_dt = datetime.fromisoformat(f"{date_str}T12:00:00")
    end_dt   = datetime.fromisoformat(f"{date_str}T16:00:00")
    increment = timedelta(hours=1)

    snapshot_times = []
    current_dt = start_dt
    while current_dt <= end_dt:
        iso_str = current_dt.isoformat(timespec='seconds') + "Z"
        snapshot_times.append(iso_str)
        current_dt += increment

    sports = {
        "ncaaf": "americanfootball_ncaaf",
        "nfl":   "americanfootball_nfl"
    }

    snapshots = {}

    for stime in snapshot_times:
        print(f"\n=== Fetching snapshot at {stime} ===")
        snapshots[stime] = {}
        for label, sport_key in sports.items():
            print(f" - Sport: {label} ({sport_key}) at {stime}")
            data = fetch_historical_odds(sport_key, stime)
            if data is None:
                snapshots[stime][label] = {"error": "API call failed"}
            else:
                snapshots[stime][label] = data

    out_filename = f"historical_4hr_window_{date_str}.json"
    print(f"\nWill write all snapshots to {out_filename}")
    with open(out_filename, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, indent=2)

    print(f"Saved data to {out_filename}")

def main():
    """ Parse command-line arguments and execute the corresponding functionality. """
    if len(sys.argv) < 2:
        print("Usage: python main.py <option> [date] [end_date]")
        print("Options:")
        print("  1 <date>       Fetch historical odds and save to JSON")
        print("  2 <date>       Read saved JSON and insert data into PostgreSQL")
        sys.exit(1)

    option = sys.argv[1]
    print(f"sys.argv: {sys.argv}")

    # Default date if not provided
    date_str = sys.argv[2] if len(sys.argv) > 2 else "2024-10-04"

    if option == "1":
        fetch_and_save_odds(date_str)

    elif option == "2":
        # Call the new module for processing saved JSON into PostgreSQL
        json_filename = f"historical_4hr_window_{date_str}.json"
        process_saved_json(json_filename)

    else:
        print("Invalid option. Use 1 to fetch odds, 2 to process saved JSON.")

if __name__ == "__main__":
    main()
