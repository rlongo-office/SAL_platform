#!/usr/bin/env python3
import os
import json
import glob

# ─── CONFIG ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))
ODDS_DIR    = os.path.join(PROJECT_DIR, "output", "mlb")
ODDS_GLOB   = os.path.join(ODDS_DIR, "historical_mlb_*.json")

# ─── YOUR 18 MISSING IDS ─────────────────────────────────────────────────────────
MISSING = {
    "0761d558b9bd0c01ee6af1e5f48c07cb",
    "0869f7d26c5a346844a614538af0fc99",
    "136295e92bb9e259eaca4748d941a568",
    "15887187d59ee45403630947e2cb2977",
    "203105022faca779ca816069dc659f06",
    "311bc23ddfbd2c91bc4c852c23c8b5d1",
    "649ad82dac37a64e347cdbb34c7d73e7",
    "953ec62ef6350914f20ca4527e55ceeb",
    "97cb40d67828df4232f2c2aaa2ccb67f",
    "9cbf87bba0ce6052ea37a12f36806423",
    "a8eb78abb9402eabd2edf5e19522382b",
    "af6cbf4622faf260343233f0df65eaea",
    "b496740e94bb0a80eb3fd346307c2d83",
    "d68003f9f81f3f7943ad319cd5cfc2bb",
    "e733003124020d282d3b699e1ceb1857",
    "e75516c0e8e178a345913ab089287d7d",
    "ecca476c39cadcf531a1fd82a546af56",
    "f9057adbf0d713420e591e5dc96617fd",
}

print(f"Scanning odds files in {ODDS_DIR} for missing IDs…\n")

for odds_path in sorted(glob.glob(ODDS_GLOB)):
    # historical_mlb_YYYY-MM-DD.json → YYYY-MM-DD
    date_str = os.path.basename(odds_path).split("_")[-1].replace(".json", "")
    with open(odds_path, "r", encoding="utf-8") as f:
        odds = json.load(f)
    for snapshot_ts, snapshot in odds.items():
        for rec in snapshot.get("data", []):
            rec_id = rec.get("id")
            if rec_id in MISSING:
                print(f"→ {rec_id}  (file-date: {date_str}, snapshot: {snapshot_ts})")
                print(f"     home_team      = {rec.get('home_team')}")
                print(f"     away_team      = {rec.get('away_team')}")
                print(f"     commence_time  = {rec.get('commence_time')}")
                print(f"     ——————————————————————\n")
