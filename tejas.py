"""
tejas.py — Tejas Live Trading Entry Point
==============================================
THE MAIN RUNNER. Start with:
    python tejas.py               # paper trading (DRY_RUN=true in .env)
    DRY_RUN=false python tejas.py # live trading

Architecture (NSE equity; market data from Yahoo, orders via an Indian broker):
  RegimeEngine      → India-VIX/trend-based market regime detection
  SignalEngineGold  → scans the live NSE universe for ML-validated edge
  DynamicRiskManager → position sizing, ALL safety limits
  ExecutionEngine   → market entry; SL/TP are SOFTWARE-managed (see ExitEngine)
  ExitEngine        → the SOLE enforcer of SL/TP/trailing (no exchange-side stop)
  Scheduler         → background: retrain ML, daily summary, DB cleanup
  Telegram          → every trade alert, drawdown warning, daily PnL

⚠️  Stops are software-only: there is no native bracket for NSE delivery equity,
    so SL/TP fire only while this process is alive and monitoring each cycle.

SAFETY LAYERS (must pass ALL before any order fires):
  1. Account/data health    (halt entries if equity unknown or prices stale)
  2. Market hours gate      (20min after open, 35min before close)
  3. Regime gate            (extreme_fear = halt; high_fear = tiny sizes)
  4. Earnings blackout      (3 days before / 1 day after)
  5. Sentiment gate         (very negative news = hard block)
  6. Edge gate              (net edge after slippage > MIN_EDGE)
  7. Risk manager approval  (drawdown, position limits, sector caps, beta cap)
  8. DRY_RUN=true default   (no real orders without explicit env override)
"""
import json
import logging
import math
import logging.handlers
import os
import signal as _signal_mod
import sys
import threading
import time
from datetime import datetime, timezone

import db
import edge_engine
import telegram_alerts
from brokers import get_broker
from config import Config
from dynamic_risk import DynamicRiskManager
from earnings_filter import EarningsFilter
from execution_engine import ExecutionEngine
from exit_engine import ExitEngine
from feature_builder_gold import FeatureBuilderGold
from ml_engine_gold import MLEngineGold
from models import Portfolio, Trade, FeatureVector
from regime_engine import RegimeEngine
from scheduler import build_scheduler
from sentiment_engine import SentimentEngine
from signal_engine_gold import SignalEngineGold

# ★ Rotating log: the old append-forever FileHandler grew tejas.log past 270MB
# (per-symbol analyst lines × every 30s cycle). 25MB × 4 backups caps disk use
# while keeping days of history.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tejas.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_PATH, mode="a", encoding="utf-8",
            maxBytes=25 * 1024 * 1024, backupCount=4),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")

_RUNNING = True
_LOCK_SOCK = None   # held for process lifetime to enforce single-instance
# ★ Interruptible sleep: the main loop sleeps 30–600s between cycles, and a bare
# time.sleep can't be woken — a graceful stop used to wait out the full nap
# (why the dashboard's Stop felt stuck for up to a minute). All loop sleeps go
# through _sleep(), which returns immediately once a stop is requested.
_STOP_EVENT = threading.Event()


def _sleep(secs: float) -> None:
    _STOP_EVENT.wait(secs)


def _acquire_single_instance() -> bool:
    """Bind a localhost port as a mutex. If another Tejas already holds it,
    refuse to start — prevents two bots trading the same account/DB.

    ★ A drain thread ACCEPTS every probe connection. Without it, the first
    dashboard probe filled the listen(1) backlog forever, so every later
    `is the bot running?` check timed out and reported STOPPED while the bot
    traded — defeating duplicate-start protection and the reset guard.
    The same channel doubles as a graceful-stop endpoint: a peer that sends
    b"TEJAS_STOP" flips _RUNNING so the bot finishes its cycle and exits
    cleanly (Windows terminate() is a hard kill that skips all shutdown code)."""
    global _LOCK_SOCK
    import socket, threading
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 8766))
        s.listen(8)
        _LOCK_SOCK = s   # keep reference open so the lock persists

        def _drain():
            global _RUNNING
            while True:
                try:
                    conn, _ = s.accept()
                except OSError:
                    return   # socket closed at shutdown
                try:
                    conn.settimeout(0.5)
                    data = b""
                    try:
                        data = conn.recv(32)
                    except OSError:
                        pass   # plain liveness probe — connect & close
                    if data.strip() == b"TEJAS_STOP":
                        if not _RUNNING:
                            # ★ Escalation: a REPEAT stop while already stopping
                            # means the loop is wedged mid-cycle (e.g. a slow
                            # data-feed crawl) and can't reach the flag check.
                            # Exit hard — open positions are in the DB and the
                            # restore path handles them on next start.
                            log.warning("[Main] Second stop request while stopping "
                                        "— forcing immediate exit")
                            os._exit(2)
                        log.info("[Main] Graceful stop requested — finishing cycle")
                        _RUNNING = False
                        _STOP_EVENT.set()   # wake any in-progress sleep immediately
                finally:
                    conn.close()

        threading.Thread(target=_drain, daemon=True, name="lock-drain").start()
        return True
    except OSError:
        return False


def _signal_handler(sig, frame):
    global _RUNNING
    log.info("[Main] Shutdown signal — finishing current cycle then stopping")
    _RUNNING = False
    _STOP_EVENT.set()   # wake any in-progress sleep immediately


# ──────────────────────────────────────────────────────────────────────────────
# STARTUP: RESTORE OPEN POSITIONS FROM DB
# ──────────────────────────────────────────────────────────────────────────────

