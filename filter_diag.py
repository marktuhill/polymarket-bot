#!/usr/bin/env python3
"""
Pre-registered filter diagnostic for the range monitor.

REGISTERED 2026-06-10 -- before forward data accumulates. Parameters are FIXED.
No sweeps, no tuning. Predictions stated in advance:

  F1 (rejection confirmation): trades with a confirmed 1H rejection candle at
    the zone outperform unconfirmed trades.
  F2 (touch count): win rate decays as zone touch count at alert time increases
    (3rd test > 4th > 5th+).
  F3 (HTF trend alignment): trend-aligned trades (BUY when price above D1
    EMA50, SELL when below) outperform counter-trend trades.

AMENDED 2026-06-10: F1 stop geometry and F2 touch counting were measurement
bugs -- definitions corrected before any forward data accumulated. Predictions
unchanged.
  F1 fix: confirmed trades now use IDENTICAL stop/target geometry to
    unconfirmed (anchored to the original zone-boundary entry). Only the fill
    price changes (open of the bar after the rejection candle). Report both
    (i) win-rate at the shared stop/target, and (ii) realized expectancy at the
    actual fill, so the A/B is purely about whether rejection predicts
    resolution -- not about whether a tiny stop near support survives.
  F2 fix: replace the naive per-bar tally with the LIVE detector's
    _genuine_touches over swing pivots (cluster tol = zone_atr_mult * ATR), so a
    multi-bar visit counts as one touch, matching the production semantics.

AMENDED 2026-06-10 (F3 / regime): F3 prediction (aligned > counter) is retained
  and on track for rejection. NEW hypothesis F3' registered before forward
  data: counter-trend trades (BUY below D1 EMA50, SELL above D1 EMA50)
  outperform aligned trades. F3' must hold on forward data at the next two
  checkpoints to be accepted. The short-regime tag is retired as an independent
  signal (83% overlap with F3-aligned).

Acceptance rule: a filter is only accepted if its predicted direction holds on
FORWARD data (trades timestamped after 2026-06-10) at the next two checkpoints.
Results on the existing trades are IN-SAMPLE and hypothesis-generating only.
Every output is labelled IN-SAMPLE or FORWARD.

Read-only. Does not modify the live monitor, its state, the CSV, or the
experiment code. Reuses the live kline fetch and exit-resolution math so the R
numbers tie out with --experiment.

Usage:
    python filter_diag.py                # IN-SAMPLE (every collected trade)
    python filter_diag.py --forward      # FORWARD only (timestamp > 2026-06-10)
    python filter_diag.py --csv X.csv    # alternate alerts CSV
"""
import argparse
import csv
import os
from datetime import datetime, timezone

import crypto_range_monitor as crm

# ---- pre-registered constants (DO NOT TUNE) --------------------------------
REGISTRATION_DATE = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
CELLS = [(0.1, 1.5), (0.2, 1.0)]

# F1 -- 1H rejection candle
F1_REJECT_WINDOW_BARS_1H = 12
F1_PENETRATION_ATR = 0.25
F1_WICK_FRACTION = 0.5

# F2 -- touch count at alert (4H lookback)
F2_LOOKBACK_BARS_4H = 120
F2_TOUCH_TOL_ATR = 0.5

# F3 -- HTF trend
F3_EMA_PERIOD = 50

MS_1H = 60 * 60 * 1000
MS_4H = 4 * 60 * 60 * 1000
MS_1D = 24 * 60 * 60 * 1000


