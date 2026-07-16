"""
models.py — All Data Structures
Single source of truth for every dataclass in the system.
US equity trading: long-only, Alpaca broker.

FeatureVector has 34 fields (22 base + 12 gold).
The old monkey-patch approach has been replaced with a clean dataclass.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# QUOTE / BAR
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Quote:
    symbol:    str
    bid:       float
    ask:       float
    bid_size:  float
    ask_size:  float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 0.0


@dataclass
class Bar:
    symbol:    str
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    int
    vwap:      float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE VECTOR — 34 features (22 base + 12 gold)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FeatureVector:
    trade_id:   str
    symbol:     str
    timestamp:  datetime

    # ── Price / Trend (3) ────────────────────────────────────────────────────
    price_vs_sma20:      float = 0.0   # (price - SMA20) / SMA20
    price_vs_ema9:       float = 0.0   # (price - EMA9)  / EMA9
    ema9_vs_ema21:       float = 0.0   # EMA9/EMA21 - 1

    # ── Momentum (5) ─────────────────────────────────────────────────────────
    rsi_14:              float = 0.5   # normalized 0-1
    momentum_5d:         float = 0.0
    momentum_10d:        float = 0.0
    momentum_20d:        float = 0.0
    rate_of_change:      float = 0.0

    # ── MACD (2) ──────────────────────────────────────────────────────────────
    macd_histogram:      float = 0.0
    macd_trend:          float = 0.0

    # ── Volatility (4) ───────────────────────────────────────────────────────
    atr_pct:             float = 0.02  # ATR / price
    bb_position:         float = 0.5   # 0=lower band, 1=upper band
    bb_squeeze:          float = 0.02  # BB width / price
    volatility_20d:      float = 0.20  # 20-day annualized realized vol

    # ── Volume (3) ───────────────────────────────────────────────────────────
    volume_ratio:        float = 1.0   # today / 20d avg
    volume_trend:        float = 0.0   # slope of volume MA
    vwap_deviation:      float = 0.0   # (price - VWAP) / VWAP

    # ── Market context (3) ───────────────────────────────────────────────────
    spy_corr:            float = 0.5   # 20d rolling correlation with SPY
    relative_strength:   float = 0.0   # stock return vs SPY (20d)
    sector_strength:     float = 0.0   # sector ETF 10d momentum

    # ── Meta (2) ─────────────────────────────────────────────────────────────
    sector_code:         int   = 6     # 0=tech 1=fin 2=health 3=cons 4=energy 5=etf 6=other
    candle_stability:    float = 0.5   # |close−open|/(high−low): 1=decisive candle, 0=doji
                                       # (was the unused "reserved" slot — same position 22,
                                       #  so pre-existing 34-feature models stay loadable)

    # ── Gold features (12) ───────────────────────────────────────────────────
    williams_r:          float = 0.5   # Williams %R, normalized 0-1
    stoch_k:             float = 0.5   # Stochastic %K, normalized 0-1
    stoch_d:             float = 0.5   # Stochastic %D, normalized 0-1
    cci:                 float = 0.0   # CCI, normalized to [-1, 1]
    obv_trend:           float = 0.0   # On-Balance Volume 10d slope
    mfi:                 float = 0.5   # Money Flow Index, normalized 0-1
    dist_52w_high:       float = 0.2   # distance below 52-week high (0=at high)
    dist_52w_low:        float = 0.5   # distance above 52-week low
    price_vs_ema200:     float = 0.0   # (price - EMA200) / EMA200
    higher_highs:        float = 0.5   # fraction of last 4 days with higher high
    intraday_range_ratio:float = 1.0   # today range / ATR (1=normal)
    bid_ask_spread:      float = 0.001 # live spread pct

    # ── ★ Market-context features (3) — date-aligned to the feature window ───
    market_trend_20d:    float = 0.0   # NIFTY 20d momentum (the market's own trend)
    market_vol_20d:      float = 0.0   # NIFTY 20d realized vol, annualized (regime proxy)
    trend_alignment:     float = 0.5   # fraction of 5 trend signals agreeing bullish

    # ── Outcome (filled at exit) ─────────────────────────────────────────────
    outcome: Optional[int] = None      # 1=WIN 0=LOSS None=pending

    def to_list(self) -> List[float]:
        """Returns the feature list in canonical order (matches FEATURE_NAMES)."""
        return [
            # Base 22
            self.price_vs_sma20,
            self.price_vs_ema9,
            self.ema9_vs_ema21,
            self.rsi_14,
            self.momentum_5d,
            self.momentum_10d,
            self.momentum_20d,
            self.rate_of_change,
            self.macd_histogram,
            self.macd_trend,
            self.atr_pct,
            self.bb_position,
            self.bb_squeeze,
            self.volatility_20d,
            self.volume_ratio,
            self.volume_trend,
            self.vwap_deviation,
            self.spy_corr,
            self.relative_strength,
            self.sector_strength,
            float(self.sector_code),
            self.candle_stability,
            # Gold 12
            self.williams_r,
            self.stoch_k,
            self.stoch_d,
            self.cci,
            self.obv_trend,
            self.mfi,
            self.dist_52w_high,
            self.dist_52w_low,
            self.price_vs_ema200,
            self.higher_highs,
            self.intraday_range_ratio,
            self.bid_ask_spread,
            # ★ Market context 3
            self.market_trend_20d,
            self.market_vol_20d,
            self.trend_alignment,
        ]


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Signal:
    symbol:       str
    side:         str          # "BUY"
    entry_price:  float
    ml_prob:      float        # ML probability of profit
    ml_conf:      float        # model confidence
    edge:         float        # ml_prob - 0.50 (> 0 = bullish edge)
    features:     FeatureVector
    stop_loss:    float
    take_profit:  float
    generated_at: datetime = field(default_factory=datetime.utcnow)
    # ── Analyst reasoning (for transparency / UI) ────────────────────────────
    analyst_score: float = 0.0          # composite [-1,+1]
    thesis:        str   = ""           # human-readable rationale
    factors:       dict  = field(default_factory=dict)  # per-factor sub-scores
    fund_health:   str   = ""           # "strong"/"fine"/"fragile"/"unknown"
    rationale:     str   = ""           # ★ full one-line WHY (persisted per trade)


# ─────────────────────────────────────────────────────────────────────────────
# TRADE (open/closed position)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    trade_id:          str
    symbol:            str
    side:              str           # "BUY"
    shares:            float
    size_usd:          float
    entry_price:       float
    stop_loss:         float
    take_profit:       float
    ml_probability:    float
    entry_quality:     float
    kelly_pct:         float
    features:          FeatureVector
    alpaca_order_id:   str = ""
    entry_time:        datetime = field(default_factory=datetime.utcnow)
    status:            str = "open"
    pnl_usd:           float = 0.0
    pnl_pct:           float = 0.0
    exit_time:         Optional[datetime] = None
    exit_reason:       Optional[str]      = None
    exit_price:        Optional[float]    = None
    highest_price:     Optional[float]    = None   # trailing stop high-water mark
    lowest_price:      Optional[float]    = None
    last_ml_prob:      Optional[float]    = None   # live re-score EMA
    last_check_time:   Optional[float]    = None   # time.monotonic()
    # ── ★ Staged exit state ──────────────────────────────────────────────────
    trim_shares:       float              = 0.0    # shares sold at first target
    trim_price:        Optional[float]    = None   # fill price of the trim
    rationale:         str                = ""     # entry WHY (from Signal)


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Portfolio:
    bankroll:        float
    peak_bankroll:   float
    open_trades:     List[Trade]       = field(default_factory=list)
    sector_exposure: Dict[str, float]  = field(default_factory=dict)

    @property
    def drawdown(self) -> float:
        if self.peak_bankroll <= 0:
            return 0.0
        return max(0.0, (self.peak_bankroll - self.bankroll) / self.peak_bankroll)

    @property
    def total_exposure(self) -> float:
        return sum(t.size_usd for t in self.open_trades)

    @property
    def available_capital(self) -> float:
        return max(0.0, self.bankroll - self.total_exposure)

    def update_peak(self) -> None:
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
