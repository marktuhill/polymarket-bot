//+------------------------------------------------------------------+
//|  WFTunedTriplet.mq5                                              |
//|  Walk-forward tuned 3-leg portfolio EA                           |
//|  Legs: EMACross/BTC + BBevent/LTC + EMALong/XAU                  |
//|                                                                  |
//|  Designed for IC Markets MT5 Raw Spread accounts.                |
//|  Attach to ANY chart (it self-fetches each symbol's daily bars). |
//|                                                                  |
//|  Behaviour:                                                      |
//|    - Runs once per day at RunHourUTC:RunMinuteUTC.               |
//|    - For each leg: computes signal, rolling Sharpe, rolling vol. |
//|    - Drops legs whose rolling 6-month Sharpe is non-positive.    |
//|    - Equal-weights surviving legs to per_leg_equity allocation.  |
//|    - Sizes each leg to TargetVolAnn using rolling realised vol.  |
//|    - Caps notional at MaxNotionalPct of per-leg equity.          |
//|    - Trips a kill switch at -KillSwitchDDPct drawdown from peak. |
//|                                                                  |
//|  Only manages positions tagged with Magic number 19850528.       |
//+------------------------------------------------------------------+
#property copyright "WF Tuned Triplet"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//==================== INPUT PARAMETERS ============================
input string  __sym__              = "===== Symbols =====";
input string  Symbol_BTC           = "BTCUSD";
input string  Symbol_LTC           = "LTCUSD";
input string  Symbol_XAU           = "XAUUSD";

input string  __ema_btc__          = "===== EMACross on BTC =====";
input int     EmaCross_Fast        = 10;
input int     EmaCross_Slow        = 20;

input string  __bb_ltc__           = "===== BBevent on LTC =====";
input int     BB_Period            = 20;
input double  BB_StdMult           = 2.0;
input int     ATR_Period           = 14;
input double  ATR_StopMult         = 1.5;
input double  BBevent_RiskPerTrade = 0.005;   // 0.5% of leg equity on stop (further capped by notional cap)

input string  __ema_xau__          = "===== EMALong on XAU =====";
input int     EmaLong_Fast         = 50;
input int     EmaLong_Slow         = 100;

input string  __sizing__           = "===== Sizing & risk =====";
input double  TargetVolAnn         = 0.14;    // per-leg annualised vol target before clipping
input int     RollingWindow        = 180;     // bars for rolling Sharpe/vol
input double  MaxNotionalPctEq     = 0.02;    // hard cap per leg as % of TOTAL equity (2% default)
input double  KillSwitchDDPct      = 0.20;    // halt at -20% from peak

input string  __exec__             = "===== Execution =====";
input int     Magic                = 19850528;
input int     DeviationPoints      = 50;
input int     RunHourUTC           = 23;      // daily trigger hour (server time)
input int     RunMinuteUTC         = 30;
input bool    AllowShortLTC        = true;    // BBevent can go short LTC
input double  MinDeltaNotional     = 5.0;     // skip dust trades

//==================== GLOBALS =====================================
CTrade        trade;
datetime      last_run_date = 0;
double        peak_equity   = 0.0;
bool          kill_switch_tripped = false;

//==================== STRUCTS =====================================
struct LegSignal {
   string  symbol;
   int     position;       // -1, 0, +1 (raw direction)
   double  sharpe;         // rolling annualised Sharpe (180d)
   double  vol_scale;      // target_vol / realised_vol (vol-targeted legs)
   bool    is_active;      // false = muted by negative Sharpe
   string  name;
   bool    uses_internal_sizing; // true for BBevent (sizes by stop distance)
   double  stop_distance;  // in points, for BBevent
};

//==================== LIFECYCLE ===================================
int OnInit() {
   trade.SetExpertMagicNumber(Magic);
   trade.SetDeviationInPoints(DeviationPoints);
   trade.SetTypeFillingBySymbol(Symbol_BTC); // adapts per-symbol later

   peak_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   EventSetTimer(60); // wake every minute, only ACT once per day

   Print("WFTunedTriplet initialised. Magic=", Magic, " starting equity=", peak_equity);
   EnsureSymbolSelected(Symbol_BTC);
   EnsureSymbolSelected(Symbol_LTC);
   EnsureSymbolSelected(Symbol_XAU);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
}