def _restore_open_trades(portfolio: Portfolio) -> None:
    """
    On restart, reload any open trades from the DB so the exit engine
    can monitor them. Without this, a restart orphans open positions.
    """
    rows = db.fetchall(
        "SELECT * FROM trades WHERE status='open' ORDER BY entry_time ASC"
    )
    if not rows:
        log.info("[Main] No open trades to restore")
        return

    restored = 0
    for row in rows:
        try:
            # Try to get features from training_data
            feat_row = db.fetchone(
                "SELECT features_json FROM training_data WHERE trade_id=?",
                (row["trade_id"],),
            )
            if feat_row and feat_row["features_json"]:
                feat_dict = json.loads(feat_row["features_json"])
                features = _dict_to_feature_vector(
                    row["trade_id"], row["symbol"],
                    datetime.fromisoformat(row["entry_time"]),
                    feat_dict,
                )
            else:
                features = _minimal_feature_vector(
                    row["trade_id"], row["symbol"],
                    datetime.fromisoformat(row["entry_time"]),
                )

            trade = Trade(
                trade_id=row["trade_id"],
                symbol=row["symbol"],
                side=row["side"],
                shares=float(row["shares"] or 0),
                size_usd=float(row["size_usd"]),
                entry_price=float(row["entry_price"]),
                stop_loss=float(row["stop_loss"]),
                take_profit=float(row["take_profit"]),
                ml_probability=float(row["ml_probability"] or 0.55),
                entry_quality=float(row["entry_quality"] or 0.50),
                kelly_pct=float(row["kelly_pct"] or 0.02),
                features=features,
                alpaca_order_id=row["alpaca_order_id"] or "",
                entry_time=datetime.fromisoformat(row["entry_time"]),
                status="open",
                # ★ Restore the persisted trailing high-water mark — resetting it
                # to entry_price disarmed profit protection on every restart.
                highest_price=max(
                    float(row["highest_price"] or 0), float(row["entry_price"])),
                last_ml_prob=float(row["ml_probability"] or 0.55),
                trim_shares=float(row["trim_shares"] or 0),
                trim_price=float(row["trim_price"]) if row["trim_price"] else None,
                rationale=row["rationale"] or "",
            )
            portfolio.open_trades.append(trade)
            # ★ Rebuild sector exposure — without this the sector caps saw an
            # empty book after every restart and approved ~2x the sector limit.
            from dynamic_risk import _SECTOR_NAMES
            sector = _SECTOR_NAMES.get(trade.features.sector_code, "other")
            portfolio.sector_exposure[sector] = (
                portfolio.sector_exposure.get(sector, 0.0) + trade.size_usd)
            restored += 1
            log.info(
                f"[Main] Restored: {trade.symbol} ₹{trade.size_usd:.2f} "
                f"@ {trade.entry_price:.2f} | SL={trade.stop_loss:.2f}"
            )
        except Exception as e:
            log.warning(f"[Main] Could not restore trade {row['trade_id'][:8]}: {e}")

    log.info(f"[Main] Restored {restored} open position(s)")


def _dict_to_feature_vector(
    trade_id: str, symbol: str, timestamp: datetime, d: dict
) -> FeatureVector:
    """Reconstruct FeatureVector from stored JSON dict.

    Older/runtime-corrupted rows must not make an open position disappear from
    the in-memory book on restart. Invalid numeric values fall back to the same
    neutral defaults used by FeatureVector.
    """
    if not isinstance(d, dict):
        d = {}

    def value(name: str, default: float) -> float:
        try:
            result = float(d.get(name, default))
            return result if math.isfinite(result) else default
        except (TypeError, ValueError):
            return default

    return FeatureVector(
        trade_id=trade_id, symbol=symbol, timestamp=timestamp,
        price_vs_sma20=value("price_vs_sma20", 0.0),
        price_vs_ema9=value("price_vs_ema9", 0.0),
        ema9_vs_ema21=value("ema9_vs_ema21", 0.0),
        rsi_14=value("rsi_14", 0.5),
        momentum_5d=value("momentum_5d", 0.0),
        momentum_10d=value("momentum_10d", 0.0),
        momentum_20d=value("momentum_20d", 0.0),
        rate_of_change=value("rate_of_change", 0.0),
        macd_histogram=value("macd_histogram", 0.0),
        macd_trend=value("macd_trend", 0.0),
        atr_pct=value("atr_pct", 0.02),
        bb_position=value("bb_position", 0.5),
        bb_squeeze=value("bb_squeeze", 0.02),
        volatility_20d=value("volatility_20d", 0.2),
        volume_ratio=value("volume_ratio", 1.0),
        volume_trend=value("volume_trend", 0.0),
        vwap_deviation=value("vwap_deviation", 0.0),
        spy_corr=value("spy_corr", 0.5),
        relative_strength=value("relative_strength", 0.0),
        sector_strength=value("sector_strength", 0.0),
        sector_code=int(value("sector_code", 6)),
        candle_stability=value("candle_stability", 0.5),
        # Gold features
        williams_r=value("williams_r", 0.5),
        stoch_k=value("stoch_k", 0.5),
        stoch_d=value("stoch_d", 0.5),
        cci=value("cci", 0.0),
        obv_trend=value("obv_trend", 0.0),
        mfi=value("mfi", 0.5),
        dist_52w_high=value("dist_52w_high", 0.2),
        dist_52w_low=value("dist_52w_low", 0.5),
        price_vs_ema200=value("price_vs_ema200", 0.0),
        higher_highs=value("higher_highs", 0.5),
        intraday_range_ratio=value("intraday_range_ratio", 1.0),
        bid_ask_spread=value("bid_ask_spread", 0.001),
        market_trend_20d=value("market_trend_20d", 0.0),
        market_vol_20d=value("market_vol_20d", 0.0),
        trend_alignment=value("trend_alignment", 0.5),
    )


