import requests
import time
import logging
from pymongo import MongoClient
from requests.auth import HTTPBasicAuth
import os
import base64
from datetime import datetime
import pytz  # Required for timezone conversion


def get_nfl_game_date(utc_start_time):
    """Convert UTC game start time to Eastern Time and extract the correct date."""
    game_datetime = datetime.strptime(utc_start_time, "%Y-%m-%dT%H:%M:%S.%fZ")

    # Convert UTC time to Eastern Time (ET)
    utc_zone = pytz.utc
    est_zone = pytz.timezone("America/New_York")
    game_datetime = utc_zone.localize(game_datetime).astimezone(est_zone)

    return game_datetime.strftime("%Y%m%d")  # Return formatted game date in YYYYMMDD format


# Create 'logs' directory if it doesn't exist
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Generate timestamped log file
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"msf_data_collection_{timestamp}.log")

# Configure logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
SEASON_COLLECTION = "seasons"
PBP_COLLECTION = "pbp"

# MySportsFeeds API Configuration
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl/"

# Define allowed seasons and types
#SEASONS = [2020, 2021, 2022, 2023]  # List of allowed seasons
#SEASON_TYPES = ["Preseason", "Regular", "Playoff"]
# Define allowed seasons and types
SEASONS = [2025]  # List of allowed seasons
SEASON_TYPES = ["Playoff"]

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
seasons_collection = db[SEASON_COLLECTION]
pbp_collection = db[PBP_COLLECTION]

def fetch_and_store_season_responses():
    """Fetch and store season-level responses from the API."""
    existing_records = {
        (record["season"], record["season_type"]) for record in seasons_collection.find(
            {"season": {"$in": SEASONS}, "season_type": {"$in": SEASON_TYPES}},
            {"season": 1, "season_type": 1, "_id": 0}
        )
    }

    for season in SEASONS:
        for season_type in SEASON_TYPES:
            if (season, season_type) in existing_records:
                logging.info(f"Skipping {season} {season_type} (already exists in MongoDB)")
                continue

            url = f"{BASE_URL}{season}-{season_type}/games.json"
            try:
                response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
                response.raise_for_status()

                data = response.json()
                seasons_collection.insert_one({"season": season, "season_type": season_type, "response": data})

                logging.info(f"Stored full response for {season} {season_type} ({len(data.get('games', []))} games)")
            except requests.exceptions.HTTPError as e:
                logging.error(f"HTTPError fetching {season} {season_type}: {e}")
            except requests.exceptions.RequestException as e:
                logging.error(f"RequestException fetching {season} {season_type}: {e}")

            time.sleep(2)  # Increased sleep to avoid rate limits

def fetch_and_store_pbp_responses():
    """Fetch and store play-by-play (pbp) data for games in the selected seasons."""
    existing_pbp_games = {
        record["game_id"] for record in pbp_collection.find({}, {"game_id": 1, "_id": 0})
    }

    games_to_fetch = []

    for season_doc in seasons_collection.find({"season": {"$in": SEASONS}}):
        season_year = season_doc["season"]
        season_type = season_doc["season_type"].lower()
        season_data = season_doc["response"]
        games = season_data.get("games", [])

        for game in games:
            game_date = get_nfl_game_date(game["schedule"]["startTime"])
            away_team = game["schedule"]["awayTeam"]["abbreviation"]
            home_team = game["schedule"]["homeTeam"]["abbreviation"]
            game_id = f"{game_date}-{away_team}-{home_team}"

            if game_id in existing_pbp_games:
                logging.info(f"Skipping play-by-play for game {game_id} (already exists in MongoDB)")
                continue

            games_to_fetch.append((game_id, season_year, season_type))

    for game_id, season_year, season_type in games_to_fetch:
        formatted_season_type = "playoff" if season_type.lower() == "playoffs" else season_type.lower()
        url = f"{BASE_URL}{season_year}-{formatted_season_type}/games/{game_id}/playbyplay.json"

        logging.info(f"[DEBUG] Fetching PBP for Game ID: {game_id}")
        logging.info(f"[DEBUG] Season Year: {season_year}, Season Type: {formatted_season_type}")
        logging.info(f"[DEBUG] Constructed URL: {url}")

        auth_header = base64.b64encode(f"{API_KEY}:{PASSWORD}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_header}"}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            # ✅ Debug Log First 500 Characters of Response
            logging.info(f"[DEBUG] Raw API Response for {game_id}: {response.text[:500]}")

            if not response.text.strip():  # ✅ Handle empty responses
                logging.warning(f"[WARNING] Empty response for game {game_id}. Skipping...")
                continue

            pbp_data = response.json()  # ✅ Catch JSON decoding error
            pbp_collection.insert_one({"game_id": game_id, "response": pbp_data})

            logging.info(f"Stored play-by-play response for game {game_id}")

        except requests.exceptions.HTTPError as e:
            if response.status_code == 403:
                logging.warning(f"[WARNING] Forbidden access for {game_id}: {e}")
            else:
                logging.error(f"HTTPError fetching play-by-play for {game_id}: {e}")
        except requests.exceptions.RequestException as e:
            logging.error(f"RequestException fetching play-by-play for {game_id}: {e}")
        except ValueError as e:
            logging.error(f"[ERROR] JSON Decode Error for {game_id}: {e}")

        time.sleep(2)  # Increased sleep to 2 seconds to avoid rate limits

# Run the data retrieval process
fetch_and_store_season_responses()
fetch_and_store_pbp_responses()