void OnTimer() {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if (dt.hour != RunHourUTC || dt.min != RunMinuteUTC) return;

   datetime today = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
   if (today == last_run_date) return;

   RunDaily();
   last_run_date = today;
}

//==================== MAIN STRATEGY ===============================
void RunDaily() {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if (equity > peak_equity) peak_equity = equity;
   double dd = equity / peak_equity - 1.0;
   Print("=== Daily run ", TimeToString(TimeCurrent()), " equity=", equity,
         " dd=", DoubleToString(dd * 100, 2), "% ===");

   if (dd <= -KillSwitchDDPct) {
      kill_switch_tripped = true;
      PrintFormat("KILL SWITCH: drawdown %.2f%% breached -%.2f%%", dd * 100, KillSwitchDDPct * 100);
   }
   if (kill_switch_tripped) {
      FlattenAll();
      return;
   }

   LegSignal btc = SignalEMACross(Symbol_BTC, EmaCross_Fast, EmaCross_Slow, true);
   btc.name = "EMACross/BTC";
   LegSignal ltc = SignalBBEvent(Symbol_LTC);
   ltc.name = "BBevent/LTC";
   LegSignal xau = SignalEMACross(Symbol_XAU, EmaLong_Fast, EmaLong_Slow, false); // long-only
   xau.name = "EMALong/XAU";

   int active = (btc.is_active ? 1 : 0) + (ltc.is_active ? 1 : 0) + (xau.is_active ? 1 : 0);
   if (active == 0) active = 1;
   double per_leg_equity = equity / active;

   PrintFormat("  active legs=%d  per_leg_equity=%.2f", active, per_leg_equity);
   PrintFormat("  %s [%s] pos=%+d sharpe=%+.2f vol_scale=%.2f",
               btc.name, btc.is_active ? "ON" : "OFF", btc.position, btc.sharpe, btc.vol_scale);
   PrintFormat("  %s [%s] pos=%+d sharpe=%+.2f",
               ltc.name, ltc.is_active ? "ON" : "OFF", ltc.position, ltc.sharpe);
   PrintFormat("  %s [%s] pos=%+d sharpe=%+.2f vol_scale=%.2f",
               xau.name, xau.is_active ? "ON" : "OFF", xau.position, xau.sharpe, xau.vol_scale);

   ApplyLeg(btc, per_leg_equity);
   ApplyLeg(ltc, per_leg_equity);
   ApplyLeg(xau, per_leg_equity);
}

//==================== SIGNALS =====================================

// EMA crossover. long_short=true allows -1; otherwise long-only {0, +1}.
LegSignal SignalEMACross(string sym, int fast, int slow, bool long_short) {
   LegSignal sig;
   sig.symbol = sym;
   sig.uses_internal_sizing = false;
   sig.stop_distance = 0;

   double close[];
   ArraySetAsSeries(close, true);
   int need = MathMax(slow + RollingWindow + 10, 400);
   int n = CopyClose(sym, PERIOD_D1, 0, need, close);
   if (n < slow + RollingWindow) {
      Print("  WARNING: not enough bars for ", sym, " (got ", n, ")");
      sig.position = 0; sig.sharpe = 0; sig.vol_scale = 0; sig.is_active = false;
      return sig;
   }

   // Compute EMA series so we can build the returns history too.
   double ema_f[];
   double ema_s[];
   ArrayResize(ema_f, n);
   ArrayResize(ema_s, n);
   ComputeEMASeries(close, n, fast, ema_f);
   ComputeEMASeries(close, n, slow, ema_s);

   // Latest position (close[0] is most recent because ArraySetAsSeries(true))
   bool fast_above = (ema_f[0] >= ema_s[0]);
   if (long_short) sig.position = fast_above ? 1 : -1;
   else            sig.position = fast_above ? 1 : 0;

   // Build daily strategy returns (aligned to close-to-close)
   double rets[];
   BuildEMACrossReturns(close, ema_f, ema_s, n, long_short, rets);

   sig.sharpe = RollingSharpe(rets, RollingWindow);
   double vol_ann = RollingVolAnn(rets, RollingWindow);
   sig.vol_scale = (vol_ann > 0) ? TargetVolAnn / vol_ann : 0;
   sig.is_active = (sig.sharpe > 0);
   return sig;
}

