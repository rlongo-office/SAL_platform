import os
import logging
from datetime import datetime
from pymongo import MongoClient

# ✅ Setup MongoDB Connection
client = MongoClient("mongodb://localhost:27017/")
db = client["nfl-msf"]
odds_collection = db["odds"]

# ✅ Setup Logging with UTF-8 Encoding
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

logging.info("[INFO] Extracting games with opening moneyline of -150...")

# ✅ Define Aggregation Pipeline
pipeline = [
    { "$unwind": "$response.gameLines" },
    { "$unwind": "$response.gameLines.lines" },
    { "$unwind": "$response.gameLines.lines.moneyLines" },
    { "$sort": { "response.gameLines.lines.moneyLines.asOfTime": 1 } },
    { "$group": {
        "_id": {
            "gameId": "$response.gameLines.game.id",
            "sportsbook": "$response.gameLines.lines.source.name"
        },
        "gameTime": { "$first": "$response.gameLines.game.startTime" },
        "awayTeam": { "$first": "$response.gameLines.game.awayTeamAbbreviation" },
        "homeTeam": { "$first": "$response.gameLines.game.homeTeamAbbreviation" },
        "openingMoneyLine": { "$first": "$response.gameLines.lines.moneyLines.moneyLine" }
    }},
    { "$match": {
        "$or": [
            { "openingMoneyLine.homeLine.american": -150 },
            { "openingMoneyLine.awayLine.american": -150 }
        ]
    }}
]

# ✅ Execute Query
results = list(odds_collection.aggregate(pipeline))

# ✅ Log Results
if results:
    logging.info(f"[INFO] Found {len(results)} games with opening moneyline of -150:")
    missing_count = 0
    for game in results:
        game_id = game.get('_id', {}).get('gameId', 'UNKNOWN')
        sportsbook = game.get('_id', {}).get('sportsbook', 'UNKNOWN')
        game_time = game.get('gameTime', 'UNKNOWN')
        home_team = game.get('homeTeam', 'UNKNOWN')
        away_team = game.get('awayTeam', 'UNKNOWN')
        money_line = game.get('openingMoneyLine', 'UNKNOWN')

        if game_id == 'UNKNOWN':
            missing_count += 1
            logging.warning(f"[WARNING] Missing gameId for game: {game}")
            continue

        logging.info(f"Game ID: {game_id}, Sportsbook: {sportsbook}, Time: {game_time}, "
                     f"Home: {home_team}, Away: {away_team}, MoneyLine: {money_line}")

    logging.info(f"[INFO] Skipped {missing_count} games due to missing gameId.")
else:
    logging.info("[INFO] No games found with an opening moneyline of -150.")

logging.info(f"[INFO] Extraction complete. Log file saved: {LOG_FILE}")
