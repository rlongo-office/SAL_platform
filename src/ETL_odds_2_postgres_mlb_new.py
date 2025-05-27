#!/usr/bin/env python3
import os
import glob
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import DictCursor

# ─── CONFIGURATION ───────────────────────────────────────────────────────────────
load_dotenv()  # expects a .env in your project root

BASE_DIR   = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
OUTPUT_DIR = os.path.join(BASE_DIR, "output", "mlb")
LOG_DIR    = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

DB_PARAMS = {
    "dbname":   "neondb",
    "user":     "neondb_owner",
    "password": "npg_aKWdUeCXV10c",
    "host":     "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech",
    "port":     "5432",
    "sslmode":  "require",
}

# number of rows after which to auto-commit
BATCH_SIZE = 1000

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(
    filename=os.path.join(LOG_DIR, f"etl_mlb_{datetime.now():%Y%m%d_%H%M%S}.log"),
    level=logging.DEBUG,
    format=LOG_FORMAT,
)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
console.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(console)

# ─── DB CONNECTION ────────────────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as c:
        c.execute("SET search_path TO msf_mlb,public;")
    return conn

# ─── HELPERS ──────────────────────────────────────────────────────────────────────
def upsert_book(cur, title):
    """Ensure bookmaker exists in msf_mlb.books; return its id."""
    cur.execute("SELECT id FROM books WHERE name = %s", (title,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO books (name, region, is_online, is_las_vegas)
        VALUES (%s, NULL, TRUE, FALSE)
        RETURNING id
        """,
        (title,),
    )
    return cur.fetchone()[0]

def insert_game_odds(cur, game_id, book_id, as_of_time, odds_type, segment="full_game"):
    cur.execute(
        """
        INSERT INTO game_odds (game_id, book_id, as_of_time, game_segment, odds_type)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (game_id, book_id, as_of_time, segment, odds_type),
    )
    return cur.fetchone()[0]

def insert_odds(cur, game_odds_id, outcome_type, price, point, market_key):
    odds_american = price
    spread       = point if market_key == "spreads" else None
    over_under   = point if market_key == "totals"  else None

    cur.execute(
        """
        INSERT INTO odds
          (game_odds_id, outcome_type, odds_american,
           odds_decimal, odds_fractional, spread, over_under)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            game_odds_id,
            outcome_type,
            odds_american,
            None,  # decimal
            None,  # fractional
            spread,
            over_under,
        ),
    )

# ─── PROCESSING ──────────────────────────────────────────────────────────────────
def process_snapshot(cur, snapshot_ts, payload):
    try:
        as_of_time = datetime.fromisoformat(snapshot_ts.rstrip("Z"))
    except ValueError:
        logging.warning(f"✗ Bad timestamp format: {snapshot_ts}, skipping")
        return 0

    rows = 0
    for game in payload.get("data", []):
        api_game_id = game.get("id")
        if not api_game_id:
            logging.debug("✗ Missing game.id, skipping game")
            continue

        home = game.get("home_team")
        away = game.get("away_team")

        for book in game.get("bookmakers", []):
            title = book.get("title")
            if not title:
                logging.debug("✗ Missing bookmaker.title, skipping")
                continue

            try:
                book_id = upsert_book(cur, title)
            except Exception:
                logging.exception(f"Failed upserting book {title}")
                continue

            for market in book.get("markets", []):
                mkey = market.get("key")
                try:
                    game_odds_id = insert_game_odds(cur, api_game_id, book_id, as_of_time, mkey)
                except Exception:
                    logging.exception("Failed inserting game_odds")
                    continue

                for outcome in market.get("outcomes", []):
                    name  = outcome.get("name")
                    price = outcome.get("price")
                    point = outcome.get("point") if "point" in outcome else None

                    # determine outcome_type
                    if mkey in ("h2h", "spreads"):
                        if name == home:
                            typ = "home"
                        elif name == away:
                            typ = "away"
                        else:
                            logging.debug(f"✗ Unknown team '{name}', skipping")
                            continue
                    elif mkey == "totals":
                        low = name.lower()
                        if low in ("over", "under"):
                            typ = low
                        else:
                            logging.debug(f"✗ Unknown total '{name}', skipping")
                            continue
                    else:
                        logging.debug(f"✗ Unhandled market '{mkey}', skipping")
                        continue

                    try:
                        insert_odds(cur, game_odds_id, typ, price, point, mkey)
                        rows += 1
                    except Exception:
                        logging.exception("Failed inserting odds row")

    return rows

def process_file(conn, filepath):
    logging.info(f"→ Processing file: {filepath}")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            snaps = json.load(f)
    except Exception:
        logging.exception(f"Failed to load JSON: {filepath}")
        return

    cur = conn.cursor(cursor_factory=DictCursor)
    total = 0

    for snap_ts, payload in snaps.items():
        inserted = process_snapshot(cur, snap_ts, payload)
        total += inserted
        if total and total % BATCH_SIZE == 0:
            conn.commit()
            logging.info(f"Committed batch of {BATCH_SIZE} rows")

    conn.commit()
    logging.info(f"Committed {total} odds rows for {os.path.basename(filepath)}")
    cur.close()

# ─── MAIN ─────────────────────────────────────────────────────────────────────────
def main():
    logging.info("ETL starting")
    if not os.path.isdir(OUTPUT_DIR):
        logging.error(f"No such directory: {OUTPUT_DIR}")
        return

    conn = pg_connect()
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