// BBevent on LTC. Sized internally by ATR stop + risk_per_trade rather than vol-scaled.
LegSignal SignalBBEvent(string sym) {
   LegSignal sig;
   sig.symbol = sym;
   sig.uses_internal_sizing = true;
   sig.vol_scale = 1.0;

   double close[]; double high[]; double low[];
   ArraySetAsSeries(close, true); ArraySetAsSeries(high, true); ArraySetAsSeries(low, true);
   int need = MathMax(BB_Period + RollingWindow + 10, 400);
   int n = CopyClose(sym, PERIOD_D1, 0, need, close);
   int nh = CopyHigh(sym, PERIOD_D1, 0, need, high);
   int nl = CopyLow(sym, PERIOD_D1, 0, need, low);
   if (n < BB_Period + RollingWindow || nh != n || nl != n) {
      Print("  WARNING: not enough bars for ", sym, " (close=", n, ")");
      sig.position = 0; sig.sharpe = 0; sig.is_active = false; sig.stop_distance = 0;
      return sig;
   }

   // Bands at latest close
   double mid, sd, upper, lower;
   BollingerLatest(close, BB_Period, BB_StdMult, mid, sd, upper, lower);

   // Determine current LTC position from open positions (BBevent is path-dependent
   // — easiest in MQL5 is to read what we already hold and check if exit triggered).
   double current_units = GetSignedUnits(sym);
   int current_dir = (current_units > 0) ? 1 : ((current_units < 0) ? -1 : 0);
   double cur_close = close[0];
   double prev_close = close[1];

   int new_dir = current_dir;
   // Exit on middle-band cross in favourable direction
   if (current_dir == 1 && cur_close >= mid) new_dir = 0;
   else if (current_dir == -1 && cur_close <= mid) new_dir = 0;

   // Look for entry only if flat
   if (new_dir == 0) {
      bool crossed_below_lower = (prev_close >= /*prev*/Bollinger_LowerAt(close, BB_Period, BB_StdMult, 1)) && (cur_close < lower);
      bool crossed_above_upper = (prev_close <= Bollinger_UpperAt(close, BB_Period, BB_StdMult, 1)) && (cur_close > upper);
      if (crossed_below_lower) new_dir = 1;
      else if (crossed_above_upper && AllowShortLTC) new_dir = -1;
   }
   sig.position = new_dir;

   // Stop distance for sizing
   double atr = ATRSimple(high, low, close, ATR_Period);
   sig.stop_distance = ATR_StopMult * atr;

   // Rolling Sharpe on close-to-close returns (proxy; BBevent's true strategy returns
   // need a full replay which is more code — we use price-return proxy weighted by
   // historical position). For leg-dropping this is good enough.
   double rets[];
   BuildBBEventReturnProxy(close, BB_Period, BB_StdMult, n, rets);
   sig.sharpe = RollingSharpe(rets, RollingWindow);
   sig.is_active = (sig.sharpe > 0);
   return sig;
}

