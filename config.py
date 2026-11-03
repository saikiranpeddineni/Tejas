"""
config.py — StockBot ULTRA GOLD Configuration (FIXED)
======================================================
KEY FIXES in this version:
  ★ REGIME_VIX_EXTREME raised: 35→45 (now uses high_fear at 35-45 instead of halt)
  ★ SCAN_INTERVAL_SECS: 60→30 (twice as many opportunities per session)
  ★ high_fear added to REGIME_SIZE_MULT with 10% size
  ★ TRAILING_STOP_PCT added (was missing from original config)
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Resolve configuration relative to this module, not the caller's current
# directory.  This prevents an unrelated parent-directory .env from silently
# supplying broker credentials when Tejas is launched by a scheduler/container.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# ★ Trust the OS (Windows) certificate store, not just certifi's bundle. A
# corporate VPN/proxy that intercepts TLS (e.g. Zscaler) re-signs every HTTPS
# response with its own root CA — certifi doesn't know it, so ALL market-data
# calls die with SSLCertVerificationError whenever the VPN is on. The OS store
# has that CA, so this keeps Yahoo/Telegram/AlphaVantage working either way.
# config.py is the first import of every entry point, so this covers them all.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass   # optional dependency — without it, behavior is unchanged

# Keep the paper/live flag aligned with brokers.factory.  Unknown broker names
# deliberately fall back to the simulated adapter there; they must not be
# labelled LIVE merely because DRY_RUN was set false.
_LIVE_BROKERS = frozenset({
    "zerodha", "upstox", "angelone", "angel", "smartapi", "dhan", "fyers",
})

_LIVE_ACK_TEXT = "I_UNDERSTAND_LIVE_TRADING"


class Config:
    # ── Broker selection (Indian market — NSE equity) ──────────────────────────
    # One of: simulated | zerodha | upstox | angelone | dhan | fyers
    BROKER  = os.getenv("BROKER", "simulated").lower()
    # Holding type: DELIVERY (multi-day, recommended) or INTRADAY (auto square-off)
    PRODUCT = os.getenv("PRODUCT", "DELIVERY").upper()

    # ── Live-order acknowledgement ─────────────────────────────────────────────
    # A live broker plus DRY_RUN=false is still monitor-only until both explicit
    # opt-ins are present. Keeping this separate from DRY_RUN means existing live
    # positions can still be monitored/flattened when the entry acknowledgement is
    # missing, while a new entry can never be enabled by a stale dashboard setting.
    LIVE_TRADING_ACK_TEXT = _LIVE_ACK_TEXT
    LIVE_AUTOSTART_ACK_TEXT = "I_UNDERSTAND_LIVE_AUTOSTART"
    ENABLE_LIVE_TRADING = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower() == "true"
    LIVE_TRADING_ACK = os.getenv("LIVE_TRADING_ACK", "").strip()

    # ── Runtime ────────────────────────────────────────────────────────────────
    DRY_RUN               = os.getenv("DRY_RUN", "true").lower() == "true"
    LIVE_MODE             = BROKER in _LIVE_BROKERS and not DRY_RUN
    LIVE_FILTERS_ENABLED  = (
        os.getenv("USE_EARNINGS_GATE", "true").lower() == "true"
        and os.getenv("USE_SENTIMENT_GATE", "true").lower() == "true"
        and os.getenv("USE_FUNDAMENTALS_GATE", "true").lower() == "true"
    )
    LIVE_ENTRIES_ENABLED  = (
        LIVE_MODE
        and ENABLE_LIVE_TRADING
        and LIVE_TRADING_ACK == LIVE_TRADING_ACK_TEXT
        and LIVE_FILTERS_ENABLED
    )
    # PAPER = simulated/unknown fallback broker OR DRY_RUN on (no real orders)
    PAPER_TRADING = (BROKER not in _LIVE_BROKERS) or DRY_RUN

    # Index symbols used for regime/VIX/benchmark (logical names; data layer maps
    # them — e.g. Yahoo: NIFTY->^NSEI, INDIAVIX->^INDIAVIX)
    MARKET_INDEX_SYMBOL = os.getenv("MARKET_INDEX_SYMBOL", "NIFTY")
    VIX_FALLBACK_SYMBOL = os.getenv("VIX_FALLBACK_SYMBOL", "INDIAVIX")

    # ── Trading universe (dynamic, market-derived — NOT a hand-picked list) ─────
    # Pulled live from NSE index constituents. nifty50|nifty100|nifty200|nifty500
    # |watchlist. Default nifty500 ≈ the whole investable NSE market.
    UNIVERSE     = os.getenv("UNIVERSE", "nifty500").lower()
    UNIVERSE_MAX = int(os.getenv("UNIVERSE_MAX", "500"))

    # Forward-return forecast horizon (trading days) the ML model is trained to predict
    FORECAST_HORIZON = int(os.getenv("FORECAST_HORIZON", "5"))
    # Testing only: bypass the NSE-hours gate so you can run outside 09:15–15:30
    # IST (e.g. weekends) against the most recent real data. Never set in live.
    IGNORE_MARKET_HOURS = os.getenv("IGNORE_MARKET_HOURS", "false").lower() == "true"

    # AlphaVantage (free: 500 calls/day) — earnings + sentiment
    ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "demo")

    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    SCAN_INTERVAL_SECS    = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))   # ★ was 60
    MONITOR_INTERVAL_SECS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "30"))

    # ── Storage ────────────────────────────────────────────────────────────────
    DB_PATH   = str(BASE_DIR / "tejas.db")
    MODEL_DIR = str(BASE_DIR / "models")

    # ── Capital (amounts are in INR ₹) ─────────────────────────────────────────
    BANKROLL          = float(os.getenv("BANKROLL", "100000"))   # ₹1,00,000
    MIN_POSITION_USD  = float(os.getenv("MIN_POSITION", "5000"))  # ₹ (name kept for compat)
    MAX_POSITION_USD  = float(os.getenv("MAX_POSITION", "25000")) # ₹
    MAX_POSITION_PCT  = float(os.getenv("MAX_POSITION_PCT", "0.12"))
    KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "0.25"))

    # ── Stock Universe (NSE — NIFTY large/mid caps) ────────────────────────────
    WATCHLIST = [
        # Index / IT
        "RELIANCE", "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM",
        # Banks / Financials
        "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
        "BAJAJFINSV", "INDUSINDBK", "SBILIFE", "HDFCLIFE",
        # Auto
        "MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO",
        # Energy / Metals
        "ONGC", "NTPC", "POWERGRID", "COALINDIA", "TATASTEEL", "JSWSTEEL",
        "HINDALCO", "ADANIENT", "ADANIPORTS",
        # FMCG / Consumer
        "ITC", "HINDUNILVR", "NESTLEIND", "BRITANNIA", "TATACONSUM", "TITAN",
        # Pharma / Healthcare
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
        # Cement / Infra / Others
        "ULTRACEMCO", "GRASIM", "LT", "ASIANPAINT", "BHARTIARTL", "DMART",
        "BPCL", "UPL",
    ]

    # ── Market Filters (INR prices) ────────────────────────────────────────────
    MIN_PRICE      = float(os.getenv("MIN_PRICE", "20.0"))
    MAX_PRICE      = float(os.getenv("MAX_PRICE", "100000.0"))
    MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.004"))

    # ── Signal & Edge ──────────────────────────────────────────────────────────
    # 2.5% (was 1.8%): with the ML model still immature (AUC ~0.57 on 11 resolved
    # trades) the marginal 1.8–2.5%-edge signals were coin flips that lost to
    # costs. Lower back toward 1.8% once walk-forward AUC holds ≥ 0.60.
    MIN_EDGE             = float(os.getenv("MIN_EDGE", "0.025"))
    MIN_ENTRY_QUALITY    = float(os.getenv("MIN_ENTRY_QUALITY", "0.40"))
    MAX_TRADES_PER_CYCLE = int(os.getenv("MAX_TRADES_PER_CYCLE", "3"))
    # ★ 220 (was 120): the feature builder needs ≥200 daily bars for the EMA200
    #   feature (price_vs_ema200). At 120 it silently defaulted to 0 for every
    #   stock, killing the long-term-trend signal in the live scan.
    SIGNAL_LOOKBACK_BARS = int(os.getenv("SIGNAL_LOOKBACK_BARS", "220"))

    # ── Technical Parameters ───────────────────────────────────────────────────
    RSI_PERIOD  = 14
    MACD_FAST   = 12;  MACD_SLOW   = 26;  MACD_SIGNAL = 9
    BB_PERIOD   = 20;  BB_STD      = 2.0
    ATR_PERIOD  = 14
    EMA_SHORT   = 9;   EMA_LONG    = 21;  EMA_200     = 200
    VOLUME_MA   = 20
    STOCH_K     = 14;  STOCH_D     = 3
    WILLIAMS_R  = 14;  CCI_PERIOD  = 20;  MFI_PERIOD  = 14

    # ── ★ FIXED: Market Regime Thresholds ─────────────────────────────────────
    REGIME_VIX_LOW     = 15.0   # VIX < 15  → low-vol bull
    REGIME_VIX_HIGH    = 25.0   # VIX 15-25 → elevated
    REGIME_VIX_FEAR    = 35.0   # VIX 25-35 → high fear → 25% size
    REGIME_VIX_EXTREME = 45.0   # VIX > 45  → TRUE halt (was 35 — caused April 6 lockout)

    REGIME_SIZE_MULT = {
        "bull_low_vol":   1.20,
        "bull_high_vol":  0.80,
        "bear_low_vol":   0.50,
        "bear_high_vol":  0.25,
        "range_low_vol":  0.90,
        "range_high_vol": 0.40,
        "high_fear":      0.10,   # ★ NEW: VIX 35-45 → tiny positions, not halt
        "extreme_fear":   0.00,   # ★ Only VIX > 45 now triggers full halt
    }

    # ── Earnings Blackout ─────────────────────────────────────────────────────
    EARNINGS_BLACKOUT_DAYS_BEFORE = 3
    EARNINGS_BLACKOUT_DAYS_AFTER  = 1
    EARNINGS_EXIT_BEFORE_MINS     = 120

    # ── News Sentiment ────────────────────────────────────────────────────────
    SENTIMENT_MIN_SCORE   = -0.3
    SENTIMENT_BOOST_SCORE = 0.3
    SENTIMENT_CACHE_MINS  = 30
    SENTIMENT_EDGE_BOOST  = 0.005
    SENTIMENT_EDGE_PENALTY= 0.010
    SENTIMENT_SCAN_BUDGET_SECS = float(os.getenv("SENTIMENT_SCAN_BUDGET_SECONDS", "45"))
    EARNINGS_SCAN_BUDGET_SECS = float(os.getenv("EARNINGS_SCAN_BUDGET_SECONDS", "30"))
    FUNDAMENTALS_SCAN_BUDGET_SECS = float(os.getenv("FUNDAMENTALS_SCAN_BUDGET_SECONDS", "30"))

    # ── ★ Stop/Target (ATR-based) ─────────────────────────────────────────────
    # STOP_MAX 4% (was 6%): realized winners cluster at +0.5–2%, so a single 6%
    # stop-out erased ~3 average winners (ZFCVINDIA −6.26% = worst trade on
    # record). A name whose ATR demands a wider stop than 4% is too volatile to
    # manage at this win-size profile.
    STOP_MIN_PCT       = 0.020
    STOP_MAX_PCT       = 0.040
    TAKE_PROFIT_RR     = 2.5
    TRAILING_STOP_PCT  = float(os.getenv("TRAILING_STOP_PCT", "0.04"))  # ★ relaxed to 4%
    STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT",   "0.03"))
    TAKE_PROFIT_PCT    = float(os.getenv("TAKE_PROFIT_PCT", "0.07"))

    # ── Portfolio Beta Cap ────────────────────────────────────────────────────
    MAX_PORTFOLIO_BETA    = 1.30

    # ── Exit Triggers ─────────────────────────────────────────────────────────
    PROB_DROP_THRESHOLD = 0.18
    TIME_DECAY_HOURS    = 120

    # ── ★ Churn control / hold discipline ──────────────────────────────────────
    # The model forecasts a 5-day forward return, so intraday flip-flop exits are
    # a horizon mismatch. These gates stop the enter→breakeven→exit churn seen in
    # the logs (9 positions opened 11:00, all dumped 13:47 at 0%).
    MIN_HOLD_HOURS         = float(os.getenv("MIN_HOLD_HOURS", "18"))      # no ML/time exit before this (SL/TP always live)
    REENTRY_COOLDOWN_HOURS = float(os.getenv("REENTRY_COOLDOWN_HOURS", "24"))  # don't re-buy a name just exited
    ML_REVERSAL_CONFIRM_N  = int(os.getenv("ML_REVERSAL_CONFIRM_N", "3"))  # consecutive breaches before ML exit fires
    ML_EXIT_REQUIRE_LOSS   = float(os.getenv("ML_EXIT_REQUIRE_LOSS", "-0.005"))  # only ML-exit if also losing (avoid flushing breakeven)

    # ── ★ Entry-quality hard gates (analyse before buying) ──────────────────────
    MAX_ENTRY_RSI       = float(os.getenv("MAX_ENTRY_RSI", "0.72"))   # normalized RSI; 0.72 ≈ RSI 72 → don't chase overbought
    MIN_VOLUME_RATIO    = float(os.getenv("MIN_VOLUME_RATIO", "0.5")) # need real participation
    MAX_ENTRY_ATR_PCT   = float(os.getenv("MAX_ENTRY_ATR_PCT", "0.08"))  # skip names too volatile to manage

    # ── ★ Liquidity gate (beyond volume_ratio): minimum average daily traded
    #   VALUE in ₹. A relative volume ratio says nothing about absolute
    #   tradability — a thin micro-cap can spike its own ratio. We require a real
    #   ₹ turnover floor so entry/exit slippage stays negligible vs position size.
    MIN_ADV_VALUE       = float(os.getenv("MIN_ADV_VALUE", "250000000"))  # ₹25 cr median daily turnover
    ADV_LOOKBACK_DAYS   = int(os.getenv("ADV_LOOKBACK_DAYS", "20"))

    # ── ★ Extended / parabolic guard: don't chase names that have already run
    #   hard AND are highly volatile (classic mean-reversion bait — this is what
    #   stopped out NETWEB −10% and trapped the OLECTRA/TEJASNET small-caps).
    #   Rejected only when BOTH conditions hold.
    MAX_EXT_MOMENTUM    = float(os.getenv("MAX_EXT_MOMENTUM", "0.15"))  # 20-day return > 15% …
    MAX_EXT_ATR_PCT     = float(os.getenv("MAX_EXT_ATR_PCT", "0.035"))  # … AND ATR > 3.5% → skip

    # ── ★ News / earnings gating toggles ────────────────────────────────────────
    USE_EARNINGS_GATE   = os.getenv("USE_EARNINGS_GATE", "true").lower() == "true"
    USE_SENTIMENT_GATE  = os.getenv("USE_SENTIMENT_GATE", "true").lower() == "true"
    # ★ Fundamentals screen: drop confirmed-fragile businesses (negative operating
    #   margin AND negative FCF) from final signal candidates + small edge tilt.
    USE_FUNDAMENTALS_GATE = os.getenv("USE_FUNDAMENTALS_GATE", "true").lower() == "true"

    # ── ★ Staged exits: trim at first target, let the rest ride ─────────────────
    # At PARTIAL_TP_AT × the TP distance, sell PARTIAL_TP_FRACTION of the position
    # and raise the stop to breakeven+costs — books a real gain while keeping the
    # remainder's upside to full TP (replaces the old close-everything EOD harvest).
    USE_PARTIAL_EXITS   = os.getenv("USE_PARTIAL_EXITS", "true").lower() == "true"
    PARTIAL_TP_FRACTION = float(os.getenv("PARTIAL_TP_FRACTION", "0.5"))
    PARTIAL_TP_AT       = float(os.getenv("PARTIAL_TP_AT", "0.6"))

    # ── ★ Cost-aware viability gate (slippage + spread) ─────────────────────────
    USE_VIABILITY_GATE  = os.getenv("USE_VIABILITY_GATE", "true").lower() == "true"
    MIN_NET_EDGE        = float(os.getenv("MIN_NET_EDGE", "0.005"))   # net EV after round-trip costs must clear 0.5%
    # Fixed round-trip fees/taxes deducted from realized P&L on close (STT +
    # brokerage + GST + stamp, approx for NSE delivery). Modeled slippage is added
    # on top of this in ExecutionEngine.close_trade.
    FEE_PCT             = float(os.getenv("FEE_PCT", "0.0011"))
    # Data-feed health: halt new entries if the market-index quote is older than
    # this (targets hours/days-stale data; generous vs Yahoo's normal delay).
    MAX_QUOTE_STALENESS_MINS = float(os.getenv("MAX_QUOTE_STALENESS_MINS", "30"))
    # A stalled process must not resume opening risk after its liveness marker has
    # gone quiet. This is a local software-health check, not exchange protection.
    MAX_HEARTBEAT_AGE_SECS = float(os.getenv("MAX_HEARTBEAT_AGE_SECONDS", "180"))

    # ── Portfolio Risk ────────────────────────────────────────────────────────
    MAX_DRAWDOWN_HALT  = float(os.getenv("MAX_DRAWDOWN_HALT", "0.10"))
    # ★ 20 (was 10): a hard count cap was rejecting good candidates while real
    # capital sat idle — on 2026-07-06 the book hit 10/10 at ₹71k deployed while
    # the 85% exposure cap allows ₹85k, so ~₹14k of room (2-3 more positions)
    # went unused and 11 signals were rejected purely on count. At min position
    # size (₹5,000) capital/sector/beta caps naturally bind around ~17 positions
    # anyway, so 20 lets THOSE principled limits govern instead of an arbitrary
    # integer — rotation still protects any healthy holding; this just stops
    # turning away new opportunities at the door while capital is available.
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
    # =============================================================================
    # [MODIFIED 2026-11-03]: Expand sector limits for diversified industries & energy
    # ORIGINAL INTENT: SECTOR_LIMITS capped sector exposure as a fraction of equity.
    # 'other' defaulted to 0.20 (20%), which instantly saturated when holding 2 stocks
    # across the ~200 non-standard sectors in NIFTY 500 (auto, capital goods, telecom, realty).
    # REASON FOR CHANGE: Prevent 1,142+ warnings and candidate rejections for non-correlated
    # industrial and telecom stocks by expanding 'other' headroom to 35% and 'energy' to 25%.
    # SAFE ROLLBACK: The original code is preserved below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    #     SECTOR_LIMITS = {
    #         "technology": 0.40, "finance": 0.25, "healthcare": 0.25,
    #         "consumer":   0.20, "energy":  0.15, "etf": 0.30, "other": 0.20,
    #     }
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    SECTOR_LIMITS = {
        "technology": float(os.getenv("SECTOR_LIMIT_TECH", "0.40")),
        "finance":    float(os.getenv("SECTOR_LIMIT_FINANCE", "0.25")),
        "healthcare": float(os.getenv("SECTOR_LIMIT_HEALTHCARE", "0.25")),
        "consumer":   float(os.getenv("SECTOR_LIMIT_CONSUMER", "0.20")),
        "energy":     float(os.getenv("SECTOR_LIMIT_ENERGY", "0.25")),
        "etf":        float(os.getenv("SECTOR_LIMIT_ETF", "0.30")),
        "other":      float(os.getenv("SECTOR_LIMIT_OTHER", "0.35")),
    }
    # =============================================================================

    # ── ★ Position rotation (swap a decayed holding for a much stronger name) ────
    #   Problem this fixes: once the book is full (MAX_OPEN_POSITIONS) the loop used
    #   to just stop, so stale "weak"/"watch" holdings were kept while fresh STRONG
    #   BUY candidates (higher analyst composite) sat unowned. Rotation dumps the
    #   weakest *decayed* holding ONLY when an incoming candidate is materially
    #   stronger — never churns a still-healthy position.
    ENABLE_ROTATION           = os.getenv("ENABLE_ROTATION", "true").lower() == "true"
    # Incoming candidate must itself be at least this strong (composite [-1,1]);
    # 0.35 ≈ "BUY-worthy" or better. We never rotate in a mediocre name.
    ROTATION_MIN_CANDIDATE_SCORE = float(os.getenv("ROTATION_MIN_CANDIDATE_SCORE", "0.35"))
    # A holding is only eligible to be dropped if its *current* composite has
    # decayed below this (0.20 ≈ "watch" or worse) OR its news has turned negative.
    ROTATION_STALE_SCORE      = float(os.getenv("ROTATION_STALE_SCORE", "0.20"))
    # Candidate composite must exceed the dumped holding's by at least this margin
    # (covers the round-trip cost of selling one name to buy another).
    ROTATION_MIN_SCORE_GAP    = float(os.getenv("ROTATION_MIN_SCORE_GAP", "0.25"))
    # Don't rotate a position younger than this (respects hold discipline / churn).
    ROTATION_MIN_HOLD_HOURS   = float(os.getenv("ROTATION_MIN_HOLD_HOURS", "18"))
    # Cap swaps per cycle so a fresh scan can't thrash the whole book at once.
    MAX_ROTATIONS_PER_CYCLE   = int(os.getenv("MAX_ROTATIONS_PER_CYCLE", "2"))

    # ── Ensemble ML ───────────────────────────────────────────────────────────
    USE_ENSEMBLE        = True
    ENSEMBLE_LGB_WEIGHT = 0.60
    ENSEMBLE_XGB_WEIGHT = 0.40
    # Minimum validation/walk-forward AUC before the ML model is trusted enough to
    # blend into the decision (below this, the analyst engine drives alone).
    ML_MIN_AUC          = float(os.getenv("ML_MIN_AUC", "0.52"))
    MIN_TRAIN_SAMPLES   = int(os.getenv("MIN_TRAIN_SAMPLES", "40"))
    RETRAIN_EVERY_N     = int(os.getenv("RETRAIN_EVERY_N",   "15"))
    # ★ Never retrain mid-session (it shifts the probability surface and triggers
    #   mass exits/rotation). The scheduler retrains after-hours instead.
    RETRAIN_IN_SESSION  = os.getenv("RETRAIN_IN_SESSION", "false").lower() == "true"
    OVERFIT_AUC_DELTA   = 0.08

    @classmethod
    def live_entry_status(cls):
        """Return ``(allowed, reason)`` for new orders in the current process.

        Paper and dry-run configurations are allowed to use their existing
        simulated/observation path. A real broker remains usable for monitoring
        and exits when any live-entry prerequisite is absent.
        """
        if not (cls.BROKER in _LIVE_BROKERS and not cls.DRY_RUN):
            return True, "paper or dry-run"
        if not cls.ENABLE_LIVE_TRADING:
            return False, "ENABLE_LIVE_TRADING is not true"
        if cls.LIVE_TRADING_ACK != cls.LIVE_TRADING_ACK_TEXT:
            return False, "LIVE_TRADING_ACK is missing or invalid"
        if getattr(cls, "IGNORE_MARKET_HOURS", False):
            return False, "IGNORE_MARKET_HOURS is unsafe for live entries"
        if not all((getattr(cls, "USE_EARNINGS_GATE", False),
                    getattr(cls, "USE_SENTIMENT_GATE", False),
                    getattr(cls, "USE_FUNDAMENTALS_GATE", False))):
            return False, "all live data gates must remain enabled"
        return True, "enabled"

    # ── Trading Hours ─────────────────────────────────────────────────────────
    # =============================================================================
    # [MODIFIED 2026-11-02]: Parameterize EOD_EXECUTION_ONLY with environment variable
    # ORIGINAL INTENT: Hardcoded to True to restrict trading to the final 60 minutes of the session.
    # REASON FOR CHANGE: Blocked intraday entries during regular market hours (e.g. 09:35 to 14:30 IST),
    # preventing paper and live day trades from executing.
    # SAFE ROLLBACK: Preserved original implementation below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # EOD_EXECUTION_ONLY        = True
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    EOD_EXECUTION_ONLY        = os.getenv("EOD_EXECUTION_ONLY", "false").lower() == "true"
    # =============================================================================
    EOD_EXECUTION_WINDOW_MINS = 60
    TRADE_AFTER_OPEN_MIN   = 20
    TRADE_BEFORE_CLOSE_MIN = 5

    # ── Cache ──────────────────────────────────────────────────────────────────
    QUOTE_CACHE_TTL   = 5
    REGIME_CACHE_SECS = 300
