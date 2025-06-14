#!/usr/bin/env python3
"""
fix_mlb_outcomes_and_odds.py

1) Backfill any missing mlb_game_outcomes for schedule dates in range.
2) For those newly added games:
     • Compute open & close snapshots
     • Pull and insert odds snapshots (pre_game / in_play), skipping duplicates.
3) For existing outcomes, verify we have closing and opening odds,
   and log any gaps for manual review.

Usage:
    python fix_mlb_outcomes_and_odds.py --start 2024-01-01 --end 2024-12-31
"""
import os
import argparse
import logging
from datetime import datetime, timedelta
import subprocess
import requests
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2.errors import UniqueViolation
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & LOGGING ─────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY    = "baseball_mlb"
DB = dict(
    dbname   = os.getenv("DB_NAME",   "neondb"),
    user     = os.getenv("DB_USER",   "neondb_owner"),
    password = os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT",   "5432"),
    sslmode  = "require",
)

LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "fix_mlb.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())

def pg_connect():
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── STEP 1: BACKFILL OUTCOMES ─────────────────────────────────────────────────
def backfill_outcomes(conn, start_dt, end_dt):
    """
    Find schedule.game_id between start/end not in mlb_game_outcomes,
    and invoke your outcome‐loader to insert them.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT s.game_id, s.start_time::date
          FROM msf_mlb.schedule s
     LEFT JOIN msf_mlb.mlb_game_outcomes o
            ON o.game_id = s.game_id
         WHERE s.start_time::date BETWEEN %s AND %s
           AND o.game_id IS NULL
    """, (start_dt, end_dt))
    missing = cur.fetchall()
    logger.info("Found %d missing outcome(s) to backfill", len(missing))

    for game_id, sched_date in missing:
        logger.info("  ↪ backfilling outcomes for game %s on %s", game_id, sched_date)
        # TODO: replace the following with a direct function call or subprocess
        # that runs your combined_outcome_api_mlb_capture for that date.
        #
        # e.g.:
        # subprocess.run([
        #     "python", "combined_outcome_api_mlb_capture.py",
        #     "--date", sched_date.isoformat()
        # ], check=True)
        #
        # Or import and call an insert_outcomes_for_date(sched_date) function.
    conn.commit()
    return [g for g,_ in missing]

# ─── STEP 2: SNAP + ODDS FOR NEW GAMES ────────────────────────────────────────
def compute_snapshots_for_game(start_time):
    """
    Returns two datetimes:
      close ≈ start_time - 15m
      open  ≈ midpoint of [start_time - 30h, start_time - 18h]
    """
    close_ts = start_time - timedelta(minutes=15)
    open_lower = start_time - timedelta(hours=30)
    open_upper = start_time - timedelta(hours=18)
    open_ts = open_lower + (open_upper - open_lower) / 2
    return open_ts, close_ts

