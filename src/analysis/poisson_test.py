import argparse
import logging
import os
from datetime import datetime

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from statsmodels.api import GLM, add_constant
from statsmodels.genmod.families import Poisson
from sklearn.metrics import mean_absolute_error, mean_squared_error

# Configure logging
log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../logs"))
os.makedirs(log_dir, exist_ok=True)
log_filename = os.path.join(log_dir, f"poisson_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)

DB_PARAMS = {
    "dbname": "SAL_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": 5432,
}

def pg_connect():
    return psycopg2.connect(**DB_PARAMS)

def fetch_game_data(start_date, end_date):
    conn = pg_connect()
    try:
        # Step 1: Get scheduled games in date range
        games_query = """
            SELECT game_id, start_time AS game_datetime, home_team_id, away_team_id
            FROM msf_mlb.schedule
            WHERE start_time BETWEEN %s AND %s
            ORDER BY start_time
        """
        games_df = pd.read_sql(games_query, conn, params=[start_date, end_date])
        logger.debug("Fetched schedule sample:\n%s", games_df.head(10))

        all_game_data = []

        for _, row in games_df.iterrows():
            game_id = row["game_id"]
            game_datetime = row["game_datetime"]
            home_team_id = row["home_team_id"]
            away_team_id = row["away_team_id"]

            # Step 2: Fetch actual score
            score_query = """
                SELECT home_score, away_score
                FROM msf_mlb.mlb_game_outcomes
                WHERE game_id = %s
            """
            score_df = pd.read_sql(score_query, conn, params=[game_id])
            if score_df.empty:
                continue
            home_score, away_score = score_df.iloc[0]

            # Step 3: Team snapshots (latest before game)
            snapshot_query = """
                SELECT * FROM msf_mlb.team_stats_snapshots
                WHERE team_id = %s AND snapshot_date < %s
                ORDER BY snapshot_date DESC LIMIT 1
            """
            home_snap = pd.read_sql(snapshot_query, conn, params=[home_team_id, game_datetime])
            away_snap = pd.read_sql(snapshot_query, conn, params=[away_team_id, game_datetime])
            if home_snap.empty or away_snap.empty:
                continue

            # Step 4: Get starting pitcher ID (sequence = 0)
            starter_query = """
                SELECT player_id, side FROM msf_mlb.pitcher_boxscores
                WHERE game_id = %s AND sequence = 0
            """
            starters_df = pd.read_sql(starter_query, conn, params=[game_id])
            starter_home_id = starters_df[starters_df["side"] == "home"]["player_id"].values
            starter_away_id = starters_df[starters_df["side"] == "away"]["player_id"].values
            if len(starter_home_id) == 0 or len(starter_away_id) == 0:
                continue

            # Step 5: Get pitcher snapshots
            pitcher_snap_query = """
                SELECT * FROM msf_mlb.pitcher_stats_snapshots
                WHERE player_id = %s AND snapshot_date < %s
                ORDER BY snapshot_date DESC LIMIT 1
            """
            ph_snap = pd.read_sql(pitcher_snap_query, conn, params=[int(starter_home_id[0]), game_datetime])
            pa_snap = pd.read_sql(pitcher_snap_query, conn, params=[int(starter_away_id[0]), game_datetime])
            if ph_snap.empty or pa_snap.empty:
                continue

            all_game_data.append({
                "game_id": game_id,
                "game_datetime": game_datetime,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "home_score": home_score,
                "away_score": away_score,
                "home_obp": home_snap.iloc[0]["obp"],
                "home_slg": home_snap.iloc[0]["slg"],
                "away_obp": away_snap.iloc[0]["obp"],
                "away_slg": away_snap.iloc[0]["slg"],
                "opp_obp_home": ph_snap.iloc[0]["obp_allowed"],
                "opp_slg_home": ph_snap.iloc[0]["slg_allowed"],
                "opp_obp_away": pa_snap.iloc[0]["obp_allowed"],
                "opp_slg_away": pa_snap.iloc[0]["slg_allowed"]
            })

        df = pd.DataFrame(all_game_data)
        logger.debug("Final dataset sample:\n%s", df.head(10))
        return df

    finally:
        conn.close()

def train_and_predict(df):
    df = df.dropna()
    logger.info("Rows remaining after dropna: %d", len(df))
    if df.empty:
        logger.warning("No rows available after filtering. Exiting.")
        return

    # Predict home team runs
    X_home = add_constant(df[["home_obp", "home_slg", "opp_obp_away", "opp_slg_away"]])
    y_home = df["home_score"]
    model_home = GLM(y_home, X_home, family=Poisson()).fit()
    df["home_pred"] = model_home.predict(X_home)

    # Predict away team runs
    X_away = add_constant(df[["away_obp", "away_slg", "opp_obp_home", "opp_slg_home"]])
    y_away = df["away_score"]
    model_away = GLM(y_away, X_away, family=Poisson()).fit()
    df["away_pred"] = model_away.predict(X_away)

    # Log output
    for _, row in df.iterrows():
        logger.info(f"Game {row['game_id']} | Home: {row['home_score']} (pred: {row['home_pred']:.2f}), "
                    f"Away: {row['away_score']} (pred: {row['away_pred']:.2f}) | "
                    f"Diff: {abs(row['home_score'] - row['away_score'])}")

    # Summary metrics
    summary = {
        "home_mae": mean_absolute_error(df["home_score"], df["home_pred"]),
        "away_mae": mean_absolute_error(df["away_score"], df["away_pred"]),
        "home_rmse": mean_squared_error(df["home_score"], df["home_pred"], squared=False),
        "away_rmse": mean_squared_error(df["away_score"], df["away_pred"], squared=False)
    }
    logger.info("Summary metrics: %s", summary)

def main(start_date, end_date):
    logger.info(f"Starting Poisson run from {start_date} to {end_date}")
    logger.info("Fetching game + snapshot data from database...")
    df = fetch_game_data(start_date, end_date)
    logger.info(f"Total rows fetched from DB: {len(df)}")
    train_and_predict(df)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    main(args.start, args.end)
