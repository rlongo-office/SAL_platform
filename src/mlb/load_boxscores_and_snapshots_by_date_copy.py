#!/usr/bin/env python3
"""
mlb_stats_loader.py
===================
A one-stop script to populate and back-fill MLB statistics tables in the
`msf_mlb` schema of your Neon Postgres database.

Execution order (single run):
1. Team batting boxscores → `team_boxscores`
2. Pitcher boxscores & player upserts → `pitcher_boxscores`, `players`
3. Cumulative pitcher snapshots → `pitcher_stats_snapshots`
4. Team-level cumulative snapshots → `team_stats_snapshots`
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Dict,Optional
from typing import Any
import psycopg2
import psycopg2.extras
from psycopg2.extras import DictCursor
import requests
from dotenv import find_dotenv, load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

###############################################################################
# Configuration & Logging                                                     #
###############################################################################

load_dotenv(find_dotenv())

DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

# Logs live two levels above this file: <project>/logs
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, os.pardir, os.pardir, "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"mlb_stats_loader_v2_{datetime.now():%Y%m%d_%H%M%S}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

###############################################################################
# Helpers                                                                     #
###############################################################################

def pg_connect():
    conn = psycopg2.connect(
        keepalives         = 1,
        keepalives_idle    = 30,
        keepalives_interval= 30,
        keepalives_count   = 5,
        **DB_PARAMS,
    )
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn


def get_http_session(retries: int = 5) -> requests.Session:
    """Return a `requests` session with retry logic for 5xx/429."""
    retry_strategy = Retry(
        total=retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    sess = requests.Session()
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def compute_rate(numer: int | float, denom: int | float) -> float:
    return round(numer / denom, 3) if denom else 0.0

###############################################################################
# Stage 1 — Team batting boxscores                                            #
###############################################################################

def load_team_boxscores(
    conn,
    session: requests.Session,
    start_date: date | None = None,
    end_date:   date | None = None,
) -> None:
    """
    Stage 1: Populate `team_boxscores` for games in [start_date, end_date].
    If start_date/end_date is None, pulls all games.
    """
    #conn = pg_connect()
    cur  = conn.cursor(cursor_factory=DictCursor)

    # ── 1) grab all valid team_ids once ─────────────────────────────────────
    cur.execute("SELECT id FROM msf_mlb.teams;")
    valid_team_ids = {r["id"] for r in cur.fetchall()}

    # ── 2) select only games in date range ─────────────────────────────────
    sql = """
        SELECT game_id, date_played, away_team_id, home_team_id
          FROM mlb_game_outcomes
    """
    params: list[date] = []
    if start_date and end_date:
        sql += " WHERE date_played BETWEEN %s AND %s"
        params = [start_date, end_date]
    elif start_date:
        sql += " WHERE date_played >= %s"
        params = [start_date]
    elif end_date:
        sql += " WHERE date_played <= %s"
        params = [end_date]

    sql += " ORDER BY date_played, game_id"
    cur.execute(sql, tuple(params))
    games = cur.fetchall()
    logger.info("[Stage 1] Processing %d games for team boxscores", len(games))

    insert_sql = """
    INSERT INTO team_boxscores (
        game_id, game_date, team_id, opponent_id, side,
        at_bats, hits, base_on_balls, hit_by_pitch,
        sacrifice_flys, total_bases
    ) VALUES (
        %(game_id)s, %(game_date)s, %(team_id)s, %(opp_id)s, %(side)s,
        %(ab)s, %(h)s, %(bb)s, %(hbp)s, %(sf)s, %(tb)s
    ) ON CONFLICT (game_id, team_id) DO NOTHING
    """

    def extract_raw_stats(side_data: dict) -> dict[str, int]:
        st = side_data["teamStats"]["batting"]
        return {
            "ab":  st.get("atBats", 0),
            "h":   st.get("hits", 0),
            "bb":  st.get("baseOnBalls", 0),
            "hbp": st.get("hitByPitch", 0),
            "sf":  st.get("sacrificeFlys", 0),
            "tb":  st.get("totalBases", 0),
        }

    rows_inserted = 0
    for g in games:
        gid, gdate, away_id, home_id = g.values()
        url = f"{MLB_API_BASE}/game/{gid}/boxscore"
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[Stage 1] Skip game %s (boxscore fetch failed: %s)", gid, exc)
            continue

        teams = resp.json().get("teams", {})
        if not {"away", "home"}.issubset(teams):
            logger.warning("[Stage 1] Skip game %s (malformed JSON)", gid)
            continue

        if away_id in valid_team_ids:
            cur.execute(insert_sql, {
                "game_id":   gid,
                "game_date": gdate,
                "team_id":   away_id,
                "opp_id":    home_id,
                "side":      "away",
                **extract_raw_stats(teams["away"]),
            })
            rows_inserted += cur.rowcount
        else:
            logger.warning("[Stage 1] Unknown away team_id %s for game %s", away_id, gid)

        if home_id in valid_team_ids:
            cur.execute(insert_sql, {
                "game_id":   gid,
                "game_date": gdate,
                "team_id":   home_id,
                "opp_id":    away_id,
                "side":      "home",
                **extract_raw_stats(teams["home"]),
            })
            rows_inserted += cur.rowcount
        else:
            logger.warning("[Stage 1] Unknown home team_id %s for game %s", home_id, gid)

    conn.commit()
    logger.info("[Stage 1] Inserted %d new team boxscore rows", rows_inserted)

###############################################################################
# Stage 2 — Pitcher boxscores & player upserts                                #
###############################################################################

def upsert_player(
    cur: psycopg2.extensions.cursor,
    pid: int,
    team_id: int,
    first: str | None,
    last:  str | None,
) -> None:
    """
    Insert a pitcher into `players` if not already present.
    """
    cur.execute(
        """
        INSERT INTO players (id, team_id, first_name, last_name, position_group)
        VALUES (%s, %s, %s, %s, 'Pitcher')
        ON CONFLICT (id) DO NOTHING
        """,
        (pid, team_id, first, last),
    )

def load_pitcher_boxscores(
    conn: psycopg2.extensions.connection,
    session: requests.Session,
    start_date: date | None = None,
    end_date:   date | None = None,
) -> None:
    """
    Stage 2: Populate `pitcher_boxscores` (with workload columns) for games
    in [start_date, end_date].  Safe to re-run (upserts existing rows).
    Also upserts new pitchers into `players`.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── 1) valid MLB team IDs ────────────────────────────────────────────────
    cur.execute("SELECT id FROM msf_mlb.teams;")
    valid_team_ids = {r["id"] for r in cur.fetchall()}

    # ── 2) select only games in date range ─────────────────────────────────
    sql = """
        SELECT game_id, date_played, away_team_id, home_team_id
          FROM msf_mlb.mlb_game_outcomes
    """
    params: list[date] = []
    if start_date and end_date:
        sql += " WHERE date_played BETWEEN %s AND %s"
        params = [start_date, end_date]
    elif start_date:
        sql += " WHERE date_played >= %s"
        params = [start_date]
    elif end_date:
        sql += " WHERE date_played <= %s"
        params = [end_date]

    sql += " ORDER BY date_played, game_id"
    cur.execute(sql, tuple(params))
    games = cur.fetchall()
    logger.info("[Stage 2] Processing %d games for pitcher boxscores", len(games))

    insert_pb = """
        INSERT INTO msf_mlb.pitcher_boxscores (
            game_id, player_id, side, sequence,
            at_bats, hits_allowed, bb_allowed, hbp_allowed, sf_allowed,
            total_bases, runs_allowed,
            outs_recorded, batters_faced, pitches_thrown, strike_outs, earned_runs,
            date_played
        ) VALUES (
            %(game_id)s, %(player_id)s, %(side)s, %(seq)s,
            %(at_bats)s, %(hits)s, %(bb)s, %(hbp)s, %(sf)s,
            %(tb)s, %(runs)s,
            %(outs)s, %(bf)s, %(pitches)s, %(ks)s, %(er)s,
            %(date_played)s
        )
        ON CONFLICT (game_id, player_id, sequence)
        DO UPDATE SET
            at_bats        = EXCLUDED.at_bats,
            hits_allowed   = EXCLUDED.hits_allowed,
            bb_allowed     = EXCLUDED.bb_allowed,
            hbp_allowed    = EXCLUDED.hbp_allowed,
            sf_allowed     = EXCLUDED.sf_allowed,
            total_bases    = EXCLUDED.total_bases,
            runs_allowed   = EXCLUDED.runs_allowed,
            outs_recorded  = EXCLUDED.outs_recorded,
            batters_faced  = EXCLUDED.batters_faced,
            pitches_thrown = EXCLUDED.pitches_thrown,
            strike_outs    = EXCLUDED.strike_outs,
            earned_runs    = EXCLUDED.earned_runs
    """

    rows_upserted = 0
    for g in games:
        gid, gdate, away_tid, home_tid = g.values()
        try:
            box = session.get(f"{MLB_API_BASE}/game/{gid}/boxscore", timeout=10).json()
        except Exception as exc:
            logger.warning("[Stage 2] Skip game %s (fetch failed: %s)", gid, exc)
            continue

        teams = box.get("teams", {})
        for side in ("away", "home"):
            tid = away_tid if side == "away" else home_tid
            if tid not in valid_team_ids:
                logger.warning("[Stage 2] Skipping %s side for unknown team %s in game %s",
                               side, tid, gid)
                continue

            plist = teams.get(side, {}).get("pitchers", [])
            pmap  = teams.get(side, {}).get("players", {})

            for seq, pid in enumerate(plist, start=1):
                pdata = pmap.get(f"ID{pid}")
                if not pdata or "stats" not in pdata or "pitching" not in pdata["stats"]:
                    continue
                st = pdata["stats"]["pitching"]

                # basic events
                hits = st.get("hits", 0)
                bb   = st.get("baseOnBalls", 0)
                hbp  = st.get("hitByPitch", 0)
                sf   = st.get("sacFlies", st.get("sacrificeFlys", 0))
                runs = st.get("runs", 0)
                er   = st.get("earnedRuns", 0)

                # workload (NEW)
                outs    = st.get("outs", 0)
                bf      = st.get("battersFaced", 0)
                pitches = st.get("numberOfPitches", 0)
                ks      = st.get("strikeOuts", 0)

                # total bases
                doubles = st.get("doubles", 0)
                triples = st.get("triples", 0)
                homers  = st.get("homeRuns", 0)
                singles = max(0, hits - doubles - triples - homers)
                tb      = singles + 2*doubles + 3*triples + 4*homers

                # derive at-bats
                atb = max(0, bf - bb - hbp - sf)

                # ensure pitcher exists in `players`
                person = pdata.get("person", {})
                upsert_player(cur, pid, tid,
                              person.get("firstName"), person.get("lastName"))

                # upsert into pitcher_boxscores
                cur.execute(insert_pb, {
                    "game_id":      gid,
                    "player_id":    pid,
                    "side":         side,
                    "seq":          seq,
                    "at_bats":      atb,
                    "hits":         hits,
                    "bb":           bb,
                    "hbp":          hbp,
                    "sf":           sf,
                    "tb":           tb,
                    "runs":         runs,
                    "outs":         outs,
                    "bf":           bf,
                    "pitches":      pitches,
                    "ks":           ks,
                    "er":           er,
                    "date_played":  gdate,
                })
                rows_upserted += 1  # count even if just overwrote

    conn.commit()
    logger.info("[Stage 2] Upserted %d pitcher-boxscore rows", rows_upserted)

