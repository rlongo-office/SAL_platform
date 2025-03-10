import requests
import json
import logging
import os
import pymongo
from datetime import datetime
from requests.auth import HTTPBasicAuth

# MySportsFeeds API Credentials
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl/"

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
PLAYER_COLLECTION = "players"

# Connect to MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
players_collection = db[PLAYER_COLLECTION]

# Season to Fetch
SEASON = "2024-2025-regular"

# Ensure 'logs' directory exists
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Create timestamped log file
log_filename = os.path.join(LOG_DIR, f"fetch_2024_players_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Function to fetch and store player data
def fetch_and_store_players():
    url = f"{BASE_URL}players.json?season={SEASON}"
    logging.info(f"Fetching player data for season: {SEASON} | URL: {url}")

    try:
        response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
        response.raise_for_status()

        data = response.json()
        players = data.get("players", [])

        if players:
            # Remove existing records for this season before inserting new ones
            players_collection.delete_many({"season_id": SEASON})
            players_collection.insert_many(players)

            logging.info(f"✅ Successfully inserted {len(players)} players into MongoDB.")
        else:
            logging.warning("⚠️ No player data found in API response.")

    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ HTTPError: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ RequestException: {e}")
    except Exception as e:
        logging.exception(f"❌ Unexpected error: {e}")

# Run the script
fetch_and_store_players()

logging.info("🎉 Player data fetch completed.")
