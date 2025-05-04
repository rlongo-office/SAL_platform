import psycopg2
import pymongo
import os
import logging
from datetime import datetime

# PostgreSQL Configuration
PG_HOST = "localhost"
PG_DATABASE = "SAL-db"
PG_USER = "postgres"
PG_PASSWORD = "password"
PG_SCHEMA = "msf-nfl"

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "nfl-msf"
MONGO_COLLECTION = "players"

# Create 'logs' directory if it doesn't exist
os.makedirs("logs", exist_ok=True)

# Create timestamped log file
log_filename = os.path.join("logs", f"load_players_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging (No terminal output, only logs)
logging.basicConfig(filename=log_filename, level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Connect to MongoDB
mongo_client = pymongo.MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
players_collection = mongo_db[MONGO_COLLECTION]

# Connect to PostgreSQL
pg_conn = psycopg2.connect(
    host=PG_HOST,
    database=PG_DATABASE,
    user=PG_USER,
    password=PG_PASSWORD
)
pg_conn.autocommit = True  # ✅ Prevent transaction locking
pg_cursor = pg_conn.cursor()

# PostgreSQL Insert Query
INSERT_QUERY = """
INSERT INTO players (
    id, first_name, last_name, primary_position, jersey_number, height, weight,
    birth_date, age, birth_city, birth_country, rookie, high_school, college,
    handedness, official_image_src, current_roster_status, current_team_id,
    contract_signing_team_id, contract_signed_on, contract_total_years,
    contract_total_salary, contract_total_bonuses, contract_expiry_status,
    draft_year, draft_team_id, draft_round, draft_round_pick, draft_overall_pick
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (id) DO UPDATE SET
    first_name = EXCLUDED.first_name,
    last_name = EXCLUDED.last_name,
    primary_position = EXCLUDED.primary_position,
    jersey_number = EXCLUDED.jersey_number,
    height = EXCLUDED.height,
    weight = EXCLUDED.weight,
    birth_date = EXCLUDED.birth_date,
    age = EXCLUDED.age,
    birth_city = EXCLUDED.birth_city,
    birth_country = EXCLUDED.birth_country,
    rookie = EXCLUDED.rookie,
    high_school = EXCLUDED.high_school,
    college = EXCLUDED.college,
    handedness = EXCLUDED.handedness,
    official_image_src = EXCLUDED.official_image_src,
    current_roster_status = EXCLUDED.current_roster_status,
    current_team_id = EXCLUDED.current_team_id,
    contract_signing_team_id = EXCLUDED.contract_signing_team_id,
    contract_signed_on = EXCLUDED.contract_signed_on,
    contract_total_years = EXCLUDED.contract_total_years,
    contract_total_salary = EXCLUDED.contract_total_salary,
    contract_total_bonuses = EXCLUDED.contract_total_bonuses,
    contract_expiry_status = EXCLUDED.contract_expiry_status,
    draft_year = EXCLUDED.draft_year,
    draft_team_id = EXCLUDED.draft_team_id,
    draft_round = EXCLUDED.draft_round,
    draft_round_pick = EXCLUDED.draft_round_pick,
    draft_overall_pick = EXCLUDED.draft_overall_pick;
"""

def clean_height(height):
    """Fix height formatting to be consistent (e.g., '5\'11"' -> '5-11')."""
    if height:
        height = height.replace("'", "-")  # Convert feet format from 5'11" to 5-11
        height = height.replace('"', '')  # Remove inches quotation marks
        return height
    return None

def extract_handedness(data):
    """Extract the handedness field correctly if it's a dictionary."""
    if isinstance(data, dict):
        return data.get("throws")  # Extract the throws field if it exists
    return data  # Otherwise, return it as is

# Process Players from MongoDB
players = players_collection.find()
for player_doc in players:
    player = player_doc["player"]

    # Extract contract details safely
    contract_year = player.get("currentContractYear")
    overall_contract = contract_year.get("overallContract") if contract_year else None
    signing_team = overall_contract.get("signingTeam") if overall_contract else None

    # Extract draft details safely
    drafted_info = player.get("drafted")
    draft_team = drafted_info.get("team") if drafted_info else None

    player_data = (
        player["id"],
        player.get("firstName"),
        player.get("lastName"),
        player.get("primaryPosition"),
        player.get("jerseyNumber"),
        clean_height(player.get("height")),
        player.get("weight"),
        player.get("birthDate"),
        player.get("age"),
        player.get("birthCity"),
        player.get("birthCountry"),
        player.get("rookie", False),
        player.get("highSchool"),
        player.get("college"),
        extract_handedness(player.get("handedness")),  # ✅ Fix for handedness field
        player.get("officialImageSrc").replace("%", "%%") if player.get("officialImageSrc") else None,
        player.get("currentRosterStatus"),
        None,  # current_team_id
        signing_team.get("id") if signing_team else None,
        overall_contract.get("signedOn") if overall_contract else None,
        overall_contract.get("totalYears") if overall_contract else None,
        overall_contract.get("totalSalary") if overall_contract else None,
        overall_contract.get("totalBonuses") if overall_contract else None,
        overall_contract.get("expiryStatus") if overall_contract else None,
        drafted_info.get("year") if drafted_info else None,
        draft_team.get("id") if draft_team else None,
        drafted_info.get("round") if drafted_info else None,
        drafted_info.get("roundPick") if drafted_info else None,
        drafted_info.get("overallPick") if drafted_info else None,
    )

    try:
        pg_cursor.execute(INSERT_QUERY, player_data)
        logging.info(f"✅ Successfully inserted Player ID {player_data[0]}")
    except Exception as e:
        logging.error(f"❌ SQL Execution Error: {e}")
        logging.error(f"❌ Problematic Data (Player ID {player_data[0]}): {player_data}")
        pg_conn.rollback()  # ✅ Rollback to avoid transaction blocking

pg_cursor.close()
pg_conn.close()
logging.info("✅ Player data migration completed.")