def _minimal_feature_vector(
    trade_id: str, symbol: str, timestamp: datetime
) -> FeatureVector:
    """Minimal safe FeatureVector for restored trades when features were lost."""
    return FeatureVector(
        trade_id=trade_id, symbol=symbol, timestamp=timestamp,
        price_vs_sma20=0.0, price_vs_ema9=0.0, ema9_vs_ema21=0.0,
        rsi_14=0.5, momentum_5d=0.0, momentum_10d=0.0, momentum_20d=0.0,
        rate_of_change=0.0, macd_histogram=0.0, macd_trend=0.0,
        atr_pct=0.02, bb_position=0.5, bb_squeeze=0.02, volatility_20d=0.20,
        volume_ratio=1.0, volume_trend=0.0, vwap_deviation=0.0,
        spy_corr=0.5, relative_strength=0.0, sector_strength=0.0,
        sector_code=6,
    )


# ──────────────────────────────────────────────────────────────────────────────
# EMERGENCY: CLOSE ALL POSITIONS
# ──────────────────────────────────────────────────────────────────────────────

def _emergency_close_all(
    portfolio: Portfolio,
    exec_e: ExecutionEngine,
    risk_mgr: DynamicRiskManager,
    exit_e: ExitEngine,
    broker,
    reason: str,
) -> None:
    """Emergency: close every open position immediately.

    Routes each close through risk_mgr.close_trade so the daily-loss breaker,
    sector budgets and re-entry cooldowns all see the liquidation (the old
    open_trades.clear() bypassed every one of those — the biggest loss event of
    the day never hit the breaker and sector budgets were left stuck)."""
    if not portfolio.open_trades:
        return
    log.warning(f"[Main] EMERGENCY CLOSE ALL — reason: {reason}")
    for trade in list(portfolio.open_trades):
        try:
            quote = broker.get_quote(trade.symbol, force=True)
            exit_price = quote.mid if quote and quote.mid > 0 else trade.entry_price
            pnl_pct = (exit_price - trade.entry_price) / max(trade.entry_price, 0.01)
            if not exec_e.close_trade(trade, exit_price, pnl_pct, reason):
                log.error(f"[Main] Emergency close REJECTED by broker for {trade.symbol} — stays in book for retry")
                continue
            risk_mgr.close_trade(trade, portfolio)
            exit_e.purge([trade.trade_id], [trade.symbol])
            log.warning(f"[Main] Emergency closed {trade.symbol} @ {exit_price:.2f}")
        except Exception as e:
            log.error(f"[Main] Emergency close failed {trade.symbol}: {e}")
    # Anything that failed to close stays in the book so we retry it, rather than
    # silently forgetting it with a blanket clear().


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY ATTEMPT  (single source of truth for all pre-buy gates + sizing + order)
# ──────────────────────────────────────────────────────────────────────────────

def _prevalidate_entry(signal, portfolio, regime, ml, quiet: bool = False):
    """
    All book-INDEPENDENT pre-buy checks: entry gates, setup quality, Kelly sizing,
    and cost-aware viability. None of these depend on a free slot, so rotation can
    run them BEFORE selling anything — if a candidate can't clear them, we never
    sell a holding for it.

    Returns (ok, reason, entry_quality, kelly_pct, raw_size).
    reason ∈ {"ok", "gate", "size", "risk"}. `quiet` suppresses skip logging
    (used for the rotation pre-check so we don't spam the log).
    """
    f = signal.features
    try:
        entry_price = float(signal.entry_price)
        stop_loss = float(signal.stop_loss)
        take_profit = float(signal.take_profit)
    except (TypeError, ValueError):
        return False, "gate", 0.0, 0.0, 0.0
    if (not all(math.isfinite(v) for v in (entry_price, stop_loss, take_profit))
            or entry_price <= 0 or stop_loss <= 0
            or stop_loss >= entry_price or take_profit <= entry_price):
        return False, "gate", 0.0, 0.0, 0.0

    def _skip(msg, reason):
        if not quiet:
            log.info(msg)
        return False, reason, 0.0, 0.0, 0.0

    if f.rsi_14 > Config.MAX_ENTRY_RSI:
        return _skip(f"[Main] ⤫ {signal.symbol}: overbought RSI≈{f.rsi_14*100:.0f} "
                     f"(> {Config.MAX_ENTRY_RSI*100:.0f}) — skip", "gate")
    if f.volume_ratio < Config.MIN_VOLUME_RATIO:
        return _skip(f"[Main] ⤫ {signal.symbol}: thin volume {f.volume_ratio:.2f}x — skip", "gate")
    if f.atr_pct > Config.MAX_ENTRY_ATR_PCT:
        return _skip(f"[Main] ⤫ {signal.symbol}: too volatile ATR={f.atr_pct:.1%} — skip", "gate")

    # ── ★ Extended/parabolic guard (previously configured but never enforced):
    #   don't chase a name that has already run hard AND is highly volatile —
    #   classic mean-reversion bait. Rejected only when BOTH conditions hold.
    if (f.momentum_20d > Config.MAX_EXT_MOMENTUM
            and f.atr_pct > Config.MAX_EXT_ATR_PCT):
        return _skip(f"[Main] ⤫ {signal.symbol}: extended/parabolic "
                     f"(20d mom={f.momentum_20d:.1%}, ATR={f.atr_pct:.1%}) — skip", "gate")

    entry_quality = ml.entry_quality(signal.features, signal.edge)
    if entry_quality < Config.MIN_ENTRY_QUALITY:
        return _skip(f"[Main] ⤫ {signal.symbol}: low setup quality {entry_quality:.2f} "
                     f"(< {Config.MIN_ENTRY_QUALITY}) — skip", "gate")

    kelly_pct, raw_size = ml.kelly_bet(
        ml_prob=signal.ml_prob, entry_quality=entry_quality,
        bankroll=portfolio.bankroll, drawdown=portfolio.drawdown,
        regime_mult=regime.get("size_mult", 1.0),
        tp_pct=(take_profit - entry_price) / entry_price,
        sl_pct=(entry_price - stop_loss) / entry_price,
    )
    if raw_size < Config.MIN_POSITION_USD:
        return _skip(f"[Main] ⤫ {signal.symbol}: size ₹{raw_size:.0f} "
                     f"< min ₹{Config.MIN_POSITION_USD:.0f} (kelly={kelly_pct:.1%}) — skip", "size")

    if Config.USE_VIABILITY_GATE:
        tp_pct = (take_profit - entry_price) / entry_price
        sl_pct = max(1e-4, (entry_price - stop_loss) / entry_price)
        v = edge_engine.trade_viability(
            p=signal.ml_prob, target_pct=tp_pct, stop_pct=sl_pct,
            price=signal.entry_price, bankroll=portfolio.bankroll, order_usd=raw_size,
            adv_usd=Config.MIN_ADV_VALUE,
            spread_pct=max(0.0005, signal.features.bid_ask_spread),
            volatility=signal.features.atr_pct, min_edge=Config.MIN_NET_EDGE,
        )
        if not v.viable:
            return _skip(f"[Main] ⤫ {signal.symbol}: not viable after costs ({v.reason})", "risk")

    return True, "ok", entry_quality, kelly_pct, raw_size


