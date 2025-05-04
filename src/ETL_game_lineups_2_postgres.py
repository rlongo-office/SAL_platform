import psycopg2
import pymongo
import os
import logging
from datetime import datetime
import re

# PostgreSQL Configuration
PG_HOST = "localhost"
PG_DATABASE = "SAL-db"
PG_USER = "postgres"
PG_PASSWORD = "password"
PG_SCHEMA = "msf_nfl"

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "nfl-msf"
MONGO_COLLECTION = "game_lineups"

# Create 'logs' directory if it doesn't exist
os.makedirs("logs", exist_ok=True)

# Create timestamped log file
log_filename = os.path.join("logs", f"load_game_lineups_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging
logging.basicConfig(filename=log_filename, level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Connect to MongoDB
mongo_client = pymongo.MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
game_lineups_collection = mongo_db[MONGO_COLLECTION]

# Connect to PostgreSQL
pg_conn = psycopg2.connect(
    host=PG_HOST,
    database=PG_DATABASE,
    user=PG_USER,
    password=PG_PASSWORD
)
pg_cursor = pg_conn.cursor()

# --- INSERT INTO game_lineups ---
INSERT_GAME_LINEUP_QUERY = f"""
INSERT INTO {PG_SCHEMA}.game_lineups (game_id, team_id, lineup_type, created_at)
VALUES (%s, %s, %s, NOW())
ON CONFLICT (game_id, team_id, lineup_type) DO UPDATE 
SET created_at = EXCLUDED.created_at
RETURNING id;
"""

# --- INSERT INTO game_positions ---
INSERT_GAME_POSITION_QUERY = f"""
INSERT INTO {PG_SCHEMA}.game_positions (lineup_id, position, player_id, jersey_number, created_at)
VALUES (%s, %s, %s, %s, NOW())
ON CONFLICT (lineup_id, player_id, position) DO UPDATE 
SET jersey_number = EXCLUDED.jersey_number;
"""

def clean_jersey_number(jersey_number):
    if isinstance(jersey_number, int):
        return jersey_number  # Already a valid integer, return as-is
    if isinstance(jersey_number, str):
        match = re.fullmatch(r"(\d+)[A-Z]*", jersey_number)  # Extract leading digits
        if match:
            cleaned_number = int(match.group(1))
            logging.warning(f"⚠️ Adjusted jersey number: '{jersey_number}' → {cleaned_number}")
            return cleaned_number
    return None  # If it's neither an int nor a valid string, return None



# --- CHECK DOCUMENT COUNT ---
total_docs = game_lineups_collection.count_documents({})
logging.info(f"📊 Found {total_docs} documents in game_lineups collection.")

if total_docs == 0:
    logging.warning("⚠️ No documents found in MongoDB. Exiting script.")
    exit(1)  # Exit if there are no records to process

# --- PROCESS GAME LINEUPS ---
processed_count = 0
skipped_count = 0

game_lineups = game_lineups_collection.find({})

for index, doc in enumerate(game_lineups):
    game_id = doc.get("game_id")
    season = doc.get("season")

    if index < 5:  # Log a sample of the first few documents
        logging.debug(f"🟢 Sample document {index+1}: {doc}")

    if not game_id:
        logging.warning(f"⚠️ Skipping lineup due to missing game_id: {doc}")
        skipped_count += 1
        continue

    lineup_data = doc.get("response", {}).get("teamLineups", [])

    if not lineup_data:
        logging.warning(f"⚠️ Skipping lineup: No 'teamLineups' found for game {game_id}")
        skipped_count += 1
        continue

    for lineup in lineup_data:
        team_info = lineup.get("team", {})
        team_id = team_info.get("id")
        expected_data = lineup.get("expected")
        actual_data = lineup.get("actual")

        if not team_id:
            logging.warning(f"⚠️ Skipping lineup: Missing team_id for game {game_id}")
            skipped_count += 1
            continue

        for lineup_type, data in [("expected", expected_data), ("actual", actual_data)]:
            if data is None:
                logging.info(f"🔹 No '{lineup_type}' lineup for team {team_id} in game {game_id}. Skipping...")
                continue

            try:
                # ✅ Insert into `game_lineups` and **commit immediately** to avoid FK issues
                logging.debug(f"📌 Inserting into game_lineups: game_id={game_id}, team_id={team_id}, lineup_type={lineup_type}")
                pg_cursor.execute(INSERT_GAME_LINEUP_QUERY, (game_id, team_id, lineup_type))
                lineup_id = pg_cursor.fetchone()[0]
                pg_conn.commit()  # 🛑 Ensure the lineup exists before inserting positions

                inserted_positions = 0

                for position_data in data.get("lineupPositions", []):
                    position = position_data.get("position")

                    if not position:
                        logging.warning(f"⚠️ Skipping missing position field in game {game_id}")
                        continue

                    player_data = position_data.get("player")
                    if player_data:
                        player_id = player_data.get("id")
                        jersey_number = clean_jersey_number(player_data.get("jerseyNumber"))

                        # ✅ If jersey number is missing, set to NULL (not "N/A")
                        if jersey_number is None:
                            logging.info(f"🔸 No jersey number for player {player_id} in game {game_id}, position {position}. Skipping insert.")
                            continue

                        if not player_id:
                            logging.warning(f"⚠️ Skipping missing player_id for position {position} in game {game_id}")
                            continue

                        try:
                            logging.debug(f"📌 Inserting into game_positions: lineup_id={lineup_id}, position={position}, player_id={player_id}, jersey_number={jersey_number}")
                            pg_cursor.execute(INSERT_GAME_POSITION_QUERY, (lineup_id, position, player_id, jersey_number))
                            inserted_positions += 1
                        except Exception as e:
                            logging.error(f"❌ SQL Error inserting player {player_id} in game {game_id}, position {position}: {e}")
                            pg_conn.rollback()  # Rollback after player insertion failure

                processed_count += 1
                logging.info(f"✅ Inserted {inserted_positions} positions for Game {game_id}, Team {team_id}, Type {lineup_type}.")

            except Exception as e:
                logging.error(f"❌ SQL Error for Game ID {game_id}, Team {team_id}, lineup_type={lineup_type}: {e}")
                pg_conn.rollback()  # Rollback after lineup insertion failure
                skipped_count += 1

pg_conn.commit()
pg_cursor.close()
pg_conn.close()

logging.info(f"🎉 Finished processing. Successfully processed {processed_count} game lineups. Skipped {skipped_count} due to missing data.")
