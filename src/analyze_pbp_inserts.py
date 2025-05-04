import pymongo
import logging
import json
from datetime import datetime

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "nfl-msf"
MONGO_COLLECTION = "pbp"

# Logging Setup
log_filename = f"logs/schema_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(filename=log_filename, level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Connect to MongoDB
mongo_client = pymongo.MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
pbp_collection = mongo_db[MONGO_COLLECTION]

# List of missing fields detected
missing_fields_log = []

def clean_string(value):
    """ Prepare string values for SQL statements by escaping quotes. """
    if isinstance(value, str):
        return value.replace("'", "''")
    return value

def safe_get(dictionary, key, default="NULL"):
    """ Safely retrieve dictionary values and prevent NoneType errors. """
    return dictionary.get(key, default) if isinstance(dictionary, dict) else default

def validate_play(play, game_id):
    """ Validate a single play against the schema and generate SQL INSERT statements. """
    play_id = safe_get(play, "id", "NULL")
    description = clean_string(safe_get(play, "description", "No Description"))
    play_status = safe_get(play, "playStatus", {})

    # Identify play type
    play_type = next((key for key in ["kick", "pass", "rush", "punt", "sack", "fieldGoalAttempt", "extraPointAttempt", "penalty"] if key in play), None)
    play_data = safe_get(play, play_type, {}) if play_type else {}

    # Extract key fields from playStatus
    quarter = safe_get(play_status, "quarter", "NULL")
    seconds_elapsed = safe_get(play_status, "secondsElapsed", "NULL")
    team_in_possession = safe_get(safe_get(play_status, "teamInPossession", {}), "id", "NULL")
    down = safe_get(play_status, "currentDown", "NULL")
    yards_remaining = safe_get(play_status, "yardsRemaining", "NULL")
    overall_drive_num = safe_get(play_status, "overallDriveNum", "NULL")
    team_drive_num = safe_get(play_status, "teamDriveNum", "NULL")

    # Line of Scrimmage Information
    line_of_scrimmage = safe_get(play_status, "lineOfScrimmage", {})
    line_team_id = safe_get(safe_get(line_of_scrimmage, "team", {}), "id", "NULL")
    line_yard = safe_get(line_of_scrimmage, "yardLine", "NULL")

    # Outcome Flags
    is_touchdown = safe_get(play_data, "isTouchdown", False)
    is_penalty = safe_get(play_data, "isFirstDownPenalty", False)
    is_no_play = safe_get(play_data, "isNoPlay", False)
    is_safety = safe_get(play_data, "isSafety", False)
    is_tackled = safe_get(play_data, "isTackled", False)
    is_out_of_bounds = safe_get(play_data, "isOutOfBounds", False)
    is_two_point_conversion = safe_get(play_data, "isTwoPointConversion", False)
    is_first_down_penalty = safe_get(play_data, "isFirstDownPenalty", False)

    # Play-Specific Fields
    yards_gained = safe_get(play_data, "yardsRushed") or safe_get(play_data, "yardsKicked") or safe_get(play_data, "totalYardsGained")
    yards_rushed = safe_get(play_data, "yardsRushed", "NULL")
    yards_passed = safe_get(play_data, "yardsPassed", "NULL")
    yards_intercepted = safe_get(play_data, "yardsIntercepted", "NULL")
    total_yards_gained = safe_get(play_data, "totalYardsGained", "NULL")
    pass_type = safe_get(play_data, "passType", "NULL")
    pass_direction = safe_get(play_data, "passDirection", "NULL")
    pass_distance = safe_get(play_data, "passDistance", "NULL")
    rush_type = safe_get(play_data, "rushType", "NULL")
    rush_direction = safe_get(play_data, "rushDirection", "NULL")

    # Validate fields
    missing_fields = []
    required_fields = [
        "quarter", "seconds_elapsed", "team_in_possession", "down", "yards_remaining", 
        "line_of_scrimmage_yard", "line_of_scrimmage_team_id", "yards_gained"
    ]
    for field in required_fields:
        if locals().get(field) == "NULL":
            missing_fields.append(field)

    if missing_fields:
        logging.warning(f"⚠️ Missing fields in play {play_id}: {missing_fields}")
        missing_fields_log.append((play_id, missing_fields))

    # Generate SQL Insert Statement
    insert_sql = f"""
    INSERT INTO plays (game_id, description, play_type, quarter, seconds_elapsed, team_in_possession, 
        down, yards_remaining, line_of_scrimmage_yard, line_of_scrimmage_team_id,
        overall_drive_num, team_drive_num, yards_gained, yards_rushed, yards_passed, yards_intercepted, total_yards_gained,
        is_touchdown, is_penalty, is_no_play, is_safety, is_tackled, is_out_of_bounds, is_two_point_conversion, is_first_down_penalty,
        pass_type, pass_direction, pass_distance, rush_type, rush_direction)
    VALUES ({game_id}, '{description}', '{play_type.upper() if play_type else "UNKNOWN"}', {quarter}, 
        {seconds_elapsed}, {team_in_possession}, {down}, {yards_remaining}, {line_yard}, {line_team_id},
        {overall_drive_num}, {team_drive_num}, {yards_gained}, {yards_rushed}, {yards_passed}, {yards_intercepted}, {total_yards_gained},
        {is_touchdown}, {is_penalty}, {is_no_play}, {is_safety}, {is_tackled}, {is_out_of_bounds}, {is_two_point_conversion}, {is_first_down_penalty},
        '{pass_type}', '{pass_direction}', {pass_distance}, '{rush_type}', '{rush_direction}');
    """
    logging.info(f"🟢 Planned Play INSERT:\n{insert_sql}")

    logging.info("=" * 80)

# Process all PBP documents
total_plays_checked = 0

for pbp_doc in pbp_collection.find({}):
    game_id = safe_get(safe_get(pbp_doc, "response", {}), "game", {}).get("id")
    if not game_id:
        logging.warning(f"⚠️ Skipping document due to missing game_id: {pbp_doc}")
        continue

    plays = safe_get(safe_get(pbp_doc, "response", {}), "plays", [])
    for play in plays:
        validate_play(play, game_id)
        total_plays_checked += 1

# Log summary of missing fields
logging.info("\n⚠️ **SUMMARY: Missing Fields Across Plays** ⚠️")
for play_id, missing in missing_fields_log:
    logging.warning(f"Play {play_id}: Missing {missing}")

logging.info(f"✅ **Schema validation completed. Checked {total_plays_checked} plays.**")
logging.info(f"📄 Review the log file: {log_filename}")
