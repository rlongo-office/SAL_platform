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
from psycopg2.extras import execute_values
from dotenv import load_dotenv, find_dotenv

# ─── MANUAL GAME-ID OVERRIDE ──────────────────────────────────────────────────
# If non-empty, only these mlb_game_pk values will be processed.

OVERRIDE_GAME_IDS = {
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

PRIMARY   = [2, 4, 9, 10, 12, 16, 1, 3, 7, 11]
SECONDARY = [21, 20, 5, 18, 19, 22, 8, 17, 14, 23]

LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"odds_capture_{datetime.now():%Y%m%d_%H%M%S}.log")

# Set logging level to DEBUG so that our detailed debug statements appear.
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
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

# --------------------------------------------------------------------
# 1) Book–selection helper
# --------------------------------------------------------------------
PRIMARY   = [2, 4, 9, 10, 12, 16, 1, 3, 7, 11]          # keep these first
SECONDARY = [21, 20, 5, 18, 19, 22, 8, 17, 14, 23]      # fallback order
TARGET_BOOKS = len(PRIMARY)                             # <= 10 per game

def choose_books(available_ids, target=TARGET_BOOKS):
    """
    Return a list of book-ids (≤ target) that should be processed
    given the ids *present* in this snapshot.
    · Always keep primaries that appear.
    · Fill remaining slots with secondary ids in priority order.
    """
    chosen = [b for b in PRIMARY   if b in available_ids]

    if len(chosen) < target:
        for b in SECONDARY:
            if b in available_ids and b not in chosen:
                chosen.append(b)
                if len(chosen) == target:
                    break
    return chosen

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
import logging
from datetime import datetime
import psycopg2
from psycopg2.extras import DictCursor

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# --------------------------------------------------------------------
# 2) Refactored loader: now filters books via choose_books()
# --------------------------------------------------------------------
def load_odds_snapshot(conn, snap_ts, teams_map, seg_map, tolerance):
    """
    (docstring unchanged – truncated here for brevity)
    """

    iso = snap_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("→ snapshot %s", iso)

    # 1) fetch from odds API
    try:
        data = fetch_historical_odds(iso)
    except Exception as e:
        logger.error("HTTP error fetching odds @%s: %s", iso, e)
        return

    cur = conn.cursor()
    inserted_parents = inserted_children = skipped_name = skipped_sched = 0

    lookup = {gid: seg for (gid, seg) in seg_map.get(snap_ts, [])}
    BATCH_SIZE = 500
    statements_since_commit = 0

    # ────────────────────────────────────────────────────────────────
    for rec in data.get("data", []):

        # 1. map teams
        home_id = teams_map.get(canonicalize(rec["home_team"]))
        away_id = teams_map.get(canonicalize(rec["away_team"]))
        if not home_id or not away_id:
            skipped_name += 1
            logger.warning("Unknown teams: %s / %s",
                           rec["away_team"], rec["home_team"])
            continue

        # 2. parse commence_time
        try:
            game_time = datetime.fromisoformat(
                rec["commence_time"].replace("Z", "+00:00"))
        except Exception as e:
            logger.error("Bad commence_time %s: %s",
                         rec.get("commence_time"), e)
            continue

        # 3. strict schedule match (unchanged)  …………………………………………
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
        row = cur.fetchone()
        if row:
            mlb_game_pk = row[0]
        else:
            # fallback by date
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
                logger.warning("Falling back to date-only schedule match "
                               "for %s vs %s @ %s",
                               rec["away_team"], rec["home_team"],
                               rec["commence_time"])
            else:
                skipped_sched += 1
                logger.warning("No schedule match for %s vs %s @ %s",
                               rec["away_team"], rec["home_team"],
                               rec["commence_time"])
                continue

        if OVERRIDE_GAME_IDS and mlb_game_pk not in OVERRIDE_GAME_IDS:
            continue

        # 5. segment
        segment = ("in_play" if snap_ts >= game_time
                   else lookup.get(mlb_game_pk, "pre_game"))
        if segment == "pre_game" and mlb_game_pk not in lookup:
            logger.info("  → defaulting to pre_game for %s @ %s",
                        mlb_game_pk, iso)

        # ── NEW BLOCK ─────────────────────────────────────────────────
        # Build a list (book_id, book_json) for *all* books in this snapshot
        books_info = []
        for bk in rec.get("bookmakers", []):
            db_id = upsert_book(cur, bk["title"])        # returns int book_id
            books_info.append((db_id, bk))

        if not books_info:
            continue

        # Decide which book_ids to keep
        available = {bid for bid, _ in books_info}
        keep_ids  = set(choose_books(available))         # set for O(1) lookup
        # ──────────────────────────────────────────────────────────────

        # NEW: one-off DEBUG per game
        logger.debug(
            "[choose_books] game=%s present=%s kept=%s dropped=%s",
            mlb_game_pk,
            sorted(available),
            sorted(keep_ids),
            sorted(available - keep_ids)
        )

        # 2) WARN if any primary book is missing and we had to fall back
        missing_primaries = [b for b in PRIMARY if b not in available]
        if missing_primaries:
            logger.warning(
                "[choose_books] snapshot %s game=%s – no data for primary books %s; "
                "using fallback(s) %s",
                iso,
                mlb_game_pk,
                missing_primaries,
                sorted(keep_ids - set(PRIMARY))
            )


        # 6. loop over chosen books only
        for book_id, book in books_info:
            if book_id not in keep_ids:
                continue        # ← drop low-priority books silently

            for m in book.get("markets", []):
                mkey = m.get("key", "").lower()
                if mkey not in ("h2h", "spreads", "totals"):
                    continue
                outcomes = m.get("outcomes") or []
                if not outcomes:
                    continue

                # B) insert parent
                logger.debug(
                    "Inserting into game_odds: odds_api_game_id=%s, book_id=%s, "
                    "as_of_time=%s, game_time=%s, game_segment=%s, odds_type=%s, "
                    "mlb_game_pk=%s",
                    rec["id"], book_id, snap_ts, game_time, segment, mkey,
                    mlb_game_pk
                )
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
                     segment, mkey, mlb_game_pk)
                )
                got = cur.fetchone()
                if got:
                    go_id = got[0]
                    inserted_parents += 1
                else:
                    cur.execute(
                        """
                        SELECT id FROM msf_mlb.game_odds
                         WHERE odds_api_game_id=%s
                           AND book_id=%s
                           AND as_of_time=%s
                           AND odds_type=%s
                        """,
                        (rec["id"], book_id, snap_ts, mkey)
                    )
                    go_id = cur.fetchone()[0]

                # C) children
                for o in outcomes:
                    ot = ("home" if o["name"] == rec["home_team"]
                          else "away") if mkey in ("h2h", "spreads") else o["name"].lower()
                    spread     = o.get("point") if mkey == "spreads" else None
                    over_under = o.get("point") if mkey == "totals"  else None
                    cur.execute(
                        """
                        INSERT INTO msf_mlb.odds
                          (game_odds_id, outcome_type, odds_american,
                           spread, over_under)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (go_id, ot, o.get("price"), spread, over_under)
                    )
                    inserted_children += 1

                statements_since_commit += 1
                if statements_since_commit >= BATCH_SIZE:
                    conn.commit()
                    statements_since_commit = 0

    if statements_since_commit:
        conn.commit()
    cur.close()

    logger.info("  → inserted %d parent rows (game_odds) and %d child rows "
                "(odds); skipped_name=%d, skipped_sched=%d",
                inserted_parents, inserted_children,
                skipped_name, skipped_sched)

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

    snaps, seg_map = coalesce_snap_times(compute_windows(schedule), bucket_hours=8)

    logger.info(
        "=== Starting odds capture %s → %s (%d snapshots for %d games) ===",
        start_dt, end_dt, len(snaps), len(missing_ids)
    )
    conn = pg_connect()
    errors = 0
    for ts in snaps:
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