//==================== EXECUTION ===================================
void ApplyLeg(const LegSignal &sig, double per_leg_equity) {
   if (!SymbolSelect(sig.symbol, true)) {
      Print("  ", sig.name, ": symbol_select failed");
      return;
   }

   double contract_size = SymbolInfoDouble(sig.symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double volume_min    = SymbolInfoDouble(sig.symbol, SYMBOL_VOLUME_MIN);
   double volume_step   = SymbolInfoDouble(sig.symbol, SYMBOL_VOLUME_STEP);
   double price         = SymbolInfoDouble(sig.symbol, SYMBOL_BID); // approx for sizing
   if (price <= 0 || contract_size <= 0) return;

   double total_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double notional_cap = MaxNotionalPctEq * total_equity;  // per leg, in TOTAL equity terms

   double target_units = 0.0;
   if (sig.is_active && sig.position != 0) {
      double desired_notional;
      if (sig.uses_internal_sizing) {
         // BBevent: size by risk-per-trade × leg equity / stop distance
         double risk_usd = BBevent_RiskPerTrade * per_leg_equity;
         double stop_dist = sig.stop_distance;
         if (stop_dist <= 0) return;
         double units = (risk_usd / stop_dist) * sig.position;
         desired_notional = units * price;
      } else {
         desired_notional = sig.position * sig.vol_scale * per_leg_equity;
      }
      if (MathAbs(desired_notional) > notional_cap) {
         desired_notional = (desired_notional > 0) ? notional_cap : -notional_cap;
      }
      target_units = desired_notional / price;
   }

   double current_units = GetSignedUnits(sig.symbol);
   double delta_units = target_units - current_units;
   double delta_notional = MathAbs(delta_units * price);
   if (delta_notional < MinDeltaNotional) {
      PrintFormat("  %s: no-op (delta notional $%.2f < $%.2f)", sig.name, delta_notional, MinDeltaNotional);
      return;
   }

   // Convert to lots
   double lots = MathAbs(delta_units) / contract_size;
   lots = MathRound(lots / volume_step) * volume_step;
   if (lots < volume_min) {
      PrintFormat("  %s: rounded to %.4f lots < min %.4f; skipping", sig.name, lots, volume_min);
      return;
   }

   bool is_buy = (delta_units > 0);
   PrintFormat("  %s: %s %.4f lots (target %.6f units, have %.6f)",
               sig.name, is_buy ? "BUY" : "SELL", lots, target_units, current_units);

   bool ok = is_buy
      ? trade.Buy(lots, sig.symbol, 0, 0, 0, "wf_triplet")
      : trade.Sell(lots, sig.symbol, 0, 0, 0, "wf_triplet");
   if (!ok) {
      PrintFormat("  %s: order FAILED retcode=%d (%s)", sig.name, trade.ResultRetcode(), trade.ResultRetcodeDescription());
   }
}

void FlattenAll() {
   string syms[] = {Symbol_BTC, Symbol_LTC, Symbol_XAU};
   CPositionInfo pos;
   for (int i = 0; i < ArraySize(syms); i++) {
      for (int j = PositionsTotal() - 1; j >= 0; j--) {
         if (!pos.SelectByIndex(j)) continue;
         if (pos.Magic() != Magic) continue;
         if (pos.Symbol() != syms[i]) continue;
         trade.PositionClose(pos.Ticket());
      }
   }
}

//==================== HELPERS =====================================
void EnsureSymbolSelected(string sym) {
   if (!SymbolSelect(sym, true)) {
      PrintFormat("WARNING: could not add %s to Market Watch. Enable it manually.", sym);
   }
}

double GetSignedUnits(string sym) {
   CPositionInfo pos;
   double total_lots_signed = 0.0;
   for (int i = PositionsTotal() - 1; i >= 0; i--) {
      if (!pos.SelectByIndex(i)) continue;
      if (pos.Magic() != Magic) continue;
      if (pos.Symbol() != sym) continue;
      double v = pos.Volume();
      total_lots_signed += (pos.PositionType() == POSITION_TYPE_BUY) ? v : -v;
   }
   double cs = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
   return total_lots_signed * cs;
}

// Standard recursive EMA with seed = first close. Matches pandas .ewm(adjust=False).
// close[] is series (close[0] is most recent). out_ema[] same orientation.
void ComputeEMASeries(const double &close[], int n, int period, double &out_ema[]) {
   double alpha = 2.0 / (period + 1.0);
   // Work in chronological order (oldest first), then flip back.
   double ema_chrono[];
   ArrayResize(ema_chrono, n);
   ema_chrono[0] = close[n - 1]; // oldest seed
   for (int i = 1; i < n; i++) {
      double price_chrono = close[n - 1 - i];
      ema_chrono[i] = alpha * price_chrono + (1.0 - alpha) * ema_chrono[i - 1];
   }
   // Copy back into series orientation: out_ema[0] = newest
   for (int i = 0; i < n; i++) out_ema[i] = ema_chrono[n - 1 - i];
}

// Build per-bar strategy return: pos[i-1] * (close[i]/close[i-1] - 1)
// Indices in chronological order at the end.
void BuildEMACrossReturns(const double &close[], const double &ema_f[], const double &ema_s[],
                          int n, bool long_short, double &out_rets[]) {
   ArrayResize(out_rets, n);
   for (int k = 0; k < n; k++) out_rets[k] = 0.0;
   // close[k] series: close[0] = newest. Loop from oldest to newest.
   for (int chrono = 1; chrono < n; chrono++) {
      int idx = n - 1 - chrono;
      int prev_idx = idx + 1;
      bool fast_above_prev = (ema_f[prev_idx] >= ema_s[prev_idx]);
      int prev_pos = long_short ? (fast_above_prev ? 1 : -1) : (fast_above_prev ? 1 : 0);
      double ret = close[idx] / close[prev_idx] - 1.0;
      out_rets[chrono] = prev_pos * ret;
   }
}

// Proxy returns for BBevent: when close is below lower band, contribute +ret next day;
// when above upper, contribute -ret next day. Captures the mean-reversion P&L flavour
// well enough to compute a rolling Sharpe for the leg-dropping decision.
void BuildBBEventReturnProxy(const double &close[], int period, double std_mult, int n, double &out_rets[]) {
   ArrayResize(out_rets, n);
   for (int k = 0; k < n; k++) out_rets[k] = 0.0;
   for (int chrono = period + 1; chrono < n; chrono++) {
      int idx = n - 1 - chrono;
      int prev_idx = idx + 1;
      double mid, sd, upper, lower;
      // Compute BB at prev bar
      double sum = 0.0;
      for (int j = 0; j < period; j++) sum += close[prev_idx + j];
      double m = sum / period;
      double v = 0.0;
      for (int j = 0; j < period; j++) v += MathPow(close[prev_idx + j] - m, 2);
      double s = MathSqrt(v / period);
      double prev_lower = m - std_mult * s;
      double prev_upper = m + std_mult * s;
      int pos = 0;
      if (close[prev_idx] < prev_lower) pos = 1;
      else if (close[prev_idx] > prev_upper) pos = -1;
      double ret = close[idx] / close[prev_idx] - 1.0;
      out_rets[chrono] = pos * ret;
   }
}

double RollingSharpe(const double &rets[], int window) {
   int n = ArraySize(rets);
   int start = MathMax(n - window, 0);
   double sum = 0; int cnt = 0;
   for (int i = start; i < n; i++) { sum += rets[i]; cnt++; }
   if (cnt < window / 2) return 0.0;
   double mean = sum / cnt;
   double v = 0;
   for (int i = start; i < n; i++) v += MathPow(rets[i] - mean, 2);
   double sd = MathSqrt(v / cnt);
   if (sd <= 0) return 0.0;
   return (mean / sd) * MathSqrt(365.0);
}

double RollingVolAnn(const double &rets[], int window) {
   int n = ArraySize(rets);
   int start = MathMax(n - window, 0);
   double sum = 0; int cnt = 0;
   for (int i = start; i < n; i++) { sum += rets[i]; cnt++; }
   if (cnt < 2) return 0.0;
   double mean = sum / cnt;
   double v = 0;
   for (int i = start; i < n; i++) v += MathPow(rets[i] - mean, 2);
   return MathSqrt(v / cnt) * MathSqrt(365.0);
}

void BollingerLatest(const double &close[], int period, double std_mult,
                     double &out_mid, double &out_sd, double &out_upper, double &out_lower) {
   double sum = 0.0;
   for (int j = 0; j < period; j++) sum += close[j];
   out_mid = sum / period;
   double v = 0.0;
   for (int j = 0; j < period; j++) v += MathPow(close[j] - out_mid, 2);
   out_sd = MathSqrt(v / period);
   out_upper = out_mid + std_mult * out_sd;
   out_lower = out_mid - std_mult * out_sd;
}

// Bollinger upper / lower at offset 'offset' bars back (series indexing).
double Bollinger_LowerAt(const double &close[], int period, double std_mult, int offset) {
   double sum = 0.0;
   for (int j = 0; j < period; j++) sum += close[offset + j];
   double m = sum / period;
   double v = 0.0;
   for (int j = 0; j < period; j++) v += MathPow(close[offset + j] - m, 2);
   double s = MathSqrt(v / period);
   return m - std_mult * s;
}

double Bollinger_UpperAt(const double &close[], int period, double std_mult, int offset) {
   double sum = 0.0;
   for (int j = 0; j < period; j++) sum += close[offset + j];
   double m = sum / period;
   double v = 0.0;
   for (int j = 0; j < period; j++) v += MathPow(close[offset + j] - m, 2);
   double s = MathSqrt(v / period);
   return m + std_mult * s;
}

// Simple-mean ATR (not Wilder's), matching backtest/bb_event.py
double ATRSimple(const double &high[], const double &low[], const double &close[], int period) {
   double sum_tr = 0.0;
   for (int i = 0; i < period; i++) {
      double tr1 = high[i] - low[i];
      double tr2 = MathAbs(high[i] - close[i + 1]);
      double tr3 = MathAbs(low[i] - close[i + 1]);
      double tr = MathMax(tr1, MathMax(tr2, tr3));
      sum_tr += tr;
   }
   return sum_tr / period;
}
