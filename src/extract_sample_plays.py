import pymongo
import re
import logging
import os
import json
import random
from collections import defaultdict

# Setup Logging
LOG_DIR = "logs"
DATA_DIR = "data"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "pbp_text_analysis.log")
SAMPLE_FILE = os.path.join(DATA_DIR, "sample_plays.json")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# MongoDB Configuration
MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "nfl-msf"
PBP_COLLECTION = "pbp"

# Connect to MongoDB
client = pymongo.MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
pbp_collection = db[PBP_COLLECTION]

# Function to extract a sample of play objects and save them as JSON
def extract_sample_plays(sample_size=50):
    """Extracts a sample of play-by-play objects and saves them as JSON."""
    logging.info(f"📥 Extracting {sample_size} random play objects for AI analysis...")

    # Retrieve all pbp documents and collect play objects
    cursor = pbp_collection.find({}, {"response.plays": 1})  # Only fetch plays to keep it lightweight
    all_plays = []

    for doc in cursor:
        plays = doc.get("response", {}).get("plays", [])
        all_plays.extend(plays)

    # If we don't have enough plays, adjust sample size
    total_plays = len(all_plays)
    if total_plays == 0:
        logging.warning("🚨 No plays found in database. Skipping sample extraction.")
        return
    elif total_plays < sample_size:
        logging.warning(f"⚠️ Only {total_plays} plays available, reducing sample size.")
        sample_size = total_plays

    # Randomly sample plays
    sampled_plays = random.sample(all_plays, sample_size)

    # Save to JSON file
    with open(SAMPLE_FILE, "w", encoding="utf-8") as f:
        json.dump(sampled_plays, f, indent=4)

    logging.info(f"✅ Saved {sample_size} sample plays to {SAMPLE_FILE}")

# Run the extraction
extract_sample_plays()
