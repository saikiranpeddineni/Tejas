import logging
import math
import threading
import time
from typing import Dict, List, Tuple
from models import Portfolio
from telegram_alerts import alert_system_message

log = logging.getLogger("reconciliation")

class ReconciliationEngine:
    def __init__(self, broker, portfolio: Portfolio, interval_secs: int = 300):
        self.broker = broker
        self.portfolio = portfolio
        self.interval_secs = max(1, int(interval_secs))
        self._thread = None
        self._running = False
        self._stop_event = threading.Event()
        self._last_alert_sig = None   # suppress identical drift spam (re-alert hourly)
        self._last_alert_ts = 0.0
        self._last_failure_alert_ts = 0.0

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="reconciliation-engine")
        self._thread.start()
        log.info("[Reconciliation] Started continuous position reconciliation loop.")

    def stop(self):
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run_loop(self):
        while self._running and not self._stop_event.wait(self.interval_secs):
            try:
                self.reconcile()
            except Exception as e:
                # Do not include a broker/library exception verbatim in a
                # notification; adapters can include request details in it.
                log.error("[Reconciliation] Unhandled exception during reconciliation: %s",
                          type(e).__name__, exc_info=True)
                now = time.time()
                if now - self._last_failure_alert_ts >= 3600:
                    self._last_failure_alert_ts = now
                    alert_system_message(
                        f"Reconciliation failure ({type(e).__name__}); see the local log."
                    )

    def reconcile(self):
        """
        Fetches positions from the broker and compares against the local portfolio.
        Alerts on any drift (missing positions, unmatched positions, or quantity mismatch).
        """
        if not self._running:
            return

        broker_positions = self.broker.get_positions()
        if not getattr(self.broker, "_last_positions_ok", False):
            # BaseBroker deliberately returns [] on adapter errors for legacy
            # callers.  Never interpret that outage as a flat account: doing so
            # would create a false "missing at broker" alert (or hide a real
            # position) and would train operators to ignore drift notifications.
            log.warning("[Reconciliation] Position fetch unavailable; retrying next round")
            return
        if broker_positions is None:
            log.warning("[Reconciliation] Broker returned no position payload; retrying next round")
            return

        # Build maps for comparison. NSE trades whole shares: compare INTEGERS
        # with a small tolerance so a 15.0-vs-15 rounding never pages anyone.
        # trim_shares == -1 is the "broker can't trim" sentinel, not shares sold.
        broker_map: Dict[str, float] = {}
        for position in broker_positions:
            if not isinstance(position, dict):
                log.warning("[Reconciliation] Position payload contained a non-object; retrying next round")
                return
            symbol = position.get("symbol")
            if not symbol:
                continue
            try:
                qty = float(position.get("qty", 0) or 0)
            except (TypeError, ValueError):
                log.warning("[Reconciliation] Position payload contained a non-numeric quantity; retrying next round")
                return
            if not math.isfinite(qty):
                log.warning("[Reconciliation] Position payload contained a non-finite quantity; retrying next round")
                return
            broker_map[symbol] = broker_map.get(symbol, 0.0) + qty

        local_map: Dict[str, float] = {}
        # Multiple local fills for one symbol are valid.  Aggregate them before
        # comparing with the broker's net position instead of letting the last
        # trade overwrite all earlier shares.
        for trade in list(self.portfolio.open_trades):
            trims = max(trade.trim_shares or 0.0, 0.0)
            local_map[trade.symbol] = local_map.get(trade.symbol, 0.0) + max(
                0.0, trade.shares - trims
            )

        drift_detected = False
        drift_messages = []
        TOL = 0.5   # half a share

        # Check for local trades missing or mismatched in broker
        for symbol, expected_qty in local_map.items():
            if expected_qty < 1:
                continue   # nothing meaningful left to reconcile
            broker_qty = broker_map.get(symbol, 0.0)
            if broker_qty == 0.0 and expected_qty > 0:
                drift_detected = True
                msg = f"Symbol {symbol} missing at broker. Expected qty: {expected_qty:.0f}."
                drift_messages.append(msg)
            elif abs(broker_qty - expected_qty) > TOL:
                drift_detected = True
                msg = f"Symbol {symbol} qty mismatch. Local: {expected_qty:.0f}, Broker: {broker_qty:.0f}."
                drift_messages.append(msg)

        # Check for broker trades missing in local
        for symbol, broker_qty in broker_map.items():
            if abs(broker_qty) <= TOL:
                continue
            local_qty = local_map.get(symbol, 0.0)
            if local_qty < 1:
                drift_detected = True
                msg = f"Symbol {symbol} missing locally. Broker has qty: {broker_qty:.0f}."
                drift_messages.append(msg)

        if drift_detected:
            sig = tuple(sorted(drift_messages))
            now = time.time()
            if sig == self._last_alert_sig and (now - self._last_alert_ts) < 3600:
                log.debug("[Reconciliation] Same drift as before — alert suppressed (1/hour)")
                return
            self._last_alert_sig, self._last_alert_ts = sig, now
            log.warning(f"[Reconciliation] Drift detected between local state and broker: {drift_messages}")
            alert_system_message("⚠️ Position drift detected\n" + "\n".join(drift_messages))
        else:
            self._last_alert_sig = None
            log.debug("[Reconciliation] Local portfolio matches broker perfectly.")
