#!/usr/bin/env python3
"""
Daily MLB matchup handicapper (probable-starter version)
────────────────────────────────────────────────────────
Given a single date (YYYY-MM-DD) this script

• pulls the day’s games from msf_mlb.schedule
• calls the MLB Stats API once to grab *probable* starters
• fetches the latest team / pitcher snapshots *before* first pitch
• compares   (team OBP+SLG)  vs  (opponent staff OBP_A+SLG_A)
• prints three tables for  ΔPI ≥ 0.04 / 0.06 / 0.08
"""

from __future__ import annotations

import os, logging, requests, sys
from datetime import datetime, date, timedelta, time
from typing import Dict, Tuple, List, Any, Optional
import pytz
import psycopg2
from psycopg2.extras import DictCursor
from dotenv import load_dotenv, find_dotenv
import dateutil.parser


# ─── CONFIG ──────────────────────────────────────────────────────────
load_dotenv(find_dotenv())

DB = dict(
    dbname   = os.getenv("DB_NAME" , "neondb"),
    user     = os.getenv("DB_USER" , "neondb_owner"),
    password = os.getenv("DB_PASS" , "npg_aKWdUeCXV10c"),
    host     = os.getenv("DB_HOST" , "ep-sweet-field-a5764df7-pooler.us-east-2.aws.neon.tech"),
    port     = os.getenv("DB_PORT" , "5432"),
    sslmode  = "require",
)

DB_SCHEMA   = "msf_mlb"
THRESHOLDS  = [0.04, 0.06, 0.08]
PACIFIC = pytz.timezone("US/Pacific")
# ─── LOGGING (repo-root/logs) ────────────────────────────────────────
dt_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"daily_matchups_{dt_str}.log")

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)
# ─── DB HELPERS ──────────────────────────────────────────────────────
def pg_connect():
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {DB_SCHEMA},public;")
    return conn

def utc_range_for_us_date(us_date: date):
    # define U.S. timezones
    pac = pytz.timezone("US/Pacific")  # adjust if you need other zones
    start_local = pac.localize(datetime.combine(us_date, time.min))  # 00:00 PDT
    end_local   = pac.localize(datetime.combine(us_date, time.max))  # 23:59:59 PDT
    return start_local.astimezone(pytz.utc), end_local.astimezone(pytz.utc)


def compute_rate(num: float, den: float) -> float:
    return round(num / den, 3) if den else 0.0


# ─── SNAPSHOT LOOK-UPS ───────────────────────────────────────────────
def get_latest_team_stats(cur, team_id: int, ts) -> Tuple[float, float] | Tuple[None, None]:
    cur.execute(
        """
        SELECT obp, slg
          FROM team_stats_snapshots
         WHERE team_id = %s AND snapshot_date < %s::date
         ORDER BY snapshot_date DESC LIMIT 1
        """,
        (team_id, ts),
    )
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def get_latest_team_allowed(cur, team_id: int, ts) -> Tuple[float, float] | Tuple[None, None]:
    cur.execute(
        """
        SELECT allowed_obp, allowed_slg
          FROM team_stats_snapshots
         WHERE team_id = %s AND snapshot_date < %s::date
         ORDER BY snapshot_date DESC LIMIT 1
        """,
        (team_id, ts),
    )
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def get_latest_pitcher_stats(cur, pid: int, ts) -> Tuple[float, float] | Tuple[None, None]:
    cur.execute(
        """
        SELECT obp_allowed, slg_allowed
          FROM pitcher_stats_snapshots
         WHERE player_id = %s AND snapshot_date < %s::date
         ORDER BY snapshot_date DESC LIMIT 1
        """,
        (pid, ts),
    )
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def _player_name(cur, pid: int | None) -> str:
    if not pid:
        return "<unknown>"
    cur.execute("SELECT full_name FROM players WHERE id = %s", (pid,))
    r = cur.fetchone()
    return r[0] if r else "<unknown>"


# ─── MLB API  (probable starters) ────────────────────────────────────
def fetch_probables(game_day: date) -> Dict[int, Dict[str, Any]]:
    """
    Query the MLB StatsAPI schedule endpoint for both game_day and
    game_day+1, then only keep games whose local Pacific date == game_day.
    Builds:
      {
        gamePk: {
          "away": {"id":..., "name":...},
          "home": {"id":..., "name":...},
        },
        ...
      }
    """
    url = "https://statsapi.mlb.com/api/v1/schedule/games/"
    probables: Dict[int, Dict[str, Dict[str, Optional[Any]]]] = {}

    # call the API twice: once for game_day and once for the next calendar day
    for d in (game_day, game_day + timedelta(days=1)):
        params = {
            "sportId": 1,
            "date":    d.isoformat(),
            "hydrate": "probablePitcher"
        }
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                # parse the full UTC timestamp the API returns
                game_dt_utc = dateutil.parser.isoparse(game["gameDate"])
                # convert to Pacific (or whichever US zone you prefer)
                game_dt_local = game_dt_utc.astimezone(PACIFIC)

                # only keep games that really belong to game_day in US local
                if game_dt_local.date() != game_day:
                    continue

                gid = game["gamePk"]
                def _starter(side: str) -> Dict[str, Any]:
                    p = game["teams"][side].get("probablePitcher")
                    if p:
                        return {"id": p.get("id"), "name": p.get("fullName")}
                    else:
                        return {"id": None, "name": None}

                probables[gid] = {
                    "away": _starter("away"),
                    "home": _starter("home"),
                }

    logger.info(
        "Fetched probable starters for %d games on %s via MLB API (US-local filtered)",
        len(probables),
        game_day,
    )
    return probables
