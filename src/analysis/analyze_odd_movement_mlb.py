#!/usr/bin/env python3
import numpy as np
import pandas as pd
from sqlalchemy import create_engine

# ─── DB SETUP ────────────────────────────────────────────────────────────────────
DB_USER = "neondb_owner"
DB_PASS = "npg_aKWdUeCXV10c"
DB_HOST = "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"
DB_NAME = "neondb"
DB_PORT = "5432"
DB_SSL  = "require"

DB_URI = (
    f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    f"?sslmode={DB_SSL}"
)

def get_engine():
    return create_engine(DB_URI, echo=False)

# ─── DATA EXTRACTION ─────────────────────────────────────────────────────────────
def fetch_all(engine):
    sql = """
    WITH first_last AS (
      SELECT
        go.mlb_game_pk  AS game_pk,
        go.book_id      AS book_id,
        go.odds_type    AS odds_type,
        o.outcome_type  AS outcome,
        MIN(go.as_of_time) AS first_time,
        MAX(go.as_of_time) AS last_time
      FROM msf_mlb.game_odds go
      JOIN msf_mlb.odds o
        ON o.game_odds_id = go.id
      GROUP BY
        go.mlb_game_pk, go.book_id, go.odds_type, o.outcome_type
    )
    SELECT
      fl.game_pk,
      fl.book_id,
      fl.odds_type,
      fl.outcome,
      mo.home_score,
      mo.away_score,
      mo.winner,

      -- opening snapshot
      o1.odds_american   AS open_line,
      o1.spread          AS open_spread,
      o1.over_under      AS open_over_under,

      -- closing snapshot
      o2.odds_american   AS close_line,
      o2.spread          AS close_spread,
      o2.over_under      AS close_over_under

    FROM first_last fl

    JOIN msf_mlb.game_odds go1
      ON go1.mlb_game_pk = fl.game_pk
     AND go1.book_id      = fl.book_id
     AND go1.odds_type    = fl.odds_type
     AND go1.as_of_time   = fl.first_time
    JOIN msf_mlb.odds o1
      ON o1.game_odds_id = go1.id
     AND o1.outcome_type = fl.outcome

    JOIN msf_mlb.game_odds go2
      ON go2.mlb_game_pk = fl.game_pk
     AND go2.book_id      = fl.book_id
     AND go2.odds_type    = fl.odds_type
     AND go2.as_of_time   = fl.last_time
    JOIN msf_mlb.odds o2
      ON o2.game_odds_id = go2.id
     AND o2.outcome_type = fl.outcome

    JOIN msf_mlb.mlb_game_outcomes mo
      ON mo.game_id = fl.game_pk;
    """
    return pd.read_sql(sql, engine)

# ─── SIMULATION ─────────────────────────────────────────────────────────────────
def american_to_decimal(odds):
    """Convert American odds to decimal odds."""
    return np.where(odds > 0, 1 + odds/100.0, 1 + 100.0/(-odds))

def simulate(df):
    df = df.copy()

    # decimal odds
    df['dec_open']  = american_to_decimal(df['open_line'])
    df['dec_close'] = american_to_decimal(df['close_line'])

    # baseline: no pushes
    df['pl_open']  = 0.0
    df['pl_close'] = 0.0

    # 1) MONEYLINE
    m = df['odds_type'] == 'h2h'
    win = df['outcome'] == df['winner']
    df.loc[m, 'pl_open']  = np.where(win[m],  df.loc[m, 'dec_open'] - 1, -1)
    df.loc[m, 'pl_close'] = np.where(win[m],  df.loc[m, 'dec_close'] - 1, -1)

    # 2) SPREADS
    s = df['odds_type'] == 'spreads'
    # calculate score differential + spread
    diff_open  = np.where(
        df.loc[s, 'outcome'] == 'home',
        df.loc[s, 'home_score'] - df.loc[s, 'away_score'] + df.loc[s, 'open_spread'],
        df.loc[s, 'away_score'] - df.loc[s, 'home_score'] + df.loc[s, 'open_spread']
    )
    diff_close = np.where(
        df.loc[s, 'outcome'] == 'home',
        df.loc[s, 'home_score'] - df.loc[s, 'away_score'] + df.loc[s, 'close_spread'],
        df.loc[s, 'away_score'] - df.loc[s, 'home_score'] + df.loc[s, 'close_spread']
    )
    win_open  = diff_open  > 0
    push_open = diff_open  == 0
    win_close = diff_close > 0
    push_close= diff_close == 0

    df.loc[s, 'pl_open']  = np.where(win_open,  df.loc[s, 'dec_open']  - 1,
                             np.where(push_open, 0, -1))
    df.loc[s, 'pl_close'] = np.where(win_close, df.loc[s, 'dec_close'] - 1,
                             np.where(push_close, 0, -1))

    # 3) TOTALS
    t = df['odds_type'] == 'totals'
    total_score = df.loc[t, 'home_score'] + df.loc[t, 'away_score']
    diff_open  = np.where(
        df.loc[t, 'outcome'] == 'over',
        total_score - df.loc[t, 'open_over_under'],
        df.loc[t, 'open_over_under'] - total_score
    )
    diff_close = np.where(
        df.loc[t, 'outcome'] == 'over',
        total_score - df.loc[t, 'close_over_under'],
        df.loc[t, 'close_over_under'] - total_score
    )
    win_open  = diff_open  > 0
    push_open = diff_open  == 0
    win_close = diff_close > 0
    push_close= diff_close == 0

    df.loc[t, 'pl_open']  = np.where(win_open,  df.loc[t, 'dec_open']  - 1,
                             np.where(push_open, 0, -1))
    df.loc[t, 'pl_close'] = np.where(win_close, df.loc[t, 'dec_close'] - 1,
                             np.where(push_close, 0, -1))

    return df

# ─── AGGREGATION ────────────────────────────────────────────────────────────────
def summarize_pnl(df):
    grp = df.groupby(['odds_type','outcome'])
    summary = grp.agg(
      count=('pl_open','size'),
      mean_open = ('pl_open','mean'),
      mean_close= ('pl_close','mean'),
      std_open  = ('pl_open','std'),
      std_close = ('pl_close','std'),
    )
    summary['se_open']  = summary['std_open']/np.sqrt(summary['count'])
    summary['se_close'] = summary['std_close']/np.sqrt(summary['count'])
    summary['delta_mean'] = summary['mean_close'] - summary['mean_open']
    print(summary[['count','mean_open','mean_close','delta_mean','se_open','se_close']])

# ─── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    engine = get_engine()
    df_raw = fetch_all(engine)
    df_sim = simulate(df_raw)
    summarize_pnl(df_sim)

if __name__ == "__main__":
    main()
