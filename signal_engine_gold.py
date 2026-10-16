"""
signal_engine_gold.py — ★ UPGRADED: Signal Engine with Regime + Sentiment + Earnings
=======================================================================================
All signals pass through 4 extra pre-filters before being ranked:
  ★ Earnings blackout check (skip if earnings within 3 days)
  ★ Sentiment gate (skip if very negative news flow)
  ★ Regime edge adjustment (raise/lower threshold based on market regime)
  ★ Sentiment edge boost (add 0.5% to edge for positive-news stocks)
"""
import logging
import math
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import feature_builder_gold as _fbg
from analyst_engine import AnalystEngine
from brokers.base import BaseBroker
from config import Config
from earnings_filter import EarningsFilter
from universe import get_universe
from feature_builder_gold import FeatureBuilderGold
from ml_engine_gold import MLEngineGold
from models import Bar, Quote, Signal
from sentiment_engine import SentimentEngine

log = logging.getLogger(__name__)


class SignalEngineGold:
    def __init__(
        self,
        broker: BaseBroker,
        ml: MLEngineGold,
        builder: FeatureBuilderGold,
        earnings: EarningsFilter,
        sentiment: SentimentEngine,
    ):
        self.broker    = broker
        self.ml        = ml
        self.builder   = builder
        self.earnings  = earnings
        self.sentiment = sentiment
        self.analyst   = AnalystEngine()
        from fundamentals_engine import FundamentalsEngine
        self.fundamentals = FundamentalsEngine()
        # ★ composite score of EVERY symbol evaluated in the last scan (incl. names
        #   below the buy threshold). Rotation reads this to know how a currently
        #   held position is scoring *right now* vs. fresh candidates.
        self.last_scores: Dict[str, float] = {}

    def scan(self, regime: Dict = None) -> List[Signal]:
        """
        Scan all watchlist symbols.
        Returns Signals sorted by adjusted edge (best first).
        """
        if regime is None:
            regime = {"regime": "unknown", "size_mult": 1.0, "edge_boost": 0.0}

        # ── Universe: dynamic, market-derived (live NSE constituents) ──
        symbols, sectors = get_universe()
        if sectors:
            _fbg.SECTOR_MAP.update(sectors)   # use NSE's own sector classification

        # ── Bulk data fetch ────────────────────────────────────────
        bars_map   = self.broker.get_bars_bulk(symbols, "1Day", limit=Config.SIGNAL_LOOKBACK_BARS)
        # For a large universe, per-symbol live quotes are skipped for speed;
        # ranking uses the latest daily close, and the precise fill price is taken
        # at execution time. Small universes still get live quotes.
        quotes_map = self.broker.get_quotes_bulk(symbols) if len(symbols) <= 80 else {}

        # If bulk bars completely empty (API outage/auth issue), abort scan gracefully
        if not bars_map:
            log.warning("[Scanner] Bulk bars returned empty — check API key/connectivity")
            return []

        # ── ★ Pre-compute earnings blackout set (batch for efficiency) ──
        blackout_set = self.earnings.get_blackout_set(symbols) if Config.USE_EARNINGS_GATE else set()
        if blackout_set:
            log.info(f"[Scanner] Earnings blackout: {', '.join(sorted(blackout_set))}")

        signals: List[Signal] = []
        self.last_scores = {}
        screened = skipped_earnings = skipped_sentiment = 0

        for symbol in symbols:
            bars  = bars_map.get(symbol, [])
            quote = quotes_map.get(symbol)

            if not bars or len(bars) < 30:
                continue

            screened += 1

            # ── ★ Earnings blackout ────────────────────────────────
            if symbol in blackout_set:
                skipped_earnings += 1
                continue

            # ── ★ Sentiment gate (hard block on very negative) ────
            if Config.USE_SENTIMENT_GATE and self.sentiment.is_blocked(symbol):
                skipped_sentiment += 1
                log.info(f"[Scanner] {symbol}: blocked by negative sentiment")
                continue

            sig = self._evaluate(symbol, bars, quote, regime)
            if sig is not None:
                signals.append(sig)

        signals.sort(key=lambda s: s.edge, reverse=True)

        # ── ★ Fundamentals screen — TOP candidates only (hard-bounded) ────
        # Only the top N ranked names get a quoteSummary fetch (cached 24h): the
        # loop can enter at most MAX_TRADES_PER_CYCLE + rotations per cycle, so
        # checking name #40 is wasted network. This ALSO has a time budget and a
        # consecutive-failure breaker — on 2026-07-06 a network outage made each
        # fetch burn ~10s in retries and an unbounded pass over 100+ signals
        # wedged the whole cycle for >20min (positions unmonitored, no entries).
        # Anything unchecked passes through untouched (fail open).
        skipped_fundamentals = 0
        if Config.USE_FUNDAMENTALS_GATE and signals:
            top_n   = Config.MAX_TRADES_PER_CYCLE + Config.MAX_ROTATIONS_PER_CYCLE + 10
            budget  = _time.monotonic() + 45.0
            consec_fail = 0
            kept = []
            for i, s in enumerate(signals):
                if i >= top_n or _time.monotonic() > budget or consec_fail >= 6:
                    kept.extend(signals[i:])   # unchecked tail passes through
                    if i < len(signals) and (consec_fail >= 6 or _time.monotonic() > budget):
                        log.warning(f"[Scanner] fundamentals screen stopped at "
                                    f"{i}/{len(signals)} (budget/feed) — rest unchecked")
                    break
                data = self.fundamentals.get(s.symbol)
                consec_fail = consec_fail + 1 if (
                    s.symbol in self.fundamentals._cache
                    and self.fundamentals._cache[s.symbol][2] is False) else 0
                if not self.fundamentals.is_viable(s.symbol):
                    skipped_fundamentals += 1
                    log.info(f"[Scanner] {s.symbol}: blocked — fragile fundamentals "
                             f"(negative operating margin + cash burn)")
                    continue
                s.fund_health = self.fundamentals.health(s.symbol)
                s.edge += self.fundamentals.edge_adjustment(s.symbol)
                if s.fund_health and s.fund_health != "unknown":
                    s.rationale += f" | health {s.fund_health}"
                kept.append(s)
            signals = kept
            signals.sort(key=lambda s: s.edge, reverse=True)

        log.info(
            f"[Scanner] {len(signals)} signals | screened={screened} | "
            f"earnings_skip={skipped_earnings} sentiment_skip={skipped_sentiment} "
            f"fundamentals_skip={skipped_fundamentals} | "
            f"regime={regime.get('regime','?')}"
        )
        return signals

    def _evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        quote: Optional[Quote],
        regime: Dict,
    ) -> Optional[Signal]:
        try:
            current_price = float(quote.mid) if quote and quote.mid > 0 else float(bars[-1].close)
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(current_price) or current_price <= 0:
            return None
        if current_price < Config.MIN_PRICE or current_price > Config.MAX_PRICE:
            return None
        if quote:
            try:
                spread_pct = float(quote.spread_pct)
            except (TypeError, ValueError):
                return None
            if (not math.isfinite(spread_pct) or spread_pct < 0 or
                    spread_pct > Config.MAX_SPREAD_PCT):
                return None

        # ── ★ Liquidity floor: require real ₹ turnover so entry/exit slippage
        #   stays negligible vs position size (a relative volume ratio can't catch
        #   an absolutely thin micro-cap). Previously configured but never enforced.
        if Config.MIN_ADV_VALUE > 0:
            recent = bars[-Config.ADV_LOOKBACK_DAYS:]
            advs = sorted(b.close * b.volume for b in recent
                          if b.volume and math.isfinite(b.close) and b.close > 0)
            if not advs:
                return None
            adv = advs[len(advs) // 2]   # median daily traded value (₹)
            if adv < Config.MIN_ADV_VALUE:
                return None

        trade_id = str(uuid.uuid4())[:12]
        features = self.builder.build(symbol, bars, quote, trade_id=trade_id)
        if features is None:
            return None

        # ── Analyst (transparent multi-factor, regime-adaptive) + ML ───
        view = self.analyst.score(features, regime)
        self.last_scores[symbol] = view["composite"]   # ★ record for rotation
        ml_prob, ml_conf = self.ml.predict(features)

        ml_trained = self.ml.is_trustworthy()   # gate on AUC, not just "a model exists"
        if ml_trained:
            # trust the learned model more, but keep the analyst as a sanity tilt
            prob = 0.60 * ml_prob + 0.40 * view["prob"]
            conf = max(ml_conf, view["conf"])
        else:
            # no learned model yet → the analyst IS the brain
            prob = view["prob"]
            conf = view["conf"]

        ml_prob = prob          # downstream (kelly) expects this name
        ml_conf = conf
        base_edge = prob - 0.50

        # ★ Regime edge threshold adjustment
        regime_edge_adjustment = regime.get("edge_boost", 0.0)
        effective_min_edge = Config.MIN_EDGE + regime_edge_adjustment

        # ★ Sentiment edge boost
        sentiment_adj = self.sentiment.edge_adjustment(symbol)
        adjusted_edge = base_edge + sentiment_adj

        if adjusted_edge < effective_min_edge:
            return None

        # ATR-based stop/target (dynamic)
        atr = features.atr_pct * current_price
        risk = max(current_price * Config.STOP_MIN_PCT, atr * 2.0)
        stop_loss   = current_price - risk
        take_profit = current_price + (risk * Config.TAKE_PROFIT_RR)

        # Clamp
        stop_loss   = max(stop_loss,   current_price * (1 - Config.STOP_MAX_PCT))
        take_profit = min(take_profit, current_price * 1.15)

        # This runs once per symbol in a potentially 500-name scan. Keep the
        # detailed rationale available for debug sessions without filling the
        # rotating production log and delaying stop monitoring.
        log.debug(f"[Analyst] {symbol}: {view['thesis']} "
                  f"(score={view['composite']:+.2f} edge={adjusted_edge:.3f} conf={conf:.2f})")

        # ★ The WHY — one line a human can audit later, persisted with the trade
        # and sent in the entry alert. (Fundamentals health is appended by the
        # scan-level screen for surviving candidates.)
        news_part = f", news {sentiment_adj:+.1%}" if sentiment_adj else ""
        ml_part = f" | ML {prob:.0%}" if ml_trained else " | analyst-only"
        rationale = (
            f"{view['thesis']} (comp {view['composite']:+.2f}) | "
            f"regime={regime.get('regime', '?')} | "
            f"edge {adjusted_edge:.1%}{news_part}{ml_part}"
        )

        return Signal(
            symbol=symbol,
            side="BUY",
            entry_price=current_price,
            ml_prob=ml_prob,
            ml_conf=ml_conf,
            edge=adjusted_edge,
            features=features,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            generated_at=datetime.now(timezone.utc),
            analyst_score=view["composite"],
            thesis=view["thesis"],
            factors=view["factors"],
            rationale=rationale,
        )