# ─── STAFF-ALLOWED CALC ──────────────────────────────────────────────
def get_staff_allowed(
    cur,
    game_pk: int,
    side: str,                       # "away" | "home"  (whose *pitchers*?)
    ts,                              # timestamp near game time
    beg_date: date,                  # bullpen look-back floor
    probables: Dict[int, Dict[str, int]],
    assume_total_outs: int = 27,
    min_outings: int = 5,
) -> Tuple[float | None, float | None, str]:
    """
    Estimate composite OBP/SLG the staff should allow:

        • starter = probable starter from `probables`  (fallback → team numbers)
        • bullpen = every reliever whose *latest* snapshot falls in
                    [beg_date .. ts)   AND has ≥ min_outings
        • weight by average outs / outing
    """
    # find the team-id (home/away) for this game in our schedule table
    cur.execute(
        f"""
        SELECT away_team_id, home_team_id
          FROM {DB_SCHEMA}.schedule
         WHERE game_id = %s
        """,
        (game_pk,),
    )
    g = cur.fetchone()
    if not g:
        return (None, None, "<no game>")
    team_id = g[0] if side == "away" else g[1]

    # ── starter ──────────────────────────────────────────────────────
    starter_rec  = probables.get(game_pk, {}).get(side, {})
    starter_pid  = starter_rec.get("id")
    starter_name = starter_rec.get("name") or "<unknown>"

    if not starter_pid:
        # totally unknown starter → fall back to team-level allowed
        return (*get_latest_team_allowed(cur, team_id, ts), starter_name)

    s_obp, s_slg = get_latest_pitcher_stats(cur, starter_pid, ts)
    if s_obp is None:
        s_obp, s_slg = get_latest_team_allowed(cur, team_id, ts)

    # average outs / outing for this starter
    cur.execute(
        """
        SELECT cum_outs, cum_outings
          FROM pitcher_stats_snapshots
         WHERE player_id = %s
           AND snapshot_date < %s
         ORDER BY snapshot_date DESC LIMIT 1
        """,
        (starter_pid, ts),
    )
    row = cur.fetchone()
    s_avg_outs = row[0] / row[1] if row and row[1] else 18  # default ~6 IP

    # ── build bullpen pool ───────────────────────────────────────────
    cur.execute(
        """
        SELECT DISTINCT ON (sn.player_id)
               sn.player_id, sn.cum_outs, sn.cum_outings,
               sn.obp_allowed, sn.slg_allowed
          FROM pitcher_stats_snapshots sn
          JOIN players p ON p.id = sn.player_id
         WHERE p.team_id = %s
           AND sn.snapshot_date < %s
           AND sn.snapshot_date >= %s
           AND sn.player_id <> %s
           AND sn.cum_outings >= %s
         ORDER BY sn.player_id, sn.snapshot_date DESC
        """,
        (team_id, ts, beg_date, starter_pid, min_outings),
    )
    relievers = cur.fetchall()
    if not relievers:
        return (s_obp, s_slg, starter_name)

    relief_outs_total = sum(r[1] / r[2] for r in relievers)
    if not relief_outs_total:
        return (s_obp, s_slg, starter_name)

    starter_w    = min(s_avg_outs / assume_total_outs, 0.9)
    relief_share = 1.0 - starter_w

    staff_obp = starter_w * s_obp
    staff_slg = starter_w * s_slg

    for r in relievers:
        avg_outs = float(r[1]) / float(r[2])
        w = relief_share * avg_outs / relief_outs_total
        staff_obp += w * float(r[3])
        staff_slg += w * float(r[4])

    return (round(staff_obp, 3), round(staff_slg, 3), starter_name)


# ─── MAIN ANALYSIS ──────────────────────────────────────────────────
HEADER = ("game_pk", "side", "team", "starter",
          "B_OBP", "B_SLG", "P_OBP_A", "P_SLG_A", "ΔPI")


from typing import Any, Dict, List, Tuple
from datetime import date, datetime
from psycopg2.extras import DictCursor

