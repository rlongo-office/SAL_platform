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
LOG_FILE = os.path.join(LOG_DIR, f"moneyline_analysis_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,  # Set to DEBUG for detailed analysis
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

logging.info(f"[INFO] Analyzing games with opening moneyline {MONEYLINE_TO_TEST}...")

# ✅ Query to retrieve the **earliest** recorded -200 moneyline for each game and book
opening_odds_query = """
WITH EarliestMoneylines AS (
    -- Find the first recorded moneyline per game & book
    SELECT go.game_id, go.book_id, MIN(go.as_of_time) AS first_time
    FROM "msf-nfl".game_odds go
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE go.odds_type = 'moneyline'
    GROUP BY go.game_id, go.book_id
),
QualifiedMoneylines AS (
    -- Ensure the first recorded moneyline was exactly -200
    SELECT em.game_id, em.book_id, em.first_time
    FROM EarliestMoneylines em
    JOIN "msf-nfl".game_odds go 
        ON em.game_id = go.game_id 
        AND em.book_id = go.book_id 
        AND em.first_time = go.as_of_time
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE o.odds_american = %s  
)
-- Retrieve details of the selected valid opening lines
SELECT go.game_id, go.book_id, go.as_of_time AS open_time, 
       o.outcome_type AS open_outcome, o.odds_american AS open_odds
FROM "msf-nfl".game_odds go
JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
JOIN QualifiedMoneylines qm 
    ON go.game_id = qm.game_id 
    AND go.book_id = qm.book_id 
    AND go.as_of_time = qm.first_time
WHERE go.odds_type = 'moneyline'
AND o.odds_american = %s;
"""

# ✅ Execute Opening Line Query
pg_cursor.execute(opening_odds_query, (MONEYLINE_TO_TEST, MONEYLINE_TO_TEST))
open_lines = pg_cursor.fetchall()

# ✅ Extract relevant game IDs & book IDs for closing odds & results lookup
opening_game_book_pairs = [(row[0], row[1]) for row in open_lines]  # (game_id, book_id)
opening_game_ids = list(set([row[0] for row in open_lines]))  # Unique game IDs

logging.debug(f"[DEBUG] Retrieved {len(open_lines)} opening moneyline entries.")

# ✅ Query to retrieve **closing moneylines** for the same games/books
closing_odds_query = """
SELECT go.game_id, go.book_id, go.as_of_time AS close_time, 
       o.outcome_type AS close_outcome, o.odds_american AS close_odds
FROM "msf-nfl".game_odds go
JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
WHERE go.odds_type = 'moneyline'
AND (go.game_id, go.book_id) IN %s
AND go.as_of_time = (
    -- Find the latest recorded odds per game/book
    SELECT MAX(go2.as_of_time)
    FROM "msf-nfl".game_odds go2
    JOIN "msf-nfl".odds o2 ON go2.id = o2.game_odds_id
    WHERE go2.game_id = go.game_id
    AND go2.book_id = go.book_id
    AND go2.odds_type = 'moneyline'
);
"""

pg_cursor.execute(closing_odds_query, (tuple(opening_game_book_pairs),))
closing_lines = { (row[0], row[1]): {"close_time": row[2], "close_outcome": row[3], "close_odds": row[4]} for row in pg_cursor.fetchall() }

# ✅ Query to retrieve **game results** for games with a valid opening moneyline
game_results_query = """
SELECT g.id AS game_id, g.away_score_total, g.home_score_total,
       CASE 
           WHEN g.home_score_total > g.away_score_total THEN 'home'
           WHEN g.home_score_total < g.away_score_total THEN 'away'
           ELSE 'draw'
       END AS winner
FROM "msf-nfl".games g
WHERE g.id IN %s;
"""

pg_cursor.execute(game_results_query, (tuple(opening_game_ids),))
game_results = {row[0]: {"away_score": row[1], "home_score": row[2], "winner": row[3]} for row in pg_cursor.fetchall()}

# ✅ Logging Results
logging.info("game_id\tsportsbook\topen_time\topen_outcome\topen_odds\tclose_time\tclose_outcome\tclose_odds\taway_score_total\thome_score_total\twinner\twager_result")

wins = 0
total_games = 0
win_percentages = []

for row in open_lines:
    game_id, book_id, open_time, open_outcome, open_odds = row
    close_time = closing_lines.get((game_id, book_id), {}).get("close_time", "N/A")
    close_outcome = closing_lines.get((game_id, book_id), {}).get("close_outcome", "N/A")
    close_odds = closing_lines.get((game_id, book_id), {}).get("close_odds", "N/A")
    
    if game_id in game_results:
        total_games += 1
        away_score = game_results[game_id]["away_score"]
        home_score = game_results[game_id]["home_score"]
        winner = game_results[game_id]["winner"]

        # ✅ Determine wager result (win/loss)
        wager_result = "Unknown"
        if MONEYLINE_TO_TEST > 0:  # Positive moneyline = underdog
            won = 1 if winner == "away" else 0
            wager_result = "Win" if won else "Lose"
        else:  # Negative moneyline = favorite
            won = 1 if winner == "home" else 0
            wager_result = "Win" if won else "Lose"

        wins += won
        win_percentages.append(won)

        log_line = f"{game_id}\t{book_id}\t{open_time}\t{open_outcome}\t{open_odds}\t{close_time}\t{close_outcome}\t{close_odds}\t{away_score}\t{home_score}\t{winner}\t{wager_result}"
        logging.info(log_line)

# ✅ Summary Statistics
actual_win_percentage = wins / total_games if total_games > 0 else 0
logging.info(f"[INFO] Actual Win % for Moneyline {MONEYLINE_TO_TEST}: {actual_win_percentage:.4f}")

# ✅ Close DB Connection
pg_cursor.close()
pg_conn.close()

print(f"Analysis complete. Check the log file: {LOG_FILE}")
