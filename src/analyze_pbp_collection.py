import pymongo
import os
import logging
from datetime import datetime

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "nfl-msf"
MONGO_COLLECTION = "pbp"

# Create 'logs' directory if it doesn't exist
os.makedirs("logs", exist_ok=True)

# Create timestamped log file
log_filename = os.path.join("logs", f"pbp_field_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Configure logging
logging.basicConfig(filename=log_filename, level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Connect to MongoDB
mongo_client = pymongo.MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
pbp_collection = mongo_db[MONGO_COLLECTION]

# Dictionary to track key occurrences across ALL documents
key_counts = {}

# Recursive function to process all keys
def track_keys(data, parent_path=""):
    """
    Recursively track the presence of keys in nested JSON.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            full_path = f"{parent_path}.{key}" if parent_path else key  # Build full key path

            # Initialize count tracking
            if full_path not in key_counts:
                key_counts[full_path] = {"non_null_count": 0, "total_count": 0, "missing_count": 0}

            # Update counts
            key_counts[full_path]["total_count"] += 1
            if value is not None:
                key_counts[full_path]["non_null_count"] += 1

            # Recurse into nested objects
            track_keys(value, full_path)

    elif isinstance(data, list):
        # If it's a list, track how many times this key is used
        for item in data:
            track_keys(item, parent_path)

# --- PROCESS ALL PLAY-BY-PLAY DOCUMENTS ---
total_documents = pbp_collection.count_documents({})
logging.info(f"📊 Found {total_documents} play-by-play documents in MongoDB.")

if total_documents == 0:
    logging.warning("⚠️ No documents found in MongoDB. Exiting script.")
    exit(1)

# Process each document
for index, doc in enumerate(pbp_collection.find({})):
    plays = doc.get("response", {}).get("plays", [])
    
    if not plays:
        logging.warning(f"⚠️ Skipping game {doc.get('game_id')} - No plays recorded!")
        continue

    track_keys(plays, "response.plays")

# --- LOG RESULTS ---
logging.info("🔍 Play-by-Play Field Analysis Report")
logging.info("=" * 50)

for key, counts in sorted(key_counts.items()):
    total = counts["total_count"]
    non_null = counts["non_null_count"]
    missing = total_documents - total  # How many documents didn't even have this key?

    logging.info(f"{key}: {non_null}/{total} non-null occurrences, missing in {missing} documents")

logging.info("=" * 50)
logging.info("🎉 Analysis Complete! Check the log file for details.")
