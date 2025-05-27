#!/usr/bin/env python3
"""
pull_mlb_odds.py

Fetches dynamic MLB odds (opening/mid/closing and in-play) using:
  • The schedule table for open/mid/close windows
  • Bucketed snapshot times to minimize API calls
  • Robust error handling and detailed logging

Usage:
    python pull_mlb_odds.py --start 2024-07-01 --end 2024-09-29
"""
import os
import argparse
import logging
from datetime import datetime, timedelta
import requests
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & LOGGING ─────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY    = "baseball_mlb"

DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}

# Place logs at project_root/logs
LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"odds_capture_{datetime.now():%Y%m%d_%H%M%S}.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(console)

# Canonical map for team name mismatches
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
    if not isinstance(name, str):
        return ""
    lw = name.strip().lower()
    return _CANON.get(lw, lw)

# ─── DB CONNECT ───────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── LOAD SCHEDULE & TEAM MAP ─────────────────────────────────────────────────
def load_schedule_and_teams(start_date, end_date):
    logger.info("Loading schedule and teams for %s → %s", start_date, end_date)
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("""
        SELECT game_id, away_team_id, home_team_id, start_time
          FROM schedule
         WHERE start_time::date BETWEEN %s AND %s
         ORDER BY start_time
    """, (start_date, end_date))
    schedule = cur.fetchall()
    logger.info("  → %d scheduled games", len(schedule))

    cur.execute("""
        SELECT id, LOWER(locationname || ' ' || teamname) AS name_lc
          FROM teams
    """)
    teams_map = {canonicalize(r["name_lc"]): r["id"] for r in cur.fetchall()}
    logger.info("  → built team map with %d entries", len(teams_map))
    conn.close()
    return schedule, teams_map

# ─── COMPUTE OPEN/MID/CLOSE WINDOWS ─────────────────────────────────────────
def compute_windows(schedule):
    logger.info("Computing open/mid/close windows…")
    prev_end = {}
    snaps = []
    for row in schedule:
        gid, away_id, home_id, start = (
            row["game_id"], row["away_team_id"],
            row["home_team_id"], row["start_time"]
        )
        ends = [prev_end.get(t) for t in (away_id, home_id) if prev_end.get(t)]
        base = max(ends) if ends else (start - timedelta(hours=12))
        t_open  = base + timedelta(minutes=15)
        t_close = start - timedelta(minutes=15)
        t_mid   = t_open + (t_close - t_open) / 2
        snaps += [(gid,"open",t_open),(gid,"mid",t_mid),(gid,"close",t_close)]
        finish = start + timedelta(hours=3)
        prev_end[away_id] = finish
        prev_end[home_id] = finish

    logger.info("  → computed %d raw snapshots", len(snaps))
    return snaps

# ─── COALESCE INTO BUCKETS ──────────────────────────────────────────────────
def coalesce_snap_times(snaps, bucket_hours=4):
    """
    Floor each timestamp into a bucket_hours window.
    Returns (calls, snap_map).
    """
    logger.info("Coalescing %d raw snapshots into %dh buckets…", len(snaps), bucket_hours)
    snapshot_map = {}
    for gid, seg, ts in snaps:
        floored_hour = (ts.hour // bucket_hours) * bucket_hours
        key = ts.replace(hour=floored_hour, minute=0, second=0, microsecond=0)
        snapshot_map.setdefault(key, []).append((gid, seg))

    calls = sorted(snapshot_map.keys())
    logger.info("  → %d unique snapshots after bucketing", len(calls))
    return calls, snapshot_map

# ─── FETCH ODDS ───────────────────────────────────────────────────────────────
def fetch_historical_odds(iso_ts):
    url = f"https://api.the-odds-api.com/v4/historical/sports/{SPORT_KEY}/odds"
    resp = requests.get(url, params={
        "apiKey":     ODDS_API_KEY,
        "regions":    "us",
        "markets":    "h2h,spreads,totals",
        "oddsFormat": "american",
        "date":       iso_ts,
    })
    resp.raise_for_status()
    return resp.json()

# ─── UPSERT BOOK ─────────────────────────────────────────────────────────────
def upsert_book(cur, title):
    """
    Lookup or insert a book and return its ID.
    """
    cur.execute("SELECT id FROM msf_mlb.books WHERE name = %s", (title,))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        """
        INSERT INTO msf_mlb.books (name, region, is_online, is_las_vegas)
             VALUES (%s, NULL, TRUE, FALSE)
        RETURNING id
        """,
        (title,)
    )
    return cur.fetchone()[0]

