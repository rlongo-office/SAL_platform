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
LOG_FILE = os.path.join(LOG_DIR, f"poisson_test_noPF_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
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
    dbname   = os.getenv("DB_NAME",   "neondb"),
    user     = os.getenv("DB_USER",   "neondb_owner"),
    password = os.getenv("DB_PASS",   "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST",   "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT",   "5432"),
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
    Batch-fetches everything needed between `start_date` and `end_date`
    (inclusive).  Returns a tidy dataframe ready for modelling.
    """

    # ---------- 0. Parse CLI dates once, as tz-aware UTC -------------------
    start = pd.to_datetime(start_date, utc=True)
    end   = pd.to_datetime(end_date,   utc=True)

    with pg_connect() as conn:
        # ---------- 1. Schedule ------------------------------------------------
        games_query = """
            SELECT game_id,
                   start_time        AS game_datetime,
                   home_team_id,
                   away_team_id
            FROM   msf_mlb.schedule
            WHERE  start_time BETWEEN %s AND %s
            ORDER  BY start_time;
        """
        games_df = pd.read_sql(games_query, conn, params=[start, end])
        games_df["game_datetime"] = pd.to_datetime(games_df["game_datetime"], utc=True)

        if games_df.empty:
            logger.warning("No games found between %s and %s", start_date, end_date)
            return pd.DataFrame()

        game_ids: List[int] = games_df["game_id"].tolist()
        teams_home = set(games_df["home_team_id"])
        teams_away = set(games_df["away_team_id"])
        team_ids: List[int] = list(teams_home | teams_away)

        # ---------- 2. Outcomes & starters (still only two lightweight queries)
        scores_df = pd.read_sql(
            """
            SELECT game_id, home_score, away_score
            FROM   msf_mlb.mlb_game_outcomes
            WHERE  game_id = ANY(%s);
            """,
            conn, params=[game_ids]
        )

        starters_df = pd.read_sql(
            """
            SELECT game_id, player_id, side
            FROM   msf_mlb.pitcher_boxscores
            WHERE  game_id = ANY(%s) AND sequence = 1;
            """,
            conn, params=[game_ids]
        )

        # ---------- 3. Snapshots (big-ish, but fetched once) -------------------
        team_snaps_df = pd.read_sql(
            """
            SELECT *
            FROM   msf_mlb.team_stats_snapshots
            WHERE  team_id = ANY(%s);
            """,
            conn, params=[team_ids]
        )
        team_snaps_df["snapshot_date"] = pd.to_datetime(
            team_snaps_df["snapshot_date"], utc=True
        )

        pitcher_ids: List[int] = starters_df["player_id"].unique().tolist()
        pitcher_snaps_df = pd.read_sql(
            """
            SELECT *
            FROM   msf_mlb.pitcher_stats_snapshots
            WHERE  player_id = ANY(%s);
            """,
            conn, params=[pitcher_ids]
        )
        pitcher_snaps_df["snapshot_date"] = pd.to_datetime(
            pitcher_snaps_df["snapshot_date"], utc=True
        )

    # ---------- 4. Build one big record per game ------------------------------
    records = []
    for g in games_df.itertuples(index=False):
        gid        = g.game_id
        gtime      = g.game_datetime      # tz-aware UTC
        home_id    = g.home_team_id
        away_id    = g.away_team_id

        # ---- outcomes
        scores = scores_df[scores_df["game_id"] == gid]
        if scores.empty:
            logger.debug("skip game %s – no score yet", gid)
            continue
        home_score, away_score = scores.iloc[0][["home_score", "away_score"]]

        # ---- starters
        starters = starters_df[starters_df["game_id"] == gid]
        starter_home = starters.loc[starters["side"] == "home", "player_id"]
        starter_away = starters.loc[starters["side"] == "away", "player_id"]
        if starter_home.empty or starter_away.empty:
            logger.debug("skip game %s – missing starters", gid)
            continue
        sp_home_id = int(starter_home.iloc[0])
        sp_away_id = int(starter_away.iloc[0])

        # ---- team snapshots (latest < game time)
        hsnap = (
            team_snaps_df[
                (team_snaps_df["team_id"] == home_id)
                & (team_snaps_df["snapshot_date"] < gtime)
            ]
            .sort_values("snapshot_date", ascending=False)
            .head(1)
        )
        asnap = (
            team_snaps_df[
                (team_snaps_df["team_id"] == away_id)
                & (team_snaps_df["snapshot_date"] < gtime)
            ]
            .sort_values("snapshot_date", ascending=False)
            .head(1)
        )
        if hsnap.empty or asnap.empty:
            logger.debug("skip game %s – missing team snapshot", gid)
            continue

        # ---- pitcher snapshots
        psnap_home = (
            pitcher_snaps_df[
                (pitcher_snaps_df["player_id"] == sp_home_id)
                & (pitcher_snaps_df["snapshot_date"] < gtime)
            ]
            .sort_values("snapshot_date", ascending=False)
            .head(1)
        )
        psnap_away = (
            pitcher_snaps_df[
                (pitcher_snaps_df["player_id"] == sp_away_id)
                & (pitcher_snaps_df["snapshot_date"] < gtime)
            ]
            .sort_values("snapshot_date", ascending=False)
            .head(1)
        )
        if psnap_home.empty or psnap_away.empty:
            logger.debug("skip game %s – missing pitcher snapshot", gid)
            continue

        records.append(
            {
                "game_id": gid,
                "game_datetime": gtime,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_score": home_score,
                "away_score": away_score,
                "home_obp": hsnap.iloc[0]["obp"],
                "home_slg": hsnap.iloc[0]["slg"],
                "away_obp": asnap.iloc[0]["obp"],
                "away_slg": asnap.iloc[0]["slg"],
                "opp_obp_home": psnap_home.iloc[0]["obp_allowed"],
                "opp_slg_home": psnap_home.iloc[0]["slg_allowed"],
                "opp_obp_away": psnap_away.iloc[0]["obp_allowed"],
                "opp_slg_away": psnap_away.iloc[0]["slg_allowed"],
            }
        )

    df = pd.DataFrame(records)
    logger.info("Final game-rows kept: %d (of %d scheduled)", len(df), len(games_df))
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

# ──────────────────────────────────────────────────────────────────────────────
# Statistical helpers
# ──────────────────────────────────────────────────────────────────────────────

def compute_ci(p: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    """
    Compute normal-approx 95% confidence interval for a proportion p with n samples.
    """
    if n <= 0:
        return 0.0, 0.0
    se = np.sqrt(p * (1 - p) / n)
    lower = max(p - z * se, 0.0)
    upper = min(p + z * se, 1.0)
    return lower, upper

def train_and_predict(df: pd.DataFrame) -> None:
    """
    Fit Poisson models, place bets at multiple tolerance levels, and log results.
    """
    # 1) clean
    df = df.dropna(subset=[
        "home_obp","home_slg","opp_obp_away","opp_slg_away",
        "away_obp","away_slg","opp_obp_home","opp_slg_home",
        "home_score","away_score"
    ]).copy()
    logger.info("Rows after dropna: %d", len(df))
    if df.empty:
        logger.warning("No data after cleaning – exiting")
        return

    # 2) fit the two Poisson models (no park factor)
    df = fit_models(df)

    # 3) simulate bets at multiple tolerances
    tolerances = [0.0, 0.2, 0.3, 0.5]
    rows_by_tol, summary_by_tol = simulate_bets(df, tolerances)

    # 4) write out the per-game details for tol = 0.0 (no rejection)
    if rows_by_tol[0.0]:
        df_rows = pd.DataFrame(rows_by_tol[0.0])
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(LOG_DIR, f"poisson_game_details_{ts}.csv")
        df_rows.to_csv(out_path, index=False)
        logger.info("Per-game details written to %s", out_path)

    # 5) log enriched summary metrics for each tolerance
    for tol in tolerances:
        summary = summary_by_tol[tol]
        ml_profit = summary["ml_profit"]
        ml_bets   = summary["ml_bets"]
        ml_win    = summary["ml_win_pct"]
        roi_ml    = summary["roi_ml_per_bet"]
        ci_ml_lo, ci_ml_hi = summary["ml_win_ci95"]

        tot_profit = summary["totals_profit"]
        tot_bets   = summary["totals_bets"]
        tot_win    = summary["totals_win_pct"]
        roi_tot    = summary["roi_totals_per_bet"]
        ci_tot_lo, ci_tot_hi = summary["totals_win_ci95"]

        logger.info(
            "Tolerance %.2f → ML: profit=%.2f on %d bets (win%%=%.2f); ROI=%.4f; 95%% CI=[%.4f–%.4f] | "
            "Totals: profit=%.2f on %d bets (win%%=%.2f); ROI=%.4f; 95%% CI=[%.4f–%.4f]",
            tol,
            ml_profit, ml_bets, ml_win, roi_ml, ci_ml_lo, ci_ml_hi,
            tot_profit, tot_bets, tot_win, roi_tot, ci_tot_lo, ci_tot_hi
        )

    # 6) granular breakdown on the full (tol=0) cohort
    log_granular_stats(pd.DataFrame(rows_by_tol[0.0]))

def fit_models(df: pd.DataFrame) -> pd.DataFrame:
    # home model
    Xh = add_constant(df[["home_obp","home_slg","opp_obp_away","opp_slg_away"]])
    yh = df["home_score"]
    mh = GLM(yh, Xh, family=Poisson()).fit()
    df["home_pred"] = mh.predict(Xh)

    # away model
    Xa = add_constant(df[["away_obp","away_slg","opp_obp_home","opp_slg_home"]])
    ya = df["away_score"]
    ma = GLM(ya, Xa, family=Poisson()).fit()
    df["away_pred"] = ma.predict(Xa)

    # winners
    df["actual_winner"] = np.where(df.home_score > df.away_score, "home",
                            np.where(df.away_score > df.home_score, "away","tie"))
    df["predicted_winner"] = np.where(df.home_pred > df.away_pred, "home",
                              np.where(df.away_pred > df.home_pred, "away","tie"))
    df["prediction_correct"] = df.actual_winner == df.predicted_winner

    return df

def simulate_bets_raw(df: pd.DataFrame, tolerances: list[float]):
    """
    For each tol in tolerances, maintain its own bankroll ledgers
    and rows list, but now:
      - ML & Spread use abs(run_diff) >= tol
      - Totals use abs(total_pred - O/U) >= tol
    Returns:
      - rows_by_tol: dict[tol -> list of per-game dicts]
      - summary_by_tol: dict[tol -> summary dict]
    """
    BANK0 = 10_000.0
    UNIT  = 0.01

    # one ledger per tolerance
    state = {
        tol: {
            "bank_ml": BANK0,
            "bank_spread": BANK0,
            "bank_totals": BANK0,
            "wins_ml": 0,
            "wins_spread": 0,
            "wins_totals": 0,
            "total_bets": 0,
            "rows": []
        }
        for tol in tolerances
    }

    with pg_connect() as conn:
        for r in df.itertuples(index=False):
            pred_diff   = r.home_pred - r.away_pred
            actual_diff = r.home_score  - r.away_score

            market = get_game_market_snapshot(r.game_id, conn)
            if None in market.values():
                continue

            # precompute total_pred and O/U line
            total_pred = r.home_pred + r.away_pred
            over_line  = market["totals"]["over"]["total"]
            total_diff = total_pred - over_line

            for tol in tolerances:
                s = state[tol]

                # — MONEYLINE & SPREAD gating on run_diff —
                if abs(pred_diff) >= tol:
                    # MONEYLINE
                    stake     = UNIT * s["bank_ml"]
                    fav       = market["moneyline"]["favorite"]["team"]
                    ml_side   = "favorite" if fav == r.predicted_winner else "underdog"
                    ml_line   = market["moneyline"][ml_side]["odds"]
                    ml_win    = (r.predicted_winner == r.actual_winner)
                    prof_ml   = american_to_profit(stake, ml_line, ml_win)
                    s["bank_ml"]   += prof_ml
                    s["wins_ml"]   += int(ml_win)

                    # SPREAD
                    stake      = UNIT * s["bank_spread"]
                    run_fav    = market["spread"]["favorite"]["runline"]
                    spread_side= decide_spread_bet(pred_diff, run_fav)
                    sel        = market["spread"][spread_side]
                    rl         = sel["runline"]
                    spread_odds= sel["odds"]
                    spread_win = (
                        (spread_side=="favorite" and actual_diff > -rl) or
                        (spread_side=="underdog"  and actual_diff < -rl)
                    )
                    prof_sp    = american_to_profit(stake, spread_odds, spread_win)
                    s["bank_spread"]   += prof_sp
                    s["wins_spread"]   += int(spread_win)
                else:
                    # skip ML & spread
                    ml_side = None
                    ml_line = None
                    ml_win  = None
                    prof_ml = 0.0
                    spread_side = None
                    spread_win  = None
                    prof_sp      = 0.0
                    rl           = None

                # — TOTALS gating on total_diff —
                if abs(total_diff) >= tol:
                    stake    = UNIT * s["bank_totals"]
                    tot_side = decide_totals_bet(total_pred, over_line)
                    tot_odds = market["totals"][tot_side]["odds"]
                    tot_win  = (
                        (tot_side=="over"  and r.home_score + r.away_score > over_line) or
                        (tot_side=="under" and r.home_score + r.away_score < over_line)
                    )
                    prof_to  = american_to_profit(stake, tot_odds, tot_win)
                    s["bank_totals"]  += prof_to
                    s["wins_totals"]  += int(tot_win)
                else:
                    tot_side = None
                    tot_win  = None
                    prof_to  = 0.0

                # only record if we placed at least one bet this tol
                if all(x is None for x in (ml_side, spread_side, tot_side)):
                    continue

                s["total_bets"] += 1
                bank_net = (
                    s["bank_ml"]
                  + s["bank_spread"]
                  + s["bank_totals"]
                  - 3 * BANK0
                )

                s["rows"].append({
                    "game_id":       r.game_id,
                    "ml_side":       ml_side,
                    "ml_line":       ml_line,
                    "ml_win":        ml_win,
                    "ml_profit":     prof_ml,
                    "spread_side":   spread_side,
                    "runline":       rl,
                    "spread_win":    spread_win,
                    "spread_profit": prof_sp,
                    "tot_side":      tot_side,
                    "total_line":    over_line,
                    "tot_win":       tot_win,
                    "tot_profit":    prof_to,
                    "pred_diff":     pred_diff,
                    "actual_diff":   actual_diff,
                    "bank_net":      round(bank_net, 2),
                })

    # build summaries
    summary_by_tol = {}
    for tol in tolerances:
        s = state[tol]
        summary_by_tol[tol] = {
            "ml_profit":      round(s["bank_ml"]   - BANK0,  2),
            "spread_profit":  round(s["bank_spread"] - BANK0, 2),
            "totals_profit":  round(s["bank_totals"] - BANK0, 2),
            "bets_placed":    s["total_bets"],
            "ml_win_pct":     s["wins_ml"]   / s["total_bets"] if s["total_bets"] else 0,
            "spread_win_pct": s["wins_spread"]/ s["total_bets"] if s["total_bets"] else 0,
            "totals_win_pct": s["wins_totals"]/ s["total_bets"] if s["total_bets"] else 0,
        }

    rows_by_tol = {tol: state[tol]["rows"] for tol in tolerances}
    return rows_by_tol, summary_by_tol

def simulate_bets(df: pd.DataFrame, tolerances: List[float]):
    """
    For each tol in tolerances, maintain its own bankroll ledgers
    and rows list, but now:
      - ML & Spread use abs(run_diff) >= tol
      - Totals use abs(total_pred - O/U) >= tol
    Returns:
      - summary: dict[tol -> summary dict]
    """
    BANK0 = 10_000.0
    UNIT  = 0.01

    # prepare state
    state: Dict[float, Dict[str, Any]] = {
        tol: {
            "bank_ml": BANK0,
            "bank_spread": BANK0,
            "bank_totals": BANK0,
            "wins_ml": 0,
            "wins_spread": 0,
            "wins_totals": 0,
            "ml_bets": 0,
            "spread_bets": 0,
            "totals_bets": 0,
            "rows": []
        }
        for tol in tolerances
    }

    with pg_connect() as conn:
        for r in df.itertuples(index=False):
            pred_diff = r.home_pred - r.away_pred
            actual_diff = r.home_score - r.away_score
            total_pred = r.home_pred + r.away_pred

            # fetch market snapshot (skip if any missing)
            market = get_game_market_snapshot(r.game_id, conn)
            if None in market.values():
                continue

            # precompute O/U line and diff
            over_line = market["totals"]["over"]["total"]
            total_diff = total_pred - over_line

            for tol in tolerances:
                s = state[tol]

                # MONEYLINE & SPREAD
                if abs(pred_diff) >= tol:
                    # moneyline
                    stake_ml = UNIT * s["bank_ml"]
                    fav = market["moneyline"]["favorite"]["team"]
                    ml_side = "favorite" if fav == r.predicted_winner else "underdog"
                    ml_line = market["moneyline"][ml_side]["odds"]
                    ml_win = (r.predicted_winner == r.actual_winner)
                    prof_ml = american_to_profit(stake_ml, ml_line, ml_win)
                    s["bank_ml"] += prof_ml
                    s["wins_ml"] += int(ml_win)
                    s["ml_bets"] += 1

                    # spread
                    stake_sp = UNIT * s["bank_spread"]
                    run_fav = market["spread"]["favorite"]["runline"]
                    spread_side = decide_spread_bet(pred_diff, run_fav)
                    sel = market["spread"][spread_side]
                    rl = sel["runline"]
                    spread_odds = sel["odds"]
                    spread_win = ((spread_side == "favorite" and actual_diff > -rl) or
                                  (spread_side == "underdog" and actual_diff < -rl))
                    prof_sp = american_to_profit(stake_sp, spread_odds, spread_win)
                    s["bank_spread"] += prof_sp
                    s["wins_spread"] += int(spread_win)
                    s["spread_bets"] += 1
                else:
                    ml_side = None; ml_line = None; ml_win = None; prof_ml = 0.0
                    spread_side = None; rl = None; spread_win = None; prof_sp = 0.0

                # TOTALS
                if abs(total_diff) >= tol:
                    stake_to = UNIT * s["bank_totals"]
                    tot_side = decide_totals_bet(total_pred, over_line)
                    tot_odds = market["totals"][tot_side]["odds"]
                    tot_win = ((tot_side == "over" and r.home_score + r.away_score > over_line) or
                               (tot_side == "under" and r.home_score + r.away_score < over_line))
                    prof_to = american_to_profit(stake_to, tot_odds, tot_win)
                    s["bank_totals"] += prof_to
                    s["wins_totals"] += int(tot_win)
                    s["totals_bets"] += 1
                else:
                    tot_side = None; tot_win = None; prof_to = 0.0

                # record if any bet placed
                if all(x is None for x in (ml_side, spread_side, tot_side)):
                    continue

                # capture row (unchanged fields preserved)
                bank_net = (s["bank_ml"] + s["bank_spread"] + s["bank_totals"] - 3 * BANK0)
                s["rows"].append({
                    "game_id":       r.game_id,
                    "ml_side":       ml_side,
                    "ml_line":       ml_line,
                    "ml_win":        ml_win,
                    "ml_profit":     prof_ml,
                    "spread_side":   spread_side,
                    "runline":       rl,
                    "spread_win":    spread_win,
                    "spread_profit": prof_sp,
                    "tot_side":      tot_side,
                    "total_line":    over_line,
                    "tot_win":       tot_win,
                    "tot_profit":    prof_to,
                    "pred_diff":     pred_diff,
                    "actual_diff":   actual_diff,
                    "bank_net":      round(bank_net, 2),
                })

        # build enriched summaries
    summary = {}
    for tol in tolerances:
        s = state[tol]
        n_ml = s["ml_bets"]
        n_tot = s["totals_bets"]
        profit_ml = round(s["bank_ml"] - BANK0, 2)
        profit_tot = round(s["bank_totals"] - BANK0, 2)
        win_ml = s["wins_ml"]
        win_tot = s["wins_totals"]
        p_ml = win_ml / n_ml if n_ml else 0.0
        p_tot = win_tot / n_tot if n_tot else 0.0
        ci_ml = compute_ci(p_ml, n_ml)
        ci_tot = compute_ci(p_tot, n_tot)
        roi_ml = profit_ml / n_ml if n_ml else 0.0
        roi_tot = profit_tot / n_tot if n_tot else 0.0

        summary[tol] = {
            # raw
            "ml_profit": profit_ml,
            "totals_profit": profit_tot,
            "ml_bets": n_ml,
            "totals_bets": n_tot,
            # proportions
            "ml_win_pct": round(p_ml, 4),
            "totals_win_pct": round(p_tot, 4),
            # ROI per bet
            "roi_ml_per_bet": round(roi_ml, 4),
            "roi_totals_per_bet": round(roi_tot, 4),
            # 95% CIs on win-rate
            "ml_win_ci95": (round(ci_ml[0], 4), round(ci_ml[1], 4)),
            "totals_win_ci95": (round(ci_tot[0], 4), round(ci_tot[1], 4)),
        }

    # extract rows per tolerance for output consistency
    rows_by_tol = {tol: state[tol]["rows"] for tol in tolerances}
    summary_by_tol = summary
    return rows_by_tol, summary_by_tol

def log_granular_stats(bets_df: pd.DataFrame):
    # Moneyline split
    fav_ml = bets_df[bets_df.ml_side=="favorite"]
    dog_ml = bets_df[bets_df.ml_side=="underdog"]
    logger.info(
        "ML Favorites: %d bets, profit=%.2f, win%%=%.2f",
        len(fav_ml), fav_ml.ml_profit.sum(), fav_ml.ml_win.mean()
    )
    logger.info(
        "ML Underdogs: %d bets, profit=%.2f, win%%=%.2f",
        len(dog_ml), dog_ml.ml_profit.sum(), dog_ml.ml_win.mean()
    )

    # Odds‐bucket performance
    bins   = [-1e9, -200, -150, -110, 0, 110, 150, 200, 1e9]
    labels = ["<=-200","-200~-150","-150~-110","-110~0","0~110",
              "110~150","150~200",">200"]
    bets_df["odds_bucket"] = pd.cut(bets_df.ml_line, bins=bins, labels=labels)
    for b, grp in bets_df.groupby("odds_bucket"):
        logger.info(
            " ML odds %s: bets=%d, profit=%.2f, win%%=%.2f",
            b, len(grp), grp.ml_profit.sum(), grp.ml_win.mean()
        )

    # Totals split
    over  = bets_df[bets_df.tot_side=="over"]
    under = bets_df[bets_df.tot_side=="under"]
    logger.info(
        "Totals Over: %d bets, profit=%.2f, win%%=%.2f",
        len(over), over.tot_profit.sum(), over.tot_win.mean()
    )
    logger.info(
        "Totals Under: %d bets, profit=%.2f, win%%=%.2f",
        len(under), under.tot_profit.sum(), under.tot_win.mean()
    )

    # Spread split
    fav_sp = bets_df[bets_df.spread_side=="favorite"]
    dog_sp = bets_df[bets_df.spread_side=="underdog"]
    logger.info(
        "Spread Favorites: %d bets, profit=%.2f, win%%=%.2f",
        len(fav_sp), fav_sp.spread_profit.sum(), fav_sp.spread_win.mean()
    )
    logger.info(
        "Spread Underdogs: %d bets, profit=%.2f, win%%=%.2f",
        len(dog_sp), dog_sp.spread_profit.sum(), dog_sp.spread_win.mean()
    )

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
