#!/usr/bin/env python3
import os
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

# ─── ENV & DB SETUP ───────────────────────────────────────────
load_dotenv()  # expects your .env with DB_NAME, DB_USER, etc.
DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── CANONICALIZATION ─────────────────────────────────────────
_CANON = {
    "arizona diamondbacks": "phoenix d-backs",
    "colorado rockies":     "denver rockies",
    "los angeles angels":   "anaheim angels",
    "minnesota twins":      "minneapolis twins",
    "new york mets":        "flushing mets",
    "new york yankees":     "bronx yankees",
    "oakland athletics":    "sacramento athletics",
    "tampa bay rays":       "tampa rays",
    "texas rangers":        "arlington rangers",
}
def canonicalize(name: str) -> str:
    lw = name.strip().lower()
    return _CANON.get(lw, lw)

# ─── MAIN LOOKUP ───────────────────────────────────────────────
def find_game_ids(warnings_file):
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=DictCursor)

    with open(warnings_file, encoding="utf-16") as f:
        for line in f:
            # split off the part after "No schedule match for "
            _, rest = line.split("No schedule match for ", 1)
            teams, iso = rest.rsplit(" @ ", 1)
            iso = iso.strip()[:-1] + "+00:00"
            away_raw, home_raw = teams.split(" vs ")

            dt = datetime.fromisoformat(iso)
            away_can = canonicalize(away_raw)
            home_can = canonicalize(home_raw)

            # 1) strict ±2h match
            cur.execute("""
                SELECT s.game_id, s.start_time
                  FROM schedule s
                  JOIN teams ta ON ta.id = s.away_team_id
                  JOIN teams th ON th.id = s.home_team_id
                 WHERE LOWER(ta.locationname||' '||ta.teamname) = %s
                   AND LOWER(th.locationname||' '||th.teamname) = %s
                   AND ABS(EXTRACT(EPOCH FROM (s.start_time - %s))) < 7200
                 LIMIT 1
            """, (away_can, home_can, dt))
            row = cur.fetchone()

            # 2) fallback: date‐only ±1 day
            if not row:
                cur.execute("""
                    SELECT s.game_id, s.start_time
                      FROM schedule s
                      JOIN teams ta ON ta.id = s.away_team_id
                      JOIN teams th ON th.id = s.home_team_id
                     WHERE LOWER(ta.locationname||' '||ta.teamname) = %s
                       AND LOWER(th.locationname||' '||th.teamname) = %s
                       AND s.start_time::date
                           BETWEEN %s::date - INTERVAL '1 day'
                               AND %s::date + INTERVAL '1 day'
                     LIMIT 1
                """, (away_can, home_can, dt.date(), dt.date()))
                row = cur.fetchone()
                if row:
                    # warn that we used the ±1d fallback
                    print(f"⚠️  Fallback ±1d for {away_raw} vs {home_raw} @ {iso[:-6]}Z →")
            
            game_id = row["game_id"] if row else None
            start_ts = row["start_time"] if row else None

            print(f"{iso[:-6]}Z  {away_raw} vs {home_raw}  →  "
                  f"{game_id or 'NOT FOUND'}  ({start_ts})")

    cur.close()
    conn.close()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("warnings_file", help="Path to warnings.txt")
    args = p.parse_args()
    find_game_ids(args.warnings_file)
