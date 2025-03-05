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
LOG_FILE = os.path.join(LOG_DIR, f"etl_mongo_2_pg_season_{timestamp}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logging.info("Starting ETL Process...")

try:
    # MongoDB Connection
    mongo_client = pymongo.MongoClient("mongodb://localhost:27017/")
    mongo_db = mongo_client["nfl-msf"]
    seasons_collection = mongo_db["seasons"]

    # PostgreSQL Connection
    pg_conn = psycopg2.connect(
        dbname="SAL-db",
        user="postgres",
        password="password",
        host="localhost",
        port="5432"
    )
    pg_cursor = pg_conn.cursor()

    # Fetch all season documents from MongoDB
    seasons_docs = seasons_collection.find()

    for season_doc in seasons_docs:
        season_year = int(season_doc["season"])
        season_type = season_doc["season_type"]
        logging.debug(f"Processing Season: {season_year} - {season_type}")

        # Check if season already exists in PostgreSQL
        pg_cursor.execute("""
            SELECT id FROM "msf-nfl".seasons WHERE year = %s AND season_type = %s;
        """, (season_year, season_type))
        season_id = pg_cursor.fetchone()

        if season_id:
            logging.info(f"Season {season_year} ({season_type}) already exists. Skipping game insertions.")
            continue  # Skip processing this season

        # Insert Season if it doesn't exist
        pg_cursor.execute("""
            INSERT INTO "msf-nfl".seasons (year, season_type)
            VALUES (%s, %s)
            RETURNING id;
        """, (season_year, season_type))
        season_id = pg_cursor.fetchone()[0]
        logging.info(f"Inserted new Season: {season_year} ({season_type}) with ID {season_id}")

        # Track processed data to prevent duplicate inserts
        processed_teams = set()
        processed_venues = set()
        processed_officials = set()
        processed_broadcasters = {}

        # Process each game in the season
        games_list = season_doc.get("response", {}).get("games", [])
        if not games_list:
            logging.warning(f"No games found for season {season_year} ({season_type})!")
            continue

        logging.info(f"Processing {len(games_list)} games for season {season_year} ({season_type}).")

        for game in games_list:
            try:
                game_id = game["schedule"]["id"]
                logging.debug(f"Processing Game ID: {game_id}")

                week = game["schedule"]["week"]
                start_time = game["schedule"]["startTime"]
                ended_time = game["schedule"]["endedTime"]
                away_team = game["schedule"]["awayTeam"]
                home_team = game["schedule"]["homeTeam"]
                venue = game["schedule"]["venue"]
                venue_allegiance = game["schedule"]["venueAllegiance"]
                schedule_status = game["schedule"]["scheduleStatus"]
                played_status = game["schedule"]["playedStatus"]
                attendance = game["schedule"]["attendance"]

                # Weather Data
                weather = game["schedule"].get("weather")
                wind = weather.get("wind", {}) if weather else {}
                temperature = weather.get("temperature", {}) if weather else {}

                # Final Scores
                score = game.get("score", {})
                away_score_total = score.get("awayScoreTotal")
                home_score_total = score.get("homeScoreTotal")

                # Insert Teams
                for team in [away_team, home_team]:
                    if team["id"] not in processed_teams:
                        pg_cursor.execute("""
                            INSERT INTO "msf-nfl".teams (id, abbreviation)
                            VALUES (%s, %s)
                            ON CONFLICT (id) DO NOTHING;
                        """, (team["id"], team["abbreviation"]))
                        processed_teams.add(team["id"])
                        logging.debug(f"Inserted Team: {team['abbreviation']} (ID: {team['id']})")

                # Insert Venue
                if venue["id"] not in processed_venues:
                    pg_cursor.execute("""
                        INSERT INTO "msf-nfl".venues (id, name)
                        VALUES (%s, %s)
                        ON CONFLICT (id) DO NOTHING;
                    """, (venue["id"], venue["name"]))
                    processed_venues.add(venue["id"])
                    logging.debug(f"Inserted Venue: {venue['name']} (ID: {venue['id']})")

                # Insert Game with weather and scores
                pg_cursor.execute("""
                    INSERT INTO "msf-nfl".games 
                    (id, season_id, week, start_time, ended_time, away_team_id, home_team_id, venue_id, 
                     venue_allegiance, schedule_status, played_status, attendance,
                     weather_type, weather_description, wind_speed_mph, wind_speed_kph, 
                     wind_direction_degrees, wind_direction_label, temperature_f, temperature_c, 
                     humidity_percent, away_score_total, home_score_total)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                    SET played_status = EXCLUDED.played_status,
                        attendance = EXCLUDED.attendance,
                        weather_type = EXCLUDED.weather_type,
                        weather_description = EXCLUDED.weather_description,
                        wind_speed_mph = EXCLUDED.wind_speed_mph,
                        wind_speed_kph = EXCLUDED.wind_speed_kph,
                        wind_direction_degrees = EXCLUDED.wind_direction_degrees,
                        wind_direction_label = EXCLUDED.wind_direction_label,
                        temperature_f = EXCLUDED.temperature_f,
                        temperature_c = EXCLUDED.temperature_c,
                        humidity_percent = EXCLUDED.humidity_percent,
                        away_score_total = EXCLUDED.away_score_total,
                        home_score_total = EXCLUDED.home_score_total;
                """, (game_id, season_id, week, start_time, ended_time, away_team["id"], home_team["id"], venue["id"],
                      venue_allegiance, schedule_status, played_status, attendance,
                      weather.get("type") if weather else None,
                      weather.get("description") if weather else None,
                      wind.get("speed", {}).get("milesPerHour") if weather else None,
                      wind.get("speed", {}).get("kilometersPerHour") if weather else None,
                      wind.get("direction", {}).get("degrees") if weather else None,
                      wind.get("direction", {}).get("label") if weather else None,
                      temperature.get("fahrenheit") if weather else None,
                      temperature.get("celsius") if weather else None,
                      weather.get("humidityPercent") if weather else None,
                      away_score_total, home_score_total))

                logging.info(f"Inserted/Updated Game ID: {game_id}")

            except Exception as e:
                logging.error(f"Error processing game ID {game_id}: {e}", exc_info=True)

    pg_conn.commit()
    logging.info("ETL Process Complete!")

except Exception as e:
    logging.critical("Critical error in ETL process", exc_info=True)

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
