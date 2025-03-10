import requests
import time
import logging
import os
import json
from datetime import datetime
import pymongo
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

# List of seasons to fetch
SEASONS = ["2018-2019-regular", "2019-2020-regular", "2020-2021-regular",
           "2021-2022-regular", "2022-2023-regular"]

# Ensure 'logs' and 'data' directories exist
LOG_DIR = "logs"
DATA_DIR = "data"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Create timestamped log file
log_filename = os.path.join(LOG_DIR, f"player_data_fetch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging
logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Function to fetch player data from MySportsFeeds API
def fetch_players(season):
    url = f"{BASE_URL}players.json?season={season}&rosterstatus=assigned-to-roster&force=true"
    logging.info(f"Fetching player data for season: {season} | URL: {url}")
    
    try:
        response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
        response.raise_for_status()

        if response.status_code == 200:
            logging.info(f"✅ Successfully fetched player data for season {season}.")
            
            # Save API response to a file in the "data" folder
            response_filepath = os.path.join(DATA_DIR, f"player_data_{season}.json")
            with open(response_filepath, "w", encoding="utf-8") as f:
                json.dump(response.json(), f, indent=4)
            logging.info(f"📄 API response for season {season} saved to {response_filepath}")

            return response.json().get("players", [])
        else:
            logging.error(f"❌ Unexpected response for season {season}: HTTP {response.status_code} | Response: {response.text}")
            return []
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ HTTPError fetching {season}: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ RequestException fetching {season}: {e}")
    except Exception as e:
        logging.exception(f"❌ Unexpected error fetching {season}: {e}")
    
    return []

# Process and store data
for season in SEASONS:
    players = fetch_players(season)
    
    logging.info(f"🔄 Processing {len(players)} players for season {season}.")
    
    for player in players:
        player_id = player.get("id")
        first_name = player.get("firstName", "Unknown")
        last_name = player.get("lastName", "Unknown")
        full_name = f"{first_name} {last_name}"
        team_id = player.get("team", {}).get("id")
        team_name = player.get("team", {}).get("name")
        position = player.get("primaryPosition", {}).get("abbreviation")
        height = player.get("height", "Unknown")
        weight = player.get("weight", "Unknown")
        birth_date = player.get("birthDate", "Unknown")

        # Check if player exists in MongoDB
        existing_player = players_collection.find_one({"player_id": player_id})

        if existing_player:
            # Append new season if not already there
            if not any(s["season_id"] == season for s in existing_player["seasons"]):
                logging.info(f"🔄 Updating player {full_name} (ID: {player_id}) with new season: {season}.")
                
                existing_player["seasons"].append({
                    "season_id": season,
                    "team_id": team_id,
                    "position": position,
                    "height": height,
                    "weight": weight,
                    "birth_date": birth_date
                })
                players_collection.update_one({"player_id": player_id}, {"$set": existing_player})
                logging.info(f"✅ Successfully updated player {full_name} (ID: {player_id}).")
            else:
                logging.info(f"🟢 Player {full_name} (ID: {player_id}) already has season {season} recorded.")
        else:
            # Insert new player
            new_player = {
                "player_id": player_id,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "team_id": team_id,
                "team_name": team_name,
                "seasons": [{
                    "season_id": season,
                    "team_id": team_id,
                    "position": position,
                    "height": height,
                    "weight": weight,
                    "birth_date": birth_date
                }]
            }
            players_collection.insert_one(new_player)
            logging.info(f"✅ Inserted new player {full_name} (ID: {player_id}).")

    # ✅ Respect API Rate Limits
    logging.info(f"⏳ Sleeping for 2 seconds before next API call...")
    time.sleep(2)

logging.info("🎉 Player data import process completed successfully!")