def fetch_and_insert_odds(conn, game_id, snapshot_ts, segment):
    iso = snapshot_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("    ↪ [%s] odds for %s @ %s", segment, game_id, iso)
    resp = requests.get(
        f"https://api.the-odds-api.com/v4/historical/sports/{SPORT_KEY}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
            "date": iso
        }
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    cur = conn.cursor()
    total = 0

    for rec in data:
        # only care about our specific game
        if str(rec["id"]) != str(game_id):
            continue

        # upsert book
        for book in rec.get("bookmakers", []):
            cur.execute("SELECT id FROM msf_mlb.books WHERE name = %s", (book["title"],))
            row = cur.fetchone()
            if row:
                book_id = row[0]
            else:
                cur.execute("""
                    INSERT INTO msf_mlb.books(name,region,is_online,is_las_vegas)
                    VALUES (%s,NULL,TRUE,FALSE)
                    RETURNING id
                """, (book["title"],))
                book_id = cur.fetchone()[0]

            for m in book.get("markets", []):
                try:
                    # insert game_odds
                    cur.execute("""
                        INSERT INTO msf_mlb.game_odds
                         (odds_api_game_id,book_id,as_of_time,game_time,game_segment,odds_type,mlb_game_pk)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                    """, (
                        rec["id"],
                        book_id,
                        snapshot_ts,
                        datetime.fromisoformat(rec["commence_time"].replace("Z","+00:00")),
                        segment,
                        m["key"],
                        game_id
                    ))
                    go_id = cur.fetchone()[0]
                except UniqueViolation:
                    conn.rollback()
                    logger.debug("      • duplicate snapshot skipped: %s/%s/%s/%s",
                                 rec["id"], book_id, snapshot_ts, m["key"])
                    continue

                # insert outcomes
                for o in m.get("outcomes", []):
                    if m["key"] in ("h2h","spreads"):
                        ot = "home" if o["name"] == rec["home_team"] else "away"
                    else:
                        ot = o["name"].lower()
                    cur.execute("""
                        INSERT INTO msf_mlb.odds
                          (game_odds_id,outcome_type,odds_american,spread,over_under)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (
                        go_id,
                        ot,
                        o.get("price"),
                        o.get("point") if m["key"] == "spreads" else None,
                        o.get("point") if m["key"] == "totals"  else None
                    ))
                    total += 1

    conn.commit()
    logger.info("      • inserted %d odds rows for %s [%s]", total, game_id, segment)

# ─── STEP 3: VERIFY EXISTING OUTCOMES ─────────────────────────────────────────
def verify_existing_odds(conn, start_dt, end_dt):
    """
    For each mlb_game_outcomes in that date range:
      - require ≥1 game_odds within ±1h of start_time (closing)
      - and ≥1 18–30h before (opening)
    """
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("""
        SELECT o.game_id, s.start_time
          FROM msf_mlb.mlb_game_outcomes o
          JOIN msf_mlb.schedule s ON s.game_id = o.game_id
         WHERE s.start_time::date BETWEEN %s AND %s
    """, (start_dt, end_dt))

    for game_id, start_time in cur.fetchall():
        # closing window
        cur.execute("""
            SELECT 1 FROM msf_mlb.game_odds
             WHERE mlb_game_pk=%s
               AND as_of_time BETWEEN %s AND %s
        """, (
            game_id,
            start_time - timedelta(hours=1),
            start_time + timedelta(hours=1)
        ))
        if not cur.fetchone():
            logger.warning("No closing odds found for %s", game_id)

        # opening window
        cur.execute("""
            SELECT 1 FROM msf_mlb.game_odds
             WHERE mlb_game_pk=%s
               AND as_of_time BETWEEN %s AND %s
        """, (
            game_id,
            start_time - timedelta(hours=30),
            start_time - timedelta(hours=18)
        ))
        if not cur.fetchone():
            logger.warning("No opening odds found for %s", game_id)

def main():
    p = argparse.ArgumentParser("Fix MLB outcomes & odds")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end",   required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    start_dt = datetime.fromisoformat(args.start).date()
    end_dt   = datetime.fromisoformat(args.end).date()
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    conn = pg_connect()
    # 1) Backfill missing outcomes
    new_games = backfill_outcomes(conn, start_dt, end_dt)

    # 2) For each newly added, pull open & close odds
    for game_id in new_games:
        cur = conn.cursor()
        cur.execute("SELECT start_time FROM msf_mlb.schedule WHERE game_id=%s", (game_id,))
        start_time = cur.fetchone()[0]
        open_ts, close_ts = compute_snapshots_for_game(start_time)

        logger.info("Snapshots for %s: open=%s, close=%s",
                    game_id, open_ts, close_ts)

        fetch_and_insert_odds(conn, game_id, open_ts,  "pre_game")
        fetch_and_insert_odds(conn, game_id, close_ts, "pre_game")

    # 3) Verify existing
    verify_existing_odds(conn, start_dt, end_dt)
    conn.close()
    logger.info("=== Fix run complete ===")

if __name__ == "__main__":
    main()
