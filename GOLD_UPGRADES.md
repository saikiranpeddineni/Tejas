# StockBot ULTRA GOLD — Complete Upgrade Guide
## What Was Added & Why Each Upgrade Earns More

---

## The 10 Gold-Level Upgrades (and the theory behind each)

---

### ★ 1. Market Regime Detection (`regime_engine.py`)

**What:** Classifies the current market into one of 6 states using VIX level + SPY 50/200 MA crossover.

| Regime | VIX | Trend | Position Size | Strategy |
|---|---|---|---|---|
| `bull_low_vol` | < 15 | Up | 120% | Full momentum |
| `bull_high_vol` | 15-25 | Up | 80% | Careful momentum |
| `bear_low_vol` | < 25 | Down | 50% | Defensive |
| `bear_high_vol` | > 25 | Down | 25% | Survival |
| `range_low_vol` | < 15 | Sideways | 90% | Mean reversion |
| `extreme_fear` | > 35 | Any | 0% | **NO NEW TRADES** |

**Why it earns more:** Most retail losses happen when traders keep buying during bear markets. This system automatically reduces exposure when conditions are bad and increases it when conditions are favorable. Professional quant funds all run regime detection.

---

### ★ 2. Earnings Blackout Calendar (`earnings_filter.py`)

**What:** Fetches earnings dates from AlphaVantage API. Blocks all entries within 3 days of earnings. Auto-exits open positions 2 hours before earnings.

**Why it earns more:** A stock can gap -20% on a single earnings miss. This risk is unquantifiable and unhedgeable. Professional traders either: (a) avoid earnings entirely, or (b) have dedicated earnings strategies. This bot does (a), which is the safer choice for systematic traders.

**Example:** NVDA reports earnings. Bot blocks NVDA entries for 3 days before. If you're already in NVDA, bot closes the position 2 hours before the report. No binary risk.

---

### ★ 3. News Sentiment Scoring (`sentiment_engine.py`)

**What:** Scans Alpaca's news feed (included free) + AlphaVantage NEWS_SENTIMENT. Scores -1.0 (disaster) to +1.0 (great news). Adjusts signal edge accordingly.

**Effect on edge:**
- Score > +0.3 → adds 0.5% to edge (tailwind)
- Score < -0.3 → subtracts 1.0% from edge (headwind)
- Score < -0.5 → hard blocks the trade entirely

**Why it earns more:** Stocks with strong technicals + positive news momentum have compounding tailwinds. "Trade with the narrative, not against it." Research suggests 15-25% of short-term alpha is explainable by news sentiment.

---

### ★ 4. Ensemble ML: LightGBM + XGBoost (`ml_engine_gold.py`)

**What:** Trains both LightGBM (60% weight) and XGBoost (40% weight). Final probability is their weighted average. Confidence = how much the models agree.

**Why it earns more:** Each model makes different types of mistakes. LightGBM is better at global patterns; XGBoost handles local non-linearities better. Combining them reduces prediction variance. This is textbook ensemble theory — used by every serious ML quant fund.

**Bonus:** Model disagreement = low confidence = reduce position size automatically.

---

### ★ 5. Walk-Forward Validation (`ml_engine_gold.py`)

**What:** Instead of a simple train/test split, trains on data[0..n], tests on data[n..n+k], then trains on data[0..n+k], tests on data[n+k..n+2k], etc.

**Why it earns more:** Simple train/test splits leak future data (look-ahead bias). Walk-forward gives you the only honest measure of how the model would actually have performed. If your walk-forward AUC drops significantly below your held-out AUC, the strategy is overfitting.

**Target:** Walk-forward AUC > 0.55 before going live.

---

### ★ 6. 34 Features (was 22) (`feature_builder_gold.py`)

**12 new indicators added:**

