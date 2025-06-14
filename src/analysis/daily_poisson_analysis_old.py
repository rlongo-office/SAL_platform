import os
import argparse
import logging
from datetime import datetime, date
from typing import Tuple, Dict, Any

import pandas as pd
import numpy as np
import psycopg2
from psycopg2.extras import register_default_jsonb, DictCursor
from statsmodels.api import GLM, add_constant
from statsmodels.genmod.families import Poisson

from daily_analysis import (
    fetch_probables,
    get_latest_team_stats,
    get_staff_allowed,
    utc_range_for_us_date
)
from poisson_test import fetch_game_data

# Database connection parameters
DB_SCHEMA = os.getenv("DB_SCHEMA", "msf_mlb")
DB = dict(
    dbname   = os.getenv("DB_NAME", "neondb"),
    user     = os.getenv("DB_USER", "neondb_owner"),
    password = os.getenv("DB_PASS", "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST", "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT", "5432"),
    sslmode  = "require",
)

def pg_connect() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {DB_SCHEMA}, public;")
    return conn

# Setup logging
env_log_dir = os.getenv("LOG_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../logs")))
os.makedirs(env_log_dir, exist_ok=True)
LOG_FILE = os.path.join(
    env_log_dir,
    f"daily_poisson_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

register_default_jsonb(loads=lambda x: x)


def fetch_future_features(game_day: date) -> pd.DataFrame:
    """
    Build feature DataFrame for future games on game_day using real snapshots.
    Includes team IDs, probabilities, and adjusted for US-local date in UTC.
    """
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=DictCursor)

    # 1) fetch probable starters (handles US-local date rollover)
    probables = fetch_probables(game_day)

    # 2) fetch schedule for US-local game_day via UTC window
    start_utc, end_utc = utc_range_for_us_date(game_day)
    cur.execute(
        f"""
        SELECT
            game_id,
            start_time AS game_datetime,
            home_team_id,
            away_team_id
        FROM {DB_SCHEMA}.schedule
        WHERE start_time >= %s
          AND start_time <  %s
          AND game_type = 'reg'
        ORDER BY start_time
        """,
        (start_utc, end_utc)
    )
    games = cur.fetchall()
    

    beg_date = date(game_day.year - 3, 1, 1)
    records = []

    for gid, gtime, home_id, away_id in games:
        starter = probables.get(gid, {})
        # require both starters
        if not (starter.get('home', {}).get('id') and starter.get('away', {}).get('id')):
            continue

        home_obp, home_slg = get_latest_team_stats(cur, home_id, gtime)
        away_obp, away_slg = get_latest_team_stats(cur, away_id, gtime)
        opp_obp_home, opp_slg_home, _ = get_staff_allowed(
            cur, gid, 'away', gtime, beg_date, probables
        )
        opp_obp_away, opp_slg_away, _ = get_staff_allowed(
            cur, gid, 'home', gtime, beg_date, probables
        )

        if None in (home_obp, home_slg, away_obp, away_slg,
                    opp_obp_home, opp_slg_home,
                    opp_obp_away, opp_slg_away):
            continue

        records.append({
            'game_id':       gid,
            'game_datetime': gtime,
            'home_team_id':  home_id,
            'away_team_id':  away_id,
            'home_obp':      home_obp,
            'home_slg':      home_slg,
            'away_obp':      away_obp,
            'away_slg':      away_slg,
            'opp_obp_home':  opp_obp_home,
            'opp_slg_home':  opp_slg_home,
            'opp_obp_away':  opp_obp_away,
            'opp_slg_away':  opp_slg_away,
        })

    return pd.DataFrame(records)


