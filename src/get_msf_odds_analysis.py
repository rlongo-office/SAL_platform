import os
import logging
from datetime import datetime
from pymongo import MongoClient
from stats_util import hypothesis_test

# ✅ Setup MongoDB Connection
client = MongoClient("mongodb://localhost:27017/")
db = client["nfl-msf"]
odds_collection = db["odds"]
seasons_collection = db["seasons"]

def get_game_result(game_id):
    """Retrieve the final score of a given game ID from the seasons collection."""
    season_doc = seasons_collection.find_one(
        { "response.games.schedule.id": game_id },
        { "response.games.$": 1 }
    )
    
    if season_doc and "response" in season_doc and "games" in season_doc["response"]:
        game_data = season_doc["response"]["games"][0]  # Get first matching game
        return game_data["score"]["homeScoreTotal"], game_data["score"]["awayScoreTotal"]
    
    logging.warning(f"[WARNING] No score found for game ID {game_id}. Skipping.")
    return None, None  # Return None if scores not found

# ✅ User Input: Choose Moneyline to Analyze
MONEYLINE_TO_TEST = -200  # Change this to -140, -160, etc.

# ✅ Setup Logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"opening_moneyline_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

logging.info(f"[INFO] Extracting games with opening moneyline of {MONEYLINE_TO_TEST} and determining wins/losses...")

# ✅ Define Aggregation Pipeline to Extract Only Selected Moneyline
pipeline = [
    { "$unwind": "$response.gameLines" },
    { "$unwind": "$response.gameLines.lines" },
    { "$unwind": "$response.gameLines.lines.moneyLines" },
    { "$sort": { "response.gameLines.lines.moneyLines.asOfTime": 1 } },  

    { "$group": {
        "_id": { "game_id": "$response.gameLines.game.id", "sportsbook": "$response.gameLines.lines.source.name" },
        "firstMoneyLine": { "$first": "$response.gameLines.lines.moneyLines.moneyLine" },
        "gameTime": { "$first": "$response.gameLines.game.startTime" },
        "awayTeam": { "$first": "$response.gameLines.game.awayTeamAbbreviation" },
        "homeTeam": { "$first": "$response.gameLines.game.homeTeamAbbreviation" }
    }},
    
    { "$match": {
        "$or": [
            { "firstMoneyLine.homeLine.american": MONEYLINE_TO_TEST },
            { "firstMoneyLine.awayLine.american": MONEYLINE_TO_TEST }
        ]
    }}
]


# ✅ Execute Query
odds_results = list(odds_collection.aggregate(pipeline))
n = len(odds_results)

# ✅ Implied Probability of Favorite Winning
p0 = abs(MONEYLINE_TO_TEST) / (abs(MONEYLINE_TO_TEST) + 100)

# ✅ Track Win/Loss Data (Now Checking Actual Wins)
win_count = 0
loss_count = 0

for game in odds_results:
    game_id = game["_id"]["game_id"]
    sportsbook = game["_id"]["sportsbook"]
    home_team = game["homeTeam"]
    away_team = game["awayTeam"]

    home_moneyline = game["firstMoneyLine"].get("homeLine", {}).get("american", float('inf'))
    away_moneyline = game["firstMoneyLine"].get("awayLine", {}).get("american", float('inf'))

    # ✅ Determine the Favorite Team
    favorite_team = home_team if home_moneyline == MONEYLINE_TO_TEST else away_team

    # ✅ Retrieve the actual game result
    home_score, away_score = get_game_result(game_id)

    if home_score is None or away_score is None:
        continue  # Skip games without scores

    # ✅ Determine if the favorite won
    if (favorite_team == home_team and home_score > away_score) or \
       (favorite_team == away_team and away_score > home_score):
        win_count += 1
    else:
        loss_count += 1

# ✅ Observed Win Rate
win_rate = win_count / n if n > 0 else 0

# ✅ Compute Test Statistic using stats_util module
sample_data = [1]*win_count +[0]*loss_count #Need to pass the sample data to hypothesis_test
test_result = hypothesis_test(sample_data, p0, confidence=0.99, tail="two")

# ✅ Extract results
test_used = test_result["test_type"]
test_stat = test_result["test_stat"]
p_value = test_result["p_value"]
decision = test_result["decision"]

# ✅ Log Results
logging.info(f"[INFO] Sample Size: {n}, Win Rate: {win_rate:.4f}, Expected Win Rate: {p0:.4f}")
logging.info(f"[INFO] {test_used} Results: Test Stat = {test_stat:.4f}, p-value = {p_value:.6f}")
logging.info(f"[INFO] {decision}")

