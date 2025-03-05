import pymongo
import re
import logging
import os
from collections import defaultdict

# Setup Logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "pbp_text_analysis.log")

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

# Lexicon of key words and phrases to detect
LEXICON = {
    "blitzing": ["blitz", "rusher", "rushers", "hurry", "pressured", "pressure", "QB hit", "pass rush"],
    "coverage": ["man coverage", "zone", "cover 2", "cover 3", "cover 4", "nickel", "dime", "safety help"],
    "missed tackles": ["missed tackle", "evades", "spins away", "broken play", "escaped", "slips past"],
    "defensive line": ["in the box", "linebacker", "DL", "edge", "outside linebacker", "defensive tackle"],
    "run game": ["A-gap", "B-gap", "C-gap", "D-gap", "inside zone", "outside zone", "gap scheme", "power run"],
    "play action": ["play action", "fake handoff", "bootleg", "rollout", "double reverse", "flea flicker"],
    "passing": ["scrambles", "off balance", "deep pass", "short pass", "sideline", "across the middle", "no-look"],
    "blocking": ["pocket collapses", "pancake block", "offensive line", "pulling guard", "chip block"]
}

# Function to analyze play descriptions and log word frequency counts
def analyze_pbp_descriptions():
    word_counts = defaultdict(int)  # Store word occurrence counts
    total_plays = 0

    # Retrieve all pbp documents
    cursor = pbp_collection.find({})
    
    for doc in cursor:
        plays = doc.get("response", {}).get("plays", [])
        
        for play in plays:
            description = play.get("description", "").lower()  # Normalize text
            total_plays += 1

            # Search for each lexicon category
            for category, words in LEXICON.items():
                for word in words:
                    if re.search(rf"\b{word}\b", description):  # Match whole words
                        word_counts[word] += 1

    # Log total word counts
    logging.info(f"✅ Total Plays Processed: {total_plays}")
    logging.info(f"✅ Word Frequency Counts:")
    for word, count in sorted(word_counts.items(), key=lambda x: x[1], reverse=True):
        logging.info(f"   {word}: {count} occurrences")

# Run the analysis
analyze_pbp_descriptions()
