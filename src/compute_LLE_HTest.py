import os
import logging
from datetime import datetime
import psycopg2
import numpy as np
from scipy.stats import ttest_rel  # For statistical significance

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
MONEYLINE_TO_TEST = -150  # Change this to test different odds

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

# ✅ Query to retrieve opening moneylines
opening_odds_query = """
WITH EarliestMoneylines AS (
    SELECT go.game_id, go.book_id, MIN(go.as_of_time) AS first_time
    FROM "msf-nfl".game_odds go
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE go.odds_type = 'moneyline'
    GROUP BY go.game_id, go.book_id
),
QualifiedMoneylines AS (
    SELECT em.game_id, em.book_id, em.first_time, o.outcome_type
    FROM EarliestMoneylines em
    JOIN "msf-nfl".game_odds go 
        ON em.game_id = go.game_id 
        AND em.book_id = go.book_id 
        AND em.first_time = go.as_of_time
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE o.odds_american = %s
)
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

# ✅ Execute query for opening lines
pg_cursor.execute(opening_odds_query, (MONEYLINE_TO_TEST, MONEYLINE_TO_TEST))
open_lines = pg_cursor.fetchall()

# ✅ Extract relevant game IDs, book IDs, and outcome types
opening_game_book_matching = [(row[0], row[1], row[3]) for row in open_lines]  # (game_id, book_id, outcome_type)
opening_game_ids = list(set([row[0] for row in open_lines]))  # Unique game IDs

# ✅ Format opening_game_book_matching into SQL-compatible VALUES list
opening_game_book_pairs_sql = ",".join(["(%s, %s, %s)"] * len(opening_game_book_matching))

# ✅ Query to retrieve closing moneylines
closing_odds_query = f"""
WITH ClosingGames AS (
    SELECT go.game_id, go.book_id, o.outcome_type, MAX(go.as_of_time) AS last_time
    FROM "msf-nfl".game_odds go
    JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
    WHERE go.odds_type = 'moneyline'
    AND (go.game_id, go.book_id, o.outcome_type) IN (
        SELECT game_id, book_id, outcome_type FROM (VALUES {opening_game_book_pairs_sql}) 
        AS temp(game_id, book_id, outcome_type)
    )
    GROUP BY go.game_id, go.book_id, o.outcome_type
)
SELECT go.game_id, go.book_id, o.outcome_type, go.as_of_time AS close_time, o.odds_american AS close_odds
FROM "msf-nfl".game_odds go
JOIN "msf-nfl".odds o ON go.id = o.game_odds_id
JOIN ClosingGames cg 
    ON go.game_id = cg.game_id 
    AND go.book_id = cg.book_id 
    AND go.as_of_time = cg.last_time
    AND o.outcome_type = cg.outcome_type  
WHERE go.odds_type = 'moneyline';
"""

pg_cursor.execute(closing_odds_query, [item for triplet in opening_game_book_matching for item in triplet])
closing_lines = {(row[0], row[1], row[2]): row[4] for row in pg_cursor.fetchall()}  # (game_id, book_id, outcome_type) -> close_odds

# ✅ Query to retrieve actual game results
game_results_query = """
SELECT g.id AS game_id, g.home_score_total, g.away_score_total,
       CASE 
           WHEN g.home_score_total > g.away_score_total THEN 'home'
           WHEN g.home_score_total < g.away_score_total THEN 'away'
           ELSE 'draw'
       END AS winner
FROM "msf-nfl".games g
WHERE g.id IN %s;
"""

pg_cursor.execute(game_results_query, (tuple(opening_game_ids),))
game_results = {row[0]: (row[1], row[2], row[3]) for row in pg_cursor.fetchall()}  # game_id -> (home_score, away_score, winner)

# ✅ Compute Expected Win Probabilities
def expected_win_prob(moneyline):
    return abs(moneyline) / (abs(moneyline) + 100) if moneyline < 0 else 100 / (moneyline + 100)

# ✅ Log Every Game Line
for game_id, book_id, open_time, open_outcome, open_odds in open_lines:
    close_odds = closing_lines.get((game_id, book_id, open_outcome), None)
    home_score, away_score, winner = game_results.get(game_id, (None, None, None))
    favorite_won = "Yes" if winner == open_outcome else "No" if winner in ["home", "away"] else "Draw"
    
    logging.info(f"[INFO] Game {game_id} | Book {book_id} | Open Odds: {open_odds} | Close Odds: {close_odds} | "
                 f"Outcome Type: {open_outcome} | Home Score: {home_score} | Away Score: {away_score} | Favorite Won: {favorite_won}")

# ✅ Compute Statistics
actual_wins = [1 if game_results.get(game_id)[2] == open_outcome else 0 for game_id, _, _, open_outcome, _ in open_lines]
expected_opening_probs = [expected_win_prob(odds) for _, _, _, _, odds in open_lines]
expected_closing_probs = [expected_win_prob(closing_lines.get((game_id, book_id, open_outcome), None))
                          for game_id, book_id, _, open_outcome, _ in open_lines if (game_id, book_id, open_outcome) in closing_lines]

log_loss_opening = -np.mean(np.log(expected_opening_probs) * actual_wins + np.log(1 - np.array(expected_opening_probs)) * (1 - np.array(actual_wins)))
log_loss_closing = -np.mean(np.log(expected_closing_probs) * actual_wins + np.log(1 - np.array(expected_closing_probs)) * (1 - np.array(actual_wins)))

# ✅ Log Summary Results
logging.info(f"[INFO] Actual Win %: {np.mean(actual_wins):.4f}")
logging.info(f"[INFO] Expected Win Probability Opening: {np.mean(expected_opening_probs):.4f}")
logging.info(f"[INFO] Expected Win Probability Closing: {np.mean(expected_closing_probs):.4f}")
logging.info(f"[INFO] Log Loss Error Openers: {log_loss_opening:.4f}")
logging.info(f"[INFO] Log Loss Error Closers: {log_loss_closing:.4f}")

pg_cursor.close()
pg_conn.close()
print(f"Analysis complete. Check the log file: {LOG_FILE}")
