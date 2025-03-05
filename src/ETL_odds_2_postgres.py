import pymongo
import psycopg2
import logging
from datetime import datetime
import os

# Configure Logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Generate timestamped log file
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"etl_mongo_2_pg_odds_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logging.info("Starting Odds ETL Process...")

try:
    # MongoDB Connection
    mongo_client = pymongo.MongoClient("mongodb://localhost:27017/")
    mongo_db = mongo_client["nfl-msf"]
    odds_collection = mongo_db["odds"]

    # PostgreSQL Connection
    pg_conn = psycopg2.connect(
        dbname="SAL-db",
        user="postgres",
        password="password",
        host="localhost",
        port="5432"
    )
    pg_cursor = pg_conn.cursor()

    # Fetch all odds documents
    odds_documents = odds_collection.find()

    for odds_doc in odds_documents:
        logging.info(f"Processing season {odds_doc['season']} - {odds_doc['season_type']} (Week {odds_doc['week']})")

        for game_line in odds_doc.get("response", {}).get("gameLines", []):
            game_id = game_line["game"]["id"]
            logging.debug(f"Processing Game ID: {game_id}")

            for line in game_line.get("lines", []):
                source = line["source"]

                # Check if book exists, insert if not
                pg_cursor.execute("""SELECT id FROM "msf-nfl".books WHERE name = %s""", (source["name"],))
                book_id = pg_cursor.fetchone()

                if book_id is None:
                    pg_cursor.execute("""
                        INSERT INTO "msf-nfl".books (name, region, is_online, is_las_vegas)
                        VALUES (%s, %s, %s, %s) RETURNING id
                    """, (source["name"], source.get("region"), source["isOnlineSportsbook"], source["isLasVegas"]))
                    book_id = pg_cursor.fetchone()[0]
                    logging.debug(f"Inserted new book: {source['name']} (ID: {book_id})")
                else:
                    book_id = book_id[0]

                # Process each wager type (moneyline, point spread, over/under)
                wager_types = {
                    "moneyLines": "moneyline",
                    "pointSpreads": "point_spread",
                    "overUnders": "over_under"
                }

                for wager_key, odds_type in wager_types.items():
                    for wager in line.get(wager_key, []):
                        as_of_time = wager["asOfTime"]
                        game_segment = wager[wager_key[:-1]]["gameSegment"]  # Strip the plural "s"

                        # Insert into game_odds
                        pg_cursor.execute("""
                            INSERT INTO "msf-nfl".game_odds (game_id, book_id, as_of_time, game_segment, odds_type)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id
                        """, (game_id, book_id, as_of_time, game_segment, odds_type))
                        game_odds_id = pg_cursor.fetchone()[0]
                        logging.debug(f"Inserted game_odds (Game ID: {game_id}, Type: {odds_type}, Book ID: {book_id})")

                        # Insert into odds table
                        odds_data = wager[wager_key[:-1]]  # Get the correct odds object

                        # Moneyline
                        if odds_type == "moneyline":
                            for outcome, line_data in [("away", odds_data["awayLine"]), 
                                                       ("home", odds_data["homeLine"]), 
                                                       ("draw", odds_data["drawLine"])]:
                                if line_data["american"] is not None:
                                    pg_cursor.execute("""
                                        INSERT INTO "msf-nfl".odds (game_odds_id, outcome_type, odds_american, odds_decimal, odds_fractional)
                                        VALUES (%s, %s, %s, %s, %s)
                                    """, (game_odds_id, outcome, line_data["american"], 
                                          line_data["decimal"], line_data["fractional"]))
                                    logging.debug(f"Inserted odds: {odds_type} {outcome} {line_data['american']}")

                        # Point Spread
                        elif odds_type == "point_spread":
                            for outcome, spread_value, line_data in [
                                ("away", odds_data["awaySpread"], odds_data["awayLine"]),
                                ("home", odds_data["homeSpread"], odds_data["homeLine"])]:
                                if line_data["american"] is not None:
                                    pg_cursor.execute("""
                                        INSERT INTO "msf-nfl".odds (game_odds_id, outcome_type, odds_american, odds_decimal, odds_fractional, spread)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                    """, (game_odds_id, outcome, line_data["american"], 
                                          line_data["decimal"], line_data["fractional"], spread_value))
                                    logging.debug(f"Inserted odds: {odds_type} {outcome} {line_data['american']}")

                        # Over/Under
                        elif odds_type == "over_under":
                            for outcome, line_data in [
                                ("over", odds_data["overLine"]),
                                ("under", odds_data["underLine"])]:
                                if line_data["american"] is not None:
                                    pg_cursor.execute("""
                                        INSERT INTO "msf-nfl".odds (game_odds_id, outcome_type, odds_american, odds_decimal, odds_fractional, over_under)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                    """, (game_odds_id, outcome, line_data["american"], 
                                          line_data["decimal"], line_data["fractional"], odds_data["overUnder"]))
                                    logging.debug(f"Inserted odds: {odds_type} {outcome} {line_data['american']}")

    # Commit transactions
    pg_conn.commit()
    logging.info("ETL Process for Odds Complete!")

except Exception as e:
    logging.critical("Critical error in Odds ETL process", exc_info=True)

finally:
    try:
        if 'pg_cursor' in locals():
            pg_cursor.close()
        if 'pg_conn' in locals():
            pg_conn.close()
        if 'mongo_client' in locals():
            mongo_client.close()
        logging.info("Database connections closed.")
    except Exception as e:
        logging.warning(f"Error closing connections: {e}")
