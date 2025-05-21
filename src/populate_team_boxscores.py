#!/usr/bin/env python3
import os
import logging
import psycopg2
import psycopg2.extras
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ─── HTTP SESSION WITH RETRIES ───────────────────────────────────────────────────
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET"],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    # connect to DB
    conn = psycopg2.connect(**DB_PARAMS, options="-c search_path=msf_mlb,public")
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # fetch all games
    cur.execute("""
      SELECT game_id, date_played, away_team_id, home_team_id
      FROM msf_mlb.mlb_game_outcomes
      ORDER BY date_played;
    """)
    games = cur.fetchall()
    logger.info("Fetched %d games to backfill boxscores", len(games))

    # prepare insert (no OBP/SLG columns)
    insert_sql = """
      INSERT INTO msf_mlb.team_boxscores (
        game_id, game_date,
        team_id, opponent_id,
        side,
        at_bats, hits,
        base_on_balls, hit_by_pitch,
        sacrifice_flys, total_bases
      ) VALUES (
        %(game_id)s, %(game_date)s,
        %(team_id)s, %(opp_id)s,
        %(side)s,
        %(ab)s, %(h)s,
        %(bb)s, %(hbp)s,
        %(sf)s, %(tb)s
      )
      ON CONFLICT (game_id, team_id) DO NOTHING
    """

    for g in games:
        game_id   = g["game_id"]
        game_date = g["date_played"]
        away_id   = g["away_team_id"]
        home_id   = g["home_team_id"]

        # fetch boxscore from MLB API
        url = f"{MLB_API_BASE}/game/{game_id}/boxscore"
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("Skipping %s: boxscore fetch failed (%s)", game_id, e)
            continue

        teams_data = resp.json().get("teams", {})
        if "away" not in teams_data or "home" not in teams_data:
            logger.warning("Skipping %s: malformed boxscore JSON", game_id)
            continue

        # helper to extract raw batting stats
        def extract_raw_stats(side_data):
            st = side_data["teamStats"]["batting"]
            return {
                "ab":  st.get("atBats", 0),
                "h":   st.get("hits", 0),
                "bb":  st.get("baseOnBalls", 0),
                "hbp": st.get("hitByPitch", 0),
                "sf":  st.get("sacrificeFlys", 0),
                "tb":  st.get("totalBases", 0),
            }

        # process away team
        away_stats = extract_raw_stats(teams_data["away"])
        cur.execute(insert_sql, {
            "game_id":   game_id,
            "game_date": game_date,
            "team_id":   away_id,
            "opp_id":    home_id,
            "side":      "away",
            **away_stats
        })

        # process home team
        home_stats = extract_raw_stats(teams_data["home"])
        cur.execute(insert_sql, {
            "game_id":   game_id,
            "game_date": game_date,
            "team_id":   home_id,
            "opp_id":    away_id,
            "side":      "home",
            **home_stats
        })

        logger.info("Inserted boxscores for game %s", game_id)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Done.")

if __name__ == "__main__":
    main()
