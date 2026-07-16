"""
execution_engine.py — Order Execution
=======================================
Places bracket orders (entry + stop loss + take profit) via Alpaca.
Persists trade to DB and logs features for ML training.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
from brokers.base import BaseBroker
from config import Config
from ml_engine_gold import MLEngineGold
from models import Signal, Trade
from slippage_engine import SlippageEngine

log = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(self, broker: BaseBroker, ml: MLEngineGold):
        self.broker = broker
        self.ml     = ml
        self.slip   = SlippageEngine()

    def execute(
        self,
        signal: Signal,
        approved_size: float,
        kelly_pct: float,
        entry_quality: float,
    ) -> Optional[Trade]:
        """
        Places bracket order, persists trade, logs features.
        Returns Trade if successful, None if failed.
        """
        trade_id = str(uuid.uuid4())
        signal.features.trade_id = trade_id

        # Compute shares from notional (for record-keeping; Alpaca handles fractional)
        shares = approved_size / signal.entry_price if signal.entry_price > 0 else 0

        # ── Place bracket order ────────────────────────────────────
        result = self.broker.place_bracket_order(
            symbol=signal.symbol,
            side="buy",
            notional=approved_size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

        if result is None:
            log.error(f"[Exec] Order failed for {signal.symbol}")
            return None

        alpaca_order_id = result.get("id", "")

        # ── Build Trade object ─────────────────────────────────────
        trade = Trade(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            shares=shares,
            size_usd=approved_size,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            ml_probability=signal.ml_prob,
            entry_quality=entry_quality,
            kelly_pct=kelly_pct,
            features=signal.features,
            alpaca_order_id=alpaca_order_id,
            entry_time=datetime.now(timezone.utc),
            last_ml_prob=signal.ml_prob,
            highest_price=signal.entry_price,
            lowest_price=signal.entry_price,
            rationale=getattr(signal, "rationale", "") or signal.thesis,
        )

        # ── Persist ────────────────────────────────────────────────
        self._persist_trade(trade)
        self.ml.log_entry(signal.features, signal.side, signal.entry_price)

        mode = "PAPER" if Config.PAPER_TRADING else "LIVE"
        log.info(
            f"[Exec] [{mode}] BUY ₹{approved_size:.2f} of {signal.symbol} @ {signal.entry_price:.2f} "
            f"| SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f} "
            f"| edge={signal.edge:.3f} q={entry_quality:.3f}"
        )
        return trade

    def _persist_trade(self, trade: Trade) -> None:
        db.execute(
            """
            INSERT OR IGNORE INTO trades
                (trade_id, symbol, side, shares, size_usd, entry_price,
                 stop_loss, take_profit, ml_probability, entry_quality, kelly_pct,
                 alpaca_order_id, sector_code, status, entry_time, rationale)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.trade_id, trade.symbol, trade.side, trade.shares,
                trade.size_usd, trade.entry_price, trade.stop_loss, trade.take_profit,
                trade.ml_probability, trade.entry_quality, trade.kelly_pct,
                trade.alpaca_order_id, trade.features.sector_code,
                "open", trade.entry_time.isoformat(), trade.rationale,
            ),
        )

    def trim_trade(self, trade: Trade, price: float) -> bool:
        """★ Staged exit, leg 1: sell PARTIAL_TP_FRACTION of the position at the
        first target and raise the stop to breakeven+costs. Books a real gain
        while the remainder keeps its full-TP upside behind a risk-free stop.
        Returns True if the trim filled (trade stays OPEN either way)."""
        if trade.trim_shares > 0 or trade.status == "closed":
            return False
        qty = int(trade.shares * Config.PARTIAL_TP_FRACTION)
        if qty < 1:
            return False   # position too small to split — full TP will handle it
        result = self.broker.close_position_partial(trade.symbol, qty)
        if result is None:
            log.info(f"[Exec] {trade.symbol}: partial close unsupported/failed — trim skipped")
            return False
        fill = float(result.get("filled_avg_price") or price)

        trade.trim_shares = float(result.get("qty") or qty)
        trade.trim_price  = fill
        # Breakeven+costs stop on the remainder: worst case the trade now ends
        # ~flat overall instead of round-tripping back to the original stop.
        trade.stop_loss = max(trade.stop_loss, trade.entry_price * 1.002)
        db.execute(
            "UPDATE trades SET trim_shares=?, trim_price=?, stop_loss=? WHERE trade_id=?",
            (trade.trim_shares, trade.trim_price, trade.stop_loss, trade.trade_id),
        )
        gain_pct = (fill - trade.entry_price) / max(trade.entry_price, 0.01)
        log.info(
            f"[Exec] ✂️ TRIM {trade.symbol}: sold {trade.trim_shares:.0f}/{trade.shares:.0f} "
            f"@ ₹{fill:.2f} ({gain_pct:+.2%}) | stop→breakeven ₹{trade.stop_loss:.2f}"
        )
        return True

    def close_trade(self, trade: Trade, exit_price: float, pnl_pct: float, exit_reason: str) -> bool:
        """Close a position and update DB. Returns True if the trade is closed.

        Idempotent (a second close on an already-closed trade is a no-op, so the
        exit engine and rotation can't double-book it). Realized P&L is taken from
        the ACTUAL fill price when the broker reports one, and round-trip
        transaction costs (modeled slippage + fees/taxes) are deducted — so the
        stored P&L matches money actually moved, not a cost-free mid-price.

        ★ If a LIVE broker fails to flatten (API error / rejected / off-hours),
        we return False and keep the trade OPEN — marking it closed anyway would
        orphan real shares on the exchange with no stop, invisible to the bot."""
        if trade.status == "closed":
            log.debug(f"[Exec] {trade.symbol} already closed — skipping duplicate close")
            return True

        # Flatten at the broker; prefer the real fill price for P&L.
        real_fill = False
        if not Config.DRY_RUN:
            result = self.broker.close_position(trade.symbol)
            if result is None and not Config.PAPER_TRADING:
                log.error(
                    f"[Exec] 🛑 LIVE flatten FAILED for {trade.symbol} — trade stays "
                    f"OPEN and monitored; will retry next cycle (reason was: {exit_reason})"
                )
                return False
            if result and result.get("filled_avg_price"):
                exit_price = float(result["filled_avg_price"])
                pnl_pct = (exit_price - trade.entry_price) / max(trade.entry_price, 0.01)
                real_fill = True

        # Deduct costs. With a REAL fill the exit-leg slippage is already inside
        # the fill price — model only the entry leg; otherwise model both legs.
        f = trade.features
        spread = max(0.0005, getattr(f, "bid_ask_spread", 0.001))
        vol    = getattr(f, "atr_pct", 0.02)
        rt_slip = self.slip.estimate(trade.entry_price, trade.size_usd,
                                     spread_pct=spread, volatility=vol, urgency=1.0)
        if not real_fill:
            rt_slip += self.slip.estimate(max(exit_price, 0.01), trade.size_usd,
                                          spread_pct=spread, volatility=vol, urgency=0.7)
        cost_pct = rt_slip + getattr(Config, "FEE_PCT", 0.0011)
        # ★ Blended P&L when a first-target trim happened: the trimmed fraction
        # realized at trim_price, the remainder at exit_price.
        if trade.trim_shares > 0 and trade.shares > 0 and trade.trim_price:
            frac = min(1.0, trade.trim_shares / trade.shares)
            trim_ret = (trade.trim_price - trade.entry_price) / max(trade.entry_price, 0.01)
            rest_ret = (exit_price - trade.entry_price) / max(trade.entry_price, 0.01)
            pnl_pct = frac * trim_ret + (1.0 - frac) * rest_ret
        pnl_pct  = pnl_pct - cost_pct
        pnl_usd  = trade.size_usd * pnl_pct

        exit_time = datetime.now(timezone.utc)
        trade.pnl_usd     = pnl_usd
        trade.pnl_pct     = pnl_pct
        trade.exit_time   = exit_time
        trade.exit_reason = exit_reason
        trade.exit_price  = exit_price
        trade.status      = "closed"

        db.execute(
            """
            UPDATE trades
            SET status=?, pnl_usd=?, pnl_pct=?, exit_time=?, exit_price=?, exit_reason=?
            WHERE trade_id=?
            """,
            ("closed", pnl_usd, pnl_pct, exit_time.isoformat(), exit_price, exit_reason, trade.trade_id),
        )

        outcome = 1 if pnl_usd > 0 else 0
        self.ml.log_exit(trade.trade_id, outcome, exit_reason, pnl_pct)

        emoji = "✅" if pnl_usd >= 0 else "❌"
        log.info(
            f"[Exec] {emoji} CLOSED {trade.symbol} | "
            f"pnl={pnl_pct:+.2%} (₹{pnl_usd:+.2f}) | reason={exit_reason}"
        )
        return True
