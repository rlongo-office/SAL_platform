"""
Bankroll Monte‑Carlo simulator
------------------------------
* Spread / Over‑Under bets priced at –110  (risk 110 → win 100)
    - Base win‑prob = 0.50
    - True win‑prob = 0.50 + bettor_advantage
* Money‑lines (if enabled)
    - Base win‑prob = implied probability from posted odds
    - True win‑prob = implied_p + bettor_advantage
    (The vig is already baked into implied_p.)

Each wager risks stake_pct × current bankroll.
Logs every bet to CSV: bet # | description | rng roll | W/L | profit | bankroll
"""

import random
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import List

# ── CONFIG ────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

initial_bankroll   = 10_000.0      # starting cash
stake_pct          = 0.01          # 1 % of bankroll per bet
num_wagers         = 2_000         # bets to simulate
bettor_advantage   = 0.05          # +5 percentage‑points edge
use_moneylines     = True         # True → include money‑lines
random_seed        = None          # None → use system time
output_csv         = LOG_DIR / f"sim_results_{datetime.now():%Y%m%d_%H%M%S}.csv"
# ──────────────────────────────────────────────────────────────────────────────

MONEYLINE_ODDS = [-400, -300, -250, -200, -150, -120,
                  +100, +150, +200, +250, +300, +400]

# ---------- helper functions -------------------------------------------------
def american_to_implied_prob(odds: int) -> float:
    """Return break‑even win probability (vig included) for American odds."""
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def seed_rng(seed):
    if seed is None:
        random.seed(time.time_ns())
    else:
        random.seed(seed)


# ---------- main simulation --------------------------------------------------
def simulate() -> None:
    seed_rng(random_seed)

    bankroll = initial_bankroll
    peak     = bankroll
    track: List[float] = [bankroll]
    wins = losses = 0

    bet_menu = ["spread", "total"] if not use_moneylines else ["spread", "total", "moneyline"]

    with output_csv.open("w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["bet_number", "bet_description", "random_roll",
                         "win_loss", "profit_loss", "ending_bankroll"])

        for n in range(1, num_wagers + 1):
            if bankroll <= 0:
                print(f"Bankroll exhausted after {n-1} wagers.")
                break

            bet_type = random.choice(bet_menu)
            risk     = bankroll * stake_pct   # stake

            # -------- determine probabilities & payouts -----------------
            if bet_type in {"spread", "total"}:
                win_prob       = min(max(0.50 + bettor_advantage, 0.0), 1.0)
                profit_if_win  = risk * 100 / 110      # –110 payout
                bet_desc       = f"{bet_type} -110"

            else:  # money‑line
                odds          = random.choice(MONEYLINE_ODDS)
                implied_p     = american_to_implied_prob(odds)
                win_prob      = min(max(implied_p + bettor_advantage, 0.0), 1.0)

                profit_if_win = (risk * 100 / -odds) if odds < 0 else (risk * odds / 100)
                bet_desc      = f"moneyline {odds:+}"

            # -------- resolve wager --------------------------------------
            roll = random.random()
            if roll < win_prob:
                bankroll += profit_if_win
                profit_loss = +profit_if_win
                wl = "W"
                wins += 1
            else:
                bankroll -= risk
                profit_loss = -risk
                wl = "L"
                losses += 1

            peak = max(peak, bankroll)
            track.append(bankroll)

            writer.writerow([n, bet_desc, f"{roll:.5f}",
                             wl, f"{profit_loss:.2f}", f"{bankroll:.2f}"])

    # -------- summary --------------------------------------------------------
    total = wins + losses
    roi   = (bankroll - initial_bankroll) / initial_bankroll * 100
    hit   = wins / total * 100 if total else 0
    max_dd = (peak - min(track)) / peak * 100 if peak else 0

    print("\n===== SIMULATION SUMMARY =====")
    print(f"Bets placed              : {total}")
    print(f"Win/Loss record          : {wins}‑{losses}  ({hit:.2f} % hit rate)")
    print(f"Final bankroll           : ${bankroll:,.2f}")
    print(f"Return on initial stake  : {roi:.2f} %")
    print(f"Worst draw‑down          : {max_dd:.2f} %")
    print(f"\nDetailed log written to  : {output_csv.resolve()}\n")


if __name__ == "__main__":
    simulate()
