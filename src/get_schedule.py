import os
import sys
import requests
import json
import logging
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
from pymongo import MongoClient
import time

# Load environment variables
load_dotenv(find_dotenv())

# Setup MongoDB Connection
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "nfl-data"
client = MongoClient(MONGO_URI)
db = client[DB_NAME]  # Connect to the database

# Setup Logging
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = os.path.join(LOG_DIR, f"get_schedule_{timestamp}.log")

logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Read API key
SPORTRADAR_API_KEY = os.getenv("SPORTRADAR_API_KEY")
if not SPORTRADAR_API_KEY:
    logging.error("SPORTRADAR_API_KEY is not set in the .env file.")
    sys.exit(1)

# Base URL for the Sportradar API
BASE_URL = "https://api.sportradar.com/nfl/official/trial/v7/en"

def fetch_season_schedule(season_year, season_type):
    """
    Fetch the full season schedule from the Sportradar API.
    """
    url = f"{BASE_URL}/games/{season_year}/{season_type}/schedule.json"
    params = {"api_key": SPORTRADAR_API_KEY}

    logging.info(f"Fetching NFL schedule for {season_year} {season_type} season...")
    response = requests.get(url, params=params)

    if response.status_code != 200:
        logging.error(f"Failed to fetch schedule: {response.text}")
        return None

    logging.info("Successfully fetched season schedule.")
    return response.json()

def store_schedule_in_mongo(schedule_data, season_year, season_type):
    """
    Store the full season schedule in MongoDB under 'schedules' collection.
    Prevents duplicate schedules.
    """
    if not schedule_data:
        logging.error("No valid schedule data to store.")
        return

    existing_schedule = db.schedules.find_one({"season.year": season_year, "season.type": season_type})
    if existing_schedule:
        logging.info("Schedule already exists in MongoDB. Skipping insertion.")
    else:
        db.schedules.insert_one(schedule_data)
        logging.info("Stored season schedule in MongoDB (schedules collection).")

def fetch_and_store_rosters(game_ids):
    """
    Fetch and store the rosters for all game IDs in the season.
    Implements rate limiting to avoid 'Too Many Requests' error.
    """
    saved_game_ids = {doc["id"] for doc in db.rosters.find({}, {"id": 1})}

    for i, game_id in enumerate(game_ids):
        if game_id in saved_game_ids:
            logging.info(f"Skipping already saved roster for game_id: {game_id}")
            continue

        url = f"{BASE_URL}/games/{game_id}/roster.json"
        params = {"api_key": SPORTRADAR_API_KEY}

        logging.info(f"Fetching roster for game_id: {game_id}")
        response = requests.get(url, params=params)

        if response.status_code == 429:  # Too Many Requests
            logging.warning(f"Rate limit hit. Pausing before retrying game {game_id}...")
            time.sleep(10)  # Wait before retrying
            response = requests.get(url, params=params)

        if response.status_code != 200:
            logging.error(f"Failed to fetch roster for game {game_id}: {response.text}")
            continue  # Skip this game and move to the next

        roster_data = response.json()
        db.rosters.insert_one(roster_data)
        logging.info(f"Stored roster for game {game_id} in MongoDB.")

        # **Rate Limit Handling**: Add delay between requests
        logging.info("Pausing briefly to avoid hitting API limits...")
        time.sleep(10)  # Delay to reduce request frequency

def main():
    """
    Main function: Fetch schedule, store in MongoDB, extract game IDs, and optionally fetch rosters.
    """
    # Parse command-line arguments
    if len(sys.argv) != 4:
        print("Usage: python get_schedule.py <mode> <season_year> <season_type>")
        print("mode: 1 (schedule + rosters) | 2 (schedule only)")
        print("season_type: PRE (Preseason) | REG (Regular) | PST (Postseason)")
        sys.exit(1)

    mode = sys.argv[1]  # "1" or "2"
    season_year = sys.argv[2]
    season_type = sys.argv[3].upper()

    if season_type not in ["PRE", "REG", "PST"]:
        logging.error("Invalid season type! Use 'PRE', 'REG', or 'PST'.")
        sys.exit(1)

    logging.info(f"Starting process for {season_year} {season_type} season...")

    schedule_data = fetch_season_schedule(season_year, season_type)
    if schedule_data:
        store_schedule_in_mongo(schedule_data, season_year, season_type)

        if mode == "1":
            # Extract all game IDs from the schedule
            game_ids = [game["id"] for week in schedule_data.get("weeks", []) for game in week.get("games", []) if "id" in game]

            logging.info(f"Extracted {len(game_ids)} game IDs from schedule.")
            fetch_and_store_rosters(game_ids)
        else:
            logging.info("Skipping roster retrieval (Mode 2 selected).")

    else:
        logging.error("No schedule data found. Exiting.")

if __name__ == "__main__":
    main()