###############################################################################
# Stage 3 — Pitcher cumulative snapshots                                      #
###############################################################################

def build_pitcher_snapshots(conn: psycopg2.extensions.connection) -> None:
    """
    Re-build msf_mlb.pitcher_stats_snapshots from pitcher_boxscores,
    including workload totals and cumulative outings.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── 1 ─ fetch every appearance in player/date/seq order ───────────
    cur.execute(
        """
        SELECT player_id, date_played, sequence,
               at_bats, hits_allowed, bb_allowed, hbp_allowed, sf_allowed,
               total_bases,
               outs_recorded, batters_faced, pitches_thrown,
               strike_outs,   earned_runs
          FROM msf_mlb.pitcher_boxscores
         ORDER BY player_id, date_played, sequence
        """
    )
    rows = cur.fetchall()
    logger.info("[Stage 3] Building snapshots from %d pitcher_boxscore rows", len(rows))

    # ── 2 ─ prepared UPSERT (now includes cum_outings) ────────────────
    insert_ps = """
        INSERT INTO msf_mlb.pitcher_stats_snapshots (
            player_id, snapshot_date,
            cum_ab,  cum_h,  cum_bb,  cum_hbp,  cum_sf,  cum_tb,
            cum_outs, cum_bf, cum_pitches, cum_so, cum_er,
            cum_outings,
            obp_allowed, slg_allowed
        ) VALUES (
            %(player_id)s, %(snapshot_date)s,
            %(cum_ab)s,  %(cum_h)s,  %(cum_bb)s,  %(cum_hbp)s,  %(cum_sf)s,  %(cum_tb)s,
            %(cum_outs)s, %(cum_bf)s, %(cum_pitches)s, %(cum_so)s, %(cum_er)s,
            %(cum_outings)s,
            %(obp)s, %(slg)s
        )
        ON CONFLICT (player_id, snapshot_date)
        DO UPDATE SET
            cum_ab       = EXCLUDED.cum_ab,
            cum_h        = EXCLUDED.cum_h,
            cum_bb       = EXCLUDED.cum_bb,
            cum_hbp      = EXCLUDED.cum_hbp,
            cum_sf       = EXCLUDED.cum_sf,
            cum_tb       = EXCLUDED.cum_tb,
            cum_outs     = EXCLUDED.cum_outs,
            cum_bf       = EXCLUDED.cum_bf,
            cum_pitches  = EXCLUDED.cum_pitches,
            cum_so       = EXCLUDED.cum_so,
            cum_er       = EXCLUDED.cum_er,
            cum_outings  = EXCLUDED.cum_outings,
            obp_allowed  = EXCLUDED.obp_allowed,
            slg_allowed  = EXCLUDED.slg_allowed
    """

    accum: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    snapshots = 0

    for r in rows:
        pid = r["player_id"]
        dt  = r["date_played"]
        st  = accum[pid]

        if r["sequence"] == 1:
            st["cum_outings"] += 1

        st["cum_ab"]      += r["at_bats"]
        st["cum_h"]       += r["hits_allowed"]
        st["cum_bb"]      += r["bb_allowed"]
        st["cum_hbp"]     += r["hbp_allowed"]
        st["cum_sf"]      += r["sf_allowed"]
        st["cum_tb"]      += r["total_bases"]

        st["cum_outs"]    += r["outs_recorded"]
        st["cum_bf"]      += r["batters_faced"]
        st["cum_pitches"] += r["pitches_thrown"]
        st["cum_so"]      += r["strike_outs"]
        st["cum_er"]      += r["earned_runs"]

        pa  = st["cum_ab"] + st["cum_bb"] + st["cum_hbp"] + st["cum_sf"]
        obp = round((st["cum_h"] + st["cum_bb"] + st["cum_hbp"]) / pa, 3) if pa else 0.0
        slg = round(st["cum_tb"] / st["cum_ab"], 3) if st["cum_ab"] else 0.0

        cur.execute(
            insert_ps,
            {
                "player_id":     pid,
                "snapshot_date": dt,
                "cum_ab":        st["cum_ab"],
                "cum_h":         st["cum_h"],
                "cum_bb":        st["cum_bb"],
                "cum_hbp":       st["cum_hbp"],
                "cum_sf":        st["cum_sf"],
                "cum_tb":        st["cum_tb"],
                "cum_outs":      st["cum_outs"],
                "cum_bf":        st["cum_bf"],
                "cum_pitches":   st["cum_pitches"],
                "cum_so":        st["cum_so"],
                "cum_er":        st["cum_er"],
                "cum_outings":   st["cum_outings"],
                "obp":           obp,
                "slg":           slg,
            },
        )
        snapshots += cur.rowcount

    conn.commit()
    logger.info("[Stage 3] Upserted %d pitcher snapshot rows", snapshots)

###############################################################################
# Stage 4 — Team cumulative snapshots                                         #
###############################################################################

def fetch_season_stats(session: requests.Session, team_id: int, season: int = 2022) -> tuple[dict, dict]:
    """Return (offense_stat_dict, defense_stat_dict) for a given season."""
    url = f"{MLB_API_BASE}/teams/{team_id}/stats"

    # Offense
    params = {"stats": "season", "season": season, "group": "hitting"}
    off = session.get(url, params=params, timeout=10).json()["stats"][0]["splits"][0]["stat"]

    # Defense (pitching)
    params["group"] = "pitching"
    df = session.get(url, params=params, timeout=10).json()["stats"][0]["splits"][0]["stat"]
    return off, df

def get_game_date(
    cur: psycopg2.extensions.cursor,
    season: int,
    cutoff: bool = True
) -> Optional[date]:
    """
    Fetch either the opening day of `season` or the last regular‐season day of (season‐1).
    
    Parameters
    ----------
    cur : psycopg2 cursor (with search_path=msf_mlb,public)
    season : int
        The year for which we want opening‐day (if cutoff=False)
        or the year whose previous‐season we want cutoff (if cutoff=True).
    cutoff : bool, default True
        - If True: return the last 'reg'‐game date of (season - 1).
        - If False: return the first 'reg'‐game date of season.
    
    Returns
    -------
    A `date` object, or None if no matching rows found.
    """
    if cutoff:
        # “last regular‐season game” in (season - 1)
        sql = """
            SELECT MAX(start_time) AS last_date
              FROM msf_mlb.schedule
             WHERE EXTRACT(YEAR FROM start_time) = %s
               AND game_type = 'reg'
        """
        cur.execute(sql, (season - 1,))
    else:
        # “first regular‐season game” in season
        sql = """
            SELECT MIN(start_time) AS first_date
              FROM msf_mlb.schedule
             WHERE EXTRACT(YEAR FROM start_time) = %s
               AND game_type = 'reg'
        """
        cur.execute(sql, (season,))

    row = cur.fetchone()
    if row is None:
        return None

    # Depending on cutoff flag, the column is named last_date or first_date
    return row[0]  # either the MIN(...) or MAX(...) value

def get_season_baseline_stats(
    cur: psycopg2.extensions.cursor,
    session: requests.Session,
    team_id: int,
    season: int,
    cutoff_date: date
) -> dict[str, Any] | None:
    """
    Attempt to build a “game 0” baseline for `team_id` heading into `season`.
    1) Look for the very last team_stats_snapshot in the previous season (season−1)
       whose snapshot_date ≤ cutoff_date.
       If found, divide its cumulative fields by the number of snapshots that
       team had in that same range; return those per-snapshot averages (cum_*) plus
       the final OBP/SLG and allowed_OBP/allowed_SLG from that last row.
    2) If no such snapshots exist, fall back to MLB API totals for (season−1),
       via fetch_season_stats(..., season=season−1).  Return the AVERAGES of raw cumulative totals
       (divide each raw cumulative by games_in_season) and compute rates via compute_rate.
    Returns a dict with keys:
      cum_ab, cum_h, cum_bb, cum_hbp, cum_sf, cum_tb,
      cum_allowed_ab, cum_allowed_h, cum_allowed_bb, cum_allowed_hbp, cum_allowed_tb,
      cum_runs_scored, cum_runs_allowed,
      obp, slg, allowed_obp, allowed_slg
    or None if both snapshots and MLB API both fail.
    """
    prev_season = season - 1

    # 1) run the COUNT(*) query (note the triple‐quoted string and %s placeholder)
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM msf_mlb.schedule
        WHERE EXTRACT(YEAR FROM start_time) = %s
        AND game_type = 'reg'
        """,
        (prev_season,),
    )

    # 2) fetch the row and pull out the “cnt” column
    row = cur.fetchone()
    games_in_season = row["cnt"] if row else 0

    # a) count how many snapshots that team had in previous season up to cutoff_date
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM msf_mlb.team_stats_snapshots
         WHERE team_id = %s
           AND snapshot_date <= %s
           AND EXTRACT(YEAR FROM snapshot_date) = %s
        """,
        (team_id, cutoff_date, prev_season),
    )
    cnt_row = cur.fetchone()
    num_prev_snapshots = cnt_row["cnt"] or 0

    if num_prev_snapshots > 0:
        # b) grab that team’s very last snapshot row in previous season ≤ cutoff_date
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
               AND EXTRACT(YEAR FROM snapshot_date) = %s
             ORDER BY snapshot_date DESC
             LIMIT 1
            """,
            (team_id, cutoff_date, prev_season),
        )
        last = cur.fetchone()
        if last is None:
            return None

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

        return {
            "cum_ab":           per_ab,
            "cum_h":            per_h,
            "cum_bb":           per_bb,
            "cum_hbp":          per_hbp,
            "cum_sf":           per_sf,
            "cum_tb":           per_tb,
            "cum_runs_scored":  per_runs_scored,
            "cum_allowed_ab":   per_allowed_ab,
            "cum_allowed_h":    per_allowed_h,
            "cum_allowed_bb":   per_allowed_bb,
            "cum_allowed_hbp":  per_allowed_hbp,
            "cum_allowed_tb":   per_allowed_tb,
            "cum_runs_allowed": per_runs_allowed,
            "obp":              float(last["obp"]),
            "slg":              float(last["slg"]),
            "allowed_obp":      float(last["allowed_obp"]),
            "allowed_slg":      float(last["allowed_slg"]),
        }
    else:
        # No prior‐season snapshots ⇒ fetch prior season totals from API
        try:
            off, df = fetch_season_stats(session, team_id, season=prev_season)
        except Exception:
            return None

        cum_ab     = off.get("atBats", 0)
        cum_h      = off.get("hits", 0)
        cum_bb     = off.get("baseOnBalls", 0)
        cum_hbp    = off.get("hitByPitch", 0)
        cum_sf     = off.get("sacrificeFlys", 0)
        cum_tb     = off.get("totalBases", 0)
        cum_runs   = off.get("runs", 0)

        cum_allowed_ab   = df.get("atBatsAgainst", 0)
        cum_allowed_h    = df.get("hitsAllowed", 0)
        cum_allowed_bb   = df.get("baseOnBallsAllowed", 0)
        cum_allowed_hbp  = df.get("hitByPitch", 0)
        cum_allowed_tb   = df.get("totalBasesAgainst", 0)
        cum_runs_allowed = df.get("runsAllowed", 0)

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

        obp_numer = per_h + per_bb + per_hbp
        obp_denom = per_ab + per_bb + per_hbp + per_sf
        obp = round(obp_numer / obp_denom, 3) if obp_denom > 0 else 0.0

        slg = round(per_tb / per_ab, 3) if per_ab > 0 else 0.0

        allowed_numer = per_allowed_h + per_allowed_bb + per_allowed_hbp
        allowed_denom = per_allowed_ab + per_allowed_bb + per_allowed_hbp
        allowed_obp = round(allowed_numer / allowed_denom, 3) if allowed_denom > 0 else 0.0
        allowed_slg = round(per_allowed_tb / per_allowed_ab, 3) if per_allowed_ab > 0 else 0.0

        return {
            "cum_ab":           per_ab,
            "cum_h":            per_h,
            "cum_bb":           per_bb,
            "cum_hbp":          per_hbp,
            "cum_sf":           per_sf,
            "cum_tb":           per_tb,
            "cum_runs_scored":  per_runs_scored,
            "cum_allowed_ab":   per_allowed_ab,
            "cum_allowed_h":    per_allowed_h,
            "cum_allowed_bb":   per_allowed_bb,
            "cum_allowed_hbp":  per_allowed_hbp,
            "cum_allowed_tb":   per_allowed_tb,
            "cum_runs_allowed": per_runs_allowed,
            "obp":              obp,
            "slg":              slg,
            "allowed_obp":      allowed_obp,
            "allowed_slg":      allowed_slg,
        }


