#!/usr/bin/env python3
import os
import math
import logging
import psycopg2
import psycopg2.extras
from collections import defaultdict
from typing import Iterable, List, Dict, Any, Callable, Tuple
from decimal import Decimal 
import math
from scipy.stats import t
from psycopg2.extras import DictCursor
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
from scipy.stats import t

# ─── CONFIG & LOGGING ─────────────────────────────────────────────────────────────
load_dotenv(find_dotenv())

DB = dict(
    dbname   = os.getenv("DB_NAME",   "neondb"),
    user     = os.getenv("DB_USER",   "neondb_owner"),
    password = os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT",   "5432"),
    sslmode  = "require",
)


# Flag: when True, use team‐level allowed stats for a brand‐new starter
ESTIMATE_STATS = False
TEST_OPTION = True
DELTA_PI = 0.06   # power‐index threshold

# build and normalize the path to the project’s logs folder
log_dir  = os.path.normpath(os.path.join(os.path.dirname(__file__),"..","logs"))
log_file = os.path.join(log_dir, f"underdog_analysis_{datetime.now():%Y%m%d_%H%M%S}.log")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,              
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger("underdog_pi")

# ─── HELPERS ─────────────────────────────────────────────────────────────────────

# ── HELPER: throttle number of books per game ────────────────────────

