# Walk-Forward Tuned Triplet — Project Summary

## What this is

A three-leg quantitative trading strategy combining trend and mean reversion across crypto and gold, with parameter selection driven by walk-forward backtesting, deployed for paper trading on a $100k demo account at IC Markets via MetaTrader 5.

## The strategy

Three mechanically uncorrelated legs, equal-weighted (1/3 each), rebalanced daily:

| Leg | Type | Asset | Logic |
|---|---|---|---|
| 1 | EMA crossover (10 / 20) | BTC | Long when fast EMA ≥ slow EMA, short when below |
| 2 | Bollinger Band mean reversion (20-period, ±2σ) + 1.5×ATR stop | LTC | Long when close pierces lower band, short upper band, exit at middle band or stop |
| 3 | Long-only EMA crossover (50 / 100) | XAU | Long when fast ≥ slow, flat otherwise |

Pairwise return correlation across the three legs: **-0.04** (effectively zero). Trend on crypto, mean-reversion on an alt-coin, trend on gold — three different mechanical exposures that historically take turns carrying the equity curve.

A risk-control overlay: each leg's rolling 6-month Sharpe is checked monthly; legs with non-positive Sharpe are *dropped* until they recover. In the 2022 crypto winter this correctly muted the XAU leg until gold started trending again in late 2023, where it then contributed +14% in a single 6-month window.

## Why this combination

The honest answer is comparative testing against the dumbest possible alternatives. Over 2020-2025, vol-scaled to 10% annualised:

| Candidate | Sharpe | Ann return | Max DD |
|---|---|---|---|
| BH 50/50 BTC + XAU | 0.95 | +9.4% | -17.9% |
| Walk-forward tuned triplet | **1.64** | +10.4% | **-6.8%** |
| Default static triplet (no tuning) | 1.69 | +9.6% | -5.3% |

The strategy doesn't beat buy-and-hold on return. It earns its keep through **smoothness** — same return as 50/50 BTC+gold, but with a third of the drawdown. In a coordinated crypto + gold selloff that doesn't exist in this 6-year sample, the strategy's leg-dropping mechanism should fare materially better than passive holding.

Interesting (and disappointing) finding: **parameter tuning added effectively no value** — the default-params triplet has slightly *better* Sharpe than the walk-forward-tuned version. The legs being uncorrelated does all the work; tuning just adds turnover and friction.

## Honest caveats

- **Sample bias**: 2020-2025 includes a once-in-a-decade trend in both crypto AND gold simultaneously. Forward regime may be very different.
- **The BBevent/LTC leg is the only real edge**. It turns a Sharpe-0.56 LTC hold into Sharpe-1.00. The trend legs (EMACross/BTC, EMALong/XAU) actually *underperform* buy-and-hold of those underlyings — they're in the portfolio for diversification, not alpha.
- **Live frictions the backtest doesn't model**:
  - MT5 crypto CFD spreads: 30-100 bps vs ~10 bps on Binance spot
  - Overnight financing on LTC shorts: 20-100 bps/day
  - XAU long swap: ~-4% annualised
  - Commission: ~6 bps round-trip on Raw Spread
- **Realistic forward expectation: 3-7% annualised at Sharpe 1.0-1.4**, vs the backtest's +10.4% at 1.64 — frictions eat ~4-7% per year.
- **CFDs are not real coins** — no self-custody. Matters for size or if that's a value.

## Sizing — deliberately tiny for paper

Conservative for paper validation on a $100k demo:

- Per-leg notional cap: **2% of total equity** = $2,000 max per leg
- Max gross exposure: **6% of equity** = $6,000 across all three
- BBevent per-trade risk: 0.5% of leg equity ≈ $167 max loss per trade
- Kill switch: -20% portfolio drawdown forces flat halt
- Expected portfolio vol: ~2.8% annualised
- Expected return at these limits: **3-5% annually**

This is intentionally small. Goal of paper trading is to prove the bot does what the backtest predicts before scaling up; not to maximise return.

## Deployment

Two options shipped, both targeting IC Markets MT5 with symbols `BTCUSD`, `LTCUSD`, `XAUUSD`:

**(a) Native MT5 Expert Advisor** (`mql5/WFTunedTriplet.mq5`) — single .mq5 file, runs entirely inside MetaTrader 5, no Python install required on the VPS. Compiles in MetaEditor, attaches to any chart, triggered once daily on a timer.

**(b) Python bot + MT5 adapter** (`scripts/paper_bot.py --broker mt5`) — Python computes signals, sends orders via the `MetaTrader5` Python package. Runs from cron / Task Scheduler. Useful if you want to keep the same code that runs the backtests as the source of truth.

Both share: magic number 19850528 (so they coexist with manual trades), daily-bar trigger, leg-dropping risk control, kill switch, same sizing defaults.

## Open questions worth discussing

1. **Should we abandon walk-forward parameter tuning?** It didn't add Sharpe and added turnover. The default-params static triplet is simpler and slightly better. Walk-forward is *theoretically* protective against regime drift but empirically didn't help in this sample.
2. **2% notional cap is very conservative — when to scale up?** Could justify 5-10% after 8-12 weeks of paper validation showing live tracks backtest within 50 bps over rolling 30-day windows.
3. **The strategy's edge is fundamentally diversification, not signal alpha.** Two of three legs lose to buy-and-hold of their underlying. Is that acceptable? Could we replace the trend legs with cheaper exposure (e.g., just hold BTC and XAU directly, only run the BBevent leg actively)?
4. **What happens in a synchronised crypto + gold crash?** The legs were uncorrelated in this sample but both BTC and XAU rallied together 2024-2025 — the test was easy. The leg-dropping rule helps but isn't tested in stress.
5. **MT5 CFDs vs spot exchanges**: simpler deployment (one account) but worse frictions (spread + financing). Is the operational simplicity worth the 3-5% annual drag?

## Reproducibility

Repo: `marktuhill/polymarket-bot`, branch `claude/nautilus-strategy-catalogue-kWEmq`.

```
backtest/                       # vectorised + event-driven engines, indicators, candidates
scripts/wf_tuned_triplet.py     # walk-forward backtest, the headline result
scripts/baselines.py            # buy-and-hold and risk-parity comparison
scripts/tune_pair.py            # parameter sweep with train/test split
live/                           # daily signal generator, broker abstraction, PaperBroker
live/adapters/mt5_broker.py     # IC Markets MT5 adapter
mql5/WFTunedTriplet.mq5         # native MT5 Expert Advisor
windows/                        # setup.bat, run_daily.bat for VPS deployment
results/                        # equity curve PNGs and metric CSVs
```

The backtest is fully reproducible: `python scripts/wf_tuned_triplet.py` regenerates the headline result table and equity chart from cached data files.
