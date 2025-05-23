#!/usr/bin/env python3
import os
import logging
import psycopg2
import psycopg2.extras
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import defaultdict
from datetime import date
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ──────────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":  "neondb",
    "user":    "neondb_owner",
    "password":"npg_aKWdUeCXV10c",
    "host":    "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech",
    "port":    "5432",
    "sslmode": "require"
}
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

# where to dump logs
SCRIPT_DIR  = os.path.dirname(__file__)
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
LOG_DIR     = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ─── LOGGING ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "pitcher_loader.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("pitcher_loader")

# ─── HTTP SESSION ─────────────────────────────────────────────────────────────────
retry = Retry(total=5, backoff_factor=0.5, status_forcelist=[429,500,502,503,504])
sess  = requests.Session()
sess.mount("https://", HTTPAdapter(max_retries=retry))

# ─── HELPERS ─────────────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
        db, schema = cur.fetchone() if False else (None,None)
        # we already set search_path, no need to fetch names
    return conn

def upsert_player(cur, pid, team_id, first, last):
    cur.execute("""
      INSERT INTO msf_mlb.players (id, team_id, first_name, last_name, position_group)
      VALUES (%s, %s, %s, %s, 'Pitcher')
      ON CONFLICT (id) DO NOTHING
    """, (pid, team_id, first, last))

def compute_rate(numer, denom):
    return round(numer/denom, 3) if denom and denom>0 else 0.0

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # 1) load every game's boxscore
    cur.execute("""
      SELECT game_id, date_played, away_team_id, home_team_id
        FROM msf_mlb.mlb_game_outcomes
       ORDER BY date_played, game_id
    """)
    games = cur.fetchall()
    logger.info("Found %d games to process", len(games))

    insert_pb = """
      INSERT INTO msf_mlb.pitcher_boxscores (
        game_id, player_id, side, sequence,
        at_bats, hits_allowed, bb_allowed,
        hbp_allowed, sf_allowed, total_bases,
        runs_allowed, date_played
      ) VALUES (
        %(game_id)s, %(player_id)s, %(side)s, %(seq)s,
        %(at_bats)s, %(hits)s, %(bb)s,
        %(hbp)s, %(sf)s, %(tb)s,
        %(runs)s, %(date_played)s
      )
      ON CONFLICT (game_id, player_id, sequence) DO NOTHING
    """

    for g in games:
        gid, gdate, away_tid, home_tid = g
        logger.info("Processing game %s (%s): %s @ %s", gid, gdate, away_tid, home_tid)

        # fetch boxscore from MLB API
        url = f"{MLB_API_BASE}/game/{gid}/boxscore"
        try:
            resp = sess.get(url, timeout=10); resp.raise_for_status()
        except Exception as e:
            logger.warning(" → skipping %s: fetch failed %s", gid, e)
            continue

        teams = resp.json().get("teams", {})
        total_inserts = 0

        for side in ("away","home"):
            plist = teams.get(side,{}).get("pitchers", [])
            pmap  = teams.get(side,{}).get("players", {})
            logger.info("  %s side: %d pitchers", side, len(plist))

            for seq,pid in enumerate(plist, start=1):
                p = pmap.get(f"ID{pid}")
                if not p or "stats" not in p or "pitching" not in p["stats"]:
                    logger.warning("    [%s] pid=%s missing stats", side, pid)
                    continue
                st = p["stats"]["pitching"]

                # corrected: use "totalBases" not "totalBasesAgainst"
                hits    = st.get("hits",    0)
                doubles = st.get("doubles", 0)
                triples = st.get("triples", 0)
                homers  = st.get("homeRuns", 0)

                # compute singles and total bases allowed
                singles = hits - (doubles + triples + homers)
                tb      = max(0,
                            singles
                            + 2 * doubles
                            + 3 * triples
                            + 4 * homers
                            )

                bb   = st.get("baseOnBalls",   0)
                hbp  = st.get("hitByPitch",    0)
                sf   = st.get("sacrificeFlys", 0)
                runs = st.get("runs",          0)
                bf   = st.get("battersFaced",  0)
                atb  = bf - bb - hbp - sf


                # upsert into players
                person = p.get("person",{})
                upsert_player(cur, pid, away_tid if side=="away" else home_tid,
                              person.get("firstName"), person.get("lastName"))

                cur.execute(insert_pb, {
                    "game_id":     gid,
                    "player_id":   pid,
                    "side":        side,
                    "seq":         seq,
                    "at_bats":     max(atb,0),
                    "hits":        hits,
                    "bb":          bb,
                    "hbp":         hbp,
                    "sf":          sf,
                    "tb":          tb,
                    "runs":        runs,
                    "date_played": gdate
                })
                total_inserts += cur.rowcount

        conn.commit()
        logger.info(" → game %s done: inserted %d rows", gid, total_inserts)

    # 2) backfill cumulative snapshots
    logger.info("Building pitcher_stats_snapshots …")
    cur.execute("""
      SELECT player_id, date_played,
             at_bats, hits_allowed, bb_allowed,
             hbp_allowed, sf_allowed, total_bases
        FROM msf_mlb.pitcher_boxscores
       ORDER BY player_id, date_played, sequence
    """)
    rows = cur.fetchall()
    logger.info(" → fetched %d pitcher_boxscore rows", len(rows))

    accum = defaultdict(lambda: {
        "cum_ab":0, "cum_h":0, "cum_bb":0,
        "cum_hbp":0,"cum_sf":0, "cum_tb":0
    })
    insert_ps = """
      INSERT INTO msf_mlb.pitcher_stats_snapshots (
        player_id, snapshot_date,
        cum_ab, cum_h, cum_bb, cum_hbp, cum_sf, cum_tb,
        obp_allowed, slg_allowed
      ) VALUES (
        %(player_id)s, %(snapshot_date)s,
        %(cum_ab)s, %(cum_h)s, %(cum_bb)s, %(cum_hbp)s, %(cum_sf)s, %(cum_tb)s,
        %(obp)s, %(slg)s
      )
      ON CONFLICT (player_id, snapshot_date) DO NOTHING
    """

    for r in rows:
        pid = r["player_id"]
        dt  = r["date_played"]
        st  = accum[pid]

        # accumulate
        st["cum_ab"]  += r["at_bats"]
        st["cum_h"]   += r["hits_allowed"]
        st["cum_bb"]  += r["bb_allowed"]
        st["cum_hbp"] += r["hbp_allowed"]
        st["cum_sf"]  += r["sf_allowed"]
        st["cum_tb"]  += r["total_bases"]

        obp = compute_rate(
            st["cum_h"] + st["cum_bb"] + st["cum_hbp"],
            st["cum_ab"] + st["cum_bb"] + st["cum_hbp"] + st["cum_sf"]
        )
        slg = compute_rate(st["cum_tb"], st["cum_ab"])

        cur.execute(insert_ps, {
            "player_id":     pid,
            "snapshot_date": dt,
            "cum_ab":        st["cum_ab"],
            "cum_h":         st["cum_h"],
            "cum_bb":        st["cum_bb"],
            "cum_hbp":       st["cum_hbp"],
            "cum_sf":        st["cum_sf"],
            "cum_tb":        st["cum_tb"],
            "obp":           obp,
            "slg":           slg
        })

    conn.commit()
    logger.info("Inserted snapshots for %d pitchers", len(accum))

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
