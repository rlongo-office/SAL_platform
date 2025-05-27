#!/usr/bin/env python3
import os
import glob
import json
import psycopg2
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ───────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("DB_NAME",   "neondb"),
    "user":     os.getenv("DB_USER",   "neondb_owner"),
    "password": os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    "host":     os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    "port":     os.getenv("DB_PORT",   "5432"),
    "sslmode":  "require",
}

# point at project_root/output/mlb
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ODDS_DIR = os.path.join(BASE_DIR, "output", "mlb")

# ─── DB CONNECT ────────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO msf_mlb,public;")
    return conn

def main():
    conn    = pg_connect()
    cur     = conn.cursor()
    files   = glob.glob(os.path.join(ODDS_DIR, "*.json"))
    print(f"Found {len(files)} odds files under {ODDS_DIR}")

    updated = 0
    missing = []

    for path in sorted(files):
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        # each top-level key is a timestamp, with a "data" array
        for block in payload.values():
            for game in block.get("data", []):
                odds_id       = game.get("id")
                home_team     = game.get("home_team")
                away_team     = game.get("away_team")
                commence_time = game.get("commence_time")

                # skip any incomplete records
                if not odds_id or home_team is None or away_team is None or commence_time is None:
                    continue

                cur.execute(
                    """
                    UPDATE game_odds
                       SET home_team = %s
                         , away_team = %s
                         , game_time  = %s
                     WHERE game_id  = %s
                    """,
                    (home_team, away_team, commence_time, odds_id)
                )
                if cur.rowcount == 0:
                    missing.append(odds_id)
                else:
                    updated += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    print(f"Finished: {updated} rows updated.")
    if missing:
        print(f"WARNING: no matching game_odds rows for {len(missing)} ids:")
        for oid in missing:
            print("  •", oid)

if __name__ == "__main__":
    main()
