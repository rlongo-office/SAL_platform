import requests
import os
import json
import time
from requests.auth import HTTPBasicAuth

# Setup Directories
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# MySportsFeeds API Configuration
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl"

# Define the season and week
SEASON = "2024-2025-regular"
WEEK = "1"  # Pulling for NFL Week 1

# Construct API URL for Weekly Game Lines
url = f"{BASE_URL}/{SEASON}/week/{WEEK}/odds_gamelines.json"

# Make the API request
response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))

# Save response if successful
if response.status_code == 200:
    data = response.json()
    output_file = os.path.join(DATA_DIR, "odds_week1.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"✅ Saved weekly odds data for Week {WEEK} to {output_file}")

elif response.status_code == 429:
    print(f"❌ Error: Rate limit exceeded (429 Too Many Requests)")
    print("⚠️ Waiting 60 seconds before retrying...")
    time.sleep(60)  # Wait before retrying
    # We could retry here, but for now, let’s just note the failure

else:
    print(f"❌ Error fetching weekly odds for Week {WEEK}: {response.status_code}")
    print(response.text)  # Print error details if available

# ✅ Add delay to prevent hitting the rate limit if running multiple calls
time.sleep(10)
