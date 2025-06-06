#!/usr/bin/env python3
"""
create_2025_baseline_snapshots.py

“Zero‐out” each team’s 2024 cumulative totals and re‐express them
as a single‐game average on 2025‐01‐01, so that all 2025 snapshots
will build on top of that baseline.

Usage:
    python create_2025_baseline_snapshots.py
"""

import os
import logging
from datetime import date
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
    filename=os.path.join(LOG_DIR, f"baseline_2025_{date.today():%Y%m%d}.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("baseline_2025")


def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

import logging
from datetime import date
import requests
import psycopg2
from psycopg2.extras import DictCursor
from typing import Optional

# Assume these helpers already exist elsewhere in your module:
#   pg_connect(), fetch_season_stats(session, team_id, season)
#   and that MLB_API_BASE is defined for the MLB stats API.

logger = logging.getLogger("baseline_helper")

def build_season_baseline(
    conn: psycopg2.extensions.connection,
    season: int,
    cutoff_date: date,
    games_in_season: int = 162,
) -> None:
    """
    Create a “game 0” snapshot for each team for `season` (on `season-01-01`),
    based on whichever is available:
      • If there are snapshots ≤ `cutoff_date` (i.e. last‐year snapshots), 
        compute per‐snapshot averages from those and insert those as cum_*.
      • Otherwise, fetch prior‐season totals from the API and divide by
        `games_in_season` to get per‐game averages.

    Parameters
    ----------
    conn
        An open psycopg2 connection (search_path already set to msf_mlb)
    season
        The year for which we want to create a game 0. E.g. 2025 means
        look at 2024 snapshots or 2024 API stats, then insert a 2025‐01‐01 row.
    cutoff_date
        The last date of the previous season whose snapshots we’ll consider.
        For season=2025, cutoff_date might be date(2024, 9, 29).
    games_in_season
        Number of games in a full regular season. Default 162.
    """
    cur = conn.cursor(cursor_factory=DictCursor)
    # 1) fetch all team IDs
    cur.execute("SELECT id FROM teams")
    teams = [r["id"] for r in cur.fetchall()]
    logger.info(
        "[Baseline] Building %s‐01‐01 baseline (cutoff=%s) for %d teams",
        season, cutoff_date, len(teams),
    )

    upsert_sql = """
    INSERT INTO team_stats_snapshots (
        team_id,
        snapshot_date,
        cum_ab,
        cum_h,
        cum_bb,
        cum_hbp,
        cum_sf,
        cum_tb,
        obp,
        slg,
        cum_allowed_ab,
        cum_allowed_h,
        cum_allowed_bb,
        cum_allowed_hbp,
        cum_allowed_tb,
        allowed_obp,
        allowed_slg,
        cum_runs_scored,
        cum_runs_allowed
    )
    VALUES (
        %(team_id)s,
        %(snapshot_date)s,
        %(cum_ab)s,
        %(cum_h)s,
        %(cum_bb)s,
        %(cum_hbp)s,
        %(cum_sf)s,
        %(cum_tb)s,
        %(obp)s,
        %(slg)s,
        %(cum_allowed_ab)s,
        %(cum_allowed_h)s,
        %(cum_allowed_bb)s,
        %(cum_allowed_hbp)s,
        %(cum_allowed_tb)s,
        %(allowed_obp)s,
        %(allowed_slg)s,
        %(cum_runs_scored)s,
        %(cum_runs_allowed)s
    )
    ON CONFLICT (team_id, snapshot_date)
      DO NOTHING
    """

    session = requests.Session()  # for any API calls
    rows_upserted = 0
    prev_season = season - 1
    baseline_date = date(season, 1, 1)

    for tid in teams:
        # a) count how many snapshots for this team up to cutoff_date
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM msf_mlb.team_stats_snapshots
             WHERE team_id = %s
               AND snapshot_date <= %s
            """,
            (tid, cutoff_date),
        )
        cnt_row = cur.fetchone()
        num_prev_snapshots = cnt_row["cnt"] or 0

        if num_prev_snapshots > 0:
            # b) grab the very last snapshot ≤ cutoff_date
            cur.execute(
                """
                SELECT
                    cum_ab,
                    cum_h,
                    cum_bb,
                    cum_hbp,
                    cum_sf,
                    cum_tb,
                    obp,
                    slg,
                    cum_allowed_ab,
                    cum_allowed_h,
                    cum_allowed_bb,
                    cum_allowed_hbp,
                    cum_allowed_tb,
                    allowed_obp,
                    allowed_slg,
                    cum_runs_scored,
                    cum_runs_allowed
                  FROM msf_mlb.team_stats_snapshots
                 WHERE team_id = %s
                   AND snapshot_date <= %s
                 ORDER BY snapshot_date DESC
                 LIMIT 1
                """,
                (tid, cutoff_date),
            )
            last = cur.fetchone()
            if last is None:
                logger.warning(
                    "[Baseline] Unexpected: count>0 but no row for team %s; skipping", tid
                )
                continue

            # c) compute per‐snapshot averages (divide each cumulative by num_prev_snapshots)
            per_ab           = last["cum_ab"]           / num_prev_snapshots
            per_h            = last["cum_h"]            / num_prev_snapshots
            per_bb           = last["cum_bb"]           / num_prev_snapshots
            per_hbp          = last["cum_hbp"]          / num_prev_snapshots
            per_sf           = last["cum_sf"]           / num_prev_snapshots
            per_tb           = last["cum_tb"]           / num_prev_snapshots
            per_runs_scored  = last["cum_runs_scored"]  / num_prev_snapshots

            per_allowed_ab   = last["cum_allowed_ab"]   / num_prev_snapshots
            per_allowed_h    = last["cum_allowed_h"]    / num_prev_snapshots
            per_allowed_bb   = last["cum_allowed_bb"]   / num_prev_snapshots
            per_allowed_hbp  = last["cum_allowed_hbp"]  / num_prev_snapshots
            per_allowed_tb   = last["cum_allowed_tb"]   / num_prev_snapshots
            per_runs_allowed = last["cum_runs_allowed"] / num_prev_snapshots

            payload = {
                "team_id":           tid,
                "snapshot_date":     baseline_date,
                "cum_ab":            per_ab,
                "cum_h":             per_h,
                "cum_bb":            per_bb,
                "cum_hbp":           per_hbp,
                "cum_sf":            per_sf,
                "cum_tb":            per_tb,
                "cum_runs_scored":   per_runs_scored,
                "cum_allowed_ab":    per_allowed_ab,
                "cum_allowed_h":     per_allowed_h,
                "cum_allowed_bb":    per_allowed_bb,
                "cum_allowed_hbp":   per_allowed_hbp,
                "cum_allowed_tb":    per_allowed_tb,
                "cum_runs_allowed":  per_runs_allowed,
                # For rates, keep final‐season OBP/SLG:
                "obp":        last["obp"],
                "slg":        last["slg"],
                "allowed_obp": last["allowed_obp"],
                "allowed_slg": last["allowed_slg"],
            }

        else:
            # No prior‐season snapshots ⇒ fetch prior season totals from API
            try:
                off, df = fetch_season_stats(session, tid, season=prev_season)
            except Exception as e:
                logger.warning(
                    "[Baseline] Could not fetch %s stats for team %s: %s",
                    prev_season, tid, e
                )
                continue

            # d) Extract cumulative hitting totals from `off`
            cum_ab     = off.get("atBats", 0)
            cum_h      = off.get("hits", 0)
            cum_bb     = off.get("baseOnBalls", 0)
            cum_hbp    = off.get("hitByPitch", 0)
            cum_sf     = off.get("sacrificeFlys", 0)
            cum_tb     = off.get("totalBases", 0)
            cum_runs   = off.get("runs", 0)

            # e) Extract cumulative “allowed” totals from `df`
            cum_allowed_ab  = df.get("atBatsAgainst", 0)
            cum_allowed_h   = df.get("hitsAllowed", 0)
            cum_allowed_bb  = df.get("baseOnBallsAllowed", 0)
            cum_allowed_hbp = df.get("hitByPitch", 0)
            cum_allowed_tb  = df.get("totalBasesAgainst", 0)
            cum_runs_allowed = df.get("runsAllowed", 0)

            # f) divide each by games_in_season to get per‐game averages
            if games_in_season > 0:
                per_ab           = cum_ab     / games_in_season
                per_h            = cum_h      / games_in_season
                per_bb           = cum_bb     / games_in_season
                per_hbp          = cum_hbp    / games_in_season
                per_sf           = cum_sf     / games_in_season
                per_tb           = cum_tb     / games_in_season
                per_runs_scored  = cum_runs   / games_in_season

                per_allowed_ab   = cum_allowed_ab  / games_in_season
                per_allowed_h    = cum_allowed_h   / games_in_season
                per_allowed_bb   = cum_allowed_bb  / games_in_season
                per_allowed_hbp  = cum_allowed_hbp / games_in_season
                per_allowed_tb   = cum_allowed_tb  / games_in_season
                per_runs_allowed = cum_runs_allowed / games_in_season
            else:
                per_ab = per_h = per_bb = per_hbp = per_sf = per_tb = per_runs_scored = 0.0
                per_allowed_ab = per_allowed_h = per_allowed_bb = per_allowed_hbp = per_allowed_tb = per_runs_allowed = 0.0

            # g) Compute 1‐game OBP/SLG from per‐game averages
            obp_denom = per_ab + per_bb + per_hbp + per_sf
            obp = round((per_h + per_bb + per_hbp) / obp_denom, 3) if obp_denom > 0 else 0.0
            slg = round(per_tb / per_ab, 3) if per_ab > 0 else 0.0

            allowed_denom = per_allowed_ab + per_allowed_bb + per_allowed_hbp
            allowed_obp = (
                round((per_allowed_h + per_allowed_bb + per_allowed_hbp) / allowed_denom, 3)
                if allowed_denom > 0
                else 0.0
            )
            allowed_slg = round(per_allowed_tb / per_allowed_ab, 3) if per_allowed_ab > 0 else 0.0

            payload = {
                "team_id":           tid,
                "snapshot_date":     baseline_date,
                "cum_ab":            per_ab,
                "cum_h":             per_h,
                "cum_bb":            per_bb,
                "cum_hbp":           per_hbp,
                "cum_sf":            per_sf,
                "cum_tb":            per_tb,
                "cum_runs_scored":   per_runs_scored,
                "cum_allowed_ab":    per_allowed_ab,
                "cum_allowed_h":     per_allowed_h,
                "cum_allowed_bb":    per_allowed_bb,
                "cum_allowed_hbp":   per_allowed_hbp,
                "cum_allowed_tb":    per_allowed_tb,
                "cum_runs_allowed":  per_runs_allowed,
                "obp":               obp,
                "slg":               slg,
                "allowed_obp":       allowed_obp,
                "allowed_slg":       allowed_slg,
            }

        # h) Insert the synthesized “game 0” snapshot
        cur.execute(upsert_sql, payload)
        rows_upserted += cur.rowcount

    conn.commit()
    logger.info("[Baseline] Inserted %d “%s-01-01” baseline snapshots", rows_upserted, season)



def build_team_snapshots_2025_only(conn: psycopg2.extensions.connection) -> None:
    """
    Instead of carrying forward raw 2024 totals, we “zero out” the slate
    by inserting a single 2025-01-01 row whose cum_* fields are the 
    per-snapshot averages from 2024.  Subsequent 2025 snapshots will
    build on top of that.
    """
    cur = conn.cursor(cursor_factory=DictCursor)

    # 1) fetch all team IDs
    cur.execute("SELECT id FROM teams")
    teams = [r["id"] for r in cur.fetchall()]

    logger.info("[Baseline] Building 2025 baseline from 2024 snapshots for %d teams", len(teams))

    upsert_sql = """
    INSERT INTO team_stats_snapshots (
        team_id,
        snapshot_date,
        cum_ab,
        cum_h,
        cum_bb,
        cum_hbp,
        cum_sf,
        cum_tb,
        obp,
        slg,
        cum_allowed_ab,
        cum_allowed_h,
        cum_allowed_bb,
        cum_allowed_hbp,
        cum_allowed_tb,
        allowed_obp,
        allowed_slg,
        cum_runs_scored,
        cum_runs_allowed
    )
    VALUES (
        %(team_id)s,
        %(snapshot_date)s,
        %(cum_ab)s,
        %(cum_h)s,
        %(cum_bb)s,
        %(cum_hbp)s,
        %(cum_sf)s,
        %(cum_tb)s,
        %(obp)s,
        %(slg)s,
        %(cum_allowed_ab)s,
        %(cum_allowed_h)s,
        %(cum_allowed_bb)s,
        %(cum_allowed_hbp)s,
        %(cum_allowed_tb)s,
        %(allowed_obp)s,
        %(allowed_slg)s,
        %(cum_runs_scored)s,
        %(cum_runs_allowed)s
    )
    ON CONFLICT (team_id, snapshot_date)
      DO NOTHING
    """

    rows_upserted = 0

    for tid in teams:
        # a) count how many snapshots that team had in 2024 (≤ 2024-09-29)
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM msf_mlb.team_stats_snapshots
             WHERE team_id = %s
               AND snapshot_date <= '2024-09-29'
            """,
            (tid,),
        )
        cnt_row = cur.fetchone()
        num_snapshots_2024 = cnt_row["cnt"] or 0

        if num_snapshots_2024 == 0:
            logger.warning("[Baseline] No 2024 snapshots found for team_id=%s; skipping", tid)
            continue

        # b) grab that team’s very last snapshot row (≤ 2024-09-29)
        cur.execute(
            """
            SELECT
                cum_ab,
                cum_h,
                cum_bb,
                cum_hbp,
                cum_sf,
                cum_tb,
                obp,
                slg,
                cum_allowed_ab,
                cum_allowed_h,
                cum_allowed_bb,
                cum_allowed_hbp,
                cum_allowed_tb,
                allowed_obp,
                allowed_slg,
                cum_runs_scored,
                cum_runs_allowed
              FROM msf_mlb.team_stats_snapshots
             WHERE team_id = %s
               AND snapshot_date <= '2024-09-29'
             ORDER BY snapshot_date DESC
             LIMIT 1
            """,
            (tid,),
        )
        last = cur.fetchone()
        if last is None:
            # should not happen, but just in case
            logger.warning("[Baseline] Unexpected: count>0 but no row returned for team_id=%s", tid)
            continue

        # c) compute per-snapshot averages (divide each cumulative by num_snapshots_2024)
        per_ab           = last["cum_ab"]           / num_snapshots_2024
        per_h            = last["cum_h"]            / num_snapshots_2024
        per_bb           = last["cum_bb"]           / num_snapshots_2024
        per_hbp          = last["cum_hbp"]          / num_snapshots_2024
        per_sf           = last["cum_sf"]           / num_snapshots_2024
        per_tb           = last["cum_tb"]           / num_snapshots_2024
        per_runs_scored  = last["cum_runs_scored"]  / num_snapshots_2024

        per_allowed_ab   = last["cum_allowed_ab"]   / num_snapshots_2024
        per_allowed_h    = last["cum_allowed_h"]    / num_snapshots_2024
        per_allowed_bb   = last["cum_allowed_bb"]   / num_snapshots_2024
        per_allowed_hbp  = last["cum_allowed_hbp"]  / num_snapshots_2024
        per_allowed_tb   = last["cum_allowed_tb"]   / num_snapshots_2024
        per_runs_allowed = last["cum_runs_allowed"] / num_snapshots_2024

        # d) Build payload so that “cum_*” now holds those per-snapshot averages
        payload = {
            "team_id":           tid,
            "snapshot_date":     date(2025, 1, 1),

            # CARRY FORWARD ONLY THE *AVERAGES* (pretend this was 'Game 0')
            "cum_ab":            per_ab,
            "cum_h":             per_h,
            "cum_bb":            per_bb,
            "cum_hbp":           per_hbp,
            "cum_sf":            per_sf,
            "cum_tb":            per_tb,
            "cum_runs_scored":   per_runs_scored,

            "cum_allowed_ab":    per_allowed_ab,
            "cum_allowed_h":     per_allowed_h,
            "cum_allowed_bb":    per_allowed_bb,
            "cum_allowed_hbp":   per_allowed_hbp,
            "cum_allowed_tb":    per_allowed_tb,
            "cum_runs_allowed":  per_runs_allowed,

            # For rates, just reuse the final-2024 OBP/SLG and allowed_OBP/SLG
            "obp":               last["obp"],
            "slg":               last["slg"],
            "allowed_obp":       last["allowed_obp"],
            "allowed_slg":       last["allowed_slg"],
        }

        cur.execute(upsert_sql, payload)
        rows_upserted += cur.rowcount

    conn.commit()
    logger.info("[Baseline] Inserted %d “2025-01-01” baseline snapshots", rows_upserted)

def main():
    conn = pg_connect()

    try:
        # 1) Build a true “2025‐01‐01” baseline for every team by
        #    inspecting each team’s final ≤2024‐09‐29 snapshot and
        #    dividing by “# of snapshots in 2024.”
        build_team_snapshots_2025_only(conn)
    finally:
        conn.close()
        logger.info("Finished creating 2025 baseline snapshots")


if __name__ == "__main__":
    main()
