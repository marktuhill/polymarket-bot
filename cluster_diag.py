#!/usr/bin/env python3
"""Diagnostic ONLY: do correlated (cluster) alerts resolve worse than isolated
ones? Read-only. Imports the live monitor purely to reuse its kline fetch and
exit-resolution math so the R numbers tie out with --experiment. Does not touch
the live monitor, its state, or the CSV.

Usage:
    python cluster_diag.py [--csv crypto_range_alerts_deduped.csv] [--window 60]

A "cluster" alert has >=1 other same-direction alert (BUY vs SELL) within
+-WINDOW minutes of its timestamp; "isolated" has none. Cluster size = number of
same-direction alerts in that window including the alert itself, so the smallest
cluster is size 2 (a pair). The strict ">=2 OTHERS" (size>=3) split is also
printed for reference. Clustering is computed over ALL rows in the CSV; the
win/R columns are over the subset that actually filled (same filter as
--experiment), so cluster counts can exceed filled counts.
"""
import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime, timezone

import crypto_range_monitor as crm

CELLS = [(0.1, 1.5), (0.2, 1.0)]  # (stop xATR, target xATR) -- the two cells we track


def _parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _dir_class(direction):
    return "BUY" if "BUY" in (direction or "") else "SELL"


def _read_rows():
    if not os.path.isfile(crm.ALERTS_CSV):
        return []
    with open(crm.ALERTS_CSV, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cluster_size_map(rows, window_min):
    """(timestamp, pair, direction) -> count of same-direction alerts within
    +-window_min minutes (including self). Computed over every CSV row."""
    win = window_min * 60
    parsed = []
    for r in rows:
        try:
            t = _parse_ts(r["timestamp"]).timestamp()
        except (KeyError, ValueError):
            parsed.append(None)
            continue
        parsed.append((t, _dir_class(r["direction"])))
    sizes = {}
    for i, r in enumerate(rows):
        if parsed[i] is None:
            continue
        ti, di = parsed[i]
        n = sum(1 for p in parsed if p is not None
                and p[1] == di and abs(p[0] - ti) <= win)
        sizes[(r["timestamp"], r["pair"], r["direction"])] = n
    return sizes


def collect_trades_with_ts(client, config):
    """Verbatim copy of crm._collect_trades's fill/cooldown loop, but keeps the
    timestamp + direction it normally drops so we can join cluster membership.
    Kept identical on purpose -- if --experiment changes, mirror it here."""
    rows = _read_rows()
    rows.sort(key=lambda r: r.get("timestamp", ""))
    bar_min = 5
    fill_bars = max(1, int(config["review_fill_hours"] * 60 / bar_min))
    hold_bars = max(1, int(config["review_hold_hours"] * 60 / bar_min))
    cooldown_s = config["alert_cooldown_hours"] * 3600
    last, out = {}, []
    for row in rows:
        try:
            ts = _parse_ts(row["timestamp"])
            pair, direction = row["pair"], row["direction"]
            entry, target, atr = float(row["entry"]), float(row["target"]), float(row["atr"])
        except (KeyError, ValueError):
            continue
        key = (pair, direction)
        if key in last and ts.timestamp() - last[key] < cooldown_s:
            continue
        last[key] = ts.timestamp()
        if atr <= 0:
            continue
        is_buy = "BUY" in direction
        bars = crm._fetch_klines_from(client, pair, "5m",
                                      int(ts.timestamp() * 1000), fill_bars + hold_bars + 5)
        if not bars:
            continue
        fill_i = None
        for i, b in enumerate(bars[:fill_bars]):
            hi, lo = float(b[2]), float(b[3])
            if (is_buy and lo <= entry) or (not is_buy and hi >= entry):
                fill_i = i
                break
        if fill_i is None:
            continue
        out.append(dict(pair=pair, is_buy=is_buy, entry=entry, target=target,
                        atr=atr, regime=row.get("regime", "") or "?",
                        seg=bars[fill_i:fill_i + hold_bars],
                        ts=row["timestamp"], direction=direction))
    return out


def metrics(trades, S, T):
    """Wins/losses/total-R at one (stop, target) cell. Mirrors run_experiment:
    win-rate denominator is resolved (win+loss) trades, opens excluded."""
    w = l = opn = 0
    tr = 0.0
    for t in trades:
        is_buy, atr, entry = t["is_buy"], t["atr"], t["entry"]
        stop = entry - S * atr if is_buy else entry + S * atr
        risk = S * atr
        tgt = entry + T * atr if is_buy else entry - T * atr
        res = crm._resolve_seg(t["seg"], is_buy, stop, tgt)
        if res == "win":
            w += 1
            tr += abs(tgt - entry) / risk if risk else 0.0
        elif res == "loss":
            l += 1
            tr -= 1.0
        else:
            opn += 1
    n = w + l
    return dict(fill=len(trades), n=n, w=w, l=l, opn=opn,
                wr=(100.0 * w / n if n else 0.0),
                totalR=tr, exp=(tr / n if n else 0.0))


def _row(label, m):
    flag = "  <- n<5, too small to read" if m["fill"] < 5 else ""
    return (f"  {label:<22} fill={m['fill']:<3} resolved={m['n']:<3} "
            f"win {m['wr']:>3.0f}%  totalR {m['totalR']:>+7.2f}  "
            f"exp {m['exp']:>+.2f}R{flag}")


def main():
    ap = argparse.ArgumentParser(description="Cluster vs isolated alert diagnostic (read-only).")
    ap.add_argument("--csv", default="crypto_range_alerts_deduped.csv",
                    help="Alerts CSV to analyse (default: crypto_range_alerts_deduped.csv).")
    ap.add_argument("--window", type=int, default=60,
                    help="Cluster window in minutes, +-this around each alert (default 60).")
    args = ap.parse_args()

    crm.setup_logging() if hasattr(crm, "setup_logging") else None
    crm.load_env_file(crm.ENV_FILE)
    crm.ALERTS_CSV = os.path.abspath(args.csv)
    config = crm.get_config()
    client = crm.BinanceClient(config["binance_base_url"], config["proxy"])

    all_rows = _read_rows()
    sizes = cluster_size_map(all_rows, args.window)

    print(f"\nCSV: {crm.ALERTS_CSV}")
    print(f"Cluster window: +-{args.window} min, same direction. "
          f"cluster = size>=2 (>=1 other nearby); isolated = size 1.")
    print("Clustering is over ALL rows; win/R is over the FILLED subset "
          "(same fill+cooldown filter as --experiment).")
    print("WARNING: tiny sample -> every pattern below is a hypothesis, not proof.")

    # ---- classification over ALL csv rows (the firing stream) ----
    csv_clu = csv_iso = 0
    csv_dir = defaultdict(lambda: [0, 0])  # dirclass -> [cluster, isolated]
    for r in all_rows:
        k = (r["timestamp"], r["pair"], r["direction"])
        if k not in sizes:
            continue
        dc = _dir_class(r["direction"])
        if sizes[k] >= 2:
            csv_clu += 1
            csv_dir[dc][0] += 1
        else:
            csv_iso += 1
            csv_dir[dc][1] += 1
    tot = csv_clu + csv_iso

    print("\n--- CLUSTER vs ISOLATED (all CSV rows) ---")
    if tot:
        print(f"  cluster  {csv_clu:>3}  ({100.0*csv_clu/tot:>4.0f}%)")
        print(f"  isolated {csv_iso:>3}  ({100.0*csv_iso/tot:>4.0f}%)")
        for dc in ("BUY", "SELL"):
            c, i = csv_dir[dc]
            print(f"    {dc:<4} cluster {c:>3}  isolated {i:>3}")
    else:
        print("  (no parseable rows)")

    # ---- attach cluster size to filled trades ----
    trades = collect_trades_with_ts(client, config)
    for t in trades:
        t["csize"] = sizes.get((t["ts"], t["pair"], t["direction"]), 1)
        t["cluster"] = t["csize"] >= 2

    nbuy = sum(1 for t in trades if t["is_buy"])
    print(f"\nFilled, deduped trades: {len(trades)} ({nbuy} buy / {len(trades)-nbuy} sell).")
    fclu = [t for t in trades if t["cluster"]]
    fiso = [t for t in trades if not t["cluster"]]
    print(f"  of which cluster={len(fclu)}  isolated={len(fiso)}")

    # ---- performance: cluster vs isolated, at each cell ----
    for (S, T) in CELLS:
        print(f"\n--- PERFORMANCE @ {S}/{T} (stop {S}xATR / target {T}xATR) ---")
        print(_row("cluster", metrics(fclu, S, T)))
        print(_row("isolated", metrics(fiso, S, T)))
        print("  by direction:")
        for dc, isbuy in (("BUY", True), ("SELL", False)):
            print(_row(f"cluster {dc}", metrics([t for t in fclu if t["is_buy"] == isbuy], S, T)))
            print(_row(f"isolated {dc}", metrics([t for t in fiso if t["is_buy"] == isbuy], S, T)))
        # strict alternative threshold (>=2 others == size>=3) for reference
        strict_clu = [t for t in trades if t["csize"] >= 3]
        strict_iso = [t for t in trades if t["csize"] < 3]
        print(f"  [ref] strict split (cluster = size>=3, i.e. >=2 others):")
        print(_row("strict cluster", metrics(strict_clu, S, T)))
        print(_row("strict isolated", metrics(strict_iso, S, T)))

    # ---- cluster size sensitivity ----
    def bucket(sz):
        return "5+" if sz >= 5 else str(sz)

    order = ["1", "2", "3", "4", "5+"]
    for (S, T) in CELLS:
        print(f"\n--- CLUSTER SIZE SENSITIVITY @ {S}/{T} ---")
        by = defaultdict(list)
        for t in trades:
            by[bucket(t["csize"])].append(t)
        for b in order:
            sub = by.get(b, [])
            label = f"size {b}" + (" (isolated)" if b == "1" else "")
            if not sub:
                print(f"  {label:<22} (none)")
            else:
                print(_row(label, metrics(sub, S, T)))
        # monotonic check on expectancy across populated buckets >=2
        seq = [(b, metrics(by[b], S, T)["exp"]) for b in order[1:] if by.get(b)]
        if len(seq) >= 2:
            exps = [e for _, e in seq]
            mono_down = all(exps[i] >= exps[i+1] for i in range(len(exps)-1))
            mono_up = all(exps[i] <= exps[i+1] for i in range(len(exps)-1))
            shape = ("monotonic down (bigger cluster = worse)" if mono_down
                     else "monotonic up (bigger cluster = better)" if mono_up
                     else "no monotonic pattern")
            print(f"  exp by size {[f'{b}:{e:+.2f}' for b, e in seq]} -> {shape}")

    # ---- time-of-day histogram (UTC), cluster vs isolated, filled trades ----
    print("\n--- TIME-OF-DAY (UTC hour, filled trades) ---")
    hist = defaultdict(lambda: [0, 0])  # hour -> [cluster, isolated]
    for t in trades:
        try:
            h = _parse_ts(t["ts"]).hour
        except ValueError:
            continue
        hist[h][1 - int(t["cluster"])] += 1  # idx0=cluster, idx1=isolated
    print("  hour |  clu  iso  | bar (#=cluster .=isolated)")
    for h in range(24):
        c, i = hist[h]
        if c == 0 and i == 0:
            continue
        bar = "#" * c + "." * i
        print(f"   {h:>2}  | {c:>4} {i:>4}  | {bar}")
    # crude overnight (00-08 UTC) vs rest concentration
    on_c = sum(hist[h][0] for h in range(0, 8))
    on_i = sum(hist[h][1] for h in range(0, 8))
    print(f"  00-08 UTC: cluster {on_c}, isolated {on_i}  "
          f"(of cluster {len(fclu)}, isolated {len(fiso)} total)")

    print("\nDiagnostic only. No recommendations, no live changes made.")


if __name__ == "__main__":
    main()
