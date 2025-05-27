#!/usr/bin/env python3
"""
validate_game_odds_alignment.py

Enhanced validation with spot-check CSV export:
  • raw Odds-API names canonicalize → mlb_game_outcomes teams
  • snapshot occurs before game_time
  • game_date (from game_time) within ±1 day of date_played
  • logs detailed pass/fail stats
  • exports a CSV of two categories for spot-checking:
      - 'logical_valid' (passed logic but not strict minute match)
      - 'invalid'
"""

import os
import logging
from datetime import datetime
import psycopg2
import pandas as pd
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG & LOGGING ───────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB = dict(
    dbname   = os.getenv("DB_NAME",   "neondb"),
    user     = os.getenv("DB_USER",   "neondb_owner"),
    password = os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT",   "5432"),
    sslmode  = "require",
)
LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"validate_game_odds_{datetime.now():%Y%m%d_%H%M%S}.log")
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

# custom name corrections (raw -> canonical)
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
    return _CANON.get(name.strip().lower(), name.strip().lower())

# ─── DATABASE CONNECT ─────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── DATA LOAD ───────────────────────────────────────────────────────────────
def load_data() -> pd.DataFrame:
    logger.info("Loading game_odds + outcomes + teams...")
    conn = pg_connect()
    df = pd.read_sql(
        """
        SELECT
          go.id            AS game_odds_id,
          go.home_team     AS raw_home,
          go.away_team     AS raw_away,
          go.as_of_time,
          go.game_time,
          mo.date_played,
          LOWER(ht.locationname || ' ' || ht.teamname) AS expected_home,
          LOWER(at.locationname || ' ' || at.teamname) AS expected_away
        FROM msf_mlb.game_odds go
        JOIN msf_mlb.mlb_game_outcomes mo
          ON go.mlb_game_pk = mo.game_id
        JOIN msf_mlb.teams ht ON mo.home_team_id = ht.id
        JOIN msf_mlb.teams at ON mo.away_team_id = at.id
        WHERE go.home_team IS NOT NULL
          AND go.away_team IS NOT NULL
        """,
        conn,
        parse_dates=["as_of_time", "game_time"]
    )
    conn.close()
    logger.info(f"Loaded {len(df)} rows.")
    return df

# ─── VALIDATION ──────────────────────────────────────────────────────────────
def validate(df: pd.DataFrame) -> pd.DataFrame:
    logger.info("Starting validation...")
    # tz-aware
    df["as_of_time"] = pd.to_datetime(df["as_of_time"], utc=True)
    df["game_time"]  = pd.to_datetime(df["game_time"],  utc=True)

    # names
    df["canon_home"] = df["raw_home"].map(canonicalize)
    df["canon_away"] = df["raw_away"].map(canonicalize)
    df["match_home"] = df["canon_home"] == df["expected_home"]
    df["match_away"] = df["canon_away"] == df["expected_away"]

    # time before game
    df["time_diff_min"] = (
        df["game_time"] - df["as_of_time"]
    ).dt.total_seconds().div(60).round()
    df["time_ok"] = df["time_diff_min"] >= 0

        # date ±1 day
    df["game_date"] = df["game_time"].dt.date
    df["date_diff"] = (
        pd.to_datetime(df["game_date"]) - pd.to_datetime(df["date_played"])
    ).dt.days.abs()
    df["date_ok"] = df["date_diff"] <= 1

    # strict minute-match (game_time vs date_played to minute) (game_time vs date_played to minute)
    # Since date_played is date-only, strict means date_ok & time_diff_min == 0
    df["strict_ok"] = df["time_diff_min"] == 0

    # logical valid (passed logic but not strict)
    df["logical_valid"] = (
        df["match_home"] & df["match_away"] &
        df["time_ok"] & df["date_ok"] & ~df["strict_ok"]
    )

    # final valid = logical_valid OR strict
    df["valid"] = df["logical_valid"] | df["strict_ok"]

    logger.info("Validation checks complete.")
    return df

# ─── EXPORT SPOTCHECK CSV ─────────────────────────────────────────────────────
def export_spotcheck(df: pd.DataFrame):
    # select rows for logical_valid and invalid only
    spot = df[df["logical_valid"] | ~df["valid"]].copy()
    spot["category"] = spot.apply(
        lambda r: 'logical_valid' if r['logical_valid'] else 'invalid', axis=1
    )
    # define columns to keep
    cols = [
        'game_odds_id','raw_home','expected_home',
        'raw_away','expected_away','time_diff_min',
        'date_played','game_date','date_diff',
        'match_home','match_away','time_ok','date_ok',
        'strict_ok','category'
    ]
    out_dir = os.path.abspath(os.path.join(os.getcwd(), os.pardir, 'output'))
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, 'validate_game_odds_spotcheck.csv')
    spot[cols].to_csv(out_file, index=False)
    logger.info(f"Wrote spotcheck CSV ({len(spot)} rows) to {out_file}")

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
def summarize(df: pd.DataFrame):
    total = len(df)
    valid = df["valid"].sum()
    invalid = total - valid
    logger.info(f"Total={total}, Valid={valid}, Invalid={invalid}")
    print(f"\nChecked {total} records")
    print(f"  ✔ Valid   = {valid}")
    print(f"  ✘ Invalid = {invalid} ({invalid/total:.1%})\n")

# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    logger.info("=== Starting validation ===")
    df = load_data()
    df = validate(df)
    summarize(df)
    export_spotcheck(df)
    logger.info("=== Validation finished ===")

if __name__ == "__main__":
    main()
