#!/usr/bin/env python3
"""
backfill_mlb_outcomes.py

Fetches missing MLB game outcomes from the MLB Stats API and upserts them into
mlb_game_outcomes. Optionally (PROCESS_ODDS) can trigger odds processing, but
by default it focuses only on outcomes.

Usage:
    python backfill_mlb_outcomes.py --start 2024-07-01 --end 2024-09-29
"""
import os
import logging
from datetime import date, datetime, timedelta
import argparse
import requests
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ─────────────────────────────────────────────────────
# If you later add odds-processing logic, guard it with this flag:
PROCESS_ODDS = False

load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}

# ─── LOGGING ────────────────────────────────────────────────────
LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"backfill_outcomes_{datetime.now():%Y%m%d_%H%M%S}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ─── DB CONNECT ─────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── MLB OUTCOMES FETCHER ────────────────────────────────────────
def fetch_mlb_outcomes(target_date: str):
    """
    Pulls final MLB game outcomes for target_date ±1 day.
    Returns a list of dicts with keys:
      game_id, away_id, home_id, away_runs, home_runs, start_time
    """
    API = "https://statsapi.mlb.com/api/v1"
    sess = requests.Session()
    def games_for(d):
        resp = sess.get(
            f"{API}/schedule",
            params={
                "sportId": 1,
                "date": d.isoformat(),
                "hydrate": "teams,linescore"
            },
            timeout=10
        )
        resp.raise_for_status()
        out = []
        for day in resp.json().get("dates", []):
            for g in day.get("games", []):
                if g.get("status", {}).get("detailedState") != "Final":
                    continue
                st = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
                away = g["teams"]["away"]["team"]
                home = g["teams"]["home"]["team"]
                lines = g["linescore"]["teams"]
                out.append({
                    "game_id":   g["gamePk"],
                    "date_played": st.date(),
                    "away_id":   away["id"],
                    "home_id":   home["id"],
                    "away_runs": lines["away"]["runs"],
                    "home_runs": lines["home"]["runs"],
                    "start_time": st,
                })
        return out

    d0 = date.fromisoformat(target_date)
    games = []
    for off in (-1, 0, 1):
        games.extend(games_for(d0 + timedelta(days=off)))
    logger.info("Fetched %d final games around %s", len(games), target_date)
    return games

# ─── BACKFILL OUTCOMES ───────────────────────────────────────────
from datetime import timedelta

def backfill_outcomes(conn, start_dt, end_dt):
    cur = conn.cursor()

    # 1) find exactly which game_ids are missing
    cur.execute("""
        SELECT s.game_id, s.start_time::date
          FROM schedule s
     LEFT JOIN mlb_game_outcomes o ON o.game_id = s.game_id
         WHERE s.start_time::date BETWEEN %s AND %s
           AND o.game_id IS NULL
    """, (start_dt, end_dt))
    missing = cur.fetchall()
    missing_ids = {gid for gid, _ in missing}
    if not missing_ids:
        logger.info("No missing outcomes between %s and %s", start_dt, end_dt)
        return []

    # one API call per unique calendar date
    missing_dates = sorted({d for _, d in missing})
    logger.info("Found %d missing outcome(s) on %d date(s) to backfill",
                len(missing), len(missing_dates))

    backfilled = []
    for sched_date in missing_dates:
        logger.info("↪ backfilling outcomes around %s", sched_date)

        # try date offsets 0, -1, +1
        fetched = []
        for delta in (0, -1, 1):
            iso = (sched_date + timedelta(days=delta)).isoformat()
            games = fetch_mlb_outcomes(iso)  # your existing API helper

            # match on whichever field the API really uses
            def pk_of(g):
                return g.get("game_id") or g.get("gamePk") or g.get("game_pk")

            matched = [g for g in games if pk_of(g) in missing_ids]
            if matched:
                if delta != 0:
                    logger.warning("   ⚠️ used fallback date %s for schedule date %s", iso, sched_date)
                fetched = matched
                break

        if not fetched:
            logger.error("   ❌ no API data found for any date around %s", sched_date)
            continue

        # upsert only the truly missing ones
        for g in fetched:
            game_pk    = pk_of(g)
            date_str   = sched_date  # or g["start_time"].date()
            away_id    = g["away_id"]
            home_id    = g["home_id"]
            away_runs  = g["away_runs"]
            home_runs  = g["home_runs"]
            winner     = "away" if away_runs > home_runs else "home"
            loser      = "home" if winner == "away" else "away"

            cur.execute("""
                INSERT INTO mlb_game_outcomes
                  (game_id, date_played, away_team_id, home_team_id,
                   away_score, home_score, winner, loser, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (game_id) DO UPDATE
                  SET away_score = EXCLUDED.away_score,
                      home_score = EXCLUDED.home_score,
                      winner     = EXCLUDED.winner,
                      loser      = EXCLUDED.loser
            """, (
                game_pk, date_str, away_id, home_id,
                away_runs, home_runs, winner, loser
            ))
            logger.info("   ✓ upserted outcome for game %s", game_pk)
            backfilled.append(game_pk)
            missing_ids.discard(game_pk)

    conn.commit()
    cur.close()
    return backfilled

# ─── MAIN ───────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser("Backfill MLB game outcomes")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end",   required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.fromisoformat(args.end)
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    conn = pg_connect()
    filled = backfill_outcomes(conn, start_dt, end_dt)
    conn.close()

    logger.info("Completed backfill for %d outcomes", len(filled))

if __name__ == "__main__":
    main()
