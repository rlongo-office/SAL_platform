import argparse
import logging
import os
from datetime import datetime
import warnings
from typing import List
import time, random
import numpy as np
from inspect import signature

from typing import Dict, Tuple, Any
import psycopg2
import pandas as pd

import pandas as pd
import psycopg2
from psycopg2 import OperationalError
from psycopg2.extras import register_default_jsonb
from statsmodels.api import GLM, add_constant
from statsmodels.genmod.families import Poisson
from sklearn.metrics import mean_absolute_error, mean_squared_error

# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"poisson_test_UsePF{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Database connection
# ──────────────────────────────────────────────────────────────────────────────
DB = dict(
    dbname   = os.getenv("DB_NAME" , "neondb"),
    user     = os.getenv("DB_USER" , "neondb_owner"),
    password = os.getenv("DB_PASS" , "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST" , "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT" , "5432"),
    sslmode  = "require",
)
def pg_connect(max_tries: int = 5, base_delay: float = 2.0):
    """
    Robust connector: retries when Neon’s control plane is still waking
    the branch or the pooler momentarily refuses sockets.
    """
    last_err = None
    delay = base_delay
    for attempt in range(1, max_tries + 1):
        try:
            conn = psycopg2.connect(**DB, connect_timeout=10)
            with conn.cursor() as cur:
                cur.execute("SET search_path TO msf_mlb,public;")
            return conn                                     # ← success
        except OperationalError as e:
            last_err = e
            logger.warning(
                "DB connect attempt %d/%d failed: %s – retrying in %.1fs",
                attempt, max_tries, str(e).splitlines()[0], delay
            )
            time.sleep(delay + random.random())             # jitter
            delay *= 2                                      # exponential back-off

    # all tries exhausted
    logger.error("Giving up after %d attempts", max_tries)
    raise last_err

# ──────────────────────────────────────────────────────────────────────────────
# Data extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

register_default_jsonb(loads=lambda x: x)  # avoid slow json decode; not essential

def american_to_prob(a: int | float | None) -> float | None:
    if a in (None, 0) or (isinstance(a, float) and np.isnan(a)):
        return None
    return 100 / (a + 100) if a > 0 else abs(a) / (abs(a) + 100)

def prob_to_american(p: float) -> int:
    # protect against rounding to 0
    p = max(1e-6, min(1 - 1e-6, p))
    return round(100 * (1 - p) / p) if p < 0.5 else -round(100 * p / (1 - p))


def get_game_market_snapshot(game_id: int, conn) -> Dict[str, Any]:
    """
    Fetch averaged market prices for a single game, using probability
    averaging (prevents impossible odds like –40, –16, …).

    Returns
    -------
    snapshot : dict
        Same nested structure you already rely on:
        {
            'moneyline': {'favorite': {…}, 'underdog': {…}},
            'spread'   : {'favorite': {…}, 'underdog': {…}},
            'totals'   : {'over': {...},   'under':   {...}},
        }
    """
    sql = """
        SELECT go.odds_type,         -- 'h2h' | 'spreads' | 'totals'
               o.outcome_type,       -- 'home'/'away'/'over'/'under'
               o.odds_american,
               o.spread,
               o.over_under
        FROM msf_mlb.game_odds  go
        JOIN msf_mlb.odds       o  ON o.game_odds_id = go.id
        WHERE go.mlb_game_pk = %s
          AND go.game_segment <> 'in_play';
    """
    raw = pd.read_sql(sql, conn, params=[game_id])

    # ── 1) convert American → implied probability --------------------------
    raw["p"] = raw["odds_american"].apply(american_to_prob)

    # ── 2) average probability + supporting numeric lines ------------------
    grp         = raw.groupby(["odds_type", "outcome_type"])
    avg_prob    = grp["p"].mean()             # Series (multi-index)
    avg_spread  = grp["spread"].mean()
    avg_total   = grp["over_under"].mean()

    out: Dict[str, Any] = {"moneyline": None, "spread": None, "totals": None}

    # -- Moneyline ----------------------------------------------------------
    try:
        p_home = float(avg_prob["h2h", "home"])
        p_away = float(avg_prob["h2h", "away"])
        ml_odds = {"home": prob_to_american(p_home),
                   "away": prob_to_american(p_away)}
        fav = "home" if p_home > p_away else "away"
        dog = "away" if fav == "home" else "home"
        out["moneyline"] = {
            "favorite": {"team": fav, "odds": ml_odds[fav]},
            "underdog": {"team": dog, "odds": ml_odds[dog]},
        }
    except KeyError:
        pass  # one side missing

    # -- Spread -------------------------------------------------------------
    if ("spreads", "home") in avg_prob and ("spreads", "away") in avg_prob:
        ph, pa = float(avg_prob["spreads", "home"]), float(avg_prob["spreads", "away"])
        odds_h, odds_a = prob_to_american(ph), prob_to_american(pa)
        spread_h       = float(avg_spread.get(("spreads", "home"), np.nan))
        spread_a       = float(avg_spread.get(("spreads", "away"), np.nan))
        fav = "home" if spread_h < 0 else "away"
        dog = "away" if fav == "home" else "home"
        out["spread"] = {
            "favorite": {
                "team": fav,
                "runline": spread_h if fav == "home" else spread_a,
                "odds": odds_h if fav == "home" else odds_a,
            },
            "underdog": {
                "team": dog,
                "runline": spread_a if fav == "home" else spread_h,
                "odds": odds_a if fav == "home" else odds_h,
            },
        }

    # -- Totals -------------------------------------------------------------
    if ("totals", "over") in avg_prob and ("totals", "under") in avg_prob:
        p_over  = float(avg_prob["totals", "over"])
        p_under = float(avg_prob["totals", "under"])
        total_ln = float(avg_total.get(("totals", "over"),
                                       avg_total.get(("totals", "under"))))
        out["totals"] = {
            "over":  {"total": total_ln, "odds": prob_to_american(p_over)},
            "under": {"total": total_ln, "odds": prob_to_american(p_under)},
        }

    return out


