import os
import json
import psycopg2
import logging
from datetime import datetime
from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Create logs directory if it doesn't exist
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_filename = os.path.join(LOG_DIR, f"process_json_{timestamp}.log")

logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def connect_db():
    """ Establish a connection to the PostgreSQL database. """
    return psycopg2.connect(
        host="localhost",
        port=5432,
        database="SAL-db",
        user="postgres",
        password=os.getenv("POSTGRES_PASSWORD")
    )

def preload_data(cursor):
    """ Preload supporting tables into memory to reduce database queries. """
    def fetch_dict(query, key_col, value_col):
        cursor.execute(query)
        return {row[key_col]: row[value_col] for row in cursor.fetchall()}

    logging.info("Preloading database tables into memory.")
    return {
        "participants": fetch_dict("SELECT participant_name, participant_id FROM participants;", 0, 1),
        "books": fetch_dict("SELECT book_name, books_id FROM books;", 0, 1),
        "sports": fetch_dict("SELECT sport_key, sport_id FROM sports;", 0, 1),
        "wager_types": fetch_dict("SELECT wager_type, wager_type_id FROM wager_types;", 0, 1)
    }

def read_json_file(json_file):
    """ Read the JSON file and return its contents as a dictionary. """
    if not os.path.exists(json_file):
        logging.error(f"Error: File {json_file} not found.")
        return None

    logging.info(f"Reading JSON file: {json_file}")
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Debugging: Check first few records
    logging.info(f"First few keys in JSON: {list(data.keys())[:5]}")
    return data

def process_saved_json(json_file):
    """ Read JSON, check for existing data, and insert only new records into PostgreSQL. """
    print(f"Loaded POSTGRES_PASSWORD: {os.getenv('POSTGRES_PASSWORD')}")
    conn = connect_db()
    cursor = conn.cursor()

    logging.info(f"Processing JSON file: {json_file}")
    cache = preload_data(cursor)

    data = read_json_file(json_file)
    if data is None:
        logging.error("No valid JSON data found. Exiting process.")
        return

    # Iterate over each snapshot timestamp (e.g., "2024-10-04T12:00:00Z")
    for ts, snapshot in data.items():
        logging.info(f"--- Timestamp: {ts} ---")
        # Each snapshot contains one or more sport keys (like "ncaaf", "nfl")
        for sport_key_short, sport_content in snapshot.items():
            # Log the sport key as seen at the snapshot level (may be abbreviated)
            logging.info(f"Processing sport (top-level key): {sport_key_short}")
            
            # Get the array of events from the sport content
            events = sport_content.get("data", [])
            logging.info(f"Found {len(events)} events under sport '{sport_key_short}'")
            
            # Iterate over each event in the 'data' array
            for event in events:
                # Use the event-level sport_key for lookups (this should be the full key)
                event_sport_key = event.get("sport_key", sport_key_short)
                event_name = f"{event['home_team']} vs {event['away_team']}"
                commence_time = event["commence_time"]
                home_team = event["home_team"]
                away_team = event["away_team"]

                logging.info(f"Processing event: {event_name} (ID: {event.get('id', 'N/A')}, Commence: {commence_time}, Sport: {event_sport_key})")

                # 1️⃣ Fetch Sport ID & Event Type ID from the Sports Table using the event-level sport_key
                cursor.execute("SELECT sport_id, event_type_id FROM \"SAL-schema\".sports WHERE sport_key = %s;", (event_sport_key,))
                sport_row = cursor.fetchone()
                if sport_row:
                    sport_id, event_type_id = sport_row
                else:
                    logging.error(f"Skipping event {event_name} - Missing sport_id for sport_key '{event_sport_key}'")
                    continue  # Skip this event if sport is missing

                # 2️⃣ Ensure Participants Exist Before Creating the Event
                home_id = insert_participant(cursor, cache, home_team, "TEAM", event_sport_key)
                away_id = insert_participant(cursor, cache, away_team, "TEAM", event_sport_key)
                if home_id is None or away_id is None:
                    logging.error(f"Skipping event {event_name} due to participant insertion failure.")
                    continue

                # 3️⃣ Insert Event (enforcing uniqueness on participants & commence_time)
                event_id = insert_event(cursor, event_name, commence_time, sport_id, event_type_id, home_id, away_id)
                if not event_id:
                    logging.error(f"Skipping event {event_name} - Failed to insert event.")
                    continue  # Skip odds if event wasn't inserted

                # 4️⃣ Process Bookmakers for this event
                for bookmaker in event.get("bookmakers", []):
                    book_name = bookmaker.get("title", "Unknown Book")
                    logging.info(f"Processing bookmaker: {book_name}")
                    books_id = insert_book(cursor, cache, book_name)
                    if books_id is None:
                        logging.error(f"Skipping bookmaker {book_name} for event {event_name}.")
                        continue

                    # 5️⃣ Process Markets (Wager Types) for this bookmaker
                    for market in bookmaker.get("markets", []):
                        market_key = market.get("key", "Unknown Market")
                        last_update = market.get("last_update", "No Update Time")
                        logging.info(f"Processing market: {market_key}")
                        wager_type_id = insert_wager_type(cursor, cache, market_key)
                        if wager_type_id is None:
                            logging.error(f"Skipping market {market_key} for event {event_name}.")
                            continue

                        # 6️⃣ Process Outcomes (Odds) for this market
                        for outcome in market.get("outcomes", []):
                            outcome_name = outcome.get("name", "Unknown Outcome")
                            price = outcome.get("price", None)
                            point = outcome.get("point", None)
                            logging.info(f"Processing outcome: {outcome_name} (Price: {price}, Point: {point})")
                            insert_odds(cursor, event_id, books_id, wager_type_id, last_update, outcome_name, price, point)

    logging.info("Committing transaction to database...")
    conn.commit()
    logging.info("Transaction committed successfully.")
    cursor.close()
    conn.close()
    logging.info(f"Finished processing {json_file} into PostgreSQL.")

