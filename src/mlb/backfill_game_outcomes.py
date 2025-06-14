#!/usr/bin/env python3
"""
backfill_mlb_outcomes.py

Fetches MLB game outcomes from the MLB Stats API and upserts them into
mlb_game_outcomes, ensuring status_code, coded_game_state, and detailed_state
are populated. By default only missing or incomplete records are processed;
if --overwrite is specified, all games in the date range are (re-)upserted.

Usage:
    python backfill_game_outcomes.py --start YYYY-MM-DD --end YYYY-MM-DD [--overwrite]
"""
import os
import logging
from datetime import date, datetime, timedelta
import argparse
import requests
import psycopg2
from psycopg2.extras import DictCursor, execute_values
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ─────────────────────────────────────────────────────
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
log_file = os.path.join(
    LOG_DIR,
    f"backfill_outcomes_{datetime.now():%Y%m%d_%H%M%S}.log"
)
logging.basicConfig(
    filename=log_file,
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
API = "https://statsapi.mlb.com/api/v1"

def fetch_mlb_outcomes(target_date: str):
    """
    Pulls only Final games for target_date ±1 day, capturing status fields.
    Returns list of dicts with keys including status_code, coded_game_state, detailed_state.
    """
    sess = requests.Session()

    def games_for(d: date):
        resp = sess.get(
            f"{API}/schedule",
            params={
                "sportId": 1,
                "date": d.isoformat(),
                "hydrate": "teams,linescore,status"
            },
            timeout=10
        )
        resp.raise_for_status()
        out = []
        data = resp.json().get("dates", [])
        for day in data:
            for g in day.get("games", []):
                status = g.get("status", {})
                if status.get("detailedState") != "Final":
                    continue
                st = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
                away_id = g["teams"]["away"]["team"]["id"]
                home_id = g["teams"]["home"]["team"]["id"]
                runs = g["linescore"]["teams"]
                out.append({
                    "game_id":           g["gamePk"],
                    "date_played":       st,
                    "away_team_id":      away_id,
                    "home_team_id":      home_id,
                    "away_score":        runs["away"]["runs"],
                    "home_score":        runs["home"]["runs"],
                    "winner":            "home" if runs["home"]["runs"] > runs["away"]["runs"] else "away",
                    "loser":             "away" if runs["home"]["runs"] > runs["away"]["runs"] else "home",
                    "status_code":       status.get("statusCode"),
                    "coded_game_state":  status.get("codedGameState"),
                    "detailed_state":    status.get("detailedState"),
                })
        return out

    today = date.fromisoformat(target_date)
    all_games = []
    for delta in (-1, 0, 1):
        all_games.extend(games_for(today + timedelta(days=delta)))

    # de-duplicate by game_id, keep last
    unique = {g["game_id"]: g for g in all_games}
    return list(unique.values())

# ─── BACKFILL FUNCTION ────────────────────────────────────────────
def backfill_outcomes(conn, start_dt, end_dt, overwrite=False):
    """
    Upserts MLB game outcomes. If overwrite=False, only missing/incomplete games
    (game_id NULL or status fields NULL) are fetched; if overwrite=True, all
    scheduled games in the date range are processed.
    """
    cur = conn.cursor()

    if overwrite:
        # fetch every scheduled game_id in window
        cur.execute(
            "SELECT game_id, start_time::date FROM msf_mlb.schedule "
            "WHERE start_time::date BETWEEN %s AND %s",
            (start_dt, end_dt)
        )
        missing = cur.fetchall()
    else:
        # fetch only missing or incomplete outcomes
        cur.execute(
            "SELECT s.game_id, s.start_time::date "
            "FROM msf_mlb.schedule AS s "
            "LEFT JOIN msf_mlb.mlb_game_outcomes AS o "
            "  ON o.game_id = s.game_id "
            "WHERE s.start_time::date BETWEEN %s AND %s "
            "  AND (o.game_id IS NULL OR o.status_code IS NULL "
            "       OR o.coded_game_state IS NULL OR o.detailed_state IS NULL)",
            (start_dt, end_dt)
        )
        missing = cur.fetchall()

    if not missing:
        msg = "All outcomes up-to-date" if not overwrite else "No scheduled games in range"
        logger.info(msg + " between %s and %s", start_dt, end_dt)
        return []

    missing_ids = {gid for gid, _ in missing}
    missing_dates = sorted({d for _, d in missing})
    backfilled = []

    for sched_date in missing_dates:
        logger.info("↪ processing date %s", sched_date)
        fetched = []
        for delta in (0, -1, 1):
            iso = (sched_date + timedelta(days=delta)).isoformat()
            games = fetch_mlb_outcomes(iso)
            matches = [g for g in games if g["game_id"] in missing_ids]
            if matches:
                if delta != 0:
                    logger.warning(" used fallback date %s", iso)
                fetched = matches
                break
        if not fetched:
            logger.error(" no API data for %s (±1 day)", sched_date)
            continue

        # upsert all fetched games
        sql = (
            "INSERT INTO msf_mlb.mlb_game_outcomes ("
            "game_id, date_played, away_team_id, home_team_id,"
            "away_score, home_score, winner, loser,"
            "status_code, coded_game_state, detailed_state) VALUES %s "
            "ON CONFLICT (game_id) DO UPDATE SET "
            "date_played       = EXCLUDED.date_played,"
            "away_score        = EXCLUDED.away_score,"
            "home_score        = EXCLUDED.home_score,"
            "winner            = EXCLUDED.winner,"
            "loser             = EXCLUDED.loser,"
            "status_code       = EXCLUDED.status_code,"
            "coded_game_state  = EXCLUDED.coded_game_state,"
            "detailed_state    = EXCLUDED.detailed_state"
        )
        values = [(
            g["game_id"],
            g["date_played"],
            g["away_team_id"],
            g["home_team_id"],
            g["away_score"],
            g["home_score"],
            g["winner"],
            g["loser"],
            g["status_code"],
            g["coded_game_state"],
            g["detailed_state"]
        ) for g in fetched]

        execute_values(cur, sql, values)
        conn.commit()

        for g in fetched:
            backfilled.append(g["game_id"])
            missing_ids.discard(g["game_id"])
            logger.info(" upserted game %s", g["game_id"])

    cur.close()
    return backfilled

# ─── MAIN ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser("Backfill MLB game outcomes")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end",   required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="If set, re-fetch and upsert EVERY scheduled game in the date range"
    )
    args = parser.parse_args()

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.fromisoformat(args.end)
    if end_dt < start_dt:
        parser.error("--end must be on or after --start")

    conn = pg_connect()
    filled = backfill_outcomes(conn, start_dt, end_dt, overwrite=args.overwrite)
    conn.close()

    logger.info("Completed backfill for %d outcomes", len(filled))
