#!/usr/bin/env python3
"""
pull_mlb_odds_filtered.py (refactored)

Fetches dynamic MLB odds (opening/mid/closing and in-play) for only missing games:
  • Auto-detects schedule games (by date) with no odds in game_odds
  • Pads schedule window by configurable days
  • Buckets snapshots into 4h windows
  • Loosens schedule-match tolerance and falls back to date-only match
  • Skips any game not listed in OVERRIDE_GAME_IDS
  • Robust error handling and detailed logging

Usage:
    python pull_mlb_odds_filtered.py --start 2024-07-01 --end 2024-09-29 \
        [--tolerance 7200] [--date-pad 1]
"""
import os
import argparse
import logging
from datetime import datetime, timedelta, date
import requests
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv

# ─── MANUAL GAME-ID OVERRIDE ──────────────────────────────────────────────────
# If non-empty, only these mlb_game_pk values will be processed.
OVERRIDE_GAME_IDS = {
    746658, 746490, 746809, 746891, 746572, 746320, 746481,
    745751, 745659, 747039, 747121, 745816, 745891, 745157, 746773,
    745310, 746602, 745713, 745708,
    # “warning” games:
    746755, 746429, 746430, 745451, 746986,
    746343, 746345, 745775, 746905, 746256, 746903,
    746826, 746421, 746417
}

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

# ─── NAME CANONICALIZATION ───────────────────────────────────────────────────
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

# ─── LOAD SCHEDULE & TEAMS (WITH PADDING) ─────────────────────────────────────
def load_schedule_and_teams(start_date, end_date, pad_days):
    load_start = start_date - timedelta(days=pad_days)
    load_end   = end_date   + timedelta(days=pad_days)
    logger.info("Loading schedule [%s → %s] (padded %d days)", load_start, load_end, pad_days)
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute(
        """
        SELECT game_id, away_team_id, home_team_id, start_time
          FROM schedule
         WHERE start_time::date BETWEEN %s AND %s
         ORDER BY start_time
        """,
        (load_start, load_end)
    )
    schedule = cur.fetchall()
    logger.info("  → %d scheduled games (including padding)", len(schedule))

    cur.execute(
        """
        SELECT id, LOWER(locationname || ' ' || teamname) AS name_lc
          FROM teams
        """
    )
    teams_map = {canonicalize(r[1]): r[0] for r in cur.fetchall()}
    logger.info("  → built team map with %d entries", len(teams_map))
    conn.close()
    return schedule, teams_map

# ─── COMPUTE WINDOWS ─────────────────────────────────────────────────────────
def compute_windows(schedule):
    prev_end = {}
    snaps = []
    for row in schedule:
        gid, away_id, home_id, start = row["game_id"], row["away_team_id"], row["home_team_id"], row["start_time"]
        ends = [prev_end[t] for t in (away_id, home_id) if t in prev_end]
        base = max(ends) if ends else (start - timedelta(hours=12))
        t_open  = base + timedelta(minutes=15)
        t_mid   = t_open + ((start - timedelta(minutes=15) - t_open) / 2)
        t_close = start - timedelta(minutes=15)
        snaps.extend([(gid, "open", t_open), (gid, "mid", t_mid), (gid, "close", t_close)])
        finish = start + timedelta(hours=3)
        prev_end[away_id] = finish
        prev_end[home_id] = finish
    logger.info("  → computed %d raw snapshots", len(snaps))
    return snaps

