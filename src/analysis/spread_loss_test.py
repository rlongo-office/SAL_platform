import pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------
# 1) Load data
# ---------------------------------------------------------------
CSV = Path("../../logs/poisson_game_details_20250609_122336.csv")  # adjust if needed
df = pd.read_csv(CSV)

# ---------------------------------------------------------------
# 2) Derive helper columns
# ---------------------------------------------------------------
df["is_favorite_bet"] = df["ml_line"] < 0
df["is_over_bet"]     = df["tot_side"] == "over"
df["pred_total"]      = df["pred_home"] + df["pred_away"]
df["pred_abs_diff"]   = df["pred_diff"].abs()

# ---------------------------------------------------------------
# 3) Compute headline summary
# ---------------------------------------------------------------
summary = (
    df.agg(
        ml_win_pct     = ("ml_win",        "mean"),
        ml_roi         = ("ml_profit",     lambda s: s.sum() / 10_000),
        spread_win_pct = ("spread_win",    "mean"),
        spread_roi     = ("spread_profit", lambda s: s.sum() / 10_000),
        tot_win_pct    = ("tot_win",       "mean"),
        tot_roi        = ("tot_profit",    lambda s: s.sum() / 10_000),
    )
    .round(3)
)

# ---------------------------------------------------------------
# 4) Write summary (and any further tables) to a log file
# ---------------------------------------------------------------
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_path  = CSV.parent / f"spread_analysis_{timestamp}.log"

with open(log_path, "w", encoding="utf-8") as f:
    f.write("=== Money-line / Spread / Totals Summary ===\n")
    f.write(summary.to_string())
    f.write("\n\n")

    # --- Example: add a bin table on predicted diff vs spread win ---
    bins = pd.cut(df["pred_abs_diff"],
                  [0, 0.5, 1, 1.5, 2, 3, np.inf],
                  right=False,
                  labels=["<.5",".5-1","1-1.5","1.5-2","2-3","≥3"])
    bin_table = df.groupby(bins)["spread_win"].agg(count="size", win_rate="mean")
    f.write("=== Spread win-rate by |predicted diff| ===\n")
    f.write(bin_table.to_string(float_format="%.3f"))

print(f"Summary written to: {log_path}")
