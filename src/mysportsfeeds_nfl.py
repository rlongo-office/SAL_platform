import os
import json
import base64
import requests
from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Retrieve API key from environment
#api_key = os.getenv('MYSPORTSFEEDS_API_KEY')
api_key = "b6619248-cfe0-48d1-8c84-b2798b"
# Manually encode the API key for Basic Auth
auth_string = f"{api_key}:MYSPORTSFEEDS"
encoded_auth = base64.b64encode(auth_string.encode()).decode()

# Define API request parameters (using v2.1 instead of v2.0)
league = 'nfl'
season = '2024-preseason'  # Adjust as needed
#game_id = '20240905-BAL-KC'  # Example game ID from the docs
#game_id = '20240906-GB-PHI'  # Example game ID from the docs
game_id = '20240808-CAR-NE'  # Example game ID from the docs

# Construct the updated v2.1 API URL
url = f"https://api.mysportsfeeds.com/v2.1/pull/{league}/{season}/games/{game_id}/playbyplay.json"

# Headers with manually encoded Authorization
headers = {
    "Authorization": f"Basic {encoded_auth}",
    "Accept-Encoding": "gzip",
    "User-Agent": "MySportsFeeds Python/2.1.1"
}

# Make the request using requests
response = requests.get(url, headers=headers)

# Check if the request was successful
if response.status_code == 200:
    # Save response to JSON file
    data_folder = os.path.join(os.path.dirname(__file__), "../data")
    os.makedirs(data_folder, exist_ok=True)
    output_file = os.path.join(data_folder, f"playbyplay_{game_id}.json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(response.json(), f, indent=4)

    print(f"✅ Play-by-play data saved to {output_file}")

else:
    print(f"❌ Error: API request failed with status code {response.status_code}")
