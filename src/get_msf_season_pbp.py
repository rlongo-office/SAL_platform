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
    # Convert string to datetime object (assuming UTC)
    game_datetime = datetime.strptime(utc_start_time, "%Y-%m-%dT%H:%M:%S.%fZ")
    
    # Convert UTC time to Eastern Time (ET)
    utc_zone = pytz.utc
    est_zone = pytz.timezone("America/New_York")
    game_datetime = utc_zone.localize(game_datetime).astimezone(est_zone)

    # Return formatted game date in YYYYMMDD format
    return game_datetime.strftime("%Y%m%d")


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
SEASONS = [2024]  # List of allowed seasons
#SEASON_TYPES = ["Regular", "Playoffs"]  # List of allowed season types
SEASON_TYPES = ["Preseason"]

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
seasons_collection = db[SEASON_COLLECTION]
pbp_collection = db[PBP_COLLECTION]

# Function to fetch and store full season responses
def fetch_and_store_season_responses():
    """Fetch and store season-level responses from the API."""
    # Fetch existing records in bulk to minimize individual queries
    existing_records = {
        (record["season"], record["season_type"]) for record in seasons_collection.find(
            {"season": {"$in": SEASONS}, "season_type": {"$in": SEASON_TYPES}},
            {"season": 1, "season_type": 1, "_id": 0}  # Fetch only necessary fields
        )
    }

    for season in SEASONS:
        for season_type in SEASON_TYPES:
            if (season, season_type) in existing_records:
                logging.info(f"Skipping {season} {season_type} (already exists in MongoDB)")
                continue  # Skip fetching if already in DB

            url = f"{BASE_URL}{season}-{season_type}/games.json"
            try:
                response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
                response.raise_for_status()  # Raises an error for non-2xx responses
                
                data = response.json()
                seasons_collection.insert_one({"season": season, "season_type": season_type, "response": data})
                
                logging.info(f"Stored full response for {season} {season_type} ({len(data.get('games', []))} games)")
            except requests.exceptions.HTTPError as e:
                logging.error(f"HTTPError fetching {season} {season_type}: {e}")
            except requests.exceptions.RequestException as e:
                logging.error(f"RequestException fetching {season} {season_type}: {e}")

            time.sleep(1)  # Respect API rate limits

# Function to fetch and store full play-by-play responses
def fetch_and_store_pbp_responses():
    """Fetch and store play-by-play (pbp) data for games in the selected seasons."""

    # ✅ Step 1: Get all existing game_ids from the PBP collection (to avoid duplicate API calls)
    existing_pbp_games = {
        record["game_id"] for record in pbp_collection.find({}, {"game_id": 1, "_id": 0})
    }

    # ✅ Step 2: Loop through season documents (each has a fixed season type)
    games_to_fetch = []
    
    for season_doc in seasons_collection.find({"season": {"$in": SEASONS}}):
        season_type = season_doc["season_type"].lower()  # ✅ Use season type from the document
        season_data = season_doc["response"]
        games = season_data.get("games", [])

        for game in games:
            # Convert UTC to ET for correct game date
            game_date = get_nfl_game_date(game["schedule"]["startTime"])
            away_team = game["schedule"]["awayTeam"]["abbreviation"]
            home_team = game["schedule"]["homeTeam"]["abbreviation"]
            game_id = f"{game_date}-{away_team}-{home_team}"  # Properly formatted game ID

            # ✅ Skip if we already have the PBP data
            if game_id in existing_pbp_games:
                logging.info(f"Skipping play-by-play for game {game_id} (already exists in MongoDB)")
                continue

            # ✅ Store only missing games
            games_to_fetch.append((game_id, season_type))

    # ✅ Step 3: Fetch only missing PBP data
    for game_id, season_type in games_to_fetch:
        url = f"{BASE_URL}2024-{season_type}/games/{game_id}/playbyplay.json"

        # ✅ Fix Authentication for API Request
        auth_header = base64.b64encode(f"{API_KEY}:{PASSWORD}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_header}"}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            pbp_data = response.json()
            pbp_collection.insert_one({"game_id": game_id, "response": pbp_data})

            logging.info(f"Stored play-by-play response for game {game_id}")

        except requests.exceptions.HTTPError as e:
            logging.error(f"HTTPError fetching play-by-play for {game_id}: {e}")
        except requests.exceptions.RequestException as e:
            logging.error(f"RequestException fetching play-by-play for {game_id}: {e}")

        time.sleep(1)  # Respect API rate limits

# Run the data retrieval process
fetch_and_store_season_responses()  # Fetch all season responses and store in MongoDB
fetch_and_store_pbp_responses()  # Fetch play-by-play for each game and store in MongoDB
