#!/usr/bin/env python3
"""
load_mlb_schedule.py

Bulk upserts an MLB season’s schedule into the msf_mlb.schedule table.
Usage:
    python load_mlb_schedule.py --season 2024 [--start YYYY-MM-DD] [--end YYYY-MM-DD]

If --start/--end are omitted, defaults to March 28 and September 29 of the given season.
"""
import os
import argparse
import requests
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv, find_dotenv

# ── CONFIG ─────────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}
API_URL = "https://statsapi.mlb.com/api/v1/schedule"

# ── DB CONNECT ────────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ── FETCH & UPSERT FOR ONE DAY ─────────────────────────────────────────────────
def upsert_day(conn, current_date: date, season: int):
    params = {
        "sportId": 1,
        "date":    current_date.isoformat(),
        "hydrate": "teams"
    }
    resp = requests.get(API_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    with conn.cursor() as cur:
        for day in data.get("dates", []):
            for game in day.get("games", []):
                game_id = game["gamePk"]
                start_ts = game["gameDate"].replace("Z", "+00:00")
                away_id  = game["teams"]["away"]["team"]["id"]
                home_id  = game["teams"]["home"]["team"]["id"]
                cur.execute(
                    """
                    INSERT INTO schedule
                      (game_id, season, away_team_id, home_team_id, start_time)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (game_id) DO UPDATE
                      SET away_team_id = EXCLUDED.away_team_id,
                          home_team_id = EXCLUDED.home_team_id,
                          start_time   = EXCLUDED.start_time
                    """,
                    (game_id, season, away_id, home_id, start_ts)
                )
        conn.commit()

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Upsert MLB schedule for a given season into msf_mlb.schedule"
    )
    parser.add_argument("--season", type=int, required=True,
                        help="Season year (e.g. 2024)")
    parser.add_argument("--start", type=str,
                        help="Start date YYYY-MM-DD, defaults to Mar 28 of season")
    parser.add_argument("--end", type=str,
                        help="End date YYYY-MM-DD, defaults to Sep 29 of season")
    args = parser.parse_args()

    season = args.season
    # default season boundaries
    default_start = date(season, 3, 28)
    default_end   = date(season, 9, 29)
    # parse provided or use defaults
    start_date = date.fromisoformat(args.start) if args.start else default_start
    end_date   = date.fromisoformat(args.end)   if args.end   else default_end
    if end_date < start_date:
        parser.error("--end must be on or after --start")

    conn = pg_connect()
    current = start_date
    print(f"Starting schedule load for season {season} from {start_date} to {end_date}")
    while current <= end_date:
        print(f"  Upserting {current}")
        try:
            upsert_day(conn, current, season)
        except Exception as e:
            print(f"    Error on {current}: {e}")
        current += timedelta(days=1)
    conn.close()
    print("Schedule load complete.")

if __name__ == "__main__":
    main()
