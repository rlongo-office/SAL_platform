#!/usr/bin/env python3
import os
import re
import json
import glob
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import psycopg2
import psycopg2.extras
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict
from dotenv import load_dotenv, find_dotenv

# ─── HTTP SESSION WITH RETRIES ─────────────────────────────────────────────────
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

# ─── CONFIG ─────────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("POSTGRES_DB",   "SAL-db"),
    "user":     os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host":     os.getenv("POSTGRES_HOST", "localhost"),
    "port":     os.getenv("POSTGRES_PORT", "5432"),
}
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

SCRIPT_DIR  = os.path.dirname(__file__)
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
ODDS_DIR    = os.path.join(PROJECT_DIR, "output", "mlb")
ODDS_GLOB   = os.path.join(ODDS_DIR, "historical_mlb_*.json")

# ─── LOGGING ────────────────────────────────────────────────────────────────────
LOG_DIR  = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "mlb_outcomes.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")]
)
logger = logging.getLogger(__name__)

# ─── DATABASE ───────────────────────────────────────────────────────────────────
def pg_connect():
    return psycopg2.connect(
        **DB_PARAMS,
        options="-c search_path=msf_mlb,public",
        cursor_factory=psycopg2.extras.DictCursor
    )

# ─── HELPERS ────────────────────────────────────────────────────────────────────
def clean_name(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())

def load_team_name_map():
    resp = session.get(f"{MLB_API_BASE}/teams", params={"sportId": 1}, timeout=10)
    resp.raise_for_status()

    out = {}
    athletics_id = None

    for t in resp.json()["teams"]:
        city = t["locationName"]
        nick = t["teamName"]
        full = f"{city} {nick}"
        abbr = t["abbreviation"]
        for raw in {t["name"], nick, full, city, abbr}:
            out[clean_name(raw)] = t["id"]
        if nick.lower() == "athletics":
            athletics_id = t["id"]

    # backfill plain 'oakland' / 'oaklandathletics'
    if athletics_id:
        out["oakland"] = athletics_id
        out["oaklandathletics"] = athletics_id

    logger.info("Loaded team-name map with %d entries", len(out))
    return out

def load_mlbgames_for_date(date_str: str) -> list:
    base = date.fromisoformat(date_str)
    all_games = []

    for delta in (0, 1):                                # today and tomorrow UTC
        d = (base + timedelta(days=delta)).isoformat()
        resp = session.get(
            f"{MLB_API_BASE}/schedule",
            params={ "sportId": 1, "date": d, "hydrate": "teams,linescore" },
            timeout=10
        )
        resp.raise_for_status()
        for day in resp.json().get("dates", []):
            for g in day["games"]:
                if g["status"]["detailedState"] != "Final":
                    continue
                all_games.append({
                    "gamePk":    g["gamePk"],
                    "away_id":   g["teams"]["away"]["team"]["id"],
                    "home_id":   g["teams"]["home"]["team"]["id"],
                    "away_runs": g["linescore"]["teams"]["away"]["runs"],
                    "home_runs": g["linescore"]["teams"]["home"]["runs"],
                    "start":     datetime.fromisoformat(
                                     g["gameDate"].replace("Z", "+00:00")
                                 )
                })
    return all_games

def load_mlbgames_for_window(date_str: str) -> list:
    """
    Merge two back-to-back local dates (date_str and date_str+1)
    so that any late-night crossings get included.
    """
    d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    all_games = []
    for offset in (-1, 0, 1):
        d = d0 + timedelta(days=offset)
        all_games.extend(load_mlbgames_for_date(d.isoformat()))
    logger.info("  → fetched %d final games for %s + %s",
                len(all_games),
                d0.isoformat(),
                (d0 + timedelta(days=1)).isoformat())
    return all_games

def build_doubleheader_map(games: list) -> dict:
    by_pair = defaultdict(list)
    for g in sorted(games, key=lambda x: x["start"]):
        by_pair[(g["away_id"], g["home_id"])].append(g["gamePk"])
    return by_pair

def find_correct_pk(pair_map, away, home, used_counter):
    cands = pair_map.get((away, home), [])
    if not cands:
        return None
    idx = used_counter[(away, home)]
    used_counter[(away, home)] += 1
    return cands[idx % len(cands)]

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    conn = pg_connect()
    cur  = conn.cursor()
    team_map = load_team_name_map()

    logger.info("LOOKING in %s for JSON files", ODDS_DIR)
    for odds_path in sorted(glob.glob(ODDS_GLOB)):
        date_str = os.path.basename(odds_path) \
                     .replace("historical_mlb_", "") \
                     .replace(".json","")
        logger.info("PROCESSING odds for %s", date_str)

        # 1) pull *two* days of final games
        mlb_games = load_mlbgames_for_window(date_str)

        # 2) build DH map
        dh_map     = build_doubleheader_map(mlb_games)
        used_count = defaultdict(int)

        # 3) open odds snapshots
        with open(odds_path, "r", encoding="utf-8") as f:
            odds_data = json.load(f)

        for snapshot_ts, snapshot in odds_data.items():
            for rec in snapshot.get("data", []):
                raw_home  = rec.get("home_team", "")
                raw_away  = rec.get("away_team", "")
                clean_home = clean_name(raw_home)
                clean_away = clean_name(raw_away)
                home_id   = team_map.get(clean_home)
                away_id   = team_map.get(clean_away)

                if home_id is None or away_id is None:
                    logger.warning(
                        "SKIPPED unknown team → odds away='%s' (clean='%s'), home='%s' (clean='%s')",
                        raw_away, clean_away, raw_home, clean_home
                    )
                    continue

                # 4) pick the next gamePk in FIFO order
                game_pk = find_correct_pk(dh_map, away_id, home_id, used_count)
                if game_pk is None:
                    logger.warning(
                        "SKIPPED no schedule match → odds %s@%s on %s",
                        raw_away, raw_home, date_str
                    )
                    continue

                # 5) pull runs from our mlb_games list
                match = next((g for g in mlb_games if g["gamePk"] == game_pk), None)
                if not match:
                    logger.warning("SKIPPED missing final data for gamePk=%s", game_pk)
                    continue

                away_runs = match["away_runs"]
                home_runs = match["home_runs"]
                winner, loser = (
                    ("away","home") if away_runs > home_runs else ("home","away")
                )

                # 6) upsert outcome
                cur.execute("""
                    INSERT INTO mlb_game_outcomes
                      (game_id, date_played, away_team_id, home_team_id,
                       away_score, home_score, winner, loser, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (game_id) DO UPDATE
                      SET away_score = EXCLUDED.away_score,
                          home_score = EXCLUDED.home_score,
                          winner     = EXCLUDED.winner,
                          loser      = EXCLUDED.loser
                """, (
                    game_pk, date_str, away_id, home_id,
                    away_runs, home_runs, winner, loser
                ))
                logger.info("WROTE gamePk=%s %s-%s", game_pk, away_runs, home_runs)

                # the JSON’s `id` field is actually the odds “game_id” we want to match
                odds_game_id = rec.get("id")
                if odds_game_id:
                    cur.execute("""
                        UPDATE msf_mlb.game_odds
                           SET mlb_game_pk = %s
                         WHERE game_id       = %s
                           AND mlb_game_pk IS NULL
                    """, (game_pk, odds_game_id))

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
