import os
import sys
import logging
from pymongo import MongoClient
from collections import defaultdict

# Ensure logs directory exists
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging
log_filename = os.path.join(LOG_DIR, "roster_dedupe.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# MongoDB connection
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "nfl-data"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
rosters_collection = db["rosters"]
schedules_collection = db["schedules"]

def get_expected_games(season_year):
    """
    Get the expected number of games for PRE, REG, and PST from the schedules collection.
    """
    expected_counts = {"PRE": 0, "REG": 0, "PST": 0}

    # Query schedules collection for all season types for the given year
    schedules = schedules_collection.find({"year": season_year}, {"_id": 0, "type": 1, "weeks": 1})

    for schedule in schedules:
        season_type = schedule.get("type", "UNKNOWN")
        total_games = sum(len(week.get("games", [])) for week in schedule.get("weeks", []))  # Count all games in weeks

        if season_type in expected_counts:
            expected_counts[season_type] = total_games

    return expected_counts

def validate_rosters(season_year):
    """
    Validate that all expected games have corresponding roster records.
    """
    logging.info(f"Starting roster validation process for season {season_year}...")

    # Fetch expected game counts from schedules
    expected_counts = get_expected_games(season_year)

    # Fetch all unique game IDs from the rosters collection
    unique_games = {}
    games_per_date = defaultdict(int)
    unique_game_types = set()

    for doc in rosters_collection.find({}, {"id": 1, "scheduled": 1, "summary.season.type": 1}):
        game_id = doc.get("id")
        scheduled_date = doc.get("scheduled")
        game_type = doc.get("summary", {}).get("season", {}).get("type", "UNKNOWN")

        if scheduled_date:
            scheduled_date = scheduled_date.split("T")[0]  # Extract only the date
            games_per_date[scheduled_date] += 1

        if game_id:
            unique_games[game_id] = game_type
            unique_game_types.add(game_type)

    total_unique_games = len(unique_games)

    logging.info(f"Total unique games found: {total_unique_games}")
    logging.info(f"Total games missing scheduled date: {sum(1 for g in unique_games if g is None)}")

    logging.info("Games per event date:")
    for date, count in sorted(games_per_date.items()):
        logging.info(f"  {date}: {count} games")

    logging.info(f"Unique game types present: {', '.join(unique_game_types)}")

    # Compare expected vs. actual
    logging.info("\nExpected vs. Actual Roster Counts:")
    for season_type, expected_count in expected_counts.items():
        logging.info(f"Unique game types present: {unique_game_types}")  # Shows detected game types
        logging.info(f"Comparing against season type: {season_type}")   # Shows what we're checking

        SEASON_TYPE_MAPPING = {"PRE": "pre", "REG": "reg", "PST": "pst"}

        actual_count = sum(
            1 for game_id, game_type in unique_games.items() if game_type.lower() == SEASON_TYPE_MAPPING[season_type]
        )

        logging.info(f"  {season_type}: Expected = {expected_count}, Actual = {actual_count}")

    logging.info("Roster validation process completed.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python roster_dedupe.py <season_year>")
        sys.exit(1)

    season_year = int(sys.argv[1])
    validate_rosters(season_year)
    print(f"Roster validation completed. Logs saved to {log_filename}")