def process_saved_json_debug(json_file):
    """ Debug version: Reads the JSON file and logs the structure without inserting into PostgreSQL. """
    print(f"Loaded POSTGRES_PASSWORD: {os.getenv('POSTGRES_PASSWORD')}")
    conn = connect_db()
    cursor = conn.cursor()

    logging.info(f"Processing JSON file (DEBUG MODE): {json_file}")
    data = read_json_file(json_file)  # ✅ This loads the JSON file correctly

    if data is None:
        logging.error("No valid JSON data found. Exiting process.")
        return

    # Iterate over top-level timestamps
    for timestamp, sport_data in data.items():
        logging.info(f"--- Timestamp: {timestamp} ---")

        # Iterate over each sport section
        for sport_key, sport_content in sport_data.items():
            logging.info(f"  Sport Key: {sport_key}")

            # Verify if "data" exists and contains events
            events = sport_content.get("data", [])
            logging.info(f"  Found {len(events)} events under sport '{sport_key}'")

            # Iterate over events
            for event in events:
                event_id = event.get("id", "MISSING_ID")
                commence_time = event.get("commence_time", "MISSING_TIME")
                home_team = event.get("home_team", "MISSING_HOME_TEAM")
                away_team = event.get("away_team", "MISSING_AWAY_TEAM")
                extracted_sport_key = event.get("sport_key", "MISSING_SPORT_KEY")

                logging.info(f"    Event: {home_team} vs {away_team} (ID: {event_id}, Commence: {commence_time}, Sport: {extracted_sport_key})")

                # Iterate over bookmakers
                for bookmaker in event.get("bookmakers", []):
                    book_name = bookmaker.get("title", "MISSING_BOOK_TITLE")
                    last_update = bookmaker.get("last_update", "MISSING_UPDATE_TIME")
                    logging.info(f"      Bookmaker: {book_name} (Last Update: {last_update})")

                    # Iterate over markets
                    for market in bookmaker.get("markets", []):
                        market_key = market.get("key", "MISSING_MARKET_KEY")
                        logging.info(f"        Market: {market_key}")

                        # Iterate over outcomes
                        for outcome in market.get("outcomes", []):
                            outcome_name = outcome.get("name", "MISSING_OUTCOME")
                            price = outcome.get("price", "MISSING_PRICE")
                            point = outcome.get("point", "NO_POINT")  # Point exists for spreads/totals, not H2H

                            logging.info(f"          Outcome: {outcome_name} (Price: {price}, Point: {point})")

    cursor.close()
    conn.close()
    logging.info(f"Finished processing JSON file in DEBUG MODE: {json_file}")

def insert_participant(cursor, cache, name, participant_type, sport_key):
    """ Insert participant if not in cache and return its ID, ensuring sport exists. """
    logging.info(f"Checking participant: {name} (Sport: {sport_key})")

    if sport_key not in cache["sports"]:
        cursor.execute("SELECT sport_id FROM sports WHERE sport_key = %s;", (sport_key,))
        sport = cursor.fetchone()
        
        if sport:
            sport_id = sport[0]
            cache["sports"][sport_key] = sport_id
        else:
            logging.error(f"Sport key '{sport_key}' not found in 'sports' table. Skipping participant {name}.")
            return None

    sport_id = cache["sports"][sport_key]

    if name not in cache["participants"]:
        try:
            logging.info(f"Attempting to insert participant: {name}")
            cursor.execute(
                "INSERT INTO participants (participant_name, participant_type, sport_id) "
                "VALUES (%s, %s, %s) RETURNING participant_id;",
                (name, participant_type, sport_id)
            )
            participant_id = cursor.fetchone()[0]
            cache["participants"][name] = participant_id
            logging.info(f"Inserted participant: {name} (ID: {participant_id})")
        except psycopg2.Error as e:
            logging.error(f"Failed to insert participant {name}: {e}")
            return None

    return cache["participants"].get(name)

