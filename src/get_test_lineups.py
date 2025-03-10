import requests
import base64
import json
import logging
import os
import pymongo
from datetime import datetime
import pytz  # Required for timezone conversion

# MySportsFeeds API Credentials
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl/"

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
LINEUP_COLLECTION = "lineups"

# Connect to MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
lineup_collection = db[LINEUP_COLLECTION]

# Ensure necessary directories exist
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

# Create timestamped log file
log_filename = os.path.join("logs", f"fetch_game_lineup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ✅ FIXED: Correct Season Format
SEASON = "2024-regular"

# ✅ Game Data with UTC Timestamp
GAME_INFO = {
    "id": 134312,
    "startTime": "2024-09-06T00:20:00.000Z",  # UTC time
    "awayTeam": "BAL",
    "homeTeam": "KC"
}

# ✅ Function to Convert UTC Time to Eastern Date
def get_nfl_game_date(utc_start_time):
    """Convert UTC game start time to Eastern Time and extract the correct date."""
    game_datetime = datetime.strptime(utc_start_time, "%Y-%m-%dT%H:%M:%S.%fZ")

    # Convert UTC time to Eastern Time (ET)
    utc_zone = pytz.utc
    est_zone = pytz.timezone("America/New_York")
    game_datetime = utc_zone.localize(game_datetime).astimezone(est_zone)

    return game_datetime.strftime("%Y%m%d")  # Return formatted game date in YYYYMMDD format

# ✅ Apply Time Zone Fix
GAME_DATE = get_nfl_game_date(GAME_INFO["startTime"])
GAME_ID = f"{GAME_DATE}-{GAME_INFO['awayTeam']}-{GAME_INFO['homeTeam']}"

# ✅ Using Base64 Authentication (like MySportsFeeds Example)
auth_header = base64.b64encode(f"{API_KEY}:{PASSWORD}".encode()).decode()
headers = {"Authorization": f"Basic {auth_header}"}

# ✅ Adding "force=true" to force fetching fresh data
params = {"force": "true"}

# Function to Fetch and Store Game Lineup
def fetch_and_store_game_lineup():
    url = f"{BASE_URL}{SEASON}/games/{GAME_ID}/lineup.json"

    logging.info(f"Fetching game lineup for Game ID: {GAME_ID} | URL: {url}")

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()  # Raise an error for non-200 responses

        data = response.json()

        # Save to MongoDB
        lineup_collection.insert_one({"game_id": GAME_ID, "season": SEASON, "response": data})
        logging.info(f"✅ Successfully inserted game lineup for {GAME_ID} into MongoDB.")

        # Save to JSON file
        file_path = os.path.join("data", f"game_lineup_{GAME_ID}.json")
        with open(file_path, "w") as file:
            json.dump(data, file, indent=4)

        logging.info(f"📂 API response saved to {file_path}")
        print(f"✅ API response saved to {file_path}")

    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ HTTPError: {e}")
        print(f"HTTP Error: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ RequestException: {e}")
        print(f"Request Error: {e}")
    except Exception as e:
        logging.exception(f"❌ Unexpected error: {e}")
        print(f"Unexpected Error: {e}")

# Run the script
fetch_and_store_game_lineup()

logging.info("🎉 Game lineup fetch completed.")
