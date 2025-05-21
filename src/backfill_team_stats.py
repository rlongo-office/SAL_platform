#!/usr/bin/env python3
import os
import logging
import psycopg2
import psycopg2.extras
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import date
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & LOGGING ────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("POSTGRES_DB",   "SAL-db"),
    "user":     os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host":     os.getenv("POSTGRES_HOST", "localhost"),
    "port":     os.getenv("POSTGRES_PORT", "5432"),
}
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

# ─── HTTP SESSION WITH RETRIES ───────────────────────────────────────────────────
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET"],
)
session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=retry_strategy))

# ─── HELPERS ─────────────────────────────────────────────────────────────────────
def compute_rate(numer, denom):
    return round(numer/denom, 3) if denom and denom > 0 else 0.0

def fetch_season_stats(team_id, season=2023):
    """
    Returns two dicts: offense and defense raw counts from MLB API.
    offense keys: atBats,hits,baseOnBalls,hitByPitch,sacrificeFlys,totalBases,runs
    defense keys (from pitching group): atBatsAgainst,hitsAllowed,baseOnBallsAllowed,
      hitByPitch, totalBasesAgainst, runsAllowed
    """
    # OFFENSE
    url = f"{MLB_API_BASE}/teams/{team_id}/stats"
    params = {"stats":"season","season":season,"group":"hitting"}
    resp = session.get(url, params=params, timeout=10); resp.raise_for_status()
    off = resp.json()["stats"][0]["splits"][0]["stat"]
    # DEFENSE
    params["group"] = "pitching"
    resp = session.get(url, params=params, timeout=10); resp.raise_for_status()
    df = resp.json()["stats"][0]["splits"][0]["stat"]
    return off, df

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(**DB_PARAMS, options="-c search_path=msf_mlb,public")
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # 1) load baseline 2023 totals from API for every team
    cur.execute("SELECT id FROM msf_mlb.teams")
    teams = [r["id"] for r in cur.fetchall()]
    stats = {}
    for tid in teams:
        try:
            off, df = fetch_season_stats(tid, season=2023)
        except Exception as e:
            logger.warning("Failed to fetch 2023 for team %s: %s", tid, e)
            continue

        # unpack offense
        ab   = off.get("atBats",     0)
        h    = off.get("hits",       0)
        bb   = off.get("baseOnBalls",0)
        hbp  = off.get("hitByPitch", 0)
        sf   = off.get("sacrificeFlys",0)
        tb   = off.get("totalBases", 0)
        rs   = off.get("runs",       0)

        # unpack defense
        aba  = df.get("atBatsAgainst",     0)
        ha   = df.get("hitsAllowed",       0)
        bba  = df.get("baseOnBallsAllowed",0)
        hbpa = df.get("hitByPitch",        0)
        tba  = df.get("totalBasesAgainst", 0)
        ra   = df.get("runsAllowed",       0)

        stats[tid] = {
            "cum_ab": ab,   "cum_h": h,   "cum_bb": bb,
            "cum_hbp": hbp, "cum_sf": sf, "cum_tb": tb,
            "cum_runs_scored": rs,
            "cum_allowed_ab": aba,  "cum_allowed_h": ha,
            "cum_allowed_bb": bba,  "cum_allowed_hbp": hbpa,
            "cum_allowed_tb": tba,  "cum_runs_allowed": ra,
        }

    # 2) insert baseline row for each team at d=0 = 2024-03-27
    insert_sql = """
    INSERT INTO msf_mlb.team_stats_snapshots (
      team_id, snapshot_date,
      cum_ab,cum_h,cum_bb,cum_hbp,cum_sf,cum_tb, obp, slg,
      cum_allowed_ab,cum_allowed_h,cum_allowed_bb,cum_allowed_hbp,cum_allowed_tb, allowed_obp, allowed_slg,
      cum_runs_scored,cum_runs_allowed
    ) VALUES (
      %(team_id)s, %(snap_date)s,
      %(cum_ab)s,%(cum_h)s,%(cum_bb)s,%(cum_hbp)s,%(cum_sf)s,%(cum_tb)s,
        %(obp)s, %(slg)s,
      %(cum_allowed_ab)s,%(cum_allowed_h)s,%(cum_allowed_bb)s,%(cum_allowed_hbp)s,%(cum_allowed_tb)s,
        %(allowed_obp)s, %(allowed_slg)s,
      %(cum_runs_scored)s,%(cum_runs_allowed)s
    )
    ON CONFLICT (team_id, snapshot_date) DO NOTHING
    """
    snap0 = date(2024,3,27)
    for tid, s in stats.items():
        obp = compute_rate(s["cum_h"]+s["cum_bb"]+s["cum_hbp"],
                           s["cum_ab"]+s["cum_bb"]+s["cum_hbp"]+s["cum_sf"])
        slg = compute_rate(s["cum_tb"], s["cum_ab"])
        a_obp = compute_rate(s["cum_allowed_h"]+s["cum_allowed_bb"]+s["cum_allowed_hbp"],
                             s["cum_allowed_ab"]+s["cum_allowed_bb"]+s["cum_allowed_hbp"])
        a_slg = compute_rate(s["cum_allowed_tb"], s["cum_allowed_ab"])
        cur.execute(insert_sql, {
            **s,
            "team_id": tid, "snap_date": snap0,
            "obp": obp, "slg": slg,
            "allowed_obp": a_obp, "allowed_slg": a_slg,
        })

    conn.commit()
    logger.info("Inserted baseline snapshots for %d teams", len(stats))

    # 3) now walk through every game in date order and append a row *after* each game
    cur.execute("""
      SELECT
        o.game_id, o.date_played,
        o.away_team_id, o.home_team_id,
        o.away_score,   o.home_score
      FROM msf_mlb.mlb_game_outcomes o
      ORDER BY o.date_played, o.game_id
    """)
    games = cur.fetchall()

    # pull all boxscores into memory for lookup
    cur.execute("SELECT * FROM msf_mlb.team_boxscores")
    boxes = cur.fetchall()
    # index by (game_id, team_id)
    boxmap = {(b["game_id"], b["team_id"]): b for b in boxes}

    for g in games:
        gid = g["game_id"]
        date_played = g["date_played"]

        for side in ("away", "home"):
            tid   = g[f"{side}_team_id"]
            opp   = g["home_team_id"] if side=="away" else g["away_team_id"]
            rs    = g[f"{side}_score"]
            ra    = g[f"{'home' if side=='away' else 'away'}_score"]

            b = boxmap.get((gid, tid))
            if not b:
                logger.warning("Missing boxscore for game %s team %s", gid, tid)
                continue

            # update cumulative
            st = stats[tid]
            st["cum_ab"]   += b["at_bats"]
            st["cum_h"]    += b["hits"]
            st["cum_bb"]   += b["base_on_balls"]
            st["cum_hbp"]  += b["hit_by_pitch"]
            st["cum_sf"]   += b["sacrifice_flys"]
            st["cum_tb"]   += b["total_bases"]
            st["cum_runs_scored"] += rs

            # allowed = opponent’s batting
            st["cum_allowed_ab"]  += boxmap[(gid, opp)]["at_bats"]
            st["cum_allowed_h"]   += boxmap[(gid, opp)]["hits"]
            st["cum_allowed_bb"]  += boxmap[(gid, opp)]["base_on_balls"]
            st["cum_allowed_hbp"] += boxmap[(gid, opp)]["hit_by_pitch"]
            st["cum_allowed_tb"]  += boxmap[(gid, opp)]["total_bases"]
            st["cum_runs_allowed"] += ra

            # recompute rates
            obp = compute_rate(st["cum_h"]+st["cum_bb"]+st["cum_hbp"],
                               st["cum_ab"]+st["cum_bb"]+st["cum_hbp"]+st["cum_sf"])
            slg = compute_rate(st["cum_tb"], st["cum_ab"])
            a_obp = compute_rate(
                st["cum_allowed_h"]+st["cum_allowed_bb"]+st["cum_allowed_hbp"],
                st["cum_allowed_ab"]+st["cum_allowed_bb"]+st["cum_allowed_hbp"]
            )
            a_slg = compute_rate(st["cum_allowed_tb"], st["cum_allowed_ab"])

            # insert new snapshot
            cur.execute(insert_sql, {
                **st,
                "team_id": tid,
                "snap_date": date_played,
                "obp": obp, "slg": slg,
                "allowed_obp": a_obp, "allowed_slg": a_slg,
            })

        # commit every N games or so to avoid huge transactions
        conn.commit()

    logger.info("Done backfilling %d game snapshots", len(games)*2)
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
