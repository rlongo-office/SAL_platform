#!/usr/bin/env python3
import os
import math
import logging
import psycopg2
import psycopg2.extras
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

DELTA_PI = 0.08   # power‐index threshold

# build and normalize the path to the project’s logs folder
log_dir  = os.path.normpath(os.path.join(os.path.dirname(__file__),"..","logs"))
log_file = os.path.join(log_dir, "underdog_analysis.log")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,              
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger("underdog_pi")

# ─── HELPERS ─────────────────────────────────────────────────────────────────────
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
    logger.debug("[get_latest_team_stats] team_id=%s  as_of=%s", team_id, ts)
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
        logger.debug("  → no team snapshot for %s before %s", team_id, ts)
        return (None, None)

    obp = float(row["obp"])
    slg = float(row["slg"])
    logger.debug("  → latest team stats for %s: obp=%.3f, slg=%.3f",
                 team_id, obp, slg)
    return (obp, slg)

def get_latest_team_allowed(cur, team_id: int, ts) -> tuple[float,float]:
    """
    Grab the most recent allowed_OBP & allowed_SLG from team_stats_snapshots
    for `team_id` *before* date `ts`.
    """
    logger.debug("[get_latest_team_allowed] team_id=%s  as_of=%s", team_id, ts)
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
    logger.debug("[get_latest_pitcher_stats] player_id=%s  as_of=%s",
                 player_id, ts)
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
        logger.debug("  → no snapshot found for pitcher %s before %s",
                     player_id, ts)
        return (None, None)

    obp = float(row["obp_allowed"])
    slg = float(row["slg_allowed"])
    logger.debug("  → latest snapshot for %s: obp_allowed=%.3f, slg_allowed=%.3f",
                 player_id, obp, slg)
    return (obp, slg)

def get_staff_allowed(cur, game_pk: int, side: str, ts) -> tuple[float,float]:
    """
    Build a weighted-average staff OBP/SLG for the favorite side in game `game_pk`.
    Weights are based on at-bats faced by each pitcher in that game.
    Fallback for missing pitcher stats uses team‐allowed, controlled by ESTIMATE_STATS.
    """
    logger.debug("[get_staff_allowed] game_pk=%s  side=%s  as_of=%s",
                 game_pk, side, ts)

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
    logger.debug("  → pitchers fetched: %d rows", len(pitchers))

    total_ab = sum(row["at_bats"] for row in pitchers)
    logger.debug("  → total_ab across staff = %d", total_ab)
    if total_ab <= 0:
        logger.debug("  → no at_bats recorded, cannot compute staff stats")
        return (None, None)

    # 2) for each, grab their snapshot stats *before* this game, weight by at_bats
    staff_obp = 0.0
    staff_slg = 0.0

    for row in pitchers:
        pid, ab, seq = row["player_id"], row["at_bats"], row["sequence"]
        logger.debug("    → pitcher %s faced %d AB (seq=%d)", pid, ab, seq)

        obp, slg = get_latest_pitcher_stats(cur, pid, ts)
        if obp is None or slg is None:
            # Fallback logic
            if seq == 1:
                # starter
                if not ESTIMATE_STATS:
                    logger.debug(
                      "      → missing starter %s stats and ESTIMATE_STATS=False, skip game",
                      pid
                    )
                    return (None, None)
                else:
                    obp, slg = get_latest_team_allowed(cur, team_id, ts)
                    logger.debug(
                      "      → using team‐allowed for new starter: obp=%.3f, slg=%.3f",
                      obp, slg
                    )
                    if obp is None or slg is None:
                        return (None, None)
            else:
                # reliever
                obp, slg = get_latest_team_allowed(cur, team_id, ts)
                logger.debug(
                  "      → using team‐allowed for reliever %s: obp=%.3f, slg=%.3f",
                  pid, obp, slg
                )
                if obp is None or slg is None:
                    # if team‐allowed also missing, just skip this reliever
                    continue

        weight = ab / total_ab
        staff_obp += obp * weight
        staff_slg += slg * weight
        logger.debug(
          "      → weighted contrib: obp=%.3f, slg=%.3f (weight=%.3f)",
          obp, slg, weight
        )

    logger.debug("  → aggregated staff stats: obp=%.3f, slg=%.3f",
                 staff_obp, staff_slg)
    return (staff_obp, staff_slg)


# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    conn = pg_connect()
    cur  = conn.cursor(cursor_factory=DictCursor)

    # 1) fetch raw h2h snapshots
    logger.info("Fetching raw h2h snapshots…")
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
      WHERE go.odds_type = 'spreads'
      ORDER BY go.mlb_game_pk, go.book_id, go.as_of_time
    )
    SELECT
      fs.go_id,
      fs.book_id,
      fs.mlb_game_pk,
      fs.as_of_time,
      fs.winner,
      fs.away_team_id,
      fs.home_team_id,
      away_o.odds_american AS away_odds,
      home_o.odds_american AS home_odds
    FROM first_snap AS fs
      JOIN msf_mlb.odds AS away_o
        ON fs.go_id = away_o.game_odds_id
       AND away_o.outcome_type = 'away'
      JOIN msf_mlb.odds AS home_o
        ON fs.go_id = home_o.game_odds_id
       AND home_o.outcome_type = 'home'
    """)
    rows = cur.fetchall()
    logger.info("→ fetched %d total h2h snapshots", len(rows))

    # 2) reduce to best underdog line per game
    best = {}
    for r in rows:
        gid = r["mlb_game_pk"]
        aw, hm = r["away_odds"], r["home_odds"]
        if aw is None or hm is None or aw == hm:
            continue

        if aw > hm:
            ud_odds, ud_side, ud_tid = aw, "away", r["away_team_id"]
            fv_odds, fv_side, fv_tid = hm, "home", r["home_team_id"]
        else:
            ud_odds, ud_side, ud_tid = hm, "home", r["home_team_id"]
            fv_odds, fv_side, fv_tid = aw, "away", r["away_team_id"]

        entry = dict(r)
        entry.update({
            "ud_odds": ud_odds,
            "ud_side": ud_side,
            "ud_tid":  ud_tid,
            "fv_odds": fv_odds,
            "fv_side": fv_side,
            "fv_tid":  fv_tid
        })

        if gid not in best or ud_odds > best[gid]["ud_odds"]:
            best[gid] = entry

    logger.info("→ reduced to %d best underdog lines", len(best))

    # 3) apply PI rule & collect profits
    logger.info("Applying ΔPI≥%.2f rule…", DELTA_PI)
    profits = []

    for r in best.values():
        # underdog batting stats
        ud_obp, ud_slg = get_latest_team_stats(cur, r["ud_tid"], r["as_of_time"])
        # favorite staff allowed via weighted snapshots (with ESTIMATE_STATS logic)
        staff_obp, staff_slg = get_staff_allowed(
            cur,
            r["mlb_game_pk"],
            r["fv_side"],
            r["as_of_time"]
        )

        if None in (ud_obp, ud_slg, staff_obp, staff_slg):
            logger.debug(
                "  → skipping %s: missing stats (ud_obp=%s, ud_slg=%s, staff_obp=%s, staff_slg=%s)",
                r["mlb_game_pk"], ud_obp, ud_slg, staff_obp, staff_slg
            )
            continue

        # compute ΔPI = (UD OBP + UD SLG) - (Staff OBP + Staff SLG)
        delta_pi = (ud_obp + ud_slg) - (staff_obp + staff_slg)
        logger.debug(
            "Game %s: ΔPI=%.3f (UD=%.3f+%.3f=%.3f, STAFF=%.3f+%.3f=%.3f)",
            r["mlb_game_pk"],
            delta_pi,
            ud_obp, ud_slg, (ud_obp + ud_slg),
            staff_obp, staff_slg, (staff_obp + staff_slg)
        )

        if delta_pi < DELTA_PI:
            logger.debug("  → skipping %s: ΔPI %.3f < %.3f",
                         r["mlb_game_pk"], delta_pi, DELTA_PI)
            continue

        # calculate profit or loss
        won = (r["winner"] == r["ud_side"])
        pl  = profit_factor(r["ud_odds"]) if won else -1.0
        logger.debug("  → placing bet on %s side; won=%s → P/L=%.2f",
                     r["ud_side"], won, pl)

        profits.append(pl)

    # 4) compute summary statistics
    n      = len(profits)
    mean   = sum(profits) / n if n else 0.0
    sd     = math.sqrt(sum((x - mean) ** 2 for x in profits) / (n - 1)) if n > 1 else 0.0
    se     = sd / math.sqrt(n) if n else 0.0
    t_stat = mean / se if se else 0.0
    p_val  = 1 - t.cdf(t_stat, df=n - 1) if n > 1 else 1.0
    t_crit = t.ppf(0.975, df=n - 1) if n > 1 else 0.0
    ci_low = mean - t_crit * se
    ci_hi  = mean + t_crit * se

    # 5) final summary logging
    logger.info("ΔPI≥%.2f   #BETS=%4d   MEAN P/L=%7.3f", DELTA_PI, n, mean)
    logger.info("STD DEV=%7.3f   SE=%7.4f", sd, se)
    logger.info("t-stat=%7.3f   p(one-sided)=%7.4f", t_stat, p_val)
    logger.info("95%% CI=[%7.3f, %7.3f]", ci_low, ci_hi)

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
