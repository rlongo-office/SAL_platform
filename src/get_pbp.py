import requests
import time
import logging
import argparse
from pymongo import MongoClient
from datetime import datetime
import os

# Ensure logs directory exists
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

# Logging configuration to write only to a file
log_filename = os.path.join(log_dir, f"get_pbp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# MongoDB connection setup
client = MongoClient("mongodb://localhost:27017/")
db = client["nfl-data"]
schedules_collection = db["schedules"]
pbp_collection = db["pbp"]

# API key and base URL
API_KEY = "your_api_key_here"
BASE_URL = "https://api.sportradar.com/nfl/official/trial/v7/en/games/{game_id}/pbp.json?api_key=" + API_KEY

def should_fetch_pbp(game_info):
    """
    Checks if a given game's PBP record already exists in the MongoDB 'pbp' collection.
    """
    existing_pbp = pbp_collection.find_one({
        "summary.season.year": game_info["year"],
        "summary.season.type": game_info["type"],
        "summary.week.sequence": game_info["week_sequence"],
        "summary.home.id": game_info["home_id"],
        "summary.away.id": game_info["away_id"]
    })

    log_msg = (
        f"Checking PBP exists for Game ID: {game_info['game_id']} | "
        f"Year: {game_info['year']}, Type: {game_info['type']}, "
        f"Week: {game_info['week_sequence']}, Home: {game_info['home_id']}, Away: {game_info['away_id']} | "
        f"{'FOUND' if existing_pbp else 'NOT FOUND'}"
    )
    logging.info(log_msg)
    
    return existing_pbp is None  # True if PBP should be fetched

def get_schedule_games(season):
    """
    Fetches all game details from the schedules collection for a given season.
    """
    game_data = []
    schedules = schedules_collection.find({"year": season}, {"weeks": 1, "year": 1, "type": 1})

    for schedule in schedules:
        season_year = schedule["year"]
        season_type = schedule["type"]

        for week in schedule.get("weeks", []):
            week_sequence = week["sequence"]

            for game in week.get("games", []):
                game_info = {
                    "game_id": game["id"],
                    "year": season_year,
                    "type": season_type,
                    "week_sequence": week_sequence,
                    "home_id": game["home"]["id"],
                    "away_id": game["away"]["id"]
                }
                game_data.append(game_info)

    logging.info(f"Total games fetched from schedule: {len(game_data)}")
    return game_data

def fetch_and_store_pbp(game):
    """
    Fetches PBP data from Sportradar for a given game and stores it in MongoDB if it's not already present.
    """
    game_id = game["game_id"]

    if not should_fetch_pbp(game):
        logging.info(f"Skipping existing PBP for game {game_id}")
        return

    url = BASE_URL.format(game_id=game_id)
    try:
        response = requests.get(url)
        if response.status_code == 200:
            pbp_data = response.json()
            pbp_collection.insert_one(pbp_data)
            logging.info(f"Stored PBP for game {game_id} in MongoDB.")
        elif response.status_code == 429:
            logging.warning(f"Rate limit hit. Pausing before retrying game {game_id}...")
            time.sleep(10)
            fetch_and_store_pbp(game)  # Retry after waiting
        else:
            logging.error(f"Failed to fetch PBP for game {game_id}: {response.json()}")
    except Exception as e:
        logging.error(f"Exception occurred while fetching PBP for game {game_id}: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and store PBP data for a given NFL season.")
    parser.add_argument("season", type=int, help="Season year (e.g., 2024)")
    args = parser.parse_args()

    season = args.season

    logging.info(f"Starting PBP retrieval process for season {season}...")

    games = get_schedule_games(season)

    for game in games:
        fetch_and_store_pbp(game)
        time.sleep(2)  # Prevent hitting API rate limits

    logging.info("PBP retrieval process complete.")