| Feature | What it captures |
|---|---|
| Williams %R | Overbought/oversold with faster reaction than RSI |
| Stochastic %K/%D | Momentum turning points (crossover signal) |
| CCI (Commodity Channel Index) | Price deviation from statistical mean |
| OBV Trend | On-Balance Volume — where smart money is flowing |
| MFI (Money Flow Index) | Volume-weighted RSI — conviction behind moves |
| Distance from 52w High | Mean reversion anchor — how far from peak |
| Distance from 52w Low | Momentum — how far off the bottom |
| Price vs 200 EMA | Major long-term trend filter |
| Higher Highs Pattern | Trend structure (5-day sequence) |
| Intraday Range Ratio | Breakout detection (today's range / normal ATR) |
| Bid-Ask Spread | Real-time execution cost |

**Why it earns more:** More information → better ML discrimination. OBV in particular is one of the most underrated indicators — it shows volume-weighted accumulation/distribution before price moves.

---

### ★ 7. Portfolio Beta Cap (`dynamic_risk.py`)

**What:** Estimates each stock's beta (market sensitivity). Ensures the portfolio's weighted beta never exceeds 1.3. Reduces new position size if adding it would breach the cap.

**Why it earns more:** Without beta management, a portfolio of high-beta tech stocks (NVDA β=1.8, TSLA β=2.0, AMD β=1.7) has an effective portfolio beta of 1.8. On a -3% SPY day, that's -5.4% for you. The beta cap ensures crashes hurt you no more than 1.3× the market.

---

### ★ 8. ATR-Dynamic Stop Losses (`dynamic_risk.py`)

**What:** Stop loss is calculated as `entry - (2.0 × ATR)` instead of a fixed 3%. In high-volatility regimes, the multiplier increases to 2.5× so the stop isn't hit by normal noise.

**Why it earns more:** Fixed percentage stops have a fatal flaw: in high-volatility periods, a 3% stop gets triggered by normal intraday swings, closing positions that would have been profitable. ATR-based stops adapt to current volatility and stay outside normal noise.

| Regime | ATR Multiplier | Effect |
|---|---|---|
| Bull low-vol | 1.5× | Tight stops, ride trends |
| Bull high-vol | 2.0× | Wider, avoid noise |
| Bear high-vol | 2.5× | Maximum width |
| Extreme fear | 3.0× | Survival mode |

---

### ★ 9. Daily Loss Circuit Breaker (`dynamic_risk.py`)

**What:** Tracks P&L within each trading session. Halts all new entries if daily losses exceed 4% of portfolio.

**Why it earns more:** Bad days compound. When you're down 3% already, you're emotionally compromised (even automated systems can pile into bad conditions). Stopping at 4% prevents a bad day from becoming a catastrophic week.

---

### ★ 10. Pre-Earnings Auto-Exit + Portfolio Sync (`main.py`)

**What:** Every scan cycle checks each open position for upcoming earnings. Automatically closes 2 hours before the announcement. Also syncs bankroll from Alpaca live account every cycle.

**Why it earns more:** This is the other side of #2. Even if you enter a great trade, earnings can destroy it overnight. The pre-exit rule ensures you book whatever profit (or limit whatever loss) you have before the binary event.

---

## Quick Start (GOLD)

```bash
# 1. Get keys:
#    Alpaca: alpaca.markets (free paper account)
#    AlphaVantage: alphavantage.co (free, 500 calls/day)

# 2. Setup
cp .env.example .env
# Fill in ALPACA_API_KEY, ALPACA_API_SECRET, ALPHA_VANTAGE_KEY

# 3. Install
pip install -r requirements.txt   # includes xgboost

# 4. Run
python main.py

# 5. Monitor
tail -f goldbot.log
python backtest.py --days 30   # after 40+ resolved trades
```

---

## Performance Targets (before going live)

| Metric | Minimum | Good | Excellent |
|---|---|---|---|
| Walk-forward AUC | 0.55 | 0.60 | 0.65+ |
| Win rate | 52% | 56% | 60%+ |
| Sharpe ratio | 1.0 | 1.5 | 2.0+ |
| Max drawdown | < 15% | < 10% | < 7% |
| Profit factor | > 1.2 | > 1.5 | > 1.8 |

---

## UAE Tax Advantage Reminder

Every dollar of profit from these trades stays with you — **UAE has 0% capital gains tax**. A strategy earning 15% annually keeps 15%. The same strategy in a 20% CGT jurisdiction keeps 12%. That's a 25% effective improvement in returns just from where you live.

---

*StockBot UAE ULTRA GOLD | 10 professional upgrades on the God Mode architecture*
*Not financial advice. All trading involves risk. Always paper trade first.*
