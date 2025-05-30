#!/usr/bin/env python3
"""
backfill_all_mlb_outcomes.py

For every scheduled game-date between --start and --end:
  • Fetches that day’s full schedule (with linescore & status)
  • Upserts into mlb_game_outcomes, including new status_code / coded_game_state / detailed_state
  • Overwrites existing rows if present

Usage:
    python backfill_all_mlb_outcomes.py --start 2024-03-28 --end 2024-09-29
"""
import os
import logging
import argparse
from datetime import date, datetime, timedelta
import requests
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ─────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}

LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, f"backfill_all_outcomes_{datetime.now():%Y%m%d_%H%M%S}.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(console)
logger = logging.getLogger()

# ─── DB CONNECT ─────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── FETCH DAILY SCHEDULE + OUTCOMES ────────────────────────────────────────
API_BASE = "https://statsapi.mlb.com/api/v1"

def fetch_games_on(d: date):
    """
    Returns a list of dicts for every game on date d,
    each with all the fields we need for upsert.
    """
    resp = requests.get(
        f"{API_BASE}/schedule",
        params={
            "sportId": 1,
            "date":     d.isoformat(),
            "hydrate":  "teams,linescore,status"
        },
        timeout=10
    )
    resp.raise_for_status()

    out = []
    for day in resp.json().get("dates", []):
        for g in day.get("games", []):
            # get the actual UTC kickoff
            st = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))

            # teams
            away = g["teams"]["away"]["team"]
            home = g["teams"]["home"]["team"]

            # status fields
            status = g.get("status", {})
            code   = status.get("statusCode")
            cgs    = status.get("codedGameState")
            dgs    = status.get("detailedState")

            # linescore (may be absent for C/P/etc.)
            lines     = g.get("linescore", {}).get("teams", {})
            away_runs = lines.get("away", {}).get("runs")
            home_runs = lines.get("home", {}).get("runs")

            # winner/loser only if we have both scores
            if away_runs is not None and home_runs is not None:
                if   away_runs > home_runs: winner, loser = "away", "home"
                elif home_runs > away_runs: winner, loser = "home", "away"
                else:                       winner, loser = None,   None
            else:
                winner = loser = None

            out.append({
                "game_id":          g["gamePk"],
                "date_played":      st.date(),
                "start_time":       st,
                "away_id":          away["id"],
                "home_id":          home["id"],
                "away_runs":        away_runs,
                "home_runs":        home_runs,
                "winner":           winner,
                "loser":            loser,
                "status_code":      code,
                "coded_game_state": cgs,
                "detailed_state":   dgs,
            })

    logger.info("  → fetched %d games on %s", len(out), d.isoformat())
    return out

# ─── BACKFILL LOOP ───────────────────────────────────────────────────────────
def backfill_all(conn, start_dt: date, end_dt: date):
    cur = conn.cursor()
    # pull all distinct schedule dates
    cur.execute("""
        SELECT DISTINCT start_time::date
          FROM schedule
         WHERE start_time::date BETWEEN %s AND %s
         ORDER BY 1
    """, (start_dt, end_dt))
    dates = [r[0] for r in cur.fetchall()]
    logger.info("Will backfill outcomes on %d dates", len(dates))

    upsert_sql = """
    INSERT INTO mlb_game_outcomes
      (game_id, date_played, away_team_id, home_team_id,
       away_score, home_score, winner, loser,
       status_code, coded_game_state, detailed_state, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (game_id) DO UPDATE
      SET date_played      = EXCLUDED.date_played,
          away_team_id     = EXCLUDED.away_team_id,
          home_team_id     = EXCLUDED.home_team_id,
          away_score       = EXCLUDED.away_score,
          home_score       = EXCLUDED.home_score,
          winner           = EXCLUDED.winner,
          loser            = EXCLUDED.loser,
          status_code      = EXCLUDED.status_code,
          coded_game_state = EXCLUDED.coded_game_state,
          detailed_state   = EXCLUDED.detailed_state;
    """

    for d in dates:
        games = fetch_games_on(d)
        for g in games:
            cur.execute(upsert_sql, (
                g["game_id"], g["date_played"], g["away_id"], g["home_id"],
                g["away_runs"], g["home_runs"], g["winner"], g["loser"],
                g["status_code"], g["coded_game_state"], g["detailed_state"]
            ))
        conn.commit()
        logger.info("  ✓ committed %d upserts for %s", len(games), d)

    cur.close()

# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser("Backfill ALL MLB game outcomes")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end",   required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.fromisoformat(args.end)
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    conn = pg_connect()
    backfill_all(conn, start_dt, end_dt)
    conn.close()
    logger.info("Done backfilling all outcomes")

if __name__ == "__main__":
    main()
