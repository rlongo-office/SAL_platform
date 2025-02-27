import json
import os
import sys
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Setup logging: log file will be placed in the logs folder
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = os.path.join(LOG_DIR, f"get_game_stats_{timestamp}.log")

logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Read the Sportradar API key from .env
SPORTRADAR_API_KEY = os.getenv("SPORTRADAR_API_KEY")
if not SPORTRADAR_API_KEY:
    logging.error("SPORTRADAR_API_KEY is not set in the .env file.")
    sys.exit(1)

# Base URL for the Sportradar NFL API
BASE_URL = "https://api.sportradar.com/nfl/official/trial/v7/en"

def get_game_stats(game_id):
    """
    Fetch game statistics for a single NFL game using its game_id.
    
    Endpoint:
    GET https://api.sportradar.us/nfl/v7/games/{game_id}/statistics.json?api_key=YOUR_API_KEY
    
    :param game_id: The ID of the game.
    :return: Parsed JSON data containing game statistics.
    """
    url = f"{BASE_URL}/games/{game_id}/statistics.json"
    params = {
        "api_key": SPORTRADAR_API_KEY
    }
    logging.info(f"Fetching game statistics for game_id: {game_id}")
    response = requests.get(url, params=params)
    if response.status_code != 200:
        logging.error(f"Failed to fetch game stats: {response.text}")
        return None
    logging.info("Successfully fetched game statistics.")
    return response.json()

def get_play_by_play(game_id):
    """
    Fetch play-by-play data for a given NFL game.
    Endpoint example:
      GET https://api.sportradar.com/nfl/official/trial/v7/en/games/{game_id}/pbp.json?api_key=YOUR_API_KEY
    :param game_id: The unique ID of the game.
    :return: Parsed JSON with play-by-play data.
    """
    url = f"{BASE_URL}/games/{game_id}/pbp.json"
    params = {
        "api_key": SPORTRADAR_API_KEY
    }
    logging.info(f"Fetching play-by-play data for game_id: {game_id}")
    response = requests.get(url, params=params)
    if response.status_code != 200:
        logging.error(f"Failed to fetch play-by-play data: {response.text}")
        return None
    logging.info("Successfully fetched play-by-play data.")
    return response.json()

def write_pretty_json(data, filename):
    """
    Write the JSON data to a file using json.dump to pretty-print it.
    This ensures proper formatting (e.g., using double quotes).
    :param data: The Python dictionary containing JSON data.
    :param filename: The filename (including path) to write the JSON.
    """
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logging.info(f"Prettified JSON written to {filename}")

def main():
    """
    Hard-coded test to fetch play-by-play data and write it to a file.
    Game ID: 2adf422c-e1e2-4cc9-8fd6-7ae89e73080e
    Scheduled Date (for reference): 2025-01-12T01:00:00+00:00
    Usage: python get_game_stats.py
    """
    # Hard-coded values for testing
    game_id = "2adf422c-e1e2-4cc9-8fd6-7ae89e73080e"
    scheduled_date = "2025-01-12T01:00:00+00:00"  # For logging reference only
    
    logging.info(f"Using hard-coded game ID: {game_id}")
    logging.info(f"Game scheduled date: {scheduled_date}")

    pbp_data = get_play_by_play(game_id)
    if pbp_data is not None:
        logging.info("Play-by-Play Data successfully retrieved.")
        # Write the JSON data to the 'data' folder (ensure folder exists)
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(data_dir, exist_ok=True)
        output_filename = os.path.join(data_dir, f"pbp_{game_id}_{timestamp}.json")
        write_pretty_json(pbp_data, output_filename)
    else:
        logging.error("No play-by-play data returned.")

if __name__ == "__main__":
    main()
