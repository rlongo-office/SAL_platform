from pymongo import MongoClient

# Define the log file path
LOG_FILE = "mongo_validation_log.txt"

# Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["nfl-msf"]
collection = db["game_lineups"]

# Track issues
missing_teams = []
missing_lineups = []
empty_lineups = []
duplicate_teams = []
missing_players = []

# Process each game
with open(LOG_FILE, "w", encoding="utf-8") as log_file:
    log_file.write("🔍 MongoDB Validation Report\n")
    log_file.write("=" * 50 + "\n\n")

    for doc in collection.find({}):
        game_id = doc.get("game_id")
        response = doc.get("response", {})
        team_lineups = response.get("teamLineups", [])

        # 1️⃣ Ensure each game has two teams
        if len(team_lineups) != 2:
            missing_teams.append(game_id)
            log_file.write(f"⚠️ Game {game_id} has {len(team_lineups)} team(s) instead of 2!\n")

        # Check for duplicate team IDs
        team_ids = set()
        for team in team_lineups:
            team_id = team.get("team", {}).get("id")
            if team_id in team_ids:
                duplicate_teams.append((game_id, team_id))
                log_file.write(f"⚠️ Duplicate team {team_id} in game {game_id}\n")
            team_ids.add(team_id)

            # 2️⃣ Check if `expected` and `actual` lineups exist
            expected = team.get("expected")
            actual = team.get("actual")
            
            if not expected:
                missing_lineups.append((game_id, team_id, "expected"))
                log_file.write(f"⚠️ Missing expected lineup for team {team_id} in game {game_id}\n")
            
            if not actual:
                missing_lineups.append((game_id, team_id, "actual"))
                log_file.write(f"⚠️ Missing actual lineup for team {team_id} in game {game_id}\n")

            # 3️⃣ Ensure each lineup has at least one position
            for lineup_type, lineup in [("expected", expected), ("actual", actual)]:
                if lineup is None:
                    empty_lineups.append((game_id, team_id, lineup_type))
                    log_file.write(f"⚠️ {lineup_type} lineup is completely missing for team {team_id} in game {game_id}\n")
                    continue  # Skip further processing for this lineup
                
                positions = lineup.get("lineupPositions", [])
                if len(positions) == 0:
                    empty_lineups.append((game_id, team_id, lineup_type))
                    log_file.write(f"⚠️ {lineup_type} lineup for team {team_id} in game {game_id} has no positions!\n")

                # 4️⃣ Check for missing player IDs or jersey numbers
                for position in positions:
                    player = position.get("player")
                    if player:
                        if "id" not in player or player["id"] is None:
                            missing_players.append((game_id, team_id, position["position"]))
                            log_file.write(f"⚠️ Missing player ID for {position['position']} in game {game_id}, team {team_id}\n")
                        if "jerseyNumber" not in player or player["jerseyNumber"] is None:
                            missing_players.append((game_id, team_id, position["position"]))
                            log_file.write(f"⚠️ Missing jersey number for {player.get('firstName', '')} {player.get('lastName', '')} in game {game_id}, team {team_id}\n")

    # Summary Report
    log_file.write("\n" + "=" * 50 + "\n")
    log_file.write("🔍 MongoDB Validation Summary:\n")
    log_file.write(f"Games with missing teams: {len(missing_teams)}\n")
    log_file.write(f"Teams missing expected/actual lineups: {len(missing_lineups)}\n")
    log_file.write(f"Lineups without positions: {len(empty_lineups)}\n")
    log_file.write(f"Duplicate teams in games: {len(duplicate_teams)}\n")
    log_file.write(f"Missing player IDs or jersey numbers: {len(missing_players)}\n")

print(f"✅ Validation complete. Results saved to {LOG_FILE}.")