# ─── BUCKET SNAPSHOTS ─────────────────────────────────────────────────────────
def coalesce_snap_times(snaps, bucket_hours=4):
    snapshot_map = {}
    for gid, seg, ts in snaps:
        floored = ts.replace(
            hour=(ts.hour // bucket_hours) * bucket_hours,
            minute=0, second=0, microsecond=0
        )
        snapshot_map.setdefault(floored, []).append((gid, seg))
    calls = sorted(snapshot_map.keys())
    logger.info("  → %d unique snapshots after %dh bucketing", len(calls), bucket_hours)
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

# ─── LOAD ONE SNAPSHOT ────────────────────────────────────────────────────────
def load_odds_snapshot(conn, snap_ts, teams_map, seg_map, tolerance):
    iso = snap_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("→ snapshot %s", iso)
    try:
        data = fetch_historical_odds(iso)
    except Exception as e:
        logger.error("HTTP error fetching odds @%s: %s", iso, e)
        return

    cur = conn.cursor()
    inserted = skipped_name = skipped_sched = 0
    lookup = {gid: seg for gid, seg in seg_map.get(snap_ts, [])}

    for rec in data.get("data", []):
        home_id = teams_map.get(canonicalize(rec["home_team"]))
        away_id = teams_map.get(canonicalize(rec["away_team"]))
        if not home_id or not away_id:
            skipped_name += 1
            logger.warning("Unknown teams: %s / %s", rec["away_team"], rec["home_team"])  
            continue
        try:
            game_time = datetime.fromisoformat(rec["commence_time"].replace("Z", "+00:00"))
        except Exception as e:
            logger.error("Bad commence_time %s: %s", rec.get("commence_time"), e)
            continue

        # strict time match
        cur.execute(
            """
            SELECT game_id
              FROM msf_mlb.schedule
             WHERE away_team_id = %s
               AND home_team_id = %s
               AND ABS(EXTRACT(EPOCH FROM (start_time - %s))) < %s
             ORDER BY ABS(EXTRACT(EPOCH FROM (start_time - %s)))
             LIMIT 1
            """,
            (away_id, home_id, game_time, tolerance, game_time)
        )
        sched = cur.fetchone()
        if sched:
            mlb_game_pk = sched[0]
        else:
            # fallback: date-only
            cur.execute(
                """
                SELECT game_id
                  FROM msf_mlb.schedule
                 WHERE away_team_id = %s
                   AND home_team_id = %s
                   AND start_time::date = %s
                 LIMIT 1
                """,
                (away_id, home_id, game_time.date())
            )
            fb = cur.fetchone()
            if fb:
                mlb_game_pk = fb[0]
                logger.warning("Falling back to date-only schedule match for %s vs %s @ %s",
                               rec["away_team"], rec["home_team"], rec["commence_time"])
            else:
                skipped_sched += 1
                logger.warning("No schedule match even after fallback for %s vs %s @ %s",
                               rec["away_team"], rec["home_team"], rec["commence_time"])
                continue

        # ─── skip any game not in OVERRIDE_GAME_IDS ──────────
        if OVERRIDE_GAME_IDS and mlb_game_pk not in OVERRIDE_GAME_IDS:
            continue

        # decide segment
        if snap_ts >= game_time:
            segment = "in_play"
        else:
            segment = lookup.get(mlb_game_pk, "pre_game")
            if mlb_game_pk not in lookup:
                logger.info("  → defaulting to pre_game for %s @ %s", mlb_game_pk, iso)

        # insert/upsert
        for book in rec.get("bookmakers", []):
            book_id = upsert_book(cur, book["title"])
            for m in book.get("markets", []):
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
                    (rec["id"], book_id, snap_ts, game_time, segment, m["key"], mlb_game_pk)
                )
                row = cur.fetchone()
                if row:
                    go_id = row[0]
                else:
                    cur.execute(
                        "SELECT id FROM msf_mlb.game_odds"
                        " WHERE odds_api_game_id=%s AND book_id=%s"
                        " AND as_of_time=%s AND odds_type=%s",
                        (rec["id"], book_id, snap_ts, m["key"])  
                    )
                    go_id = cur.fetchone()[0]

                for o in m.get("outcomes", []):
                    ot = ("home" if o["name"] == rec["home_team"] else "away") if m["key"] in ("h2h","spreads") else o["name"].lower()
                    cur.execute(
                        """
                        INSERT INTO msf_mlb.odds
                          (game_odds_id, outcome_type, odds_american, spread, over_under)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (go_id, ot, o.get("price"),
                         o.get("point") if m["key"] == "spreads" else None,
                         o.get("point") if m["key"] == "totals"  else None)
                    )
                    inserted += 1

    conn.commit()
    cur.close()
    logger.info("  → inserted=%d, skipped_name=%d, skipped_sched=%d", inserted, skipped_name, skipped_sched)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser("Batch MLB odds capture for missing games")
    p.add_argument("--start",    required=True, help="YYYY-MM-DD")
    p.add_argument("--end",      required=True, help="YYYY-MM-DD")
    p.add_argument("--tolerance", type=int, default=7200, help="Schedule match tolerance in seconds")
    p.add_argument("--date-pad", type=int, default=1, help="Days to pad schedule window on each side")
    args = p.parse_args()

    start_dt = date.fromisoformat(args.start)
    end_dt   = date.fromisoformat(args.end)
    if end_dt < start_dt:
        p.error("--end must be on or after --start")

    schedule_padded, teams_map = load_schedule_and_teams(start_dt, end_dt, args.date_pad)

    # determine missing IDs (or override)
    if OVERRIDE_GAME_IDS:
        missing_ids = OVERRIDE_GAME_IDS
        logger.info("Overriding auto-detect, will process %d games from override list", len(missing_ids))
    else:
        conn_check = pg_connect()
        cur_check = conn_check.cursor()
        cur_check.execute(
            """
            SELECT game_id FROM msf_mlb.schedule
             WHERE start_time::date BETWEEN %s AND %s
            EXCEPT
            SELECT DISTINCT mlb_game_pk FROM msf_mlb.game_odds
            """,
            (start_dt, end_dt)
        )
        missing_ids = {r[0] for r in cur_check.fetchall()}
        cur_check.close()
        conn_check.close()
        logger.info("Auto-detected %d missing games", len(missing_ids))
        if not missing_ids:
            logger.info("No missing games found between %s and %s, exiting.", start_dt, end_dt)
            return

    schedule = [r for r in schedule_padded if r["game_id"] in missing_ids]
    logger.info("Filtered schedule to %d missing games", len(schedule))

    snaps      = compute_windows(schedule)
    calls, seg_map = coalesce_snap_times(snaps, bucket_hours=4)

    logger.info("=== Starting odds capture %s → %s (%d snapshots for %d games) ===",
                start_dt, end_dt, len(calls), len(missing_ids))
    conn = pg_connect()
    errors = 0
    for ts in calls:
        try:
            load_odds_snapshot(conn, ts, teams_map, seg_map, args.tolerance)
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