def train_model(historical_df: pd.DataFrame) -> Tuple[GLM, GLM]:
    # Fit two Poisson GLMs with robust filtering and stable start params
    Xh = add_constant(historical_df[['home_obp', 'home_slg', 'opp_obp_away', 'opp_slg_away']])
    yh = historical_df['home_score']
    valid_h = (
        yh.notnull() & np.isfinite(yh.values) &
        np.all(np.isfinite(Xh.values), axis=1)
    )
    Xh, yh = Xh.loc[valid_h], yh.loc[valid_h]
    var_h = Xh.var()
    Xh = Xh.loc[:, var_h > 0]
    start_h = np.ones(Xh.shape[1]) * 0.1
    model_home = GLM(
        yh, Xh, family=Poisson()
    ).fit(start_params=start_h, maxiter=100, tol=1e-8)

    Xa = add_constant(historical_df[['away_obp', 'away_slg', 'opp_obp_home', 'opp_slg_home']])
    ya = historical_df['away_score']
    valid_a = (
        ya.notnull() & np.isfinite(ya.values) &
        np.all(np.isfinite(Xa.values), axis=1)
    )
    Xa, ya = Xa.loc[valid_a], ya.loc[valid_a]
    var_a = Xa.var()
    Xa = Xa.loc[:, var_a > 0]
    start_a = np.ones(Xa.shape[1]) * 0.1
    model_away = GLM(
        ya, Xa, family=Poisson()
    ).fit(start_params=start_a, maxiter=100, tol=1e-8)

    return model_home, model_away


def predict_future(date_str: str, hist_start: str, hist_end: str, threshold: float = 0.5) -> None:
    game_date = datetime.fromisoformat(date_str).date()

    # 1) historical training
    hist_df = fetch_game_data(hist_start, hist_end)
    if hist_df.empty:
        logger.error("No historical data from %s to %s", hist_start, hist_end)
        return
    model_home, model_away = train_model(hist_df)
    logger.info("Models trained on %s to %s", hist_start, hist_end)

    # 2) build features for future
    future_df = fetch_future_features(game_date)
    if future_df.empty:
        logger.error("No future games for %s", date_str)
        return

    # 3) map team names
    conn = pg_connect()
    teams_df = pd.read_sql("SELECT id, abbreviation FROM msf_mlb.teams", conn)
    conn.close()
    name_map = dict(zip(teams_df.id, teams_df.abbreviation))
    future_df['home_team'] = future_df['home_team_id'].map(name_map)
    future_df['away_team'] = future_df['away_team_id'].map(name_map)  

    # 4) predictions
    Xh = add_constant(future_df[['home_obp','home_slg','opp_obp_away','opp_slg_away']])
    Xh = Xh[model_home.params.index]
    future_df['home_pred'] = model_home.predict(Xh).round(2)

    Xa = add_constant(future_df[['away_obp','away_slg','opp_obp_home','opp_slg_home']])
    Xa = Xa[model_away.params.index]
    future_df['away_pred'] = model_away.predict(Xa).round(2)

    # 5) combine and threshold
    future_df['total_pred'] = (future_df['home_pred'] + future_df['away_pred']).round(2)
    future_df['pred_diff']  = (future_df['home_pred'] - future_df['away_pred']).abs()
    future_df['predicted_winner'] = np.where(
        future_df['home_pred'] > future_df['away_pred'],
        future_df['home_team'], future_df['away_team']
    )
    if threshold:
        future_df = future_df[future_df['pred_diff'] >= threshold]

    # 6) output
    out = future_df[[
        'game_datetime','home_team','away_team',
        'home_pred','away_pred','total_pred',
        'predicted_winner','pred_diff'
    ]]
    out_file = os.path.join(
        env_log_dir,
        f"predictions_{date_str}_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
    )
    out.to_csv(out_file, index=False)
    logger.info(
        "Predictions (%d rows) written to %s",
        len(out), out_file
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Poisson future predictions with team names and totals"
    )
    parser.add_argument('--hist-start', required=True)
    parser.add_argument('--hist-end',   required=True)
    parser.add_argument('--date',       required=True)
    parser.add_argument(
        '--threshold',
        type=float,
        default=0.5,
        help='Min |home_pred-away_pred| to include'
    )
    args = parser.parse_args()
    predict_future(args.date, args.hist_start, args.hist_end, args.threshold)
