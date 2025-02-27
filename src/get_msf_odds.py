import requests
import os
import json
import time
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth

# Setup Directories
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# MySportsFeeds API Configuration
API_KEY = "b6619248-cfe0-48d1-8c84-b2798b"
PASSWORD = "MYSPORTSFEEDS"
BASE_URL = "https://api.mysportsfeeds.com/v2.1/pull/nfl"

# Define the game date (NFL Season Opener: September 5, 2024)
GAME_DATE = datetime.strptime("20240905", "%Y%m%d")

# Loop through 6 days before the game, up to and including game day
for i in range(7):  
    target_date = GAME_DATE - timedelta(days=6 - i)
    date_str = target_date.strftime("%Y%m%d")
    
    # Construct API URL for each date
    url = f"{BASE_URL}/2024-2025-regular/date/{date_str}/odds_gamelines.json"
    
    # Make the API request
    response = requests.get(url, auth=HTTPBasicAuth(API_KEY, PASSWORD))
    
    # Save response if successful
    if response.status_code == 200:
        data = response.json()
        output_file = os.path.join(DATA_DIR, f"odds_{date_str}.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        print(f"✅ Saved odds data for {date_str} to {output_file}")
    
    elif response.status_code == 429:
        print(f"❌ Error: Rate limit exceeded for {date_str} (429 Too Many Requests)")
        print("⚠️ Waiting 60 seconds before retrying...")
        time.sleep(60)  # Wait before retrying
        continue  # Move to the next request after waiting
    
    else:
        print(f"❌ Error fetching odds for {date_str}: {response.status_code}")
        print(response.text)  # Print error details if available
    
    # ✅ Add delay to prevent hitting the rate limit
    time.sleep(10)  # Adjust as needed to avoid hitting API limits
