#!/usr/bin/env python3
"""One-off cleanup: read crypto_range_alerts.csv, apply the same hard dedup
rule used by the live monitor (suppress an alert if a prior kept alert for the
same pair+direction within 48h had entry/stop/target all within +-0.5*ATR of
the new one), and write crypto_range_alerts_deduped.csv with only unique trades.

Prints before/after counts and every removed row with the kept row it matched."""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SCRIPT_DIR, "crypto_range_alerts.csv")
DST = os.path.join(SCRIPT_DIR, "crypto_range_alerts_deduped.csv")
WINDOW = timedelta(hours=48)
TOL_ATR_MULT = 0.5


def _parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _parse_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def main():
    src = SRC if len(sys.argv) < 2 else sys.argv[1]
    dst = DST if len(sys.argv) < 3 else sys.argv[2]
    if not os.path.isfile(src):
        print(f"ERROR: {src} not found", file=sys.stderr)
        sys.exit(1)

    with open(src, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        rows = list(reader)

    rows.sort(key=lambda r: _parse_ts(r["timestamp"]))

    kept = []
    removed = []
    for row in rows:
        try:
            ts = _parse_ts(row["timestamp"])
        except (KeyError, ValueError):
            kept.append(row)
            continue
        atr = _parse_float(row.get("atr"))
        entry = _parse_float(row.get("entry"))
        stop = _parse_float(row.get("stop"))
        target = _parse_float(row.get("target"))
        if atr is None or atr <= 0 or entry is None or stop is None or target is None:
            kept.append(row)
            continue
        tol = TOL_ATR_MULT * atr
        match = None
        for prior in kept:
            if (prior.get("pair") != row.get("pair")
                    or prior.get("direction") != row.get("direction")):
                continue
            try:
                p_ts = _parse_ts(prior["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts - p_ts > WINDOW:
                continue
            p_entry = _parse_float(prior.get("entry"))
            p_stop = _parse_float(prior.get("stop"))
            p_target = _parse_float(prior.get("target"))
            if (p_entry is not None and p_stop is not None and p_target is not None
                    and abs(p_entry - entry) <= tol
                    and abs(p_stop - stop) <= tol
                    and abs(p_target - target) <= tol):
                match = prior
                break
        if match is not None:
            removed.append((row, match))
        else:
            kept.append(row)

    with open(dst, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in kept:
            writer.writerow(row)

    print(f"Original row count: {len(rows)}")
    print(f"Deduped row count:  {len(kept)}")
    print(f"Duplicates removed: {len(removed)}")
    print(f"Wrote: {dst}")
    if removed:
        print()
        print("Removed rows (duplicate of an earlier kept row within 48h "
              "with entry/stop/target all within +-0.5*ATR):")
        print("-" * 110)
        print(f"{'timestamp':<20}  {'pair':<14}  {'direction':<19}  "
              f"{'entry':>12}  {'stop':>12}  {'target':>12}  -> duplicate of")
        print("-" * 110)
        for row, dup in removed:
            print(f"{row['timestamp']:<20}  {row['pair']:<14}  {row['direction']:<19}  "
                  f"{row['entry']:>12}  {row['stop']:>12}  {row['target']:>12}  "
                  f"-> {dup['timestamp']}")


if __name__ == "__main__":
    main()