# Example usage inside your baseline‐building code:
#
#   with pg_connect() as conn:
#       cur = conn.cursor(cursor_factory=DictCursor)
#       opening_day     = get_game_date(cur, season=2025, cutoff=False)
#       prev_year_cutoff = get_game_date(cur, season=2025, cutoff=True)
#
#   # opening_day is the first 'reg' game in 2025, e.g. 2025-03-27
#   # prev_year_cutoff is the last 'reg' game in 2024, e.g. 2024-09-29

def build_team_snapshots(
    conn: psycopg2.extensions.connection,
    session,
    season: int = 2022
) -> None:
    """
    Stage 4 — Build cumulative team snapshots for one season.

    - Creates a “game 0” (Opening-Day) baseline per team.
    - Walks every game that season, incrementing cumulative totals.
    """

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── 1 ─ Team list ──────────────────────────────────────────────────
    cur.execute("SELECT id FROM msf_mlb.teams;")
    teams = [r["id"] for r in cur.fetchall()]

    # Season boundary dates
    opening_day      = get_game_date(cur, season, cutoff=False)
    prev_year_cutoff = get_game_date(cur, season, cutoff=True)
    cur_year_cutoff  = get_game_date(cur, season + 1, cutoff=True)

    logger.info(
        "[Stage 4] season=%s  teams=%s  opening_day=%s  prev_cutoff=%s  next_cutoff=%s",
        season, len(teams), opening_day, prev_year_cutoff, cur_year_cutoff,
    )

    if opening_day is None:
        raise RuntimeError(
            f"[Stage 4] Could not determine Opening Day for {season} – "
            "is the schedule table populated?"
        )

    # ── 2 ─ Baseline UPSERT SQL ───────────────────────────────────────
    upsert_sql = """
        INSERT INTO msf_mlb.team_stats_snapshots (
            team_id, snapshot_date,
            cum_ab, cum_h, cum_bb, cum_hbp, cum_sf, cum_tb,
            obp, slg,
            cum_allowed_ab, cum_allowed_h, cum_allowed_bb, cum_allowed_hbp, cum_allowed_tb,
            allowed_obp, allowed_slg,
            cum_runs_scored, cum_runs_allowed
        )
        VALUES (
            %(team_id)s, %(snapshot_date)s,
            %(cum_ab)s, %(cum_h)s, %(cum_bb)s, %(cum_hbp)s, %(cum_sf)s, %(cum_tb)s,
            %(obp)s, %(slg)s,
            %(cum_allowed_ab)s, %(cum_allowed_h)s, %(cum_allowed_bb)s, %(cum_allowed_hbp)s, %(cum_allowed_tb)s,
            %(allowed_obp)s, %(allowed_slg)s,
            %(cum_runs_scored)s, %(cum_runs_allowed)s
        )
        ON CONFLICT (team_id, snapshot_date) DO NOTHING
    """

    # ── 3 ─ Opening-Day baseline rows ─────────────────────────────────
    team_state: dict[int, dict[str, Any]] = {}
    rows_baseline = 0

    for tid in teams:
        baseline = get_season_baseline_stats(cur, session, tid, season, prev_year_cutoff)

        if baseline is None:
            logger.warning("[Stage 4] No baseline for team_id=%s – skipped", tid)
            continue

        payload = {"team_id": tid, "snapshot_date": opening_day, **baseline}

        try:
            cur.execute(upsert_sql, payload)
        except IntegrityError as exc:
            conn.rollback()
            logger.error(
                "[Stage 4] Baseline insert failed  team_id=%s  date=%s  error=%s",
                tid, opening_day, exc,
            )
            raise
        rows_baseline += cur.rowcount
        team_state[tid] = baseline.copy()

    conn.commit()
    logger.info("[Stage 4] Inserted %s Opening-Day baseline rows", rows_baseline)

    # ── 4 ─ Fetch games for this season ───────────────────────────────
    cur.execute(
        """
        SELECT game_id, date_played,
               away_team_id, home_team_id,
               away_score,  home_score
        FROM   mlb_game_outcomes
        WHERE  date_played BETWEEN %s AND %s
        ORDER  BY date_played, game_id
        """,
        (opening_day, cur_year_cutoff),
    )
    games = cur.fetchall()
    total_games = len(games)
    logger.info("[Stage 4] Found %s games to process", total_games)

    # Pre-load team boxscores for speed
    cur.execute("SELECT * FROM msf_mlb.team_boxscores")
    boxes     = cur.fetchall()
    box_map   = {(b["game_id"], b["team_id"]): b for b in boxes}

    # ── 5 ─ Walk the schedule ─────────────────────────────────────────
    snapshot_rows      = 0
    ROWS_BEFORE_COMMIT = 50
    rows_since_commit  = 0
    commits_done       = 0

    for g in games:
        gid, date_played = g["game_id"], g["date_played"]

        for side in ("away", "home"):
            tid     = g[f"{side}_team_id"]
            opp_tid = g["home_team_id"] if side == "away" else g["away_team_id"]
            rs, ra  = g[f"{side}_score"], g[f"{'home' if side == 'away' else 'away'}_score"]

            if rs is None or ra is None:
                logger.warning("[Stage 4] Skipping game %s (%s) – missing score", gid, side)
                continue

            box, opp_box = box_map.get((gid, tid)), box_map.get((gid, opp_tid))
            if not box or not opp_box:
                logger.warning("[Stage 4] Missing boxscore game=%s team=%s", gid, tid)
                continue

            st = team_state.get(tid)
            if st is None:         # should not happen – but guard anyway
                logger.warning("[Stage 4] No running state for team=%s", tid)
                continue

            # Offense
            st["cum_ab"]          += box["at_bats"]
            st["cum_h"]           += box["hits"]
            st["cum_bb"]          += box["base_on_balls"]
            st["cum_hbp"]         += box["hit_by_pitch"]
            st["cum_sf"]          += box["sacrifice_flys"]
            st["cum_tb"]          += box["total_bases"]
            st["cum_runs_scored"] += rs

            # Defense (allowed)
            st["cum_allowed_ab"] += opp_box["at_bats"]
            st["cum_allowed_h"]  += opp_box["hits"]
            st["cum_allowed_bb"] += opp_box["base_on_balls"]
            st["cum_allowed_hbp"]+= opp_box["hit_by_pitch"]
            st["cum_allowed_tb"] += opp_box["total_bases"]
            st["cum_runs_allowed"] += ra

            # Rates
            obp = compute_rate(
                st["cum_h"] + st["cum_bb"] + st["cum_hbp"],
                st["cum_ab"] + st["cum_bb"] + st["cum_hbp"] + st["cum_sf"],
            )
            slg = compute_rate(st["cum_tb"], st["cum_ab"])
            allowed_obp = compute_rate(
                st["cum_allowed_h"] + st["cum_allowed_bb"] + st["cum_allowed_hbp"],
                st["cum_allowed_ab"] + st["cum_allowed_bb"] + st["cum_allowed_hbp"],
            )
            allowed_slg = compute_rate(st["cum_allowed_tb"], st["cum_allowed_ab"])

            try:
                cur.execute(
                    upsert_sql,
                    {
                        "team_id":          tid,
                        "snapshot_date":    date_played,
                        "cum_ab":           st["cum_ab"],
                        "cum_h":            st["cum_h"],
                        "cum_bb":           st["cum_bb"],
                        "cum_hbp":          st["cum_hbp"],
                        "cum_sf":           st["cum_sf"],
                        "cum_tb":           st["cum_tb"],
                        "obp":              obp,
                        "slg":              slg,
                        "cum_allowed_ab":   st["cum_allowed_ab"],
                        "cum_allowed_h":    st["cum_allowed_h"],
                        "cum_allowed_bb":   st["cum_allowed_bb"],
                        "cum_allowed_hbp":  st["cum_allowed_hbp"],
                        "cum_allowed_tb":   st["cum_allowed_tb"],
                        "allowed_obp":      allowed_obp,
                        "allowed_slg":      allowed_slg,
                        "cum_runs_scored":  st["cum_runs_scored"],
                        "cum_runs_allowed": st["cum_runs_allowed"],
                    },
                )
            except IntegrityError as exc:
                conn.rollback()
                logger.error(
                    "[Stage 4] Snapshot insert failed  game_id=%s team_id=%s date=%s  error=%s",
                    gid, tid, date_played, exc,
                )
                raise

            snapshot_rows     += cur.rowcount
            rows_since_commit += 1

        # Commit every N games
        if rows_since_commit >= ROWS_BEFORE_COMMIT:
            conn.commit()
            commits_done      += 1
            rows_since_commit  = 0
            logger.info(
                "[Stage 4]  ✔ commit %-4s (%5s/%5s games) – through %s",
                commits_done,
                commits_done * ROWS_BEFORE_COMMIT,
                total_games,
                date_played,
            )

    conn.commit()
    logger.info(
        "[Stage 4] Inserted %s incremental snapshot rows for season %s",
        snapshot_rows, season,
    )
