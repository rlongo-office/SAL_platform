import pymongo
import psycopg2
import logging
import re
from datetime import datetime

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "nfl-msf"
MONGO_COLLECTION = "pbp"

# PostgreSQL Configuration
PG_CONN = psycopg2.connect(
    host="localhost",
    database="SAL-db",
    user="postgres",
    password="password"
)
PG_CURSOR = PG_CONN.cursor()

# Logging Setup
log_filename = f"logs/etl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(filename=log_filename, level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Connect to MongoDB
mongo_client = pymongo.MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
pbp_collection = mongo_db[MONGO_COLLECTION]

# Utility Functions
def clean_string(value):
    """ Prepare string values for SQL statements by escaping quotes. """
    return value.replace("'", "''") if isinstance(value, str) else value

def safe_get(dictionary, key, default=None):
    """ Safely retrieve dictionary values and prevent NoneType errors. """
    return dictionary.get(key, default) if isinstance(dictionary, dict) else default

def extract_clock_time(description):
    """ Extracts (MM:SS) formatted time from the beginning of the description, if present. """
    match = re.match(r"\((\d{1,2}:\d{2})\)", description)
    return match.group(1) if match else None

def process_play(play, game_id, sequence_number):
    """ Process a single play from the MongoDB PBP document and insert into PostgreSQL """

    play_status = safe_get(play, "playStatus", {})
    quarter = safe_get(play_status, "quarter", None)
    seconds_elapsed = safe_get(play_status, "secondsElapsed", None)

    # Sequence tracking per game
    sequence_id = sequence_number

    description = clean_string(safe_get(play, "description", "No Description"))
    play_type = next((key for key in ["kick", "pass", "rush", "punt", "sack", "fieldGoalAttempt", "extraPointAttempt", "penalty"] if key in play), "UNKNOWN")

    clock_time = extract_clock_time(description)  # Extract clock time from description

    is_touchdown = safe_get(play, play_type, {}).get("isTouchdown", False)
    is_penalty = safe_get(play, play_type, {}).get("isFirstDownPenalty", False)
    is_no_play = safe_get(play, play_type, {}).get("isNoPlay", False)
    is_safety = safe_get(play, play_type, {}).get("isSafety", False)

    # Ensure uniqueness: If the play already exists (game_id + sequence_id), update instead of insert
    insert_sql = f"""
    INSERT INTO msf_nfl.plays (game_id, description, play_type, quarter, seconds_elapsed, team_in_possession,
        is_touchdown, is_penalty, is_no_play, is_safety, sequence_id, clock_time)
    VALUES ({game_id}, '{description}', '{play_type}', {quarter}, {seconds_elapsed},
        NULL, {is_touchdown}, {is_penalty}, {is_no_play}, {is_safety}, {sequence_id}, {f"'{clock_time}'" if clock_time else 'NULL'})
    ON CONFLICT (game_id, sequence_id) DO UPDATE SET
        description = EXCLUDED.description,
        play_type = EXCLUDED.play_type,
        is_touchdown = EXCLUDED.is_touchdown,
        is_penalty = EXCLUDED.is_penalty,
        is_no_play = EXCLUDED.is_no_play,
        is_safety = EXCLUDED.is_safety,
        clock_time = EXCLUDED.clock_time;
    """

    PG_CURSOR.execute(insert_sql)
    logging.info(f"🟢 Inserted Play: game_id={game_id}, sequence_id={sequence_id} (Clock Time: {clock_time})")

def process_pbp_documents():
    """ Process all PBP documents in MongoDB """
    total_plays_processed = 0

    for pbp_doc in pbp_collection.find({}):
        game_id = safe_get(safe_get(pbp_doc, "response", {}), "game", {}).get("id")
        if not game_id:
            logging.warning(f"⚠️ Skipping document due to missing game_id: {pbp_doc}")
            continue

        plays = safe_get(safe_get(pbp_doc, "response", {}), "plays", [])
        for sequence_number, play in enumerate(plays, start=1):
            process_play(play, game_id, sequence_number)
            total_plays_processed += 1

    PG_CONN.commit()
    logging.info(f"✅ **ETL Completed. Processed {total_plays_processed} plays.**")


def process_single_pbp_document():
    """ Process a single PBP document from MongoDB """
    pbp_doc = pbp_collection.find_one({})  # Fetch only one document

    if not pbp_doc:
        logging.warning("⚠️ No PBP document found in MongoDB. Exiting.")
        return

    game_id = safe_get(safe_get(pbp_doc, "response", {}), "game", {}).get("id")
    if not game_id:
        logging.warning(f"⚠️ Skipping document due to missing game_id: {pbp_doc}")
        return

    plays = safe_get(safe_get(pbp_doc, "response", {}), "plays", [])
    total_plays_processed = 0

    # Assign sequence IDs
    for sequence_number, play in enumerate(plays, start=1):
        process_play(play, game_id, sequence_number)
        total_plays_processed += 1

    PG_CONN.commit()
    logging.info(f"✅ **ETL Completed for One Document. Processed {total_plays_processed} plays.**")

# Run the ETL
process_single_pbp_document()

# Close PostgreSQL Connection
PG_CURSOR.close()
PG_CONN.close()
logging.info("🚀 PostgreSQL connection closed.")
