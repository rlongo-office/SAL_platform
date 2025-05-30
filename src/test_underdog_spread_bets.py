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
DB_PARAMS = {
  "dbname":   "neondb",
  "user":     "neondb_owner",
  "password": "npg_aKWdUeCXV10c",
  "host":     "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech",
  "port":     "5432",
  "sslmode":  "require",
}

# Flag: when True, use team‐level allowed stats for a brand‐new starter
ESTIMATE_STATS = False
TEST_OPTION = True
DELTA_PI = 0.08   # power‐index threshold

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
    ts,                              # timestamp “now” (≈ game start)
    assume_total_outs: int = 27,     # one 9-inning game
    min_outings: int = 5,            # ignore fringe arms
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
         WHERE p.team_id = %s
           AND sn.snapshot_date < %s
           AND sn.player_id <> %s
           AND sn.cum_outings >= %s
         ORDER BY sn.snapshot_date DESC
        """,
        (team_id, ts, starter_pid, min_outings),
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

def get_pitcher_staff_allowed_older(cur, game_pk: int, side: str, ts) -> tuple[float,float]:
    """
    Build a weighted-average staff OBP/SLG for the favorite side in game `game_pk`.
    Weights are based on at-bats faced by each pitcher in that game.
    Fallback for missing pitcher stats uses team‐allowed, controlled by ESTIMATE_STATS.
    """
    #logger.debug("[get_staff_allowed] game_pk=%s  side=%s  as_of=%s",game_pk, side, ts)

    # fetch the team_id for this side
    cur.execute("""
      SELECT away_team_id, home_team_id
        FROM msf_mlb.mlb_game_outcomes
       WHERE game_id = %s
    """, (game_pk,))
    game = cur.fetchone()
    team_id = game["away_team_id"] if side == "away" else game["home_team_id"]

    # 1) fetch who pitched, in sequence order
    cur.execute("""
      SELECT player_id, at_bats, sequence
        FROM msf_mlb.pitcher_boxscores
       WHERE game_id = %s
         AND side    = %s
       ORDER BY sequence
    """, (game_pk, side))
    pitchers = cur.fetchall()
    #logger.debug("  → pitchers fetched: %d rows", len(pitchers))

    total_ab = sum(row["at_bats"] for row in pitchers)
    #logger.debug("  → total_ab across staff = %d", total_ab)
    if total_ab <= 0:
        logger.debug("  → no at_bats recorded, cannot compute staff stats")
        return (None, None)

    # 2) for each, grab their snapshot stats *before* this game, weight by at_bats
    staff_obp = 0.0
    staff_slg = 0.0

    for row in pitchers:
        pid, ab, seq = row["player_id"], row["at_bats"], row["sequence"]
        #logger.debug("    → pitcher %s faced %d AB (seq=%d)", pid, ab, seq)

        obp, slg = get_latest_pitcher_stats(cur, pid, ts)
        if obp is None or slg is None:
            # Fallback logic
            if seq == 1:
                # starter
                if not ESTIMATE_STATS:
                    #logger.debug("      → missing starter %s stats and ESTIMATE_STATS=False, skip game",pid)
                    return (None, None)
                else:
                    obp, slg = get_latest_team_allowed(cur, team_id, ts)
                    #logger.debug("      → using team‐allowed for new starter: obp=%.3f, slg=%.3f",obp, slg)
                    if obp is None or slg is None:
                        return (None, None)
            else:
                # reliever
                obp, slg = get_latest_team_allowed(cur, team_id, ts)
                #logger.debug("      → using team‐allowed for reliever %s: obp=%.3f, slg=%.3f",pid, obp, slg)
                if obp is None or slg is None:
                    # if team‐allowed also missing, just skip this reliever
                    continue

        weight = ab / total_ab
        staff_obp += obp * weight
        staff_slg += slg * weight
        #logger.debug("      → weighted contrib: obp=%.3f, slg=%.3f (weight=%.3f)", obp, slg, weight)

    #logger.debug("  → aggregated staff stats: obp=%.3f, slg=%.3f",staff_obp, staff_slg)
    return (staff_obp, staff_slg)


# ─── MAIN ────────────────────────────────────────────────────────────────────────
import math
from scipy.stats import t
from psycopg2.extras import DictCursor

#DELTA_PI = 0.08  # keep whatever threshold you set elsewhere


# --------------------------------------------------------------------
# configurable switches
TEST_TYPE   = "h2h"     #  "spreads"  or  "h2h"
TEST_OPTION = True          #  True → pick under-dog by worse odds
# --------------------------------------------------------------------

def main() -> None:
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=DictCursor)

    logger.info("Fetching opening %s snapshots…", TEST_TYPE)
    cur.execute(
        """
        WITH first_snap AS (
            SELECT DISTINCT ON (go.mlb_game_pk, go.book_id)
                   go.id           AS go_id,
                   go.book_id,
                   go.mlb_game_pk,
                   go.as_of_time,
                   go.game_time,
                   outc.winner,
                   outc.away_team_id,
                   outc.home_team_id,
                   go.away_team,
                   go.home_team
            FROM   msf_mlb.game_odds         AS go
            JOIN   msf_mlb.mlb_game_outcomes AS outc
                   ON outc.game_id = go.mlb_game_pk
            WHERE  go.odds_type = %s
            ORDER  BY go.mlb_game_pk, go.book_id, go.as_of_time
        )
        SELECT fs.*,
               aw.odds_american AS away_odds,
               hm.odds_american AS home_odds,
               aw.spread        AS away_spread,
               hm.spread        AS home_spread
        FROM   first_snap        AS fs
        JOIN   msf_mlb.odds AS aw ON aw.game_odds_id = fs.go_id
                                 AND aw.outcome_type = 'away'
        JOIN   msf_mlb.odds AS hm ON hm.game_odds_id = fs.go_id
                                 AND hm.outcome_type = 'home';
        """,
        (TEST_TYPE,),
    )
    rows = cur.fetchall()
    logger.info("→ fetched %d raw rows", len(rows))

    # ── 1) choose ONE “best” under-dog line per game ─────────────────
    best: Dict[int, Dict] = {}

    for r in rows:
        gid      = r["mlb_game_pk"]
        aw_odds, hm_odds = r["away_odds"], r["home_odds"]

        # ── spreads branch ───────────────────────────────────────────
        if TEST_TYPE == "spreads":
            aw_sp, hm_sp = r["away_spread"], r["home_spread"]
            if None in (aw_sp, hm_sp) or aw_sp == hm_sp:
                continue                         # invalid spread pair

            if TEST_OPTION:                     # “dog” = worse odds
                dog_is_away = aw_odds > hm_odds
            else:                               # “dog” = gets points
                dog_is_away = aw_sp > hm_sp

            if dog_is_away:
                ud_side, fv_side = "away", "home"
            else:
                ud_side, fv_side = "home", "away"

            ud_tid = r[f"{ud_side}_team_id"]
            fv_tid = r[f"{fv_side}_team_id"]
            ud_odds = r[f"{ud_side}_odds"]
            fv_odds = r[f"{fv_side}_odds"]
            ud_spread = r[f"{ud_side}_spread"]
            fv_spread = r[f"{fv_side}_spread"]

        # ── h2h (money-line) branch ──────────────────────────────────
        else:  # TEST_TYPE == 'h2h'
            # pick the side with the bigger positive / less-negative price
            if aw_odds > hm_odds:
                ud_side, fv_side = "away", "home"
            else:
                ud_side, fv_side = "home", "away"

            ud_tid = r[f"{ud_side}_team_id"]
            fv_tid = r[f"{fv_side}_team_id"]
            ud_odds = r[f"{ud_side}_odds"]
            fv_odds = r[f"{fv_side}_odds"]
            ud_spread = fv_spread = None        # not used for h2h

        entry = dict(r)
        entry.update(
            ud_side=ud_side, ud_tid=ud_tid, ud_odds=ud_odds, ud_spread=ud_spread,
            fv_side=fv_side, fv_tid=fv_tid, fv_odds=fv_odds, fv_spread=fv_spread,
        )
        # keep the richest under-dog price for this game
        if gid not in best or ud_odds > best[gid]["ud_odds"]:
            best[gid] = entry

    logger.info("→ reduced to %d best under-dog lines", len(best))

    # ── 2) cache look-ups & evaluate bets ───────────────────────────
    team_cache:  Dict[Tuple[int, str], Tuple[float, float]] = {}
    staff_cache: Dict[Tuple[int, str], Tuple[float, float]] = {}
    profits: List[float] = []

    for r in best.values():
        gid, ts = r["mlb_game_pk"], r["as_of_time"]
        ts_key  = ts.date().isoformat()

        # batting (underdog)
        team_key = (r["ud_tid"], ts_key)
        if team_key in team_cache:
            ud_obp, ud_slg = team_cache[team_key]
        else:
            ud_obp, ud_slg = get_latest_team_stats(cur, r["ud_tid"], ts)
            team_cache[team_key] = (ud_obp, ud_slg)

        # pitching (favourite)
        staff_key = (gid, r["fv_side"])
        if staff_key in staff_cache:
            fv_obp, fv_slg = staff_cache[staff_key]
        else:
            fv_obp, fv_slg = get_staff_allowed(cur, gid, r["fv_side"], ts)
            staff_cache[staff_key] = (fv_obp, fv_slg)

        if None in (ud_obp, ud_slg, fv_obp, fv_slg):
            continue

        if (ud_obp + ud_slg) - (fv_obp + fv_slg) < DELTA_PI:
            continue

        won = (r["winner"] == r["ud_side"])
        profits.append(profit_factor(r["ud_odds"]) if won else -1.0)

    # ── 3) summary statistics ───────────────────────────────────────
    n = len(profits)
    mean = sum(profits) / n if n else 0.0
    sd   = math.sqrt(sum((x - mean) ** 2 for x in profits) / (n - 1)) if n > 1 else 0.0
    se   = sd / math.sqrt(n) if n else 0.0
    t_stat = mean / se if se else 0.0
    p_val  = 1 - t.cdf(t_stat, df=n - 1) if n > 1 else 1.0
    t_crit = t.ppf(0.975, df=n - 1) if n > 1 else 0.0
    ci_low, ci_hi = mean - t_crit * se, mean + t_crit * se

    logger.info("TEST_TYPE=%s  TEST_OPTION=%s", TEST_TYPE, TEST_OPTION)
    logger.info("ΔPI≥%.2f   #BETS=%4d   MEAN P/L=%7.3f", DELTA_PI, n, mean)
    logger.info("STD DEV=%7.3f   SE=%7.4f", sd, se)
    logger.info("t-stat=%7.3f   p(one-sided)=%7.4f", t_stat, p_val)
    logger.info("95%% CI=[%7.3f, %7.3f]", ci_low, ci_hi)

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