def _parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _read_rows():
    if not os.path.isfile(crm.ALERTS_CSV):
        return []
    with open(crm.ALERTS_CSV, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def collect_trades_with_meta(client, config, cutoff_ts=None):
    """Mirror crm._collect_trades' fill/cooldown loop verbatim, but keep the
    timestamp, direction, and zone bounds so the filters have what they need.
    Optionally restrict to trades after cutoff_ts (UTC datetime)."""
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
            entry = float(row["entry"])
            target = float(row["target"])
            atr = float(row["atr"])
        except (KeyError, ValueError):
            continue
        # Cooldown is applied BEFORE the cutoff filter so an excluded pre-cutoff
        # trade still suppresses an immediately-following duplicate, matching
        # the live dedup semantics.
        key = (pair, direction)
        if key in last and ts.timestamp() - last[key] < cooldown_s:
            continue
        last[key] = ts.timestamp()
        if atr <= 0:
            continue
        if cutoff_ts is not None and ts <= cutoff_ts:
            continue
        is_buy = "BUY" in direction
        ts_ms = int(ts.timestamp() * 1000)
        bars = crm._fetch_klines_from(client, pair, "5m", ts_ms, fill_bars + hold_bars + 5)
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
        try:
            zone_low = float(row.get("zone_low") or "nan")
            zone_high = float(row.get("zone_high") or "nan")
        except ValueError:
            zone_low = zone_high = float("nan")
        # Level being defended: support for BUY, resistance for SELL.
        level = zone_low if is_buy else zone_high
        out.append(dict(
            pair=pair, is_buy=is_buy, entry=entry, target=target, atr=atr,
            regime=row.get("regime", "") or "?",
            seg=bars[fill_i:fill_i + hold_bars],
            ts=row["timestamp"], ts_ms=ts_ms, direction=direction,
            level=level, hold_bars=hold_bars,
        ))
    return out


# ---- F1: 1H rejection candle ----------------------------------------------
def f1_check_rejection(client, t):
    """Return (confirmed_bool, new_entry, new_seg). Confirmed iff a rejection
    candle is found within F1_REJECT_WINDOW_BARS_1H 1H bars after the alert."""
    pair, is_buy, level = t["pair"], t["is_buy"], t["level"]
    atr, hold_bars, ts_ms = t["atr"], t["hold_bars"], t["ts_ms"]
    if level != level:  # nan guard
        return False, None, None
    hbars = crm._fetch_klines_from(client, pair, "1h", ts_ms,
                                   F1_REJECT_WINDOW_BARS_1H + 1)
    if not hbars:
        return False, None, None
    for i, b in enumerate(hbars[:F1_REJECT_WINDOW_BARS_1H]):
        o, hi, lo, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        rng = hi - lo
        if rng <= 0:
            continue
        if is_buy:
            lower_wick = min(o, c) - lo
            ok = (lo <= level + F1_PENETRATION_ATR * atr
                  and c > level
                  and lower_wick >= F1_WICK_FRACTION * rng)
        else:
            upper_wick = hi - max(o, c)
            ok = (hi >= level - F1_PENETRATION_ATR * atr
                  and c < level
                  and upper_wick >= F1_WICK_FRACTION * rng)
        if not ok:
            continue
        # Entry = open of the candle FOLLOWING the rejection candle.
        if i + 1 < len(hbars):
            next_bar = hbars[i + 1]
        else:
            more = crm._fetch_klines_from(client, pair, "1h",
                                          int(b[0]) + MS_1H, 1)
            if not more:
                return False, None, None
            next_bar = more[0]
        new_entry = float(next_bar[1])
        new_start_ms = int(next_bar[0])
        seg = crm._fetch_klines_from(client, pair, "5m",
                                     new_start_ms, hold_bars + 5)
        if not seg:
            return False, None, None
        return True, new_entry, seg[:hold_bars]
    return False, None, None


# ---- F2: touch count at alert ---------------------------------------------
# AMENDED 2026-06-10: replace per-bar count with the live detector's swing-
# cluster touch counter. Reuse crm.find_swings + crm._genuine_touches verbatim.
def f2_count_touches(client, t, config):
    """Genuine touches of the traded zone (support for BUY, resistance for SELL)
    in the F2_LOOKBACK_BARS_4H window before the alert, using the LIVE detector
    (crm.find_swings + crm._genuine_touches). Returns None if history short."""
    pair, is_buy = t["pair"], t["is_buy"]
    level, atr, ts_ms = t["level"], t["atr"], t["ts_ms"]
    if level != level:
        return None
    start_ms = ts_ms - F2_LOOKBACK_BARS_4H * MS_4H
    raw = crm._fetch_klines_from(client, pair, "4h",
                                 start_ms, F2_LOOKBACK_BARS_4H + 5)
    if not raw:
        return None
    # Trim to bars strictly before the alert and convert to live's (h,l,c) format
    # (matches BinanceClient.get_klines output that detect_range consumes).
    bars = []
    for b in raw:
        if int(b[0]) >= ts_ms:
            break
        try:
            bars.append((float(b[2]), float(b[3]), float(b[4])))
        except (IndexError, TypeError, ValueError):
            continue
    if len(bars) < 2 * config["swing_strength"] + 1:
        return None
    # Touch tolerance matches the live zone tolerance, NOT F2_TOUCH_TOL_ATR --
    # use whatever config["zone_atr_mult"] says, so cluster + walk-off semantics
    # are identical to detect_range. F2_TOUCH_TOL_ATR is retained as a pre-reg
    # constant on the assumption it matched; in practice both are 0.5xATR.
    tol = config["zone_atr_mult"] * atr
    kind = "low" if is_buy else "high"
    pivots = crm.find_swings(bars, config["swing_strength"], kind)
    if not pivots:
        return 0
    # Find the swing cluster whose mean is closest to the alert's actual zone
    # level (so we count touches of THIS support/resistance, not some other one).
    clusters = crm.cluster_levels(pivots, tol)
    if not clusters:
        return 0
    near = [c for c in clusters if abs(crm._cluster_mean(c) - level) <= tol]
    target_cluster = (min(near, key=lambda c: abs(crm._cluster_mean(c) - level))
                      if near else
                      min(clusters, key=lambda c: abs(crm._cluster_mean(c) - level)))
    return crm._genuine_touches(target_cluster, bars, tol, kind)


# ---- F3: HTF trend (D1 EMA50) ---------------------------------------------
def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period  # SMA seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def f3_trend_alignment(client, t):
    """Return 'aligned' / 'counter' / None. Uses the last fully-closed daily
    bar before the alert and the trailing EMA50 from there."""
    pair, is_buy, ts_ms = t["pair"], t["is_buy"], t["ts_ms"]
    history = 200
    start_ms = ts_ms - history * MS_1D
    bars = crm._fetch_klines_from(client, pair, "1d", start_ms, history + 5)
    if not bars:
        return None
    closed = [b for b in bars if int(b[0]) + MS_1D <= ts_ms]
    closes = [float(b[4]) for b in closed]
    if len(closes) < F3_EMA_PERIOD:
        return None
    ema = _ema(closes, F3_EMA_PERIOD)
    if ema is None:
        return None
    last_close = closes[-1]
    if is_buy:
        return "aligned" if last_close > ema else "counter"
    return "aligned" if last_close < ema else "counter"


# ---- metrics ---------------------------------------------------------------
def metrics(trades, S, T, entry_key="entry", seg_key="seg", anchor_key=None):
    """Win/loss/R count at one (stop, target) cell. Mirrors run_experiment:
    expectancy denominator is resolved (win+loss); opens excluded.

    `anchor_key` (optional): when set, stop and target are anchored to
    t[anchor_key] (the ORIGINAL zone-boundary entry), while the segment used
    for resolution and the fill price come from `entry_key`/`seg_key`. This is
    the F1 fix: identical stop/target geometry for confirmed vs unconfirmed,
    so the A/B isolates the filter from stop placement. Realized R per win is
    measured from the actual fill price (entry_key), so a confirmed trade that
    fills above support harvests fewer R to the same target -- that's the cost
    of waiting for the rejection, and we want to see it."""
    w = l = opn = 0
    tr = 0.0
    for t in trades:
        is_buy, atr = t["is_buy"], t["atr"]
        entry, seg = t[entry_key], t[seg_key]
        anchor = t[anchor_key] if anchor_key else entry
        stop = anchor - S * atr if is_buy else anchor + S * atr
        risk = S * atr
        tgt = anchor + T * atr if is_buy else anchor - T * atr
        res = crm._resolve_seg(seg, is_buy, stop, tgt)
        if res == "win":
            w += 1
            # Realized R = distance from actual fill to target / risk (1R).
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
    return (f"  {label:<32} fill={m['fill']:<3} resolved={m['n']:<3} "
            f"win {m['wr']:>3.0f}%  totalR {m['totalR']:>+7.2f}  "
            f"exp {m['exp']:>+.2f}R{flag}")


# ---- main ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Pre-registered filter diagnostic (read-only, no live changes).")
    ap.add_argument("--csv", default="crypto_range_alerts_deduped.csv",
                    help="Alerts CSV (default: crypto_range_alerts_deduped.csv).")
    ap.add_argument("--forward", action="store_true",
                    help="Restrict to trades after 2026-06-10 (acceptance sample).")
    args = ap.parse_args()

    if hasattr(crm, "setup_logging"):
        crm.setup_logging()
    crm.load_env_file(crm.ENV_FILE)
    crm.ALERTS_CSV = os.path.abspath(args.csv)
    config = crm.get_config()
    client = crm.BinanceClient(config["binance_base_url"], config["proxy"])

    sample = "FORWARD" if args.forward else "IN-SAMPLE"
    cutoff = REGISTRATION_DATE if args.forward else None

    print(f"\nCSV: {crm.ALERTS_CSV}")
    if args.forward:
        print(f"Sample: FORWARD (trades after {REGISTRATION_DATE:%Y-%m-%d %H:%M UTC})")
    else:
        print("Sample: IN-SAMPLE (all collected trades; hypothesis-generating only)")
    print(f"Registered: {REGISTRATION_DATE:%Y-%m-%d}.  Parameters FIXED at registration.")
    print("Predictions on record:")
    print("  F1: confirmed > unconfirmed")
    print("  F2: win rate decays as touches increase (3 > 4 > 5+)")
    print("  F3: aligned > counter-trend")
    print("Acceptance: a filter passes only if its predicted direction holds")
    print("on FORWARD samples at the next two checkpoints.")
    print("WARNING: tiny sample -> every number below is a hypothesis, not proof.")

    trades = collect_trades_with_meta(client, config, cutoff_ts=cutoff)
    nbuy = sum(1 for t in trades if t["is_buy"])
    print(f"\nTrade universe [{sample}]: {len(trades)} "
          f"({nbuy} buy / {len(trades)-nbuy} sell).")
    if not trades:
        print("No trades to analyse.")
        return

    # ----------------------- F1 -----------------------
    # AMENDED 2026-06-10: confirmed trades now use IDENTICAL stop/target
    # geometry (anchored to the original zone-boundary entry). Only the fill
    # price differs. Report (i) win-rate at shared stop/target, then (ii)
    # realized expectancy (uses actual fill, so shorter R-to-target on confirmed
    # wins is captured as the cost of waiting for confirmation).
    print(f"\n=== F1 (1H rejection confirmation) [{sample}] ===")
    print(f"  window={F1_REJECT_WINDOW_BARS_1H}x1H  "
          f"penetration<={F1_PENETRATION_ATR}xATR  "
          f"wick>={F1_WICK_FRACTION*100:.0f}% of range")
    print("  geometry: confirmed/unconfirmed share stop/target anchored to "
          "original entry; confirmed fill = open after rejection candle.")
    confirmed, unconfirmed = [], []
    for t in trades:
        ok, ne, ns = f1_check_rejection(client, t)
        # Store the original entry so confirmed trades can anchor stop/target
        # to it (identical geometry) even when the fill is the post-rejection
        # bar's open.
        t["orig_entry"] = t["entry"]
        if ok:
            t["f1_entry"], t["f1_seg"] = ne, ns
            confirmed.append(t)
        else:
            unconfirmed.append(t)
    print(f"  classified: confirmed={len(confirmed)}  "
          f"unconfirmed={len(unconfirmed)}")
    for (S, T) in CELLS:
        print(f"  @ {S}/{T}:")
        mc = metrics(confirmed, S, T, entry_key="f1_entry",
                     seg_key="f1_seg", anchor_key="orig_entry")
        mu = metrics(unconfirmed, S, T)
        print(_row(f"confirmed [{sample}]", mc))
        print(_row(f"unconfirmed [{sample}]", mu))
        print(f"  by direction @ {S}/{T}:")
        for dc, isbuy in (("BUY", True), ("SELL", False)):
            mc = metrics([t for t in confirmed if t["is_buy"] == isbuy],
                         S, T, entry_key="f1_entry",
                         seg_key="f1_seg", anchor_key="orig_entry")
            mu = metrics([t for t in unconfirmed if t["is_buy"] == isbuy], S, T)
            print(_row(f"confirmed {dc}", mc))
            print(_row(f"unconfirmed {dc}", mu))

    # ----------------------- F2 -----------------------
    # AMENDED 2026-06-10: reuse the live detector's swing-cluster touch counter
    # (crm._genuine_touches) instead of a per-bar tally, so a multi-bar visit
    # counts as one touch -- matches production semantics.
    print(f"\n=== F2 (touch count at alert) [{sample}] ===")
    print(f"  lookback={F2_LOOKBACK_BARS_4H}x4H  "
          f"tol={config['zone_atr_mult']}xATR (live zone tol)  "
          f"swing_strength={config['swing_strength']}")
    missing = 0
    for t in trades:
        tc = f2_count_touches(client, t, config)
        t["touches"] = tc
        if tc is None:
            missing += 1
    if missing:
        print(f"  (touch count unavailable for {missing} trade(s); 4H history short)")
    b2 = [t for t in trades if t["touches"] is not None and t["touches"] <= 2]
    b3 = [t for t in trades if t["touches"] == 3]
    b4 = [t for t in trades if t["touches"] is not None and t["touches"] >= 4]
    print(f"  buckets: touches<=2 n={len(b2)}  touches=3 n={len(b3)}  "
          f"touches>=4 n={len(b4)}")
    # Raw distribution under the CURRENT (per-bar) touch definition. Diagnostic
    # only -- helps tell whether the buckets need redefining or just a different
    # touch counter (e.g. swing-cluster as in the live detector).
    raw = [t["touches"] for t in trades if t["touches"] is not None]
    if raw:
        hist = {}
        for c in raw:
            hist[c] = hist.get(c, 0) + 1
        print("  raw touch-count distribution (current definition):")
        for c in sorted(hist):
            print(f"    {c:>3} |  {'#'*hist[c]} ({hist[c]})")
    for (S, T) in CELLS:
        print(f"  @ {S}/{T}:")
        print(_row(f"touches<=2 [{sample}]", metrics(b2, S, T)))
        print(_row(f"touches=3 [{sample}]", metrics(b3, S, T)))
        print(_row(f"touches>=4 [{sample}]", metrics(b4, S, T)))

    # ----------------------- F3 -----------------------
    print(f"\n=== F3 (HTF trend, D1 EMA{F3_EMA_PERIOD}) [{sample}] ===")
    aligned, counter, miss = [], [], 0
    for t in trades:
        a = f3_trend_alignment(client, t)
        t["f3"] = a
        if a == "aligned":
            aligned.append(t)
        elif a == "counter":
            counter.append(t)
        else:
            miss += 1
    if miss:
        print(f"  (trend unavailable for {miss} trade(s); insufficient daily history)")
    print(f"  classified: aligned={len(aligned)}  counter={len(counter)}")
    for (S, T) in CELLS:
        print(f"  @ {S}/{T}:")
        print(_row(f"aligned [{sample}]", metrics(aligned, S, T)))
        print(_row(f"counter [{sample}]", metrics(counter, S, T)))
        print(f"  by direction @ {S}/{T}:")
        for dc, isbuy in (("BUY", True), ("SELL", False)):
            print(_row(f"aligned {dc}",
                       metrics([t for t in aligned if t["is_buy"] == isbuy], S, T)))
            print(_row(f"counter {dc}",
                       metrics([t for t in counter if t["is_buy"] == isbuy], S, T)))
    # Overlap of F3 with the existing "short" regime tag.
    short_regime = [t for t in trades if t["regime"] == "short"]
    if short_regime:
        sells_in_short = sum(1 for t in short_regime if not t["is_buy"])
        f3_aligned_in_short = sum(1 for t in short_regime if t.get("f3") == "aligned")
        pct = 100.0 * f3_aligned_in_short / len(short_regime)
        print(f"\n  Overlap (regime vs F3): of {len(short_regime)} 'short'-regime "
              f"trades ({sells_in_short} SELL), {f3_aligned_in_short} ({pct:.0f}%) "
              f"are F3-aligned.")
    else:
        print("\n  Overlap (regime vs F3): no 'short'-regime trades in this sample.")

    print(f"\nDiagnostic only [{sample}]. No recommendations, no live changes made.")


if __name__ == "__main__":
    main()
