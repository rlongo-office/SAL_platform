#!/usr/bin/env python3
import os
import logging
import psycopg2
import psycopg2.extras
from collections import defaultdict
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & LOGGING ────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   "neondb",
    "user":     "neondb_owner",
    "password": "npg_aKWdUeCXV10c",
    "host":     "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech",
    "port":     "5432",
    "sslmode":  "require",
}

logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), "..", "logs", "pitcher_snapshots.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger("pitcher_snapshots")

def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as c:
        c.execute("SET search_path TO msf_mlb,public;")
    return conn

def compute_rate(numer: int, denom: int) -> float:
    return round(numer/denom, 3) if denom and denom > 0 else 0.0

def main():
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # 1) pull all per‐game pitcher boxscores in order
    cur.execute("""
      SELECT
        player_id, date_played,
        at_bats, hits_allowed, bb_allowed,
        hbp_allowed, sf_allowed, total_bases
      FROM msf_mlb.pitcher_boxscores
      ORDER BY player_id, date_played, sequence
    """)
    rows = cur.fetchall()
    logger.info("Fetched %d pitcher‐boxscore rows", len(rows))

    # 2) prepare accumulator & insert SQL
    accum = defaultdict(lambda: {
        "cum_ab":0, "cum_h":0, "cum_bb":0,
        "cum_hbp":0, "cum_sf":0, "cum_tb":0
    })
    insert_sql = """
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

    # 3) walk through each boxscore to build snapshots
    for r in rows:
        pid  = r["player_id"]
        dt   = r["date_played"]
        st   = accum[pid]

        # update running totals
        st["cum_ab"]  += r["at_bats"]
        st["cum_h"]   += r["hits_allowed"]
        st["cum_bb"]  += r["bb_allowed"]
        st["cum_hbp"] += r["hbp_allowed"]
        st["cum_sf"]  += r["sf_allowed"]
        st["cum_tb"]  += r["total_bases"]

        # compute cumulative rates
        obp = compute_rate(
            st["cum_h"] + st["cum_bb"] + st["cum_hbp"],
            st["cum_ab"] + st["cum_bb"] + st["cum_hbp"] + st["cum_sf"]
        )
        slg = compute_rate(st["cum_tb"], st["cum_ab"])

        # insert snapshot
        cur.execute(insert_sql, {
            "player_id":     pid,
            "snapshot_date": dt,
            "cum_ab":        st["cum_ab"],
            "cum_h":         st["cum_h"],
            "cum_bb":        st["cum_bb"],
            "cum_hbp":       st["cum_hbp"],
            "cum_sf":        st["cum_sf"],
            "cum_tb":        st["cum_tb"],
            "obp":           obp,
            "slg":           slg,
        })

    conn.commit()
    logger.info("Inserted snapshots for %d pitchers", len(accum))
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