###############################################################################
# Main                                                                         #
###############################################################################

def run_pipeline(skip, start_date, end_date):
    session = get_http_session()
    conn = pg_connect()
    SEASON = 2022

    try:
        if "team" not in skip:
            load_team_boxscores(conn, session, start_date, end_date)

        if "pitcher" not in skip:
            load_pitcher_boxscores(conn, session,start_date, end_date)

        if "pitcher_snapshots" not in skip:
            build_pitcher_snapshots(conn)

        if "team_snapshots" not in skip:
            build_team_snapshots(conn, session, SEASON)

    except psycopg2.OperationalError as e:
        logger.warning("DB connection lost, reconnecting…")
        conn.close()
        conn = pg_connect()
        # you could call each stage again where it left off, or simply fail
        raise

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Populate MLB stats into Neon Postgres.")
    parser.add_argument(
        "--skip",
        help="Comma-separated stage names to skip (team,pitcher,pitcher_snapshots,team_snapshots)",
    )
    parser.add_argument(
        "--start",
        help="Only process games on or after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        help="Only process games on or before this date (YYYY-MM-DD)",
    )

    args = parser.parse_args()
    skip = args.skip.split(",") if args.skip else []

    start_date: date | None = None
    end_date:   date | None = None

    if args.start:
        start_date = date.fromisoformat(args.start)
    if args.end:
        end_date = date.fromisoformat(args.end)

    if start_date and end_date and end_date < start_date:
        parser.error("--end must not be before --start")

    run_pipeline(skip, start_date, end_date)

if __name__ == "__main__":
    main()
