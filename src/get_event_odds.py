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
log_filename = os.path.join(LOG_DIR, f"get_event_odds_{timestamp}.log")

logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"

def get_historical_events(sport, date_str):
    """
    Get historical events for a given sport and snapshot time.
    :param sport: Sport key (e.g., 'americanfootball_nfl')
    :param date_str: ISO8601 date string (e.g., '2024-09-26T12:00:00Z')
    :return: JSON response with events data.
    """
    url = f"{BASE_URL}/historical/sports/{sport}/events"
    params = {
        "apiKey": API_KEY,
        "date": date_str,  # Return closest snapshot equal to or earlier than this timestamp.
        "dateFormat": "iso"
    }
    logging.info(f"Fetching historical events for {sport} at {date_str}")
    response = requests.get(url, params=params)
    if response.status_code != 200:
        logging.error(f"Failed to get historical events: {response.text}")
        return None
    return response.json()

def get_event_odds(sport, event_id, snapshot_date, markets="h2h,spreads,totals", regions="us", oddsFormat="american"):
    """
    Get historical odds for a single event by event_id.
    
    :param sport: Sport key (e.g., 'americanfootball_nfl')
    :param event_id: The event ID obtained from the historical events endpoint.
    :param snapshot_date: The ISO8601 timestamp to use for the snapshot (e.g., '2024-09-26T12:00:00Z')
    :param markets: Comma-separated market keys.
    :param regions: Comma-separated region codes.
    :param oddsFormat: Odds format ('american' or 'decimal')
    :return: JSON response with odds data.
    """
    url = f"{BASE_URL}/historical/sports/{sport}/events/{event_id}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": oddsFormat,
        "dateFormat": "iso",
        "date": snapshot_date  # This is the snapshot timestamp
    }
    logging.info(f"Fetching odds for event {event_id} using markets: {markets} at snapshot {snapshot_date}")
    response = requests.get(url, params=params)
    if response.status_code != 200:
        logging.error(f"Failed to get event odds: {response.text}")
        return None
    return response.json()

def main():
    """
    Usage: python get_event_odds.py <snapshot_timestamp>
    Example: python get_event_odds.py 2024-09-26T12:00:00Z
    """
    if len(sys.argv) < 2:
        logging.error("Usage: python get_event_odds.py <snapshot_timestamp>")
        sys.exit(1)

    snapshot_timestamp = sys.argv[1]
    sport = "americanfootball_nfl"  # We're interested in NFL events.

    # Fetch historical events for NFL on the given snapshot time.
    events_data = get_historical_events(sport, snapshot_timestamp)
    if events_data is None:
        logging.error("No events data returned.")
        sys.exit(1)

    # Look for the event between Dallas Cowboys and New York Giants.
    target_home = "dallas cowboys"
    target_away = "new york giants"
    target_event = None

    # The response contains a structure like:
    # { "timestamp": "...", "previous_timestamp": "...", "next_timestamp": "...", "data": [ { event }, ... ] }
    events_list = events_data.get("data", [])
    logging.info(f"Found {len(events_list)} historical events in snapshot.")

    for event in events_list:
        home_team = event.get("home_team", "").lower().strip()
        away_team = event.get("away_team", "").lower().strip()
        if (target_home in home_team or target_home in away_team) and (target_away in home_team or target_away in away_team):
            target_event = event
            break

    if not target_event:
        logging.error(f"No event found for {target_home} vs {target_away}.")
        sys.exit(1)

    event_id = target_event.get("id")
    logging.info(f"Found event ID: {event_id} for {target_home} vs {target_away}.")

    # Fetch odds for the found event.
    odds_data = get_event_odds(sport, event_id, snapshot_timestamp)
    if odds_data is None:
        logging.error("No odds data returned.")
        sys.exit(1)

    # Log the odds data (do not print to terminal)
    logging.info("Odds Data for event:")
    logging.info(odds_data)

if __name__ == "__main__":
    main()