def _attempt_entry(signal, portfolio, regime, ml, exec_e, risk_mgr):
    """
    Run every pre-buy gate, size the position, and place the order for one signal.
    Used by BOTH the normal entry loop and the rotation pass so the two paths can
    never drift apart.

    Returns (trade_or_None, reason) where reason ∈
        {"entered", "gate", "size", "risk", "failed", "dry_run"}.
    """
    ok, reason, entry_quality, kelly_pct, raw_size = _prevalidate_entry(
        signal, portfolio, regime, ml
    )
    if not ok:
        return None, reason

    # ── Risk manager approval (book-dependent: slots, sector, beta, capital) ─
    approved_size = risk_mgr.approve(
        signal=signal, kelly_pct=kelly_pct, raw_size=raw_size,
        portfolio=portfolio, regime=regime,
    )
    if approved_size is None:
        log.info(f"[Main] ⤫ {signal.symbol}: rejected by risk manager (see [Risk] above)")
        return None, "risk"

    # ── Execute ─────────────────────────────────────────────────────────────
    trade = exec_e.execute(
        signal=signal, approved_size=approved_size,
        kelly_pct=kelly_pct, entry_quality=entry_quality,
    )
    if not trade:
        if getattr(exec_e, "last_was_dry_run", False):
            return None, "dry_run"
        log.warning(f"[Main] Order failed for {signal.symbol}")
        return None, "failed"

    # ── Register in the in-memory book (open_trades, sector exposure) ────────
    #   This MUST happen for every successful entry — both the normal loop and
    #   rotation rely on portfolio.open_trades for position/sector/beta limits
    #   and for the exit engine's monitor list. Done here so neither path can
    #   forget it.
    risk_mgr.open_trade(trade, portfolio)
    telegram_alerts.alert_trade_entry(signal, trade)

    log.info(
        f"[Main] ✅ ENTRY {trade.symbol} ₹{approved_size:.2f} @ {signal.entry_price:.2f} | "
        f"SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f} | "
        f"edge={signal.edge:.3f} q={entry_quality:.3f} ml_prob={signal.ml_prob:.1%}"
    )
    return trade, "entered"


def _hours_held(entry_time: datetime, now: datetime) -> float:
    et = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
    return (now - et).total_seconds() / 3600.0


def _feed_stale(broker) -> bool:
    """True if the price feed looks stale (don't open new positions on it).

    Checks the market-index quote's *exchange* timestamp. A missing quote or one
    older than MAX_QUOTE_STALENESS_MINS means our data source is degraded — during
    which the safe action is to halt new entries rather than trade on old prices.
    (Threshold is generous to tolerate Yahoo's normal delay; it targets hours/days-
    stale data, not a 15-minute lag.)"""
    if getattr(Config, "IGNORE_MARKET_HOURS", False):
        return False
    try:
        q = broker.get_quote(Config.MARKET_INDEX_SYMBOL, force=True)
        if q is None:
            return True
        age_min = (datetime.now(timezone.utc) - q.timestamp).total_seconds() / 60.0
        return age_min > getattr(Config, "MAX_QUOTE_STALENESS_MINS", 30)
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────────────────────
# ROTATION  (news-aware: swap a decayed/negative-news holding for a stronger name)
# ──────────────────────────────────────────────────────────────────────────────