THRESHOLDS = [0.04, 0.06, 0.08]          # AGR, BAL, CON
AGR, BAL, CON = THRESHOLDS               # just names

def analyze_day(conn, game_day: date, lookback_years: int = 3) -> None:
    cur = conn.cursor(cursor_factory=DictCursor)

    # ------------------------------------------------------------------
    # 1) probable starters from MLB Stats API
    # ------------------------------------------------------------------
    probables = fetch_probables(game_day)        # {gid: {'away': {...}, 'home': {...}}, …}

    # ------------------------------------------------------------------
    # 2) day’s schedule rows (REG only), in US local date → UTC range
   # ------------------------------------------------------------------
    start_utc, end_utc = utc_range_for_us_date(game_day)
    cur.execute(
        f"""
        SELECT game_id,
               away_team_id,
               home_team_id,
               start_time
          FROM {DB_SCHEMA}.schedule
         WHERE start_time >= %s
           AND start_time <  %s
           AND game_type = 'reg'
         ORDER BY start_time
        """,
        (start_utc, end_utc),
    )
    games = cur.fetchall()
    if not games:
        logger.error("No games found for %s", game_day)
        return

    # ------------------------------------------------------------------
    # 3) handy maps → team abbr and (optionally) full team name
    # ------------------------------------------------------------------
    cur.execute(f"SELECT id, abbreviation FROM {DB_SCHEMA}.teams")
    team_map = {r["id"]: r["abbreviation"] for r in cur.fetchall()}

    beg_date = date(game_day.year - lookback_years, 1, 1)
    rows: List[Tuple[Any, ...]] = []             # we’ll collect every side

    # ------------------------------------------------------------------
    # 4) loop through games / sides
    # ------------------------------------------------------------------
    for g in games:
        gid         = g["game_id"]
        game_time   = g["start_time"]
        time_str    = game_time.strftime("%H:%M")

        for side, tid in (("away", g["away_team_id"]),
                          ("home", g["home_team_id"])):

            # ───────────────────────── offense (this team) ────────────
            b_obp, b_slg = get_latest_team_stats(cur, tid, game_time)
            if b_obp is None:
                logger.warning("No team snapshot for team_id=%s before %s", tid, game_time)
                continue

            # ───────────────────────── defense (opp. pitching) ────────
            opp_side = "home" if side == "away" else "away"
            p_obp_a, p_slg_a, _ = get_staff_allowed(
                cur, gid, opp_side, game_time, beg_date, probables
            )
            if p_obp_a is None:
                logger.warning("No staff snapshot for game_id=%s side=%s", gid, opp_side)
                continue

            # ───────────────────────── delta & flags ──────────────────
            delta = round((b_obp + b_slg) - (p_obp_a + p_slg_a), 3)
            agr   = "Yes" if delta >= AGR else "No"
            bal   = "Yes" if delta >= BAL else "No"
            con   = "Yes" if delta >= CON else "No"

            # starting-pitcher info (may be None / None)
            starter_rec  = probables.get(gid, {}).get(side, {})
            starter_name = starter_rec.get("name") or "TBD"

            rows.append(
                (
                    gid, time_str, side.upper(),
                    team_map.get(tid, str(tid)),
                    starter_name,
                    f"{b_obp:.3f}", f"{b_slg:.3f}",
                    f"{p_obp_a:.3f}", f"{p_slg_a:.3f}",
                    f"{delta:.3f}", agr, bal, con,
                )
            )

    # ------------------------------------------------------------------
    # 5) write output to the log file instead of stdout
    # ------------------------------------------------------------------
    header = (
        "GAME_ID", "TIME", "S", "TEAM", "STARTER",
        "B_OBP", "B_SLG", "P_OBP_A", "P_SLG_A", "ΔPI",
        "AGR", "BAL", "CON"
    )
    fmt = "{:<9} {:<5} {:<1} {:<4} {:<22} {:>6} {:>6} {:>7} {:>7} {:>5} {:>3} {:>3} {:>3}"

    logger.info("")
    logger.info("=== Match-ups for %s ===", game_day)
    logger.info(fmt.format(*header))
    logger.info("-" * 101)

    # order by game-time then ΔPI descending
    rows.sort(key=lambda r: (r[1], -float(r[9])))

    for r in rows:
        logger.info(fmt.format(*r))

    logger.info("Produced %d side-rows (%d games) for %s",
                len(rows), len(rows)//2, game_day)


# ─── CLI ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Handicap MLB match-ups for a single date using OBP+SLG v allowed",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--date", required=True, help="Game date YYYY-MM-DD")
    args = parser.parse_args()

    run_day = datetime.fromisoformat(args.date).date()

    conn = pg_connect()
    try:
        analyze_day(conn, run_day)
    finally:
        conn.close()
