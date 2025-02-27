import os
import sys
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
import psycopg2

# Load environment variables
load_dotenv(find_dotenv())

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = os.path.join(LOG_DIR, f"load_team_player_{timestamp}.log")

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

# Connect to PostgreSQL
def connect_db():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        database="SAL-db",
        user="postgres",
        password=os.getenv("POSTGRES_PASSWORD")
    )

# Fetch all player profiles
def fetch_player_profiles():
    url = f"{BASE_URL}/league/players.json"
    params = {"api_key": SPORTRADAR_API_KEY}
    logging.info("Fetching all player profiles from Sportradar API...")
    
    response = requests.get(url, params=params)
    if response.status_code != 200:
        logging.error(f"Failed to fetch player profiles: {response.text}")
        return None

    logging.info("Successfully fetched player profiles.")
    return response.json()

def insert_team(cursor, cache, team_data):
    team_id = team_data["id"]
    team_name = team_data.get("name", "TBD")  # Default to 'TBD' if missing
    team_market = team_data.get("market", "TBD")
    team_alias = team_data.get("alias", team_name[:3].upper())  # Use alias if available, otherwise first 3 letters

    # Use default placeholders for missing data
    state = team_data.get("state", "TBD")
    city = team_data.get("city", "TBD")
    country = team_data.get("country", "TBD")
    venue_id = None  # We don't have venue info from this endpoint
    established_year = team_data.get("established", None)  # Might be missing

    if team_id not in cache["teams"]:
        cursor.execute(
            """
            INSERT INTO nfl.teams (team_id, name, market, alias, state, city, country, venue_id, established_year)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (team_id) DO NOTHING;
            """,
            (team_id, team_name, team_market, team_alias, state, city, country, venue_id, established_year)
        )
        cache["teams"].add(team_id)
        logging.info(f"Inserted new team: {team_market} {team_name} (Alias: {team_alias}) - Some details TBD")


# Insert player into database if they don’t exist
def insert_player(cursor, cache, player_data):
    sportradar_id = player_data["id"]
    player_name = player_data["name"]
    position = player_data["position"]
    number = player_data.get("jersey", None)  # Jersey number (if available)
    height = player_data.get("height", None)  # Height in inches
    weight = player_data.get("weight", None)  # Weight in pounds
    college = player_data.get("college", None)
    draft_year = player_data.get("draft", {}).get("year", None)
    draft_round = player_data.get("draft", {}).get("round", None)
    draft_pick = player_data.get("draft", {}).get("number", None)
    team_id = player_data["team"]["id"] if "team" in player_data else None  # Associate player with team

    if sportradar_id not in cache["players"]:
        cursor.execute(
            """
            INSERT INTO nfl.players (
                sportradar_id, name, position, number, height, weight, college, 
                draft_year, draft_round, draft_pick
            ) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sportradar_id) DO NOTHING;
            """,
            (sportradar_id, player_name, position, number, height, weight, college,
             draft_year, draft_round, draft_pick)
        )
        cache["players"].add(sportradar_id)
        logging.info(f"Inserted new player: {player_name} (Position: {position})")

# Load team and player data into PostgreSQL
def load_teams_and_players():
    conn = connect_db()
    cursor = conn.cursor()

    # Preload existing teams and players to prevent duplicates
    cache = {"teams": set(), "players": set()}

    cursor.execute("SELECT team_id FROM nfl.teams;")
    cache["teams"] = {row[0] for row in cursor.fetchall()}

    cursor.execute("SELECT sportradar_id FROM nfl.players;")
    cache["players"] = {row[0] for row in cursor.fetchall()}

    player_profiles = fetch_player_profiles()
    if not player_profiles or "players" not in player_profiles:
        logging.error("No valid player data found. Exiting process.")
        conn.close()
        return

    # Process each player
    for player_data in player_profiles["players"]:
        if "team" in player_data:
            insert_team(cursor, cache, player_data["team"])  # Insert team if needed

        insert_player(cursor, cache, player_data)  # Insert player

    conn.commit()
    cursor.close()
    conn.close()
    logging.info("Finished loading teams and players into PostgreSQL.")

# Main execution
if __name__ == "__main__":
    load_teams_and_players()
