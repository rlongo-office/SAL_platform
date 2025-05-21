#!/usr/bin/env python3
import os
import requests
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ─────────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())

DB_PARAMS = {
    "dbname":   os.getenv("POSTGRES_DB",   "SAL-db"),
    "user":     os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host":     os.getenv("POSTGRES_HOST", "localhost"),
    "port":     os.getenv("POSTGRES_PORT", "5432"),
}

MLB_API_TEAMS = "https://statsapi.mlb.com/api/v1/teams"

# ─── DB CONNECT ─────────────────────────────────────────────────────────────────
def pg_connect():
    return psycopg2.connect(
        **DB_PARAMS,
        options="-c search_path=msf_mlb,public",
        cursor_factory=DictCursor
    )

def main():
    # 1) fetch from MLB
    resp = requests.get(MLB_API_TEAMS, params={"sportId": 1}, timeout=10)
    resp.raise_for_status()
    teams = resp.json().get("teams", [])

    # 2) upsert into msf_mlb.teams
    conn = pg_connect()
    cur = conn.cursor()
    for t in teams:
        cur.execute("""
            INSERT INTO msf_mlb.teams (id, locationName, teamName, abbreviation)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
              SET locationName = EXCLUDED.locationName,
                  teamName     = EXCLUDED.teamName,
                  abbreviation = EXCLUDED.abbreviation;
        """, (
            t["id"],
            t["locationName"],
            t["teamName"],
            t["abbreviation"],
        ))
        print(f"Upserted: {t['locationName']} {t['teamName']} (ID={t['id']})")

    conn.commit()
    cur.close()
    conn.close()
    print("Done.")

if __name__ == "__main__":
    main()
