"""
execution_engine.py — Order Execution
=======================================
Places bracket orders (entry + stop loss + take profit) via Alpaca.
Persists trade to DB and logs features for ML training.
"""
import logging
import math
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
        self.last_was_dry_run = False

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
        self.last_was_dry_run = False

        try:
            approved_size = float(approved_size)
            signal_entry_price = float(signal.entry_price)
        except (TypeError, ValueError):
            log.error(f"[Exec] Invalid entry size/price for {signal.symbol}")
            return None
        if (not signal.symbol or not math.isfinite(approved_size) or approved_size <= 0 or
                not math.isfinite(signal_entry_price) or signal_entry_price <= 0):
            log.error(f"[Exec] Invalid entry size/price for {signal.symbol}")
            return None
        try:
            stop_loss = float(signal.stop_loss)
            take_profit = float(signal.take_profit)
        except (TypeError, ValueError):
            log.error(f"[Exec] Invalid stop/target for {signal.symbol}")
            return None
        if (not math.isfinite(stop_loss) or not math.isfinite(take_profit) or
                stop_loss <= 0 or take_profit <= signal_entry_price or
                stop_loss >= signal_entry_price):
            log.error(f"[Exec] Invalid stop/target for {signal.symbol}")
            return None

        # ── Place bracket order ────────────────────────────────────
        result = self.broker.place_bracket_order(
            symbol=signal.symbol,
            side="buy",
            notional=approved_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        if result is None:
            log.error(f"[Exec] Order failed for {signal.symbol}")
            return None

        if not isinstance(result, dict):
            log.error(f"[Exec] Broker returned an invalid order response for {signal.symbol}")
            return None
        if result.get("dry_run"):
            # DRY_RUN is an observation mode, not a paper fill. Do not create an
            # open ledger row or feed a hypothetical position into exits, risk,
            # reconciliation, or realized P&L. Use BROKER=simulated with
            # DRY_RUN=false when a full paper book is desired.
            self.last_was_dry_run = True
            log.info(f"[Exec] [DRY RUN] Would BUY {signal.symbol} for "
                     f"₹{approved_size:.2f} | SL={stop_loss:.2f} TP={take_profit:.2f}")
            return None
        alpaca_order_id = result.get("id", "")

        # ── ★ Book what the BROKER actually filled, not the scan estimate ──
        # NSE has whole shares only: recording approved_size/scan_price gave
        # fractional shares (e.g. 15.7688) while the broker held 15 — every
        # reconciliation cycle then screamed drift and P&L was based on a
        # price the bot never paid. Prefer the broker's qty/fill; fall back to
        # a floored estimate only when it reports nothing (DRY_RUN).
        entry_price = signal_entry_price
        try:
            filled_qty = float(result.get("qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if filled_qty <= 0:
            if result.get("dry_run"):
                filled_qty = float(int(approved_size / entry_price)) if entry_price > 0 else 0.0
            else:
                log.error(f"[Exec] Live order response had no submitted/fill quantity for {signal.symbol}")
                return None
        fill_px = result.get("filled_avg_price")
        if fill_px is not None:
            try:
                px = float(fill_px)
                if math.isfinite(px) and px > 0:
                    entry_price = px
                elif not result.get("dry_run"):
                    log.error(f"[Exec] Broker returned an invalid fill price for {signal.symbol}")
                    return None
            except (TypeError, ValueError):
                if not result.get("dry_run"):
                    log.error(f"[Exec] Broker returned an invalid fill price for {signal.symbol}")
                    return None
        if not math.isfinite(filled_qty) or filled_qty < 1:
            log.error(f"[Exec] Broker returned no usable fill quantity for {signal.symbol}")
            return None
        shares = float(int(filled_qty))
        if not math.isfinite(entry_price) or entry_price <= 0:
            log.error(f"[Exec] Broker returned an invalid fill price for {signal.symbol}")
            return None
        actual_size = shares * entry_price

        # ── Build Trade object ─────────────────────────────────────
        trade = Trade(
            trade_id=trade_id,
            symbol=signal.symbol,
            side=signal.side,
            shares=shares,
            size_usd=actual_size,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            ml_probability=signal.ml_prob,
            entry_quality=entry_quality,
            kelly_pct=kelly_pct,
            features=signal.features,
            alpaca_order_id=alpaca_order_id,
            entry_time=datetime.now(timezone.utc),
            last_ml_prob=signal.ml_prob,
            highest_price=entry_price,
            lowest_price=entry_price,
            rationale=getattr(signal, "rationale", "") or signal.thesis,
        )

        # ── Persist ────────────────────────────────────────────────
        try:
            self._persist_trade(trade)
        except Exception as e:
            # A filled order without a ledger row would be unmonitored after
            # the current cycle and orphaned on restart. Try to unwind the
            # newly opened position; if that also fails, return the Trade so
            # the caller can at least keep it in the live in-memory monitor.
            log.critical(f"[Exec] Could not persist filled {signal.symbol}: {e}")
            if Config.DRY_RUN:
                return None
            try:
                unwind = self.broker.close_position(signal.symbol)
            except Exception as unwind_error:
                unwind = None
                log.critical(f"[Exec] Unwind after persistence failure failed for "
                             f"{signal.symbol}: {unwind_error}")
            if unwind is not None:
                log.error(f"[Exec] Unwound {signal.symbol} after persistence failure")
                return None
            log.critical(f"[Exec] Keeping {signal.symbol} in memory; verify broker and DB state")
        try:
            self.ml.log_entry(signal.features, signal.side, entry_price)
        except Exception as e:
            # ML telemetry must never make a successfully filled position
            # disappear from the in-memory book. The trade ledger is already
            # durable; the next training pass can tolerate a missing feature row.
            log.warning(f"[Exec] ML entry logging failed for {signal.symbol}: {e}")

        mode = "PAPER" if Config.PAPER_TRADING else "LIVE"
        log.info(
            f"[Exec] [{mode}] BUY {shares:.0f} × {signal.symbol} @ ₹{entry_price:.2f} "
            f"(₹{actual_size:.2f}) | SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f} "
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
        if not isinstance(result, dict):
            log.warning(f"[Exec] {trade.symbol}: partial close returned invalid response")
            return False
        try:
            fill = float(result.get("filled_avg_price") or price)
            reported_qty = float(result.get("qty") or qty)
        except (TypeError, ValueError):
            log.warning(f"[Exec] {trade.symbol}: partial close returned invalid fill")
            return False
        if not math.isfinite(fill) or fill <= 0 or not math.isfinite(reported_qty):
            return False
        if reported_qty < 1 or reported_qty > qty or reported_qty > trade.shares:
            log.warning(f"[Exec] {trade.symbol}: partial close quantity out of range: {reported_qty}")
            return False

        trade.trim_shares = reported_qty
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

        ★ If a broker fails to flatten (API error / rejected / off-hours), we
        return False and keep the trade OPEN — marking it closed anyway would
        orphan the position and make reconciliation harder."""
        if trade.status == "closed":
            log.debug(f"[Exec] {trade.symbol} already closed — skipping duplicate close")
            return True

        # Flatten at the broker; prefer the real fill price for P&L.
        real_fill = False
        if not Config.DRY_RUN:
            result = self.broker.close_position(trade.symbol)
            if result is None:
                log.error(
                    f"[Exec] 🛑 flatten FAILED for {trade.symbol} — trade stays "
                    f"OPEN and monitored; will retry next cycle (reason was: {exit_reason})"
                )
                return False
            if result is not None and not isinstance(result, dict):
                log.error(f"[Exec] Invalid flatten response for {trade.symbol} — retrying")
                return False
            if result and result.get("filled_avg_price") is not None:
                try:
                    broker_fill = float(result["filled_avg_price"])
                except (TypeError, ValueError):
                    broker_fill = 0.0
                if math.isfinite(broker_fill) and broker_fill > 0:
                    exit_price = broker_fill
                    pnl_pct = (exit_price - trade.entry_price) / max(trade.entry_price, 0.01)
                    real_fill = True
                else:
                    log.error(f"[Exec] Broker returned an invalid exit fill for {trade.symbol}")
                    return False

        try:
            exit_price = float(exit_price)
            pnl_pct = float(pnl_pct)
        except (TypeError, ValueError):
            log.error(f"[Exec] Invalid exit price/P&L for {trade.symbol} — keeping trade open")
            return False
        if not math.isfinite(exit_price) or exit_price <= 0 or not math.isfinite(pnl_pct):
            log.error(f"[Exec] Invalid exit price/P&L for {trade.symbol} — keeping trade open")
            return False

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
        try:
            self.ml.log_exit(trade.trade_id, outcome, exit_reason, pnl_pct)
        except Exception as e:
            # The trade/DB close above is authoritative. Do not leave a closed
            # position in the live portfolio merely because optional ML logging
            # failed.
            log.warning(f"[Exec] ML exit logging failed for {trade.symbol}: {e}")

        emoji = "✅" if pnl_usd >= 0 else "❌"
        log.info(
            f"[Exec] {emoji} CLOSED {trade.symbol} | "
            f"pnl={pnl_pct:+.2%} (₹{pnl_usd:+.2f}) | reason={exit_reason}"
        )
        return True