def _run_rotation(candidates, portfolio, regime, sig_engine,
                  ml, exec_e, exit_e, risk_mgr, broker) -> int:
    """
    Book is full. Try to swap the weakest *decayed* holding for the strongest
    fresh candidate — but only when the candidate is materially better AND the
    holding has genuinely lost its edge (low current composite OR negative news).

    `candidates` must be pre-sorted by analyst_score descending and exclude names
    already held. Returns the number of rotations performed.
    """
    if not candidates:
        return 0

    scores = sig_engine.last_scores          # symbol -> current composite [-1,1]
    now = datetime.now(timezone.utc)
    rotations = 0

    for cand in candidates:
        if rotations >= Config.MAX_ROTATIONS_PER_CYCLE:
            break
        # Candidates are sorted strongest-first, so once one is too weak to bother
        # rotating in, every later one is too — stop.
        if cand.analyst_score < Config.ROTATION_MIN_CANDIDATE_SCORE:
            break

        try:
            # ── The candidate must clear every book-INDEPENDENT gate BEFORE we
            #    sell anything. Otherwise a structurally un-enterable name (fails
            #    quality/viability/min-size every cycle) would bleed the book by
            #    triggering a sell it can never replace. ──────────────────────
            ok, _reason, _eq, _kelly, _raw = _prevalidate_entry(
                cand, portfolio, regime, ml, quiet=True
            )
            if not ok:
                continue

            # ── Which current holdings are eligible to be dropped? ──────────
            eligible = []
            for t in portfolio.open_trades:
                if _hours_held(t.entry_time, now) < Config.ROTATION_MIN_HOLD_HOURS:
                    continue
                cur_score = scores.get(t.symbol)
                neg_news  = sig_engine.sentiment.get_score(t.symbol) <= Config.SENTIMENT_MIN_SCORE
                if cur_score is None:
                    # Couldn't score it this scan (data gap / left universe). Don't
                    # dump a position we can't currently assess UNLESS news has
                    # independently turned negative — a one-cycle glitch must not
                    # flush a good holding.
                    if neg_news:
                        eligible.append((Config.ROTATION_STALE_SCORE - 0.01, True, t))
                    continue
                if cur_score < Config.ROTATION_STALE_SCORE or neg_news:
                    eligible.append((cur_score, neg_news, t))

            if not eligible:
                break   # nothing decayed enough to swap out this cycle

            # Weakest first; a holding with negative news is pushed to the front
            # so bad-news names are the first to go (news drives holding, too).
            eligible.sort(key=lambda x: x[0] - (0.5 if x[1] else 0.0))
            weak_score, weak_neg, weak = eligible[0]

            if cand.analyst_score - weak_score < Config.ROTATION_MIN_SCORE_GAP:
                # Best candidate isn't enough better than the best-droppable
                # holding; weaker candidates won't clear the bar either.
                break

            # ── Sell the decayed holding ─────────────────────────────────────
            quote = broker.get_quote(weak.symbol, force=True)
            exit_price = quote.mid if quote and quote.mid > 0 else weak.entry_price
            pnl_pct = (exit_price - weak.entry_price) / max(weak.entry_price, 0.01)
            reason = f"ROTATION→{cand.symbol}"
            news_tag = " [neg-news]" if weak_neg else ""
            log.info(
                f"[Rotation] ⇄ Dropping {weak.symbol} (score={weak_score:+.2f}{news_tag}, "
                f"pnl={pnl_pct:+.2%}) for {cand.symbol} (score={cand.analyst_score:+.2f}, "
                f"edge={cand.edge:.3f})"
            )
            if not exec_e.close_trade(weak, exit_price, pnl_pct, reason):
                # LIVE flatten failed — holding stays open & monitored; don't buy
                # the candidate on top of an unfreed slot.
                log.warning(f"[Rotation] Could not flatten {weak.symbol} — swap aborted")
                continue
            # ★ The SELL consumes the per-cycle budget, not the completed swap —
            #   otherwise N failed replacements could liquidate N holdings in one
            #   cycle with zero buys, unbounded by MAX_ROTATIONS_PER_CYCLE.
            rotations += 1
            risk_mgr.close_trade(weak, portfolio)   # frees slot + sector budget + cooldown
            exit_e.purge([weak.trade_id], [weak.symbol])
            telegram_alerts.alert_trade_exit(weak)

            # ── Buy the stronger candidate into the freed slot ──────────────
            # (_attempt_entry registers the position in the in-memory book itself)
            trade, r = _attempt_entry(cand, portfolio, regime, ml, exec_e, risk_mgr)
            if trade:
                log.info(f"[Rotation] ✅ Rotated into {cand.symbol} (replaced {weak.symbol})")
            else:
                # Sold the weak name but the candidate didn't take (rare: sector/
                # beta/capital still tight after the sell). Slot is free and the
                # normal path fills it next cycle. No position leak.
                log.warning(
                    f"[Rotation] Sold {weak.symbol} but {cand.symbol} entry failed "
                    f"({r}) — slot left open for next cycle"
                )
        except Exception as e:
            # One bad quote / transient broker error must not abort the whole
            # trading cycle — skip this candidate and carry on.
            log.warning(f"[Rotation] Skipped {getattr(cand,'symbol','?')} on error: {e}")
            continue

    return rotations


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

# ── ★ Offline-gap protection ──────────────────────────────────────────────────
# Stops are software-only: while this process is down mid-session, open positions
# have NO protection. Three defenses:
#   1. Hold a Windows keep-awake while running so the PC can't sleep in-session
#      (the supervisor only runs the bot 09:05–15:45 IST, so nights are free).
#   2. Heartbeat file written every cycle.
#   3. On startup, count how many IN-SESSION minutes passed since the last
#      heartbeat — if positions were unmonitored, warn loudly + Telegram, then
#      let the first monitor pass enforce any gapped-through stops immediately.
HEARTBEAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tejas.heartbeat")