# ─── PROCESS & INSERT ─────────────────────────────────────────────────────────
def load_odds_snapshot(conn, snap_ts, teams_map, seg_map):
    """
    Pull one historical snapshot of odds at snap_ts,
    match each record back to schedule.game_id, and insert into game_odds + odds.
    Upserts game_odds on (odds_api_game_id, book_id, as_of_time, odds_type).
    """
    iso = snap_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("→ snapshot %s", iso)

    try:
        data = fetch_historical_odds(iso)
    except Exception as e:
        logger.error("HTTP error fetching odds @%s: %s", iso, e)
        return

    cur = conn.cursor()
    inserted = skipped_name = skipped_sched = 0

    # map this timestamp back to its open/mid/close segment per game
    lookup = {gid: seg for gid, seg in seg_map.get(snap_ts, [])}

    for rec in data.get("data", []):
        home_can, away_can = canonicalize(rec["home_team"]), canonicalize(rec["away_team"])
        home_id, away_id = teams_map.get(home_can), teams_map.get(away_can)
        if not home_id or not away_id:
            skipped_name += 1
            logger.warning("Unknown teams: %s / %s", rec["away_team"], rec["home_team"])
            continue

        try:
            game_time = datetime.fromisoformat(rec["commence_time"].replace("Z", "+00:00"))
        except Exception as e:
            logger.error("Bad commence_time %s: %s", rec.get("commence_time"), e)
            continue

        cur.execute(
            """
            SELECT game_id
              FROM msf_mlb.schedule
             WHERE away_team_id = %s
               AND home_team_id = %s
               AND ABS(EXTRACT(EPOCH FROM (start_time - %s))) < 3600
             ORDER BY ABS(EXTRACT(EPOCH FROM (start_time - %s)))
             LIMIT 1
            """,
            (away_id, home_id, game_time, game_time)
        )
        row = cur.fetchone()
        if not row:
            skipped_sched += 1
            logger.warning("No schedule match for %s vs %s @ %s",
                           rec["away_team"], rec["home_team"], rec["commence_time"])
            continue

        mlb_game_pk = row[0]
        segment = "in_play" if snap_ts >= game_time else lookup.get(mlb_game_pk, "pre_game")
        if segment == "pre_game" and mlb_game_pk not in lookup:
            logger.info("  → defaulting to pre_game for %s @ %s", mlb_game_pk, iso)

        for book in rec.get("bookmakers", []):
            book_id = upsert_book(cur, book["title"])

            for m in book.get("markets", []):
                # upsert into game_odds
                cur.execute(
                    """
                    INSERT INTO msf_mlb.game_odds
                      (odds_api_game_id, book_id, as_of_time, game_time,
                       game_segment, odds_type, mlb_game_pk)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (odds_api_game_id, book_id, as_of_time, odds_type)
                      DO NOTHING
                    RETURNING id
                    """,
                    (rec["id"], book_id, snap_ts, game_time,
                     segment, m["key"], mlb_game_pk)
                )
                insert_row = cur.fetchone()
                if insert_row:
                    go_id = insert_row[0]
                else:
                    # already existed — fetch its id
                    cur.execute(
                        """
                        SELECT id
                          FROM msf_mlb.game_odds
                         WHERE odds_api_game_id = %s
                           AND book_id           = %s
                           AND as_of_time        = %s
                           AND odds_type         = %s
                        """,
                        (rec["id"], book_id, snap_ts, m["key"])
                    )
                    go_id = cur.fetchone()[0]

                for o in m.get("outcomes", []):
                    if m["key"] in ("h2h", "spreads"):
                        ot = "home" if o["name"] == rec["home_team"] else "away"
                    else:
                        ot = o["name"].lower()

                    cur.execute(
                        """
                        INSERT INTO msf_mlb.odds
                          (game_odds_id, outcome_type, odds_american, spread, over_under)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            go_id, ot, o.get("price"),
                            o.get("point") if m["key"] == "spreads" else None,
                            o.get("point") if m["key"] == "totals"  else None
                        )
                    )
                    inserted += 1

    conn.commit()
    cur.close()

    logger.info(
        "  → inserted=%d, skipped_name=%d, skipped_sched=%d",
        inserted, skipped_name, skipped_sched
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser("Batch MLB odds capture via schedule")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end",   required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    start_dt = datetime.fromisoformat(args.start).date()
    end_dt   = datetime.fromisoformat(args.end).date()
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    logger.info("=== Starting odds capture %s → %s ===", start_dt, end_dt)
    schedule, teams_map = load_schedule_and_teams(start_dt, end_dt)
    snaps              = compute_windows(schedule)
    calls, seg_map     = coalesce_snap_times(snaps, bucket_hours=4)

    logger.info("  → total snapshots to fire: %d", len(calls))
    conn = pg_connect()
    errors = 0
    for ts in calls:
        try:
            load_odds_snapshot(conn, ts, teams_map, seg_map)
        except Exception:
            errors += 1
            logger.exception("Snapshot load failed for %s", ts)
            conn.rollback()
            if errors >= 5:
                logger.critical("Too many errors (%d), aborting run.", errors)
                break
    conn.close()
    logger.info("=== Odds capture finished ===")

if __name__ == "__main__":
    main()
