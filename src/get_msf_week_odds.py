import requests
import time
import logging
from pymongo import MongoClient
from requests.auth import HTTPBasicAuth
import os
import base64
import random
from datetime import datetime

# ✅ Setup Logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"weekly_odds_collection_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# ✅ MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
ODDS_COLLECTION = "odds"
SEASON_COLLECTION = "seasons"

# ✅ MySportsFeeds API Configuration
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl/"

# ✅ Seasons to Fetch
SEASONS = [2020, 2021, 2022, 2023]
SEASON_TYPES = ["preseason", "regular", "playoff"]

# ✅ Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
odds_collection = db[ODDS_COLLECTION]
seasons_collection = db[SEASON_COLLECTION]


def get_unique_weeks(season_year, season_type):
    """Retrieve unique week numbers from the season data while preserving order."""
    season_doc = seasons_collection.find_one(
        {"season": str(season_year), "season_type": {"$regex": f"^{season_type}$", "$options": "i"}}
    )

    if not season_doc:
        logging.warning(f"[WARNING] No season data found for {season_year} {season_type}")
        return []

    # ✅ Extract games correctly
    games = season_doc.get("response", {}).get("games", [])
    if not games:
        logging.warning(f"[WARNING] No games found for {season_year} {season_type}")
        return []

    # ✅ Extract all week values while preserving order
    week_numbers = [game["schedule"]["week"] for game in games]
    unique_weeks = sorted(set(week_numbers), key=week_numbers.index)  # Preserve first occurrence order

    logging.info(f"[INFO] Found {len(unique_weeks)} unique weeks for {season_year} {season_type}: {unique_weeks}")
    return unique_weeks


def get_weeks_by_season():
    """Retrieves unique week numbers for each season type and stores them in a dictionary."""
    weeks = {}

    for season_year in SEASONS:
        weeks[season_year] = {}

        for season_type in SEASON_TYPES:
            week_list = get_unique_weeks(season_year, season_type)
            weeks[season_year][season_type] = week_list if week_list else []  # Store empty list if no weeks found

    logging.info(f"[INFO] Full weeks dataset retrieved: {weeks}")
    return weeks


def fetch_and_store_season_responses():
    """Fetch and store season-level responses from the API."""
    existing_seasons = set(
        (doc["season"], doc["season_type"].lower())
        for doc in seasons_collection.find({}, {"season": 1, "season_type": 1, "_id": 0})
    )

    for season in SEASONS:
        for season_type in SEASON_TYPES:
            if (str(season), season_type.lower()) in existing_seasons:
                logging.info(f"Skipping {season} {season_type} (already exists)")
                continue  # ✅ Skip if already stored

            url = f"{BASE_URL}{season}-{season_type}/games.json"

            try:
                response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
                response.raise_for_status()

                data = response.json()
                seasons_collection.insert_one({"season": str(season), "season_type": season_type.lower(), "response": data})

                logging.info(f"Stored full response for {season} {season_type}")

            except requests.exceptions.RequestException as e:
                logging.error(f"[ERROR] Failed to fetch {season} {season_type}: {e}")

            time.sleep(1)  # ✅ Respect API rate limits


def fetch_and_store_weekly_odds():
    """Fetches weekly odds for all season types and stores them in MongoDB."""
    weeks = get_weeks_by_season()  # ✅ Retrieve weeks for each season type
    logging.info(f"[INFO] Full weeks dataset retrieved: {weeks}")

    for season_year, season_data in weeks.items():
        for season_type, week_list in season_data.items():
            if not week_list:
                logging.warning(f"[WARNING] Skipping {season_year} {season_type}, no weeks found.")
                continue

            for week in week_list:
                season_formatted = f"{season_year}-{season_type}"
                url = f"{BASE_URL}{season_formatted}/week/{week}/odds_gamelines.json"

                logging.info(f"[INFO] Fetching: {url}")

                # ✅ Check if the document already exists before inserting
                if odds_collection.find_one({"season": season_year, "season_type": season_type, "week": week}):
                    logging.warning(f"[WARNING] Skipping duplicate insert for {season_year} {season_type} Week {week}")
                    continue  # ✅ Skip already stored data

                # ✅ Implement Retry Logic (Handles API failures)
                MAX_RETRIES = 5
                BASE_WAIT_TIME = 10  # Start with 10 seconds

                for attempt in range(MAX_RETRIES):
                    try:
                        response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
                        response.raise_for_status()

                        # ✅ If request is successful, break the retry loop
                        if response.status_code == 200:
                            odds_data = response.json()
                            odds_collection.insert_one({
                                "season": season_year,
                                "season_type": season_type,
                                "week": week,
                                "response": odds_data
                            })
                            logging.info(f"[INFO] Stored odds for {season_year} {season_type} Week {week}")
                            break  # ✅ Stop retrying if success

                    except requests.exceptions.RequestException as e:
                        logging.error(f"[ERROR] Attempt {attempt+1} failed for {season_year} {season_type} Week {week}: {e}")

                        # ✅ If max retries reached, log failure
                        if attempt == MAX_RETRIES - 1:
                            logging.error(f"[ERROR] Skipping {season_year} {season_type} Week {week} after {MAX_RETRIES} failed attempts.")
                            break

                        wait_time = BASE_WAIT_TIME * (attempt + 1) + random.randint(1, 5)  # ✅ Exponential backoff + random wait
                        logging.warning(f"[WARNING] Waiting {wait_time} seconds before retrying...")
                        time.sleep(wait_time)  # ✅ Increased wait time

                time.sleep(10)  # ✅ Respect API rate limits


# ✅ Run the Season Retrieval Process
fetch_and_store_season_responses()

# ✅ Run the Weekly Odds Retrieval
fetch_and_store_weekly_odds()

logging.info(f"[INFO] Weekly odds retrieval completed. Log file saved: {LOG_FILE}")
