from pymongo import MongoClient
import json

client = MongoClient("mongodb://localhost:27017/")
db = client["nfl-msf"]
odds_collection = db["odds"]

pipeline = [
    { "$unwind": "$response.gameLines" },
    { "$unwind": "$response.gameLines.lines" },
    { "$unwind": "$response.gameLines.lines.moneyLines" },
    { "$sort": { "response.gameLines.lines.moneyLines.asOfTime": 1 } },
    { "$group": {
        "_id": { "game_id": "$response.gameLines.game.id", "sportsbook": "$response.gameLines.lines.source.name" },
        "firstMoneyLine": { "$first": "$response.gameLines.lines.moneyLines.moneyLine" },
        "gameTime": { "$first": "$response.gameLines.game.startTime" },
        "awayTeam": { "$first": "$response.gameLines.game.awayTeamAbbreviation" },
        "homeTeam": { "$first": "$response.gameLines.game.homeTeamAbbreviation" }
    }},
    { "$match": {
        "$or": [
            { "firstMoneyLine.homeLine.american": -200 },
            { "firstMoneyLine.awayLine.american": -200 }
        ]
    }},
    { "$project": {
        "game_id": "$_id.game_id",
        "sportsbook": "$_id.sportsbook",
        "moneyline": "$firstMoneyLine",
        "gameTime": 1,
        "homeTeam": 1,
        "awayTeam": 1
    }}
]

results = list(odds_collection.aggregate(pipeline))

# Save to JSON to inspect results
with open("filtered_odds_results.json", "w") as f:
    json.dump(results, f, indent=4)

print(f"Saved {len(results)} results to filtered_odds_results.json")