def _keep_system_awake(enable: bool) -> None:
    """Prevent the machine from sleeping while positions are monitored (display
    may still turn off). Cleared on shutdown; the OS also clears it automatically
    if the process dies, so a hard kill can't leave the PC insomniac."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        )
    except Exception:
        pass


def _write_heartbeat() -> None:
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def _offline_gap_minutes() -> float:
    """In-session (NSE) minutes between the last heartbeat and now — the window
    where open positions had no stop protection. 0 if no heartbeat exists."""
    try:
        with open(HEARTBEAT_PATH, encoding="utf-8") as f:
            last_hb = datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        return 0.0
    from datetime import time as dt_time, timedelta
    from brokers.base import IST, SESSION_OPEN, SESSION_CLOSE, NSE_HOLIDAYS
    t0 = last_hb.astimezone(IST)
    t1 = datetime.now(timezone.utc).astimezone(IST)
    if t1 <= t0:
        return 0.0
    total, day = 0.0, t0.date()
    while day <= t1.date():
        if day.weekday() < 5 and day.strftime("%Y-%m-%d") not in NSE_HOLIDAYS:
            o = datetime.combine(day, dt_time(*SESSION_OPEN),  tzinfo=IST)
            c = datetime.combine(day, dt_time(*SESSION_CLOSE), tzinfo=IST)
            lo, hi = max(t0, o), min(t1, c)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


def _restore_equity_peak(start_pv: float) -> float:
    """Highest equity ever snapshotted (or the current value if higher).

    ★ The drawdown halt measured only the CURRENT process's peak before this:
    the supervisor stops the bot at 15:45 and restarts it at 09:05, so peak
    reset every morning and MAX_DRAWDOWN_HALT could never see a multi-day
    decline. The daily portfolio_snapshots table already records bankroll —
    use its max as the true high-water mark."""
    try:
        start = float(start_pv)
        if not math.isfinite(start) or start <= 0:
            start = 0.0
    except (TypeError, ValueError):
        start = 0.0
    try:
        row = db.fetchone("SELECT MAX(bankroll) AS mx FROM portfolio_snapshots")
        hist_peak = float(row["mx"] or 0) if row else 0.0
        if not math.isfinite(hist_peak) or hist_peak <= 0:
            hist_peak = 0.0
    except (TypeError, ValueError, OverflowError):
        hist_peak = 0.0
    except Exception:
        hist_peak = 0.0
    return max(start, hist_peak)


def main() -> None:
    _signal_mod.signal(_signal_mod.SIGINT,  _signal_handler)
    _signal_mod.signal(_signal_mod.SIGTERM, _signal_handler)

    mode = "📋 PAPER" if Config.PAPER_TRADING else "🔴 LIVE"
    dry  = " [DRY RUN — no real orders]" if Config.DRY_RUN else " [ORDERS ACTIVE]"

    if not _acquire_single_instance():
        log.error("[Main] Another Tejas instance is already running — exiting "
                  "to avoid double trading. Stop the other one first.")
        sys.exit(0)

    log.info("═" * 60)
    log.info(f" Tejas  {mode}{dry}")
    log.info(f" DB:      {Config.DB_PATH}")
    log.info(f" Capital: ₹{Config.BANKROLL:,.2f}")
    log.info(f" MinEdge: {Config.MIN_EDGE:.1%}  MaxDD: {Config.MAX_DRAWDOWN_HALT:.0%}  MaxPos: {Config.MAX_OPEN_POSITIONS}")
    try:
        from universe import get_universe
        _u, _ = get_universe()
        log.info(f" Universe:{len(_u)} stocks ({Config.UNIVERSE}, live NSE)  Scan: every {Config.SCAN_INTERVAL_SECS}s")
    except Exception:
        log.info(f" Universe: dynamic ({Config.UNIVERSE})  Scan: every {Config.SCAN_INTERVAL_SECS}s")
    log.info("═" * 60)

    # ── Init DB ────────────────────────────────────────────────────────────
    db.init_db()

    # ── Init all components ────────────────────────────────────────────────
    broker    = get_broker()
    ml        = MLEngineGold()
    builder   = FeatureBuilderGold(broker)
    earnings  = EarningsFilter()
    sentiment = SentimentEngine()
    regime_e  = RegimeEngine(broker)
    risk_mgr  = DynamicRiskManager(broker)
    exec_e    = ExecutionEngine(broker, ml)
    exit_e    = ExitEngine(broker, ml, builder, earnings=earnings)

    # ── Portfolio state ────────────────────────────────────────────────────
    live_pv = broker.get_portfolio_value()
    if not Config.PAPER_TRADING and live_pv <= 0:
        # A LIVE broker that can't return account equity almost always means auth
        # is broken. Refuse to start rather than trade against a guessed bankroll.
        log.error("[Main] 🛑 LIVE broker returned no account value (auth/account "
                  "failure) — refusing to start. Fix broker credentials first.")
        sys.exit(1)
    start_pv = live_pv if live_pv > 0 else Config.BANKROLL
    log.info(f"[Main] Portfolio value: ₹{start_pv:,.2f}")

    portfolio = Portfolio(bankroll=start_pv, peak_bankroll=_restore_equity_peak(start_pv))
    _restore_open_trades(portfolio)
    # ★ Restart-proof breakers: seed the daily-loss circuit breaker and re-entry
    #   cooldowns from the trades ledger (both used to reset to zero on every
    #   supervisor restart, disarming 4%-/day protection mid-day).
    risk_mgr.restore_state_from_db()

    # ── ★ Offline-gap audit + keep-awake ───────────────────────────────────
    # >10 in-session minutes = a real outage (normal supervisor restarts land
    # well under that), and only worth shouting about if positions were exposed.
    gap_mins = _offline_gap_minutes()
    _write_heartbeat()   # fresh baseline now, so a crash loop can't re-alert
    if gap_mins > 10 and portfolio.open_trades:
        log.warning(
            f"[Main] ⚠️ OFFLINE {gap_mins:.0f} in-session min — "
            f"{len(portfolio.open_trades)} open position(s) had no stop protection. "
            "First monitor pass will enforce SL/TP at current prices."
        )
        telegram_alerts.alert_offline_gap(gap_mins, [t.symbol for t in portfolio.open_trades])
    _keep_system_awake(True)   # PC must not sleep while stops are software-only

    # ── Signal engine ──────────────────────────────────────────────────────
    sig_engine = SignalEngineGold(broker, ml, builder, earnings, sentiment)

    # ── Scheduler (background jobs) ────────────────────────────────────────
    scheduler = build_scheduler(ml, portfolio, telegram_alerts, broker)
    scheduler.start()

    # ── Reconciliation Engine (background jobs) ─────────────────────────────
    from reconciliation_engine import ReconciliationEngine
    recon_engine = ReconciliationEngine(broker, portfolio)
    recon_engine.start()

    # ── Startup Telegram alert ─────────────────────────────────────────────
    telegram_alerts.alert_system_start(portfolio.bankroll, Config.PAPER_TRADING)

    # ── ML audit ───────────────────────────────────────────────────────────
    audit = ml.audit()
    log.info(
        f"[Main] ML: resolved={audit['resolved']}/{audit['total']} "
        f"WR={audit['win_rate']:.1%} AUC={audit['val_auc']:.3f}"
    )

    cycle               = 0
    last_dd_alert_mono  = 0.0
    consecutive_errors  = 0

    # ══════════════════════════════════════════════════════════════════════
    # MAIN TRADING LOOP
    # ══════════════════════════════════════════════════════════════════════
    while _RUNNING:
        try:
            cycle += 1
            cycle_start = time.monotonic()
            _write_heartbeat()   # liveness marker for the offline-gap audit

            # ── Refresh live portfolio value ───────────────────────────────
            # ★ Not in DRY_RUN: the broker book never sees dry-run fills, so its
            # equity is a constant that would erase the internally tracked P&L.
            fresh_pv = broker.get_portfolio_value()
            account_ok = True
            if Config.DRY_RUN:
                portfolio.update_peak()
            elif fresh_pv > 0:
                portfolio.bankroll = fresh_pv
                portfolio.update_peak()
            elif not Config.PAPER_TRADING:
                # Live account value unavailable mid-session (auth/API failure).
                # Keep monitoring open positions but take NO new entries this cycle.
                account_ok = False
                log.error("[Main] 🛑 Live account value unavailable — halting new "
                          "entries this cycle (monitoring only)")

            # ── Detect regime ──────────────────────────────────────────────
            regime      = regime_e.detect()
            regime_name = regime["regime"]

            log.info(
                f"[Main] ── Cycle {cycle} | ₹{portfolio.bankroll:,.2f} "
                f"dd={portfolio.drawdown:.1%} "
                f"open={len(portfolio.open_trades)} "
                f"regime={regime_name} ──"
            )

            # ── SAFETY: Max drawdown halt ──────────────────────────────────
            if portfolio.drawdown >= Config.MAX_DRAWDOWN_HALT:
                log.warning(
                    f"[Main] 🛑 DRAWDOWN HALT {portfolio.drawdown:.1%} >= "
                    f"{Config.MAX_DRAWDOWN_HALT:.0%} — closing all"
                )
                _emergency_close_all(portfolio, exec_e, risk_mgr, exit_e, broker, "DRAWDOWN_HALT")
                telegram_alerts.alert_drawdown_warning(portfolio)
                log.info("[Main] Sleeping 10min after drawdown halt")
                _sleep(600)
                continue

            # ── Drawdown warning alert (throttled to once/hour) ────────────
            now_mono = time.monotonic()
            if portfolio.drawdown >= 0.05 and now_mono - last_dd_alert_mono > 3600:
                telegram_alerts.alert_drawdown_warning(portfolio)
                last_dd_alert_mono = now_mono

            # ── Monitor existing positions (always runs, even in fear) ──────
            if portfolio.open_trades:
                exits = exit_e.monitor(list(portfolio.open_trades), regime=regime)
                closed_exits = []
                for trade, reason, exit_price, pnl_pct in exits:
                    # ── ★ Staged exit: trim at first target, position stays open ──
                    if reason == "PARTIAL_TP":
                        if exec_e.trim_trade(trade, exit_price):
                            telegram_alerts.alert_partial_tp(trade)
                            log.info(
                                f"[Main] ✂️ TRIM {trade.symbol} at {pnl_pct:+.2%} — "
                                f"half booked, rest rides to TP with breakeven stop"
                            )
                        else:
                            # Can't trim (1-share position / unsupported broker):
                            # mark so the trigger doesn't refire every cycle.
                            trade.trim_shares = -1.0
                        continue
                    # ★ A failed flatten keeps the trade open & monitored —
                    #   booking it closed would orphan the broker position.
                    if not exec_e.close_trade(trade, exit_price, pnl_pct, reason):
                        telegram_alerts.alert_error(
                            f"EXIT {trade.symbol}", "broker flatten failed — retrying")
                        continue
                    closed_exits.append(trade)
                    risk_mgr.close_trade(trade, portfolio)
                    telegram_alerts.alert_trade_exit(trade)
                    emoji = "✅" if pnl_pct >= 0 else "❌"
                    log.info(
                        f"[Main] {emoji} EXIT {trade.symbol} "
                        f"pnl={pnl_pct:+.2%} (₹{trade.pnl_usd:+.2f}) "
                        f"reason={reason}"
                    )
                if closed_exits:
                    exit_e.purge([t.trade_id for t in closed_exits],
                                 [t.symbol for t in closed_exits])

            # ── Check market open ──────────────────────────────────────────
            if not broker.is_market_open():
                log.info("[Main] Market closed — monitoring only, sleeping 60s")
                _sleep(60)
                continue

            mins_since_open = broker.minutes_since_open()
            mins_to_close   = broker.minutes_to_close()

            # Don't enter trades in first 20min (volatile open) or last 5min
            if mins_since_open >= 0 and mins_since_open < Config.TRADE_AFTER_OPEN_MIN:
                wait = Config.TRADE_AFTER_OPEN_MIN - mins_since_open
                log.info(f"[Main] Opening volatility window — wait {wait:.0f}min")
                _sleep(min(60, wait * 60))
                continue

            if getattr(Config, "EOD_EXECUTION_ONLY", False):
                window = getattr(Config, "EOD_EXECUTION_WINDOW_MINS", 60)
                if mins_to_close > window:
                    log.info(f"[Main] EOD mode active — no new entries until last {window}min ({mins_to_close:.0f}m left)")
                    _sleep(60)
                    continue

            if 0 < mins_to_close < Config.TRADE_BEFORE_CLOSE_MIN:
                log.info(f"[Main] Pre-close — no new entries ({mins_to_close:.0f}min left)")
                _sleep(30)
                continue

            # ── Extreme fear: halt new entries completely ──────────────────
            if regime.get("is_extreme"):
                log.warning(
                    f"[Main] {regime_name.upper()} (VIX≈{regime.get('vix_level',0):.0f}) "
                    f"— no new entries"
                )
                _sleep(Config.SCAN_INTERVAL_SECS)
                continue

            # ── ★ Halt new entries on account/data-feed failure ────────────
            #   (open positions were already monitored above; this only blocks
            #   OPENING new risk on a broken account or stale prices.)
            if not account_ok or _feed_stale(broker):
                if account_ok:
                    log.warning("[Main] 🛑 Price feed stale — monitoring only, no new entries")
                signals = []
            else:
                signals = sig_engine.scan(regime=regime)
                log.info(f"[Main] Scan: {len(signals)} signal(s) passed all pre-filters")

            new_trades_this_cycle = 0
            rej_size = rej_risk = 0

            for signal in signals:
                # Hard stop: never exceed MAX_TRADES_PER_CYCLE new entries
                if new_trades_this_cycle >= Config.MAX_TRADES_PER_CYCLE:
                    break

                # Global position limit — stop opening, but let rotation run below
                if len(portfolio.open_trades) >= Config.MAX_OPEN_POSITIONS:
                    log.info(f"[Main] Max positions ({Config.MAX_OPEN_POSITIONS}) reached")
                    break

                # All pre-buy gates + sizing + order live in _attempt_entry so the
                # normal path and the rotation path can never diverge.
                _, reason = _attempt_entry(signal, portfolio, regime, ml, exec_e, risk_mgr)
                if reason == "entered":
                    new_trades_this_cycle += 1
                elif reason == "size":
                    rej_size += 1
                elif reason == "risk":
                    rej_risk += 1
                # "gate"/"failed" already logged; not counted (matches prior behavior)

            # ── ★ Rotation: book full → swap a decayed holding for a stronger name ──
            rotated = 0
            if (Config.ENABLE_ROTATION
                    and len(portfolio.open_trades) >= Config.MAX_OPEN_POSITIONS):
                held_syms = {t.symbol for t in portfolio.open_trades}
                rot_cands = sorted(
                    (s for s in signals if s.symbol not in held_syms),
                    key=lambda s: s.analyst_score, reverse=True,
                )
                rotated = _run_rotation(
                    rot_cands, portfolio, regime, sig_engine,
                    ml, exec_e, exit_e, risk_mgr, broker,
                )

            # ── Per-cycle decision summary (always logged) ─────────────────
            log.info(
                f"[Decision] cycle {cycle}: candidates={len(signals)} "
                f"entered={new_trades_this_cycle} rotated={rotated} "
                f"rejected_size={rej_size} rejected_risk={rej_risk} "
                f"open={len(portfolio.open_trades)}/{Config.MAX_OPEN_POSITIONS} "
                f"equity=₹{portfolio.bankroll:,.0f}"
            )

            consecutive_errors = 0  # reset on successful cycle

            # ── Timing ────────────────────────────────────────────────────
            elapsed    = time.monotonic() - cycle_start
            sleep_secs = max(5.0, Config.SCAN_INTERVAL_SECS - elapsed)
            log.debug(f"[Main] Cycle {cycle} done in {elapsed:.1f}s — sleeping {sleep_secs:.0f}s")
            _sleep(sleep_secs)

        except KeyboardInterrupt:
            break

        except Exception as e:
            consecutive_errors += 1
            log.error(f"[Main] Cycle {cycle} unhandled error: {e}", exc_info=True)
            telegram_alerts.alert_error(f"Cycle {cycle}", e)

            # Back off exponentially on repeated errors (max 5min)
            backoff = min(300, 15 * (2 ** min(consecutive_errors - 1, 4)))
            log.info(f"[Main] Backing off {backoff}s (error #{consecutive_errors})")
            _sleep(backoff)

    # ── Graceful shutdown ──────────────────────────────────────────────────
    log.info("[Main] Shutting down Tejas…")
    _keep_system_awake(False)   # let the PC sleep again outside the session
    try:
        recon_engine.stop()
    except Exception:
        pass
    try:
        scheduler.stop()
    except Exception:
        pass

    log.info(f"[Main] Final portfolio: ₹{portfolio.bankroll:,.2f}")
    log.info(f"[Main] Open positions:  {len(portfolio.open_trades)}")
    if portfolio.open_trades:
        for t in portfolio.open_trades:
            log.info(f"[Main]   {t.symbol} ₹{t.size_usd:.2f} @ {t.entry_price:.2f}")
    log.info("[Main] Tejas stopped cleanly. ✓")


if __name__ == "__main__":
    main()
