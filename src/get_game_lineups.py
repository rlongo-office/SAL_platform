import requests
import base64
import json
import logging
import os
import pymongo
import time
from datetime import datetime

# ✅ MySportsFeeds API Credentials
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl/"

# ✅ MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
SEASON_COLLECTION = "seasons"
LINEUP_COLLECTION = "game_lineups"

# ✅ Connect to MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
seasons_collection = db[SEASON_COLLECTION]
lineup_collection = db[LINEUP_COLLECTION]

# ✅ Ensure necessary directories exist
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

# ✅ Create timestamped log file
log_filename = os.path.join("logs", f"fetch_game_lineups_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# ✅ Configure logging
logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ✅ Using Base64 Authentication
auth_header = base64.b64encode(f"{API_KEY}:{PASSWORD}".encode()).decode()
headers = {"Authorization": f"Basic {auth_header}"}

# ✅ API Parameters (Force fresh data)
params = {"force": "true"}

def fetch_and_store_game_lineups():
    """Fetch game lineups for all games in stored seasons."""
    logging.info("🔄 Starting game lineup fetch process...")

    # ✅ Iterate through all stored seasons
    for season_doc in seasons_collection.find({}, {"season": 1, "season_type": 1, "response.games": 1}):
        season_year = season_doc["season"]
        season_type = season_doc["season_type"].lower()
        season_games = season_doc.get("response", {}).get("games", [])

        formatted_season_type = "playoff" if season_type == "playoffs" else season_type
        season_str = f"{season_year}-{formatted_season_type}"  # Ex: "2024-regular"

        for game in season_games:
            game_id = game["schedule"]["id"]  # ✅ Use game ID instead of date
            game_key = f"{season_str}_game_{game_id}"

            # ✅ Check if lineup already exists
            if lineup_collection.find_one({"game_id": game_id, "season": season_str}):
                logging.info(f"⏭️ Skipping game lineup for {game_id} (Already exists)")
                continue

            # ✅ Construct API URL
            url = f"{BASE_URL}{season_str}/games/{game_id}/lineup.json"
            logging.info(f"🌍 Fetching lineup for Game ID: {game_id} | URL: {url}")

            try:
                response = requests.get(url, headers=headers, params=params)
                response.raise_for_status()

                data = response.json()

                # ✅ Store in MongoDB
                lineup_collection.insert_one({"game_id": game_id, "season": season_str, "response": data})
                logging.info(f"✅ Successfully stored game lineup for Game ID {game_id}.")

                # ✅ Save JSON response for reference
                #file_path = os.path.join("data", f"game_lineup_{game_id}.json")
                #with open(file_path, "w") as file:
                #    json.dump(data, file, indent=4)
                #logging.info(f"📂 API response saved to {file_path}")

            except requests.exceptions.HTTPError as e:
                logging.error(f"❌ HTTP Error fetching lineup for {game_id}: {e}")
            except requests.exceptions.RequestException as e:
                logging.error(f"❌ Request Error fetching lineup for {game_id}: {e}")
            except Exception as e:
                logging.exception(f"❌ Unexpected error for Game ID {game_id}: {e}")

            time.sleep(2)  # ✅ Delay to avoid API rate limits

    logging.info("🎉 Game lineup fetch process completed.")

# ✅ Run the lineup fetch process
fetch_and_store_game_lineups()#
