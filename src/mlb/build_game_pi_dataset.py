#!/usr/bin/env python3
"""
build_game_pi_dataset.py  –  export batting & pitching power-indices (PI)
for *both* sides of every MLB game in the database.
-------------------------------------------------------------

Output CSV columns
    mlb_game_pk, date_played,
    away_bats_pi, away_pitch_pi,
    home_bats_pi, home_pitch_pi
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Dict, Tuple
import sys, pathlib
from pathlib import Path
from psycopg2.extras import DictCursor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
parent = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(parent))

# ---  import the helpers you already have  --------------------------
from test_underdog_spread_bets import (          # ← change to real file / package
    pg_connect,
    get_latest_team_stats,
    get_staff_allowed,
)

OUT_DIR = PROJECT_ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
OUT_CSV = OUT_DIR / f"game_power_indices_{datetime.now():%Y%m%d_%H%M%S}.csv"

# --------------------------------------------------------------------
def main() -> None:
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=DictCursor)

    # 1) one opening snapshot per game (any market – spreads or h2h)
    cur.execute(
        """
        WITH first_snap AS (
            SELECT DISTINCT ON (mlb_game_pk)
                   mlb_game_pk       AS gid,
                   game_time,
                   as_of_time
            FROM   msf_mlb.game_odds
            ORDER  BY mlb_game_pk, as_of_time   -- earliest row per game
        )
        SELECT fs.gid,
               fs.game_time      AS gtime,
               outc.date_played,
               outc.away_team_id,
               outc.home_team_id
          FROM first_snap AS fs
          JOIN msf_mlb.mlb_game_outcomes AS outc
            ON outc.game_id = fs.gid
        ORDER BY fs.gid;
        """
    )
    games = cur.fetchall()
    print(f"Found {len(games):,} games to process")

    # caches so we don’t recalculate snapshots repeatedly
    team_cache:  Dict[Tuple[int, str], Tuple[float, float]] = {}
    staff_cache: Dict[Tuple[int, str], Tuple[float, float]] = {}

    rows_for_csv = []

    for g in games:
        gid, gtime, gdate, aw_tid, hm_tid = g
        ts = gtime          # timestamp to freeze “knowledge” before 1st pitch
        ts_key = ts.date().isoformat()

        # ----- batting PI (OBP+SLG) ---------------------------------
        for side, tid in (("away", aw_tid), ("home", hm_tid)):
            key = (tid, ts_key)
            if key in team_cache:
                obp, slg = team_cache[key]
            else:
                obp, slg = get_latest_team_stats(cur, tid, ts)
                team_cache[key] = (obp, slg)
            if side == "away":
                aw_bats_pi = (obp + slg) if None not in (obp, slg) else None
            else:
                hm_bats_pi = (obp + slg) if None not in (obp, slg) else None

        # ----- pitching PI (staff allowed) --------------------------
        # cache per (game, side) because staff mix depends on game_pk
        for side in ("away", "home"):
            key = (gid, side)
            if key in staff_cache:
                p_obp, p_slg = staff_cache[key]
            else:
                p_obp, p_slg = get_staff_allowed(cur, gid, side, ts)
                staff_cache[key] = (p_obp, p_slg)
            if side == "away":
                aw_pitch_pi = (p_obp + p_slg) if None not in (p_obp, p_slg) else None
            else:
                hm_pitch_pi = (p_obp + p_slg) if None not in (p_obp, p_slg) else None

        rows_for_csv.append(
            {
                "mlb_game_pk": gid,
                "date_played": gdate,
                "away_bats_pi":  aw_bats_pi,
                "away_pitch_pi": aw_pitch_pi,
                "home_bats_pi":  hm_bats_pi,
                "home_pitch_pi": hm_pitch_pi,
            }
        )

    # 2) write CSV ---------------------------------------------------
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "mlb_game_pk",
                "date_played",
                "away_bats_pi",
                "away_pitch_pi",
                "home_bats_pi",
                "home_pitch_pi",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_for_csv)

    print(f"Wrote {len(rows_for_csv):,} rows → {OUT_CSV}")
    conn.close()


if __name__ == "__main__":
    main()
