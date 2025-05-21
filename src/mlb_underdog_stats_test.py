#!/usr/bin/env python3
import os
import psycopg2
import psycopg2.extras
from datetime import datetime
from dotenv import load_dotenv, find_dotenv

# ─── CONFIG ──────────────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())
DB_PARAMS = {
    "dbname":   os.getenv("POSTGRES_DB",   "SAL-db"),
    "user":     os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD"),
    "host":     os.getenv("POSTGRES_HOST", "localhost"),
    "port":     os.getenv("POSTGRES_PORT", "5432"),
}

# ─── POWER‐INDEX THRESHOLD ───────────────────────────────────────────────────────
DELTA_PI = 0.09   # you can tweak this

# ─── UTILS ───────────────────────────────────────────────────────────────────────
def profit_factor(odds_american: int) -> float:
    if odds_american > 0:
        return odds_american / 100.0
    else:
        return 100.0 / abs(odds_american)

def implied_prob(odds_american: int) -> float:
    if odds_american > 0:
        return 100.0 / (odds_american + 100.0)
    else:
        return abs(odds_american) / (abs(odds_american) + 100.0)

def get_latest_team_stats(cur, team_id: int, ts) -> tuple[float,float]:
    cur.execute("""
      SELECT obp, slg
        FROM msf_mlb.team_stats_snapshots
       WHERE team_id = %s
         AND snapshot_date < %s::date
       ORDER BY snapshot_date DESC
       LIMIT 1
    """, (team_id, ts))
    row = cur.fetchone()
    return (row["obp"], row["slg"]) if row else (None, None)

def should_bet_power_index(ud_obp: float, ud_slg: float,
                           fv_obp: float, fv_slg: float,
                           delta: float = DELTA_PI) -> bool:
    return (ud_obp + ud_slg) - (fv_obp + fv_slg) >= delta


# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(**DB_PARAMS, options="-c search_path=msf_mlb,public")
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # preload team names
    cur.execute("SELECT id, locationname, teamname FROM msf_mlb.teams")
    team_map = {
        r["id"]: f"{r['locationname']} {r['teamname']}"
        for r in cur.fetchall()
    }

    # fetch the earliest h2h snapshot per game+book, plus outcome & teams & book name
    cur.execute("""
    WITH first_snap AS (
      SELECT DISTINCT ON (go.mlb_game_pk, go.book_id)
        go.id         AS go_id,
        go.book_id,
        go.mlb_game_pk,
        go.as_of_time,
        outc.winner,
        outc.away_team_id,
        outc.home_team_id
      FROM msf_mlb.game_odds AS go
      JOIN msf_mlb.mlb_game_outcomes AS outc
        ON go.mlb_game_pk = outc.game_id
      WHERE go.odds_type = 'h2h'
      ORDER BY go.mlb_game_pk, go.book_id, go.as_of_time
    )
    SELECT
      fs.go_id,
      fs.book_id,
      b.name           AS book_name,
      fs.mlb_game_pk,
      fs.as_of_time,
      fs.winner,
      fs.away_team_id,
      fs.home_team_id,
      away_o.odds_american AS away_odds,
      home_o.odds_american AS home_odds
    FROM first_snap AS fs
      JOIN msf_mlb.books AS b
        ON fs.book_id = b.id
      JOIN msf_mlb.odds AS away_o
        ON fs.go_id = away_o.game_odds_id
       AND away_o.outcome_type = 'away'
      JOIN msf_mlb.odds AS home_o
        ON fs.go_id = home_o.game_odds_id
       AND home_o.outcome_type = 'home'
    ;
    """)
    rows = cur.fetchall()

    # trackers
    pi_strategy = {"pl": 0.0, "n": 0}
    vig         = {"pl": 0.0, "n": 0}
    # store each bet we actually take
    selected_bets = []

    for r in rows:
        aw = r["away_odds"]
        hm = r["home_odds"]
        if aw is None or hm is None or aw == hm:
            continue

        # compute vigorish on every line
        ip_aw = implied_prob(aw)
        ip_hm = implied_prob(hm)
        v     = (ip_aw + ip_hm) - 1.0
        vig["pl"] += -v
        vig["n"]  += 1

        # identify underdog vs favorite
        if aw > hm:
            ud_side, ud_odds,  ud_tid = "away", aw, r["away_team_id"]
            fv_side, fv_odds, fv_tid = "home", hm, r["home_team_id"]
        else:
            ud_side, ud_odds,  ud_tid = "home", hm, r["home_team_id"]
            fv_side, fv_odds, fv_tid = "away", aw, r["away_team_id"]

        # lookup stats
        ud_obp, ud_slg = get_latest_team_stats(cur, ud_tid, r["as_of_time"])
        fv_obp, fv_slg = get_latest_team_stats(cur, fv_tid, r["as_of_time"])
        if None in (ud_obp, ud_slg, fv_obp, fv_slg):
            continue

        # decide whether to bet
        if not should_bet_power_index(ud_obp, ud_slg, fv_obp, fv_slg):
            continue

        # compute profit/loss on this $1 bet
        won = (r["winner"] == ud_side)
        pl  = profit_factor(ud_odds) if won else -1.0

        pi_strategy["pl"] += pl
        pi_strategy["n"]  += 1

        # capture all details for spot-checking
        selected_bets.append({
            "game_pk":     r["mlb_game_pk"],
            "when":        r["as_of_time"].strftime("%Y-%m-%d %H:%M"),
            "book":        r["book_name"],
            "ud_side":     ud_side,
            "ud_team":     team_map.get(ud_tid, str(ud_tid)),
            "ud_odds":     ud_odds,
            "ud_obp":      ud_obp,
            "ud_slg":      ud_slg,
            "fv_side":     fv_side,
            "fv_team":     team_map.get(fv_tid, str(fv_tid)),
            "fv_odds":     fv_odds,
            "fv_obp":      fv_obp,
            "fv_slg":      fv_slg,
            "pl":          pl,
            "won":         won,
        })

    # ─── SUMMARY REPORT ────────────────────────────────────────────────────────────
    print(f"{'STRAT':<10}  {'#BETS':>6}   {'P/L':>8}")
    print("-"*28)
    label = f"PI Δ≥{DELTA_PI:.2f}"
    print(f"{label:<10}  {pi_strategy['n']:6d}   {pi_strategy['pl']:8.2f}")
    print(f"{'VIG':<10}  {vig['n']:6d}   {vig['pl']:8.2f}")
    edge = pi_strategy["pl"] + vig["pl"]
    print(f"{'EDGE vs VIG':<10}  {pi_strategy['n']:6d}   {edge:8.2f}")
    print("-"*28)

    # ─── DETAILED SPOT-CHECK ────────────────────────────────────────────────────────
    print("\nDETAILS OF EACH TRIGGERED BET:")
    hdr = [
      "GAME", "WHEN", "BOOK", "UD SIDE", "UD TEAM", "UD ODDS",
      "FV SIDE", "FV TEAM", "FV ODDS", "UD_OBP","UD_SLG","FV_OBP","FV_SLG",
      "P/L", "WON"
    ]
    print("  ".join(f"{h:<8}" for h in hdr))
    print("-" * (len(hdr)*10))
    for b in selected_bets:
        print("  ".join([
          f"{b['game_pk']:<8}",
          f"{b['when']:<16}",
          f"{b['book']:<12}",
          f"{b['ud_side']:<8}",
          f"{b['ud_team']:<15}",
          f"{b['ud_odds']:<7}",
          f"{b['fv_side']:<8}",
          f"{b['fv_team']:<15}",
          f"{b['fv_odds']:<7}",
          f"{b['ud_obp']:<6.3f}",
          f"{b['ud_slg']:<6.3f}",
          f"{b['fv_obp']:<6.3f}",
          f"{b['fv_slg']:<6.3f}",
          f"{b['pl']:<6.2f}",
          f"{'Y' if b['won'] else 'N':<3}"
        ]))

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