def fetch_game_data(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetch all the features needed between `start_date` and `end_date`:
      - game_datetime, home/away team IDs
      - final home_score, away_score
      - home_obp, home_slg, opp_obp_away, opp_slg_away
      - away_obp, away_slg, opp_obp_home, opp_slg_home
      - park_factor (for that stadium)
    """
    # 0) Parse dates
    start = pd.to_datetime(start_date).date()
    end   = pd.to_datetime(end_date).date()

    with pg_connect() as conn:
        # 1) Schedule
        games_df = pd.read_sql(
            """
            SELECT
                s.game_id,
                s.start_time AS game_datetime,
                s.home_team_id,
                s.away_team_id
            FROM msf_mlb.schedule AS s
            WHERE s.start_time::date BETWEEN %s AND %s
            ORDER BY s.start_time;
            """,
            conn,
            params=[start, end],
        )
        if games_df.empty:
            logger.warning("No games in %s–%s", start, end)
            return pd.DataFrame()
        games_df["game_date"] = games_df["game_datetime"].dt.date

        # 2) Final scores
        scores_df = pd.read_sql(
            """
            SELECT
                game_id,
                home_score,
                away_score
            FROM msf_mlb.mlb_game_outcomes
            WHERE date_played BETWEEN %s AND %s
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            """,
            conn,
            params=[start, end],
        )

        # 3) Team snapshots (OBP, SLG)
        snaps_df = pd.read_sql(
            """
            SELECT
                team_id,
                snapshot_date AS game_date,
                obp,
                slg
            FROM msf_mlb.team_stats_snapshots
            WHERE snapshot_date BETWEEN %s AND %s
            """,
            conn,
            params=[start, end],
        )

        # 4) Park factors
        park_df = pd.read_sql(
            "SELECT team_id AS home_team_id, park_factor FROM msf_mlb.park_factors",
            conn,
        )

    # 5) Merge everything together
    df = (
        games_df
        # scores
        .merge(scores_df, on="game_id", how="inner")
        # home snapshots
        .merge(
            snaps_df.rename(columns={
                "team_id": "home_team_id",
                "obp":     "home_obp",
                "slg":     "home_slg"
            }),
            on=["home_team_id", "game_date"],
            how="left"
        )
        # away snapshots
        .merge(
            snaps_df.rename(columns={
                "team_id": "away_team_id",
                "obp":     "away_obp",
                "slg":     "away_slg"
            }),
            on=["away_team_id", "game_date"],
            how="left"
        )
        # cross‐side opp rates
        .assign(
            opp_obp_away=lambda d: d["away_obp"],
            opp_slg_away=lambda d: d["away_slg"],
            opp_obp_home=lambda d: d["home_obp"],
            opp_slg_home=lambda d: d["home_slg"],
        )
        # park factor
        .merge(park_df, on="home_team_id", how="left")
    )

    # 6) Final cleanup / diagnostics
    needed = [
        "home_obp", "home_slg", "opp_obp_away", "opp_slg_away",
        "away_obp", "away_slg", "opp_obp_home", "opp_slg_home",
        "home_score", "away_score", "park_factor"
    ]
    missing = df[needed].isna().sum()
    if missing.any():
        logger.warning("Missing data in fetch_game_data:\n%s", missing)

    return df

# ──────────────────────────────────────────────────────────────────────────────
# Modelling helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_rmse(y_true, y_pred):
    """
    Compute RMSE even if the installed mean_squared_error
    implementation does *not* accept the `squared=` keyword.
    """
    if "squared" in signature(mean_squared_error).parameters:
        # vanilla scikit-learn ≥ 0.22
        return mean_squared_error(y_true, y_pred, squared=False)
    else:
        # Intel/daal4py patch or ancient sklearn: returns MSE only
        mse = mean_squared_error(y_true, y_pred)
        return float(np.sqrt(mse))

# ---------------------------------------------------------------------------
#  Betting helpers (add near top of file, once)
# ---------------------------------------------------------------------------

def american_to_profit(stake: float, american: int | float | None, won: bool) -> float:
    """
    Convert American odds to +/- profit, given stake and result.
    If the price is missing (0 / None / NaN) the bet is skipped (profit = 0).
    """
    if not won:
        return -stake

    if american in (0, None) or (isinstance(american, float) and np.isnan(american)):
        logger.warning("Bet skipped: invalid American odds %s", american)
        return 0.0

    return stake * (american / 100.0 if american > 0 else 100.0 / abs(american))


def decide_spread_bet(pred_diff: float, runline: float) -> str:
    """Return 'favorite' if we expect to cover the negative runline, else 'underdog'."""
    return "favorite" if pred_diff > -runline else "underdog"

def decide_totals_bet(pred_total: float, line_total: float) -> str:
    return "over" if pred_total > line_total else "under"


# ---------------------------------------------------------------------------
#  Main modelling + simulation function (drop-in replacement)
# ---------------------------------------------------------------------------
def train_and_predict(df: pd.DataFrame) -> None:
    """
    Fit Poisson models, place three bets per game, and log bankroll stats.
    Includes park_factor as a predictor for both home and away run models.
    """
    # … your existing cleanup …
    # e.g. drop any rows with missing required fields
    df = df.dropna(subset=[
        "home_obp", "home_slg", "opp_obp_away", "opp_slg_away",
        "away_obp", "away_slg", "opp_obp_home", "opp_slg_home",
        "home_score", "away_score", "park_factor"
    ])

    # ------------------- fit two Poisson GLMs with park_factor as offset -------------------------------
    # compute offset once (log of the stadium factor)
    offset = np.log(df["park_factor"])

    # Home model: no park_factor in X, but offset applied
    X_home = add_constant(df[[
        "home_obp",
        "home_slg",
        "opp_obp_away",
        "opp_slg_away",
    ]])
    y_home = df["home_score"]
    model_home = GLM(y_home, X_home, family=Poisson(), offset=offset).fit()
    df["home_pred"] = model_home.predict(X_home, offset=offset)

    # Away model: same offset for visitors
    X_away = add_constant(df[[
        "away_obp",
        "away_slg",
        "opp_obp_home",
        "opp_slg_home",
    ]])
    y_away = df["away_score"]
    model_away = GLM(y_away, X_away, family=Poisson(), offset=offset).fit()
    df["away_pred"] = model_away.predict(X_away, offset=offset)

    # winner for accuracy
    df["actual_winner"] = np.where(
        df["home_score"] > df["away_score"], "home",
        np.where(df["away_score"] > df["home_score"], "away", "tie")
    )
    df["predicted_winner"] = np.where(
        df["home_pred"] > df["away_pred"], "home",
        np.where(df["away_pred"] > df["home_pred"], "away", "tie")
    )
    df["prediction_correct"] = df["actual_winner"] == df["predicted_winner"]

    # --------------- bankroll ledgers --------------------------------------
    BANK0 = 10_000.0
    UNIT_FRAC = 0.01                # 1% flat stake
    bank_ml = bank_spread = bank_totals = bank_overall = BANK0
    wins_ml = wins_spread = wins_totals = total_bets = 0
    rows = []

    with pg_connect() as conn:
        for r in df.itertuples(index=False):
            market = get_game_market_snapshot(r.game_id, conn)
            if None in market.values():
                logger.info("Game %s | odds not available – skipped", r.game_id)
                continue

            # MONEYLINE bet
            stake = UNIT_FRAC * bank_ml
            chosen_side = r.predicted_winner
            favorite = market["moneyline"]["favorite"]["team"]
            ml_line = market["moneyline"][ "favorite" if favorite == chosen_side else "underdog" ]["odds"]
            won_ml = (chosen_side == r.actual_winner)
            profit_ml = american_to_profit(stake, ml_line, won_ml)
            bank_ml += profit_ml
            wins_ml += int(won_ml)

            # SPREAD bet
            stake = UNIT_FRAC * bank_spread
            runline_fav = market["spread"]["favorite"]["runline"]
            bet_side = decide_spread_bet(r.home_pred - r.away_pred, runline_fav)
            sel = market["spread"][bet_side]
            runline = sel["runline"]
            spread_line = sel["odds"]
            actual_diff = r.home_score - r.away_score
            won_sp = (
                (bet_side == "favorite" and actual_diff > -runline) or
                (bet_side == "underdog"  and actual_diff < -runline)
            )
            profit_sp = american_to_profit(stake, spread_line, won_sp)
            bank_spread += profit_sp
            wins_spread += int(won_sp)

            # TOTALS bet
            stake = UNIT_FRAC * bank_totals
            total_line = market["totals"]["over"]["total"]
            bet_ou = decide_totals_bet(r.home_pred + r.away_pred, total_line)
            ou_line = market["totals"][bet_ou]["odds"]
            actual_total = r.home_score + r.away_score
            won_tot = (
                (bet_ou == "over"  and actual_total > total_line) or
                (bet_ou == "under" and actual_total < total_line)
            )
            profit_tot = american_to_profit(stake, ou_line, won_tot)
            bank_totals += profit_tot
            wins_totals += int(won_tot)

            # aggregate
            total_bets += 1
            bank_overall = bank_ml + bank_spread + bank_totals - 2 * BANK0

            # log per-game
            logger.info(
                "Game %s | Actual %d-%d | Pred %.2f-%.2f | Winner pred=%s act=%s | Net %.0f",
                r.game_id,
                r.home_score, r.away_score,
                r.home_pred,  r.away_pred,
                r.predicted_winner, r.actual_winner,
                bank_overall
            )

            rows.append({
                "game_id": r.game_id,
                "actual_home": r.home_score,
                "actual_away": r.away_score,
                "pred_home": round(r.home_pred, 2),
                "pred_away": round(r.away_pred, 2),
                "pred_diff": round(r.home_pred - r.away_pred, 2),
                "actual_diff": r.home_score  - r.away_score,
                "prediction_correct": r.prediction_correct,
                "ml_profit": round(profit_ml, 2),
                "spread_profit": round(profit_sp, 2),
                "totals_profit": round(profit_tot, 2),
                "bank_net": round(bank_overall, 2),
            })

    # ---------------- summary / accuracy -----------------------------------
    if rows:
        df_rows = pd.DataFrame(rows)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(LOG_DIR, f"poisson_game_details_{timestamp}.csv")
        df_rows.to_csv(out_path, index=False)
        logger.info("Per-game details written to %s", out_path)

    summary = {
        "home_mae":         mean_absolute_error(df["home_score"], df["home_pred"]),
        "away_mae":         mean_absolute_error(df["away_score"], df["away_pred"]),
        "winner_accuracy":  df["prediction_correct"].mean(),
        "bets_placed":      total_bets,
        "ml_profit":        round(bank_ml - BANK0, 2),
        "spread_profit":    round(bank_spread - BANK0, 2),
        "totals_profit":    round(bank_totals - BANK0, 2),
        "overall_profit":   round(bank_overall, 2),
        "ml_win_pct":       wins_ml / total_bets if total_bets else 0,
        "spread_win_pct":   wins_spread / total_bets if total_bets else 0,
        "totals_win_pct":   wins_totals / total_bets if total_bets else 0,
    }
    logger.info("Summary metrics & bankroll: %s", summary)

# ──────────────────────────────────────────────────────────────────────────────
# CLI wrapper
# ──────────────────────────────────────────────────────────────────────────────

def main(start: str, end: str) -> None:
    logger.info("Starting Poisson run from %s to %s", start, end)
    df = fetch_game_data(start, end)
    if df.empty:
        logger.error("No data – aborting.")
        return
    logger.info("Retrieved %d usable rows", len(df))
    train_and_predict(df)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poisson model evaluation for MLB games")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD start date (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD end date (inclusive)")
    args = parser.parse_args()
    main(args.start, args.end)