def insert_book(cursor, cache, book_name):
    logging.info(f"Checking bookmaker: {book_name}")

    if book_name not in cache["books"]:
        try:
            logging.info(f"Attempting to insert bookmaker: {book_name}")
            cursor.execute("INSERT INTO books (book_name) VALUES (%s) RETURNING books_id;", (book_name,))
            books_id = cursor.fetchone()[0]
            cache["books"][book_name] = books_id
            logging.info(f"Inserted bookmaker: {book_name} (ID: {books_id})")
        except psycopg2.Error as e:
            logging.error(f"Failed to insert bookmaker {book_name}: {e}")
            return None
    return cache["books"].get(book_name)

def insert_sport(cursor, cache, sport_key, sport_title):
    logging.info(f"Checking sport: {sport_key}")

    if sport_key not in cache["sports"]:
        try:
            logging.info(f"Attempting to insert sport: {sport_key}")
            cursor.execute("INSERT INTO sports (sport_key, sport_title) VALUES (%s, %s) RETURNING sport_id;",
                           (sport_key, sport_title))
            sport_id = cursor.fetchone()[0]
            cache["sports"][sport_key] = sport_id
            logging.info(f"Inserted sport: {sport_key} (ID: {sport_id})")
        except psycopg2.Error as e:
            logging.error(f"Failed to insert sport {sport_key}: {e}")
            return None
    return cache["sports"].get(sport_key)

def insert_wager_type(cursor, cache, market_key):
    logging.info(f"Checking wager type: {market_key}")

    if market_key not in cache["wager_types"]:
        try:
            logging.info(f"Attempting to insert wager type: {market_key}")
            cursor.execute("INSERT INTO wager_types (wager_type) VALUES (%s) RETURNING wager_type_id;", (market_key,))
            wager_type_id = cursor.fetchone()[0]
            cache["wager_types"][market_key] = wager_type_id
            logging.info(f"Inserted wager type: {market_key} (ID: {wager_type_id})")
        except psycopg2.Error as e:
            logging.error(f"Failed to insert wager type {market_key}: {e}")
            return None
    return cache["wager_types"].get(market_key)

def insert_odds(cursor, event_id, books_id, wager_type_id, last_update, outcome_name, price, point):
    logging.info(f"Checking odds for event ID: {event_id} - Outcome: {outcome_name}")

    try:
        logging.info(f"Attempting to insert odds: {outcome_name}")
        cursor.execute(
            "INSERT INTO odds (event_id, books_id, wager_type_id, last_update, outcome_name, price, point) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s);",
            (event_id, books_id, wager_type_id, last_update, outcome_name, price, point)
        )
        logging.info(f"Inserted odds: {outcome_name} (Event ID: {event_id})")
    except psycopg2.Error as e:
        logging.error(f"Failed to insert odds for {outcome_name}: {e}")

def insert_event(cursor, event_name, commence_time, sport_id, event_type_id, home_id, away_id):
    """ Insert an event if it does not already exist, enforcing uniqueness on participants & commence_time. """
    logging.info(f"Checking event: {event_name} at {commence_time}")

    cursor.execute(
        "SELECT event_id FROM events WHERE participant_1_id = %s AND participant_2_id = %s AND commence_time = %s;",
        (home_id, away_id, commence_time)
    )
    existing_event = cursor.fetchone()

    if existing_event:
        event_id = existing_event[0]
        logging.info(f"Event already exists: {event_name} (ID: {event_id})")
        return event_id
    else:
        try:
            logging.info(f"Attempting to insert event: {event_name}")
            cursor.execute(
                """
                INSERT INTO events (event_name, commence_time, sport_id, event_type_id, participant_1_id, participant_2_id) 
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING event_id;
                """,
                (event_name, commence_time, sport_id, event_type_id, home_id, away_id)
            )
            event_id = cursor.fetchone()[0]
            logging.info(f"Inserted event: {event_name} (ID: {event_id})")
            return event_id
        except psycopg2.Error as e:
            logging.error(f"Failed to insert event {event_name}: {e}")
            return None