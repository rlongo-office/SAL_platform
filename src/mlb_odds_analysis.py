#!/usr/bin/env python3
import os
import csv
import psycopg2
import psycopg2.extras
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

# ─── P/L CALC ────────────────────────────────────────────────────────────────────
def profit_factor(odds_american: int) -> float:
    if odds_american > 0:
        return odds_american / 100.0
    else:
        return 100.0 / abs(odds_american)

# ─── QUERY & ANALYSIS ───────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(**DB_PARAMS, options="-c search_path=msf_mlb,public")
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # pick earliest h2h snapshot per game+book
    sql = """
    WITH first_snap AS (
      SELECT DISTINCT ON (go.mlb_game_pk, go.book_id)
        go.id           AS game_odds_id,
        go.book_id,
        go.mlb_game_pk,
        go.as_of_time
      FROM msf_mlb.game_odds go
      JOIN msf_mlb.odds o
        ON go.id = o.game_odds_id
      WHERE go.odds_type = 'h2h'
        AND go.mlb_game_pk IS NOT NULL
      ORDER BY go.mlb_game_pk, go.book_id, go.as_of_time
    )
    SELECT
      b.name                     AS book_name,
      fs.game_odds_id,
      fs.mlb_game_pk             AS game_pk,
      fs.as_of_time,
      outc.away_team_id,
      outc.home_team_id,
      ta.locationname            AS away_location,
      ta.teamname                AS away_team_name,
      th.locationname            AS home_location,
      th.teamname                AS home_team_name,
      outc.away_score,
      outc.home_score,
      outc.winner                AS winner,
      away_line.odds_american    AS away_odds,
      home_line.odds_american    AS home_odds
    FROM first_snap fs
      JOIN msf_mlb.books b
        ON fs.book_id = b.id
      JOIN msf_mlb.mlb_game_outcomes outc
        ON fs.mlb_game_pk = outc.game_id
      JOIN msf_mlb.teams ta
        ON outc.away_team_id = ta.id
      JOIN msf_mlb.teams th
        ON outc.home_team_id = th.id
      LEFT JOIN msf_mlb.odds away_line
        ON fs.game_odds_id = away_line.game_odds_id
       AND away_line.outcome_type = 'away'
      LEFT JOIN msf_mlb.odds home_line
        ON fs.game_odds_id = home_line.game_odds_id
       AND home_line.outcome_type = 'home'
    ;
    """
    cur.execute(sql)
    rows = cur.fetchall()

    PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    OUT_PATH   = os.path.join(PROJECT_DIR, "output", "detailed_pl.csv")

    # prepare detailed‐log CSV
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as csvf:
        writer = csv.DictWriter(csvf, fieldnames=[
            "book_name", "game_pk", "as_of_time",
            "away_team_id", "away_location", "away_team_name",
            "home_team_id", "home_location", "home_team_name",
            "away_score", "home_score", "winner",
            "away_odds", "home_odds",
            "favorite_side", "favorite_odds",
            "underdog_side", "underdog_odds",
            "fav_pl", "ud_pl"
        ])
        writer.writeheader()

        summary = {}   # book_name → {fav_pl, ud_pl, n_bets}
        overall = {"fav_pl": 0.0, "ud_pl": 0.0, "n_bets": 0}

        for r in rows:
            book = r["book_name"]
            aw   = r["away_odds"]
            hm   = r["home_odds"]
            win  = r["winner"]
            gp   = r["game_pk"]
            ts   = r["as_of_time"]

            # raw outcome fields
            at_id    = r["away_team_id"]
            at_loc   = r["away_location"]
            at_name  = r["away_team_name"]
            ht_id    = r["home_team_id"]
            ht_loc   = r["home_location"]
            ht_name  = r["home_team_name"]
            at_sc    = r["away_score"]
            ht_sc    = r["home_score"]

            # skip if odds missing or identical
            if aw is None or hm is None or aw == hm:
                continue

            # favorite vs underdog
            if aw < hm:
                fav_side, fav_odds = "away", aw
                ud_side,  ud_odds  = "home", hm
            else:
                fav_side, fav_odds = "home", hm
                ud_side,  ud_odds  = "away", aw

            pf_fav = profit_factor(fav_odds)
            pf_ud  = profit_factor(ud_odds)

            fav_pl = pf_fav if win == fav_side else -1.0
            ud_pl  = pf_ud  if win == ud_side  else -1.0

            # write detail row
            writer.writerow({
                "book_name":      book,
                "game_pk":        gp,
                "as_of_time":     ts,
                "away_team_id":   at_id,
                "away_location":  at_loc,
                "away_team_name": at_name,
                "home_team_id":   ht_id,
                "home_location":  ht_loc,
                "home_team_name": ht_name,
                "away_score":     at_sc,
                "home_score":     ht_sc,
                "winner":         win,
                "away_odds":      aw,
                "home_odds":      hm,
                "favorite_side":  fav_side,
                "favorite_odds":  fav_odds,
                "underdog_side":  ud_side,
                "underdog_odds":  ud_odds,
                "fav_pl":         f"{fav_pl:.2f}",
                "ud_pl":          f"{ud_pl:.2f}",
            })

            # roll up summary
            summary.setdefault(book, {"fav_pl": 0.0, "ud_pl": 0.0, "n_bets": 0})
            summary[book]["fav_pl"] += fav_pl
            summary[book]["ud_pl"]  += ud_pl
            summary[book]["n_bets"] += 1
            overall["fav_pl"] += fav_pl
            overall["ud_pl"]  += ud_pl
            overall["n_bets"] += 1

    # print summary
    print(f"{'BOOK':<20}  {'#BETS':>5}  {'FAV P/L':>10}  {'UD P/L':>10}")
    print("-"*50)
    for book, stats in sorted(summary.items()):
        print(f"{book:<20}  {stats['n_bets']:5d}  "
              f"{stats['fav_pl']:10.2f}  {stats['ud_pl']:10.2f}")
    print("-"*50)
    print(f"{'OVERALL':<20}  {overall['n_bets']:5d}  "
          f"{overall['fav_pl']:10.2f}  {overall['ud_pl']:10.2f}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
