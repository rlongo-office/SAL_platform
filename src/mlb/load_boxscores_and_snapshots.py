#!/usr/bin/env python3
"""
mlb_stats_loader.py
===================
A one‑stop script to populate and back‑fill MLB statistics tables in the
`msf_mlb` schema of your Neon Postgres database.

It consolidates the functionality of the previous standalone utilities:

* populate_team_boxscores.py
* populate_mlb_pitcher_stats.py
* populate_pitcher_stats_snapshots.py
* backfill_team_stats.py

Execution order (single run):
1. Team batting boxscores → `team_boxscores`
2. Pitcher boxscores & player upserts → `pitcher_boxscores`, `players`
3. Cumulative pitcher snapshots → `pitcher_stats_snapshots`
4. Team‑level cumulative snapshots → `team_stats_snapshots`

The script is idempotent thanks to `ON CONFLICT DO NOTHING` clauses, so it
can be rerun safely.

Usage
-----
```bash
python mlb_stats_loader.py            # run everything
python mlb_stats_loader.py --skip "team,player"  # skip listed stages
```
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from collections import defaultdict
from typing import Dict

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
LOG_FILE = os.path.join(LOG_DIR, f"mlb_stats_loader_{datetime.now():%Y%m%d_%H%M%S}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf‑8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

###############################################################################
# Helpers                                                                     #
###############################################################################

def pg_connect():
    conn = psycopg2.connect(
        keepalives       = 1,
        keepalives_idle  = 30,   # seconds
        keepalives_interval = 30,
        keepalives_count = 5,
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

def load_team_boxscores(session: requests.Session) -> None:
    # grab a Neon-friendly conn + correct search_path
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=DictCursor)
    # grab the set of all valid team_ids once up‐front
    cur.execute("SELECT id FROM teams")
    valid_team_ids = {r["id"] for r in cur.fetchall()}

    cur.execute("""
        SELECT game_id, date_played, away_team_id, home_team_id
          FROM mlb_game_outcomes
         ORDER BY date_played
    """)
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
            "ab": st.get("atBats", 0),
            "h":  st.get("hits", 0),
            "bb": st.get("baseOnBalls", 0),
            "hbp":st.get("hitByPitch", 0),
            "sf": st.get("sacrificeFlys", 0),
            "tb": st.get("totalBases", 0),
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
        if not {"away","home"}.issubset(teams):
            logger.warning("[Stage 1] Skip game %s (malformed JSON)", gid)
            continue

        # **skip any side whose team_id isn't in our teams table**
        if away_id not in valid_team_ids:
            logger.warning("[Stage 1] Unknown team_id %s for game %s, skipping away boxscore", away_id, gid)
        else:
            cur.execute(insert_sql, {
                "game_id":  gid,
                "game_date": gdate,
                "team_id":  away_id,
                "opp_id":   home_id,
                "side":     "away",
                **extract_raw_stats(teams["away"]),
            })
            rows_inserted += cur.rowcount

        if home_id not in valid_team_ids:
            logger.warning("[Stage 1] Unknown team_id %s for game %s, skipping home boxscore", home_id, gid)
        else:
            cur.execute(insert_sql, {
                "game_id":  gid,
                "game_date": gdate,
                "team_id":  home_id,
                "opp_id":   away_id,
                "side":     "home",
                **extract_raw_stats(teams["home"]),
            })
            rows_inserted += cur.rowcount

    conn.commit()
    logger.info("[Stage 1] Inserted %d new team boxscore rows", rows_inserted)

###############################################################################
# Stage 2 — Pitcher boxscores & player upserts                                #
###############################################################################

def upsert_player(cur: psycopg2.extensions.cursor, pid: int, team_id: int, first: str | None, last: str | None) -> None:
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
) -> None:
    """Populate `pitcher_boxscores` (with new workload columns) and ensure
    pitchers exist in `players`.  Safe to re-run for back-fills (UPSERT)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # valid MLB team IDs
    cur.execute("SELECT id FROM msf_mlb.teams;")
    valid_team_ids = {r["id"] for r in cur.fetchall()}

    # every game already in outcomes
    cur.execute("""
        SELECT game_id, date_played, away_team_id, home_team_id
          FROM msf_mlb.mlb_game_outcomes
         ORDER BY date_played, game_id
    """)
    games = cur.fetchall()
    logger.info("[Stage 2] Processing %d games for pitcher boxscores", len(games))

    # UPSERT with the NEW columns
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
                hits   = st.get("hits", 0)
                bb     = st.get("baseOnBalls", 0)
                hbp    = st.get("hitByPitch", 0)
                sf = st.get("sacFlies", st.get("sacrificeFlies", 0))
                runs   = st.get("runs", 0)
                er     = st.get("earnedRuns", 0)

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

                # derive at-bats just as before
                atb = max(0, bf - bb - hbp - sf)

                # ensure player exists
                person = pdata.get("person", {})
                upsert_player(cur, pid, tid, person.get("firstName"), person.get("lastName"))

                cur.execute(insert_pb, {
                    "game_id": gid, "player_id": pid, "side": side, "seq": seq,
                    "at_bats": atb, "hits": hits, "bb": bb, "hbp": hbp, "sf": sf,
                    "tb": tb, "runs": runs,
                    "outs": outs, "bf": bf, "pitches": pitches, "ks": ks, "er": er,
                    "date_played": gdate,
                })
                rows_upserted += 1  # count even if it overwrote zeros

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

    # running accumulators keyed by pitcher id
    accum: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    snapshots = 0

    for r in rows:
        pid = r["player_id"]
        dt  = r["date_played"]
        st  = accum[pid]

        # bump outings once per pitcher-game (sequence == 1)
        if r["sequence"] == 1:
            st["cum_outings"] += 1

        # running totals
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

        # rates
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

