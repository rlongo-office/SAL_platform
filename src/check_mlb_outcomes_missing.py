#!/usr/bin/env python3
import os
import argparse
import requests
import psycopg2
from datetime import date
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & ENV ───────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   "neondb",
    "user":     "neondb_owner",
    "password": os.getenv("DB_PASS", "npg_aKWdUeCXV10c"),
    "host":     "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech",
    "port":     "5432",
    "sslmode":  "require",
}

MLB_API = "https://statsapi.mlb.com/api/v1/schedule"
SPORT_ID = 1  # MLB

# ─── DB CONNECT ──────────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── FETCH ALL FINAL GAME IDs VIA SCHEDULE API ──────────────────────────────────
def fetch_all_final_game_ids(start_date: date, end_date: date) -> set[int]:
    """
    Hits the MLB schedule API once for the full span, then collects all gamePk
    where the game status is 'Final' (or equivalent).
    """
    params = {
        "sportId": SPORT_ID,
        "startDate": start_date.isoformat(),
        "endDate":   end_date.isoformat(),
        "hydrate":   "teams,linescore"
    }
    r = requests.get(MLB_API, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    
    finals = set()
    for day in data.get("dates", []):
        for g in day.get("games", []):
            state = g["status"]["detailedState"]
            if state in ("Final", "Game Over", "Completed Early", "Game Called"):
                finals.add(g["gamePk"])
    return finals

# ─── MAIN ─────────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Find which 2024 MLB game_ids are missing from mlb_game_outcomes"
    )
    p.add_argument("--start", default="2024-03-28",
                   help="Season start date (YYYY-MM-DD)")
    p.add_argument("--end",   default="2024-09-29",
                   help="Season end date (YYYY-MM-DD)")
    args = p.parse_args()

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.fromisoformat(args.end)
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    # 1) fetch all final game IDs from the MLB API
    print(f"Fetching MLB API finals from {start_dt} to {end_dt}…")
    scheduled = fetch_all_final_game_ids(start_dt, end_dt)
    print(f"  → {len(scheduled)} total 'Final' games scheduled")

    # 2) load all your recorded outcomes
    conn = pg_connect()
    cur  = conn.cursor()
    cur.execute("SELECT game_id FROM mlb_game_outcomes")
    have = {row[0] for row in cur.fetchall()}
    conn.close()
    print(f"Loaded {len(have)} game_ids from your mlb_game_outcomes table")

    # 3) compute missing
    missing = sorted(scheduled - have)
    print(f"\nMissing {len(missing)} game_ids:")
    for gid in missing:
        print(f"  • {gid}")

    # 4) compute extras
    extra = sorted(have - scheduled)
    print(f"\nExtra {len(extra)} game_ids in DB but not in API schedule:")
    for gid in extra:
        print(f"  • {gid}")

    # 5) live‐status check for any suspect IDs
    GAME_IDS_TO_CHECK = missing + extra  # or explicitly [747064, 747139]
    print("\nChecking live detailedState for each suspect game_pk:")
    for gid in GAME_IDS_TO_CHECK:
        url = f"https://statsapi.mlb.com/api/v1/game/{gid}/feed/live"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            state = r.json()["gameData"]["status"]["detailedState"]
            print(f"  • Game {gid}: {state}")
        except Exception as e:
            print(f"  • Game {gid}: ERROR fetching status ({e})")

if __name__ == "__main__":
    main()
