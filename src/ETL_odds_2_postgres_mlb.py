#!/usr/bin/env python3
import os
import glob
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from psycopg2 import connect

# ─── CONFIGURATION ───────────────────────────────────────────────────────────────
load_dotenv()  # expects a .env in your project root

# determine project root (one level above this script)
BASE_DIR   = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# where your daily JSON files live and where logs go
OUTPUT_DIR = os.path.join(BASE_DIR, "output", "mlb")
LOG_DIR    = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Postgres connection params (make sure your .env has these set)
DB_PARAMS = {
    "dbname":   "SAL-db",
    "user":     "postgres",
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host":     "localhost",
    "port":     "5432",
}

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(
    filename=os.path.join(LOG_DIR, f"etl_mlb_{datetime.now():%Y%m%d_%H%M%S}.log"),
    level=logging.DEBUG,
    format=LOG_FORMAT
)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
console.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(console)

# ─── HELPERS ──────────────────────────────────────────────────────────────────────
def upsert_book(cur, title):
    """Ensure bookmaker exists in msf_mlb.books; return its id."""
    cur.execute("SELECT id FROM msf_mlb.books WHERE name = %s", (title,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO msf_mlb.books (name, region, is_online, is_las_vegas)
        VALUES (%s, NULL, TRUE, FALSE)
        RETURNING id
        """,
        (title,)
    )
    return cur.fetchone()[0]

# ─── ETL FUNCTION ────────────────────────────────────────────────────────────────
def process_file(conn, filepath):
    logging.debug(f"→ Starting file: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        snapshots = json.load(f)
    logging.debug(f"   Loaded JSON: {len(snapshots)} snapshots")

    cur = conn.cursor()
    total_odds_rows = 0

    for snapshot_ts, payload in snapshots.items():
        logging.debug(f"   Snapshot: {snapshot_ts}")
        # parse ISO timestamp (drop trailing Z)
        try:
            as_of_time = datetime.fromisoformat(snapshot_ts.rstrip("Z"))
        except ValueError:
            logging.warning(f"   ✗ Bad timestamp format: {snapshot_ts}, skipping")
            continue

        for game in payload.get("data", []):
            api_game_id = game.get("id")
            if not api_game_id:
                logging.debug("      ✗ Missing game.id, skipping game entry")
                continue
            home_team = game.get("home_team")
            away_team = game.get("away_team")

            for bookmaker in game.get("bookmakers", []):
                book_title = bookmaker.get("title")
                if not book_title:
                    logging.debug("      ✗ Missing bookmaker.title, skipping")
                    continue

                # 1) Upsert book
                book_id = upsert_book(cur, book_title)

                # 2) For each market: insert game_odds + child odds rows
                for market in bookmaker.get("markets", []):
                    mkey = market.get("key")  # 'h2h', 'spreads', or 'totals'
                    # insert into game_odds and get its PK
                    cur.execute(
                        """
                        INSERT INTO msf_mlb.game_odds
                          (game_id, book_id, as_of_time, game_segment, odds_type)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (api_game_id, book_id, as_of_time, "full_game", mkey)
                    )
                    game_odds_id = cur.fetchone()[0]

                    # insert each outcome into odds
                    for outcome in market.get("outcomes", []):
                        name  = outcome.get("name")
                        price = outcome.get("price")
                        point = outcome.get("point") if "point" in outcome else None

                        # determine standardized outcome_type
                        if mkey in ("h2h", "spreads"):
                            if name == home_team:
                                outcome_type = "home"
                            elif name == away_team:
                                outcome_type = "away"
                            else:
                                logging.debug(f"        ✗ Unknown team '{name}', skipping")
                                continue
                        elif mkey == "totals":
                            low = name.lower()
                            if low in ("over", "under"):
                                outcome_type = low
                            else:
                                logging.debug(f"        ✗ Unknown total '{name}', skipping")
                                continue
                        else:
                            logging.debug(f"        ✗ Unhandled market '{mkey}', skipping")
                            continue

                        # map columns
                        odds_american = price
                        spread     = point if mkey == "spreads" else None
                        over_under = point if mkey == "totals"  else None

                        # insert into odds table
                        cur.execute(
                            """
                            INSERT INTO msf_mlb.odds
                              (game_odds_id, outcome_type, odds_american,
                               odds_decimal, odds_fractional, spread, over_under)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                game_odds_id,
                                outcome_type,
                                odds_american,
                                None,          # odds_decimal
                                None,          # odds_fractional
                                spread,
                                over_under
                            )
                        )
                        total_odds_rows += 1
                        logging.debug(f"        ✔ Inserted odds row: {outcome_type} @ {price}")

    conn.commit()
    logging.info(f"   Committed {total_odds_rows} odds rows for {os.path.basename(filepath)}")
    cur.close()

# ─── MAIN ─────────────────────────────────────────────────────────────────────────
def main():
    logging.info("ETL starting")
    if not os.path.isdir(OUTPUT_DIR):
        logging.error(f"No such directory: {OUTPUT_DIR}")
        return

    try:
        conn = connect(**DB_PARAMS)
    except Exception:
        logging.exception("Failed to connect to database")
        return

    files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.json")))
    if not files:
        logging.error(f"No JSON files found in {OUTPUT_DIR}")
    else:
        for fp in files:
            try:
                process_file(conn, fp)
            except Exception:
                logging.exception(f"Error processing {fp}")

    conn.close()
    logging.info("ETL finished, connection closed")

if __name__ == "__main__":
    main()