def fetch_season_stats(session: requests.Session, team_id: int, season: int = 2023) -> tuple[dict, dict]:
    """Return (offense_stat_dict, defense_stat_dict) for a given season."""
    url = f"{MLB_API_BASE}/teams/{team_id}/stats"

    # Offense
    params = {"stats": "season", "season": season, "group": "hitting"}
    off = session.get(url, params=params, timeout=10).json()["stats"][0]["splits"][0]["stat"]

    # Defense (pitching)
    params["group"] = "pitching"
    df = session.get(url, params=params, timeout=10).json()["stats"][0]["splits"][0]["stat"]
    return off, df


def build_team_snapshots(conn: psycopg2.extensions.connection, session: requests.Session) -> None:
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT id FROM teams")
    teams = [r["id"] for r in cur.fetchall()]

    logger.info("[Stage 4] Fetching 2023 baseline stats for %d teams", len(teams))

    team_state: dict[int, dict[str, int]] = {}
    for tid in teams:
        try:
            off, df = fetch_season_stats(session, tid, season=2023)
        except Exception as exc:
            logger.warning("[Stage 4] Skip team %s (fetch failed: %s)", tid, exc)
            continue

        team_state[tid] = {
            # Offense accumulators
            "cum_ab": off.get("atBats", 0),
            "cum_h": off.get("hits", 0),
            "cum_bb": off.get("baseOnBalls", 0),
            "cum_hbp": off.get("hitByPitch", 0),
            "cum_sf": off.get("sacrificeFlys", 0),
            "cum_tb": off.get("totalBases", 0),
            "cum_runs_scored": off.get("runs", 0),
            # Defense accumulators (allowed)
            "cum_allowed_ab": df.get("atBatsAgainst", 0),
            "cum_allowed_h": df.get("hitsAllowed", 0),
            "cum_allowed_bb": df.get("baseOnBallsAllowed", 0),
            "cum_allowed_hbp": df.get("hitByPitch", 0),
            "cum_allowed_tb": df.get("totalBasesAgainst", 0),
            "cum_runs_allowed": df.get("runsAllowed", 0),
        }

    insert_sql = (
        """
        INSERT INTO team_stats_snapshots (
            team_id, snapshot_date,
            cum_ab, cum_h, cum_bb, cum_hbp, cum_sf, cum_tb, obp, slg,
            cum_allowed_ab, cum_allowed_h, cum_allowed_bb, cum_allowed_hbp, cum_allowed_tb, allowed_obp, allowed_slg,
            cum_runs_scored, cum_runs_allowed
        ) VALUES (
            %(team_id)s, %(snapshot_date)s, %(cum_ab)s, %(cum_h)s, %(cum_bb)s, %(cum_hbp)s, %(cum_sf)s, %(cum_tb)s,
            %(obp)s, %(slg)s, %(cum_allowed_ab)s, %(cum_allowed_h)s, %(cum_allowed_bb)s, %(cum_allowed_hbp)s, %(cum_allowed_tb)s,
            %(allowed_obp)s, %(allowed_slg)s, %(cum_runs_scored)s, %(cum_runs_allowed)s
        ) ON CONFLICT (team_id, snapshot_date) DO NOTHING
        """
    )

    # Baseline snapshot date: MLB 2024 opening day
    baseline_date = date(2024, 3, 27)
    baseline_rows = 0
    for tid, s in team_state.items():
        obp = compute_rate(s["cum_h"] + s["cum_bb"] + s["cum_hbp"], s["cum_ab"] + s["cum_bb"] + s["cum_hbp"] + s["cum_sf"])
        slg = compute_rate(s["cum_tb"], s["cum_ab"])
        allowed_obp = compute_rate(s["cum_allowed_h"] + s["cum_allowed_bb"] + s["cum_allowed_hbp"], s["cum_allowed_ab"] + s["cum_allowed_bb"] + s["cum_allowed_hbp"])
        allowed_slg = compute_rate(s["cum_allowed_tb"], s["cum_allowed_ab"])

        cur.execute(
            insert_sql,
            {
                **s,
                "team_id": tid,
                "snapshot_date": baseline_date,
                "obp": obp,
                "slg": slg,
                "allowed_obp": allowed_obp,
                "allowed_slg": allowed_slg,
            },
        )
        baseline_rows += cur.rowcount

    logger.info("[Stage 4] Inserted %d baseline rows", baseline_rows)

    # Load all games & team boxscores for incremental updates
    cur.execute(
        """
        SELECT game_id, date_played, away_team_id, home_team_id, away_score, home_score
          FROM mlb_game_outcomes
         ORDER BY date_played, game_id
        """
    )
    games = cur.fetchall()
    cur.execute("SELECT * FROM team_boxscores")
    boxes = cur.fetchall()
    box_map = {(b["game_id"], b["team_id"]): b for b in boxes}

    snapshot_rows = 0
    for g in games:
        gid = g["game_id"]
        date_played = g["date_played"]

        for side in ("away", "home"):
            tid = g[f"{side}_team_id"]
            opp_tid = g["home_team_id"] if side == "away" else g["away_team_id"]
            rs = g[f"{side}_score"]
            ra = g[f"{'home' if side == 'away' else 'away'}_score"]
            # skip if no valid score
            if rs is None or ra is None:
                logger.warning("[Stage 4] Skipping team snapshot for game %s side=%s (missing score)", gid, side)
                continue

            box = box_map.get((gid, tid))
            opp_box = box_map.get((gid, opp_tid))
            if not box or not opp_box:
                logger.warning("[Stage 4] Missing boxscore for game %s team %s", gid, tid)
                continue

            st = team_state[tid]
            # Offense
            st["cum_ab"] += box["at_bats"]
            st["cum_h"] += box["hits"]
            st["cum_bb"] += box["base_on_balls"]
            st["cum_hbp"] += box["hit_by_pitch"]
            st["cum_sf"] += box["sacrifice_flys"]
            st["cum_tb"] += box["total_bases"]
            st["cum_runs_scored"] += rs
            # Defense (allowed)
            st["cum_allowed_ab"] += opp_box["at_bats"]
            st["cum_allowed_h"] += opp_box["hits"]
            st["cum_allowed_bb"] += opp_box["base_on_balls"]
            st["cum_allowed_hbp"] += opp_box["hit_by_pitch"]
            st["cum_allowed_tb"] += opp_box["total_bases"]
            st["cum_runs_allowed"] += ra

            obp = compute_rate(st["cum_h"] + st["cum_bb"] + st["cum_hbp"], st["cum_ab"] + st["cum_bb"] + st["cum_hbp"] + st["cum_sf"])
            slg = compute_rate(st["cum_tb"], st["cum_ab"])
            allowed_obp = compute_rate(st["cum_allowed_h"] + st["cum_allowed_bb"] + st["cum_allowed_hbp"], st["cum_allowed_ab"] + st["cum_allowed_bb"] + st["cum_allowed_hbp"])
            allowed_slg = compute_rate(st["cum_allowed_tb"], st["cum_allowed_ab"])

            cur.execute(
                insert_sql,
                {
                    **st,
                    "team_id": tid,
                    "snapshot_date": date_played,
                    "obp": obp,
                    "slg": slg,
                    "allowed_obp": allowed_obp,
                    "allowed_slg": allowed_slg,
                },
            )
            snapshot_rows += cur.rowcount

    conn.commit()
    logger.info("[Stage 4] Inserted %d incremental snapshot rows", snapshot_rows)

###############################################################################
# Main                                                                         #
###############################################################################

def run_pipeline(skip: list[str] | None = None):
    skip = set(s.strip().lower() for s in (skip or []))
    session = get_http_session()
    with pg_connect() as conn:
        if "team" not in skip:
            load_team_boxscores(conn, session)
        if "pitcher" not in skip:
            load_pitcher_boxscores(conn, session)
        if "pitcher_snapshots" not in skip:
            build_pitcher_snapshots(conn)
        if "team_snapshots" not in skip:
            build_team_snapshots(conn, session)


def main():
    parser = argparse.ArgumentParser(description="Populate MLB stats into Neon Postgres.")
    parser.add_argument(
        "--skip",
        help="Comma‑separated stage names to skip (team,pitcher,pitcher_snapshots,team_snapshots)",
    )
    args = parser.parse_args()
    skip = args.skip.split(",") if args.skip else []

    run_pipeline(skip)


if __name__ == "__main__":
    main()