def filter_books(
    rows: Iterable[Dict[str, Any]],
    n_books: int = 1,
    *,
    chooser: str | Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]] = "first",
) -> List[Dict[str, Any]]:
    """
    Reduce `rows` so each mlb_game_pk appears at most `n_books` times.

    Parameters
    ----------
    rows     : iterable of DictCursor rows
    n_books  : max books per game to keep
    chooser  : one of
        "first"      → keep the first n rows encountered (default)
        "best_ud"    → keep rows with the most *attractive underdog* odds
        "best_fav"   → keep rows with the most *attractive favorite* odds
        callable     → a custom function that takes the list of rows for a
                       single game and returns a *subset* of ≤ n_books rows

    Returns
    -------
    list of dict (filtered, original order preserved)
    """

    # group rows by game
    by_game: defaultdict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_game[r["mlb_game_pk"]].append(r)

    def pick(game_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Internal wrapper that applies the selected strategy."""
        if callable(chooser):
            chosen = chooser(game_rows)[:n_books]
        elif chooser == "first":
            chosen = game_rows[:n_books]
        elif chooser == "best_ud":
            # higher (more positive) under-dog price wins
            chosen = sorted(
                game_rows,
                key=lambda r: r["ud_odds"],    # ud_odds already in your dict
                reverse=True,
            )[:n_books]
        elif chooser == "best_fav":
            # lower (more negative) fav price wins
            chosen = sorted(
                game_rows,
                key=lambda r: r["fv_odds"],    # fv_odds already in your dict
            )[:n_books]
        else:
            raise ValueError(f"unknown chooser strategy: {chooser}")
        return chosen

    # flatten back to original order
    result: List[Dict[str, Any]] = []
    for r in rows:
        gid = r["mlb_game_pk"]
        if r in by_game[gid]:              # row might still be unprocessed
            selected = pick(by_game[gid])
            result.extend(selected)
            by_game.pop(gid, None)         # avoid re-processing
    return result

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

def compute_rate(numer, denom):
    return round(numer/denom, 3) if denom and denom>0 else 0.0

def pg_connect():
    conn = psycopg2.connect(**DB_PARAMS)
    with conn.cursor() as c:
        c.execute("SET search_path TO msf_mlb,public;")
    return conn

def get_latest_team_stats(cur, team_id: int, ts) -> tuple[float,float]:
    """
    Grab the most recent OBP & SLG from team_stats_snapshots
    for `team_id` *before* date `ts`.
    """
    #logger.debug("[get_latest_team_stats] team_id=%s  as_of=%s", team_id, ts)
    cur.execute("""
      SELECT obp, slg
        FROM msf_mlb.team_stats_snapshots
       WHERE team_id = %s
         AND snapshot_date < %s::date
       ORDER BY snapshot_date DESC
       LIMIT 1
    """, (team_id, ts))
    row = cur.fetchone()
    if not row:
        #logger.debug("  → no team snapshot for %s before %s", team_id, ts)
        return (None, None)

    obp = float(row["obp"])
    slg = float(row["slg"])
    #logger.debug("  → latest team stats for %s: obp=%.3f, slg=%.3f", team_id, obp, slg)
    return (obp, slg)

def get_latest_team_allowed(cur, team_id: int, ts) -> tuple[float,float]:
    """
    Grab the most recent allowed_OBP & allowed_SLG from team_stats_snapshots
    for `team_id` *before* date `ts`.
    """
    #logger.debug("[get_latest_team_allowed] team_id=%s  as_of=%s", team_id, ts)
    cur.execute("""
      SELECT allowed_obp, allowed_slg
        FROM msf_mlb.team_stats_snapshots
       WHERE team_id = %s
         AND snapshot_date < %s::date
       ORDER BY snapshot_date DESC
       LIMIT 1
    """, (team_id, ts))
    row = cur.fetchone()
    if not row:
        logger.debug("  → no team‐allowed snapshot for %s before %s",
                     team_id, ts)
        return (None, None)

    obp = float(row["allowed_obp"])
    slg = float(row["allowed_slg"])
    logger.debug("  → latest team‐allowed for %s: obp=%.3f, slg=%.3f",
                 team_id, obp, slg)
    return (obp, slg)

def get_latest_pitcher_stats(cur, player_id: int, ts) -> tuple[float,float]:
    """
    Grab the most recent allowed-OBP & allowed-SLG from pitcher_stats_snapshots
    for `player_id` *before* date `ts`.
    """
    #logger.debug("[get_latest_pitcher_stats] player_id=%s  as_of=%s",player_id, ts)
    cur.execute("""
      SELECT obp_allowed, slg_allowed
        FROM msf_mlb.pitcher_stats_snapshots
       WHERE player_id   = %s
         AND snapshot_date < %s::date
       ORDER BY snapshot_date DESC
       LIMIT 1
    """, (player_id, ts))
    row = cur.fetchone()
    if not row:
        #logger.debug("  → no snapshot found for pitcher %s before %s",player_id, ts)
        return (None, None)

    obp = float(row["obp_allowed"])
    slg = float(row["slg_allowed"])
    #logger.debug("  → latest snapshot for %s: obp_allowed=%.3f, slg_allowed=%.3f",player_id, obp, slg)
    return (obp, slg)

from decimal import Decimal          # keep near your other imports
from typing import Dict, List, Tuple

def get_staff_allowed(
    cur,
    game_pk: int,
    side: str,
    ts,                 # timestamp “now”  (≈ game start)
    beg_date,           # NEW → lower-bound for reliever snapshots
    assume_total_outs: int = 27,
    min_outings: int   = 5,
) -> Tuple[float, float]:
    """
    Estimate OBP/SLG *allowed* for the pitching staff we expect to use
    before game `game_pk` starts.

    Starter  = first pitcher listed (sequence = 1).
    Relievers = other pitchers on the roster with ≥ `min_outings`.

    Weights:
        starter_w = starter_avg_outs / assume_total_outs
        reliever w_i ∝ avg_outs_i ; sum(weights) = 1.0
    """

    # ── 1. identify team & starter ──────────────────────────────────
    cur.execute(
        "SELECT away_team_id, home_team_id "
        "FROM msf_mlb.mlb_game_outcomes WHERE game_id = %s",
        (game_pk,),
    )
    g = cur.fetchone()
    team_id = g["away_team_id"] if side == "away" else g["home_team_id"]

    cur.execute(
        "SELECT player_id "
        "FROM msf_mlb.pitcher_boxscores "
        "WHERE game_id = %s AND side = %s AND sequence = 1",
        (game_pk, side),
    )
    starter_row = cur.fetchone()
    if not starter_row:
        return get_latest_team_allowed(cur, team_id, ts)

    starter_pid = starter_row["player_id"]

    # ── 2. starter snapshot & workload ──────────────────────────────
    s_obp, s_slg = get_latest_pitcher_stats(cur, starter_pid, ts)
    if s_obp is None:
        s_obp, s_slg = get_latest_team_allowed(cur, team_id, ts)

    cur.execute(
        "SELECT cum_outs, cum_outings "
        "FROM msf_mlb.pitcher_stats_snapshots "
        "WHERE player_id = %s AND snapshot_date < %s "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (starter_pid, ts),
    )
    row = cur.fetchone()
    s_avg_outs = (
        row["cum_outs"] / row["cum_outings"]
        if row and row["cum_outings"]
        else 18
    )

    # ── 3. build reliever pool ─────────────────────────────────────
    cur.execute(
        """
        SELECT sn.player_id, sn.cum_outs, sn.cum_outings,
            sn.obp_allowed, sn.slg_allowed
        FROM msf_mlb.pitcher_stats_snapshots AS sn
        JOIN msf_mlb.players                  AS p
            ON p.id = sn.player_id
        WHERE p.team_id        = %s
        AND sn.snapshot_date < %s           -- upper bound (unchanged)
        AND sn.snapshot_date >= %s          -- NEW lower bound
        AND sn.player_id     <> %s
        AND sn.cum_outings   >= %s
        ORDER BY sn.snapshot_date DESC
        """,
        (team_id, ts, beg_date, starter_pid, min_outings),
    )

    relievers_seen: set[int] = set()
    relievers: List[Dict] = []

    for row in cur.fetchall():
        pid = row["player_id"]
        if pid in relievers_seen:          # keep only latest snapshot per arm
            continue
        relievers_seen.add(pid)

        # DictRow ➜ writable dict & cast Decimal ➜ float
        relievers.append({
            k: (float(v) if isinstance(v, Decimal) else v)
            for k, v in row.items()
        })

    if not relievers:
        return (s_obp, s_slg)              # starter alone

    for r in relievers:
        r["avg_outs"] = r["cum_outs"] / r["cum_outings"]

    # guard: all relievers 0 outs?
    relief_outs_total = sum(r["avg_outs"] for r in relievers)
    if relief_outs_total == 0:
        return (s_obp, s_slg)

    # ── 4. combine with weights ─────────────────────────────────────
    starter_w = min(s_avg_outs / assume_total_outs, 0.9)   # cap if extreme
    relief_share = 1.0 - starter_w

    staff_obp = starter_w * s_obp
    staff_slg = starter_w * s_slg

    for r in relievers:
        w = relief_share * r["avg_outs"] / relief_outs_total
        staff_obp += w * r["obp_allowed"]
        staff_slg += w * r["slg_allowed"]

    return (staff_obp, staff_slg)

# ─── MAIN ────────────────────────────────────────────────────────────────────────
import math
from scipy.stats import t
from psycopg2.extras import DictCursor

#DELTA_PI = 0.08  # keep whatever threshold you set elsewhere

# --------------------------------------------------------------------
# configurable switches
# --------------------------------------------------------------------
# configurable switches
TEST_TYPE   = "spreads"        # "h2h"  or  "spreads"
# TEST_OPTION below is used for spreads-only: when it is True it means we define “dog as riskier, more profitable odds”. 
# But note this is not the traditional definition of the dog in MLB spreads. The dog taking points often ahs the lower payoff 
TEST_OPTION = False         
TEST_SIDE   = "fav"        # "und"  = bet under-dog
                           # "fav"  = bet favourite
# --------------------------------------------------------------------


# ───────────────────────── helper #1 ────────────────────────────────
def _choose_dog_spreads(row: Dict[str, Any]) -> tuple[str, str]:
    """
    Return ("away" | "home", "away" | "home") → (dog_side, fav_side)
    for a spreads market row using TEST_OPTION flag.
    """
    aw_sp, hm_sp = row["away_spread"], row["home_spread"]
    aw_odds, hm_odds = row["away_odds"], row["home_odds"]
    if None in (aw_sp, hm_sp) or aw_sp == hm_sp:
        return None, None                                    # invalid pair

    if TEST_OPTION:         # “dog” = worse price
        dog_is_away = aw_odds > hm_odds
    else:                   # “dog” = gets the points
        dog_is_away = aw_sp > hm_sp
    return ("away", "home") if dog_is_away else ("home", "away")


# ───────────────────────── helper #2 ────────────────────────────────
def _choose_dog_h2h(row: Dict[str, Any]) -> tuple[str, str]:
    """Return (dog_side, fav_side) for h2h: dog has worse price."""
    if row["away_odds"] > row["home_odds"]:
        return "away", "home"
    return "home", "away"


# ───────────────────────── helper #3 ────────────────────────────────
def pick_sides(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decide which side we’re **betting** on (labelled `bet_*`)
    and which is the opponent (`opp_*`) *given* TEST_TYPE and TEST_SIDE.
    Returns the input dict augmented with:
        bet_side, bet_tid, bet_odds, bet_spread
        opp_side, opp_tid, opp_odds, opp_spread
    """
    if TEST_TYPE == "spreads":
        dog_side, fav_side = _choose_dog_spreads(row)
    else:                              # "h2h"
        dog_side, fav_side = _choose_dog_h2h(row)

    if dog_side is None:               # malformed spread pair
        return None

    # choose the label that user wants
    if TEST_SIDE == "und":             # we bet the under-dog
        bet_side, opp_side = dog_side, fav_side
    else:                              # we bet the favourite
        bet_side, opp_side = fav_side, dog_side

    # fill the fields generically
    row = dict(row)        # shallow copy – we’ll mutate safely
    for lbl, side in (("bet", bet_side), ("opp", opp_side)):
        row[f"{lbl}_side"]   = side
        row[f"{lbl}_tid"]    = row[f"{side}_team_id"]
        row[f"{lbl}_odds"]   = row[f"{side}_odds"]
        row[f"{lbl}_spread"] = row.get(f"{side}_spread")  # h2h => None

    return row

OPENING_QUERY = """
WITH first_snap AS (
    SELECT DISTINCT ON (go.mlb_game_pk, go.book_id)
           go.id            AS go_id,
           go.book_id,
           go.mlb_game_pk,
           go.as_of_time,
           go.game_time,
           outc.winner,
           outc.away_team_id,
           outc.home_team_id,
           go.away_team,
           go.home_team
      FROM msf_mlb.game_odds         AS go
      JOIN msf_mlb.mlb_game_outcomes AS outc
        ON outc.game_id = go.mlb_game_pk
     WHERE go.odds_type = %s
       AND go.game_time::date BETWEEN %s AND %s          -- NEW filter
     ORDER BY go.mlb_game_pk, go.book_id, go.as_of_time
)
SELECT fs.*,
       aw.odds_american AS away_odds,
       hm.odds_american AS home_odds,
       aw.spread        AS away_spread,
       hm.spread        AS home_spread
  FROM first_snap        AS fs
  JOIN msf_mlb.odds AS aw ON aw.game_odds_id = fs.go_id
                         AND aw.outcome_type = 'away'
  JOIN msf_mlb.odds AS hm ON hm.game_odds_id = fs.go_id
                         AND hm.outcome_type = 'home';
"""

# ──────────────────────────── main ──────────────────────────────────
def main(start_date, end_date) -> None:
    """
    Analyze opening-line value for games whose scheduled date is within
    [start_date, end_date] (both inclusive).

    Parameters
    ----------
    start_date : datetime.date
    end_date   : datetime.date
    """
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=DictCursor)

    # 1) opening-line snapshots for the chosen market inside the date window
    logger.info(
        "Fetching opening %s snapshots between %s and %s…",
        TEST_TYPE, start_date, end_date
    )
    cur.execute(OPENING_QUERY, (TEST_TYPE, start_date, end_date))
    raw = cur.fetchall()
    logger.info("→ fetched %d raw rows", len(raw))

    # 2) keep the single “best” line per game for the side we’re betting on
    best: Dict[int, Dict] = {}
    for row in raw:
        sel = pick_sides(row)                 # helper adds bet_* / opp_* keys
        if not sel:
            continue

        gid   = sel["mlb_game_pk"]
        price = sel["bet_odds"]
        # choose richer price for UNDER-dog or cheaper for FAV
        if gid not in best or (
            TEST_SIDE == "und" and price > best[gid]["bet_odds"] or
            TEST_SIDE == "fav" and price < best[gid]["bet_odds"]
        ):
            best[gid] = sel
    logger.info("→ reduced to %d best lines (TEST_SIDE=%s)", len(best), TEST_SIDE)

    # 3) handicap & simulate (unchanged)
    team_cache, staff_cache, profits = {}, {}, []
    for r in best.values():
        ts      = r["as_of_time"]
        ts_key  = ts.date().isoformat()

        # batting (our bet side)
        key = (r["bet_tid"], ts_key)
        if key not in team_cache:
            team_cache[key] = get_latest_team_stats(cur, r["bet_tid"], ts)
        b_obp, b_slg = team_cache[key]

        # pitching (opponent)
        skey = (r["mlb_game_pk"], r["opp_side"])
        if skey not in staff_cache:
            staff_cache[skey] = get_staff_allowed(cur, r["mlb_game_pk"], r["opp_side"], ts,start_date)
        p_obp, p_slg = staff_cache[skey]

        if None in (b_obp, b_slg, p_obp, p_slg):
            continue
        if (b_obp + b_slg) - (p_obp + p_slg) < DELTA_PI:
            continue

        won = (r["winner"] == r["bet_side"])
        profits.append(profit_factor(r["bet_odds"]) if won else -1.0)

    # 4) summary (unchanged)
    n = len(profits)
    mean = sum(profits) / n if n else 0.0
    sd   = math.sqrt(sum((x - mean) ** 2 for x in profits) / (n-1)) if n > 1 else 0.0
    se   = sd / math.sqrt(n) if n else 0.0
    t_stat = mean / se if se else 0.0
    p_val  = 1 - t.cdf(t_stat, df=n-1) if n > 1 else 1.0
    ci = (mean - 1.96*se, mean + 1.96*se) if n > 1 else (0, 0)

    logger.info(
        "TEST_TYPE=%s TEST_SIDE=%s TEST_OPTION=%s  ΔPI≥%.2f  #BETS=%d",
        TEST_TYPE, TEST_SIDE, TEST_OPTION, DELTA_PI, n
    )
    logger.info("Mean P/L=%6.3f  SD=%6.3f  t-stat=%6.3f  p(one-sided)=%.4f", mean, sd, t_stat, p_val)
    logger.info("95%% CI = [%6.3f , %6.3f]", *ci)

    cur.close()
    conn.close()


if __name__ == "__main__":
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(
        description="Analyze MLB under-dog / favorite value for a date range",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--start", required=True,
        help="First game date to include (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",   required=True,
        help="Last game date to include (YYYY-MM-DD, inclusive)",
    )
    args = parser.parse_args()

    # safer to pass date objects into psycopg2
    start_date = datetime.fromisoformat(args.start).date()
    end_date   = datetime.fromisoformat(args.end).date()

    main(start_date, end_date)
