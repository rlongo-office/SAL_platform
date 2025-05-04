import os
import logging
from datetime import datetime
import psycopg2

# ✅ PostgreSQL Connection
pg_conn = psycopg2.connect(
    dbname="SAL-db",
    user="postgres",
    password="password",
    host="localhost",
    port="5432"
)
pg_cursor = pg_conn.cursor()

# ✅ User Input: Choose Moneyline to Analyze
MONEYLINE_TO_TEST = -200  # Change this to test different odds

# ✅ Setup Logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"debug_moneyline_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,  # Set to DEBUG for deep analysis
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

logging.info(f"[INFO] Debugging moneyline openers for {MONEYLINE_TO_TEST}...")

# ✅ Query to retrieve the opening moneylines (earliest recorded for each game and book)
opening_odds_query = """
WITH EarliestMoneylines AS (
    -- Step 1: Find the first recorded moneyline entry per game and book
    SELECT go.game_id, go.book_id, MIN(go.as_of_time) AS first_time
    FROM "msf-nfl".game_odds go
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE go.odds_type = 'moneyline'
    GROUP BY go.game_id, go.book_id
),
QualifiedMoneylines AS (
    -- Step 2: Ensure the first recorded moneyline was exactly -200
    SELECT em.game_id, em.book_id, em.first_time
    FROM EarliestMoneylines em
    JOIN "msf-nfl".game_odds go 
        ON em.game_id = go.game_id 
        AND em.book_id = go.book_id 
        AND em.first_time = go.as_of_time
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE o.odds_american = %s  -- ✅ Ensure only first -200 lines are kept
)
-- Step 3: Retrieve full details of valid opening lines
SELECT go.game_id, go.book_id, go.as_of_time AS open_time, 
       o.outcome_type AS open_outcome, o.odds_american AS open_odds
FROM "msf-nfl".game_odds go
JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
JOIN QualifiedMoneylines qm 
    ON go.game_id = qm.game_id 
    AND go.book_id = qm.book_id 
    AND go.as_of_time = qm.first_time
WHERE go.odds_type = 'moneyline'
AND o.odds_american = %s;  -- ✅ Apply strict filter using correct alias (o)
"""

try:
    # ✅ Execute the query
    pg_cursor.execute(opening_odds_query, (MONEYLINE_TO_TEST, MONEYLINE_TO_TEST))
    open_lines = pg_cursor.fetchall()

    # ✅ Debugging: Log all retrieved openers
    logging.debug(f"[DEBUG] Retrieved {len(open_lines)} opening odds entries.")

    mismatched_lines = []  # To track cases where the odds aren't -200
    valid_opening_lines = []  # Correct lines
    unique_game_ids = set()

    for row in open_lines:
        game_id, book_id, open_time, open_outcome, open_odds = row

        # ✅ Track unique games
        unique_game_ids.add(game_id)

        # ✅ Log every opening line retrieved
        log_line = f"{game_id} | {book_id} | {open_time} | {open_outcome} | {open_odds}"
        logging.debug(f"[DEBUG] Retrieved Opening Line: {log_line}")

        # ✅ Check for mismatches
        if open_odds != MONEYLINE_TO_TEST:
            mismatched_lines.append(log_line)
        else:
            valid_opening_lines.append(row)

    # ✅ Log number of unique games found
    logging.info(f"[INFO] Unique game count: {len(unique_game_ids)}")

    # ✅ Log incorrect openers
    if mismatched_lines:
        logging.warning(f"[WARNING] Found {len(mismatched_lines)} openers with unexpected moneylines:")
        for line in mismatched_lines[:50]:  # Log only first 50 for brevity
            logging.warning(f"[WARNING] {line}")

    # ✅ Log Correct Openers
    logging.info(f"[INFO] Found {len(valid_opening_lines)} correctly matching openers for {MONEYLINE_TO_TEST}")

    # ✅ Assertion: Ensure number of results is reasonable
    assert len(valid_opening_lines) < 5000, "[ERROR] Too many results. Possible SQL issue."

except psycopg2.Error as e:
    logging.error(f"[ERROR] Database Query Failed: {e}")

finally:
    # ✅ Closing DB Connection
    pg_cursor.close()
    pg_conn.close()

print(f"Analysis complete. Check the log file: {LOG_FILE}")
