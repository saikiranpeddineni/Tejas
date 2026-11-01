"""
scheduler.py — Background Job Scheduler
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)


class _Job:
    def __init__(self, name: str, fn: Callable, interval_secs: int):
        self.name = name
        self.fn = fn
        self.interval_secs = interval_secs
        self.last_run = 0.0

    def is_due(self) -> bool:
        return time.monotonic() - self.last_run >= self.interval_secs

    def run(self) -> None:
        try:
            self.fn()
            self.last_run = time.monotonic()
        except Exception as e:
            log.error(f"[Scheduler] Job '{self.name}' failed: {e}", exc_info=True)


class Scheduler:
    def __init__(self):
        self._jobs: list = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def add(self, name: str, fn: Callable, interval_secs: int) -> "Scheduler":
        self._jobs.append(_Job(name, fn, interval_secs))
        log.info(f"[Scheduler] Registered '{name}' every {interval_secs}s")
        return self

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while self._running and not self._stop_event.is_set():
            for job in self._jobs:
                if not self._running or self._stop_event.is_set():
                    break
                if job.is_due():
                    job.run()
            self._stop_event.wait(10)


def make_db_cleanup_job() -> Callable:
    """Prune old data to keep DB small."""
    import db

    def _run():
        # Stored bars may be ISO-8601 with a `T`/offset while SQLite's
        # datetime() returns a space-separated UTC string.  Text comparison then
        # fails (and the cache grows forever); julianday normalizes both forms.
        db.execute("DELETE FROM price_bars "
                   "WHERE julianday(bar_time) < julianday('now', '-90 days')")
        db.execute("DELETE FROM rejected_signals "
                   "WHERE julianday(rejected_at) < julianday('now', '-7 days')")

    return _run


def make_daily_summary_job(portfolio, ml_engine, telegram_alerts) -> Callable:
    # ★ Fire at 15:35 IST (just before the supervisor stops the bot at 15:45).
    # The old UTC-midnight trigger could NEVER fire: the bot only lives
    # 09:05–15:45 IST = 03:35–10:15 UTC, and midnight UTC is outside that.
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    sent_on = [None]   # IST date of the last summary

    def _run():
        now = datetime.now(IST)
        if now.hour * 60 + now.minute >= 15 * 60 + 35 and sent_on[0] != now.date():
            sent_on[0] = now.date()
            telegram_alerts.alert_daily_summary(portfolio, ml_engine)

    return _run


def make_retrain_job(ml_engine, broker=None) -> Callable:
    from config import Config
    last_trained_resolved = [None]

    def _run():
        # Only retrain after-hours — never reshape the probability surface mid-session.
        if broker is not None:
            try:
                if broker.is_market_open():
                    return
            except Exception:
                pass
        audit = ml_engine.audit()
        resolved = audit.get("resolved", 0)
        if (resolved >= Config.MIN_TRAIN_SAMPLES
                and resolved != last_trained_resolved[0]):
            log.info("[Retrain] Triggering scheduled (after-hours) retraining…")
            ml_engine.train()
            last_trained_resolved[0] = resolved

    return _run


def make_portfolio_snapshot_job(portfolio) -> Callable:
    import db
    from datetime import timedelta

    def _run():
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        db.execute(
            """
            INSERT OR REPLACE INTO portfolio_snapshots (snap_date, bankroll, open_trades)
            VALUES (?, ?, ?)
            """,
            (today, portfolio.bankroll, len(portfolio.open_trades)),
        )

    return _run


def build_scheduler(ml_engine, portfolio, telegram_alerts, broker=None) -> Scheduler:
    sched = Scheduler()
    sched.add("daily_summary",    make_daily_summary_job(portfolio, ml_engine, telegram_alerts), 300)
    sched.add("retrain_ml",       make_retrain_job(ml_engine, broker),   3600)
    sched.add("db_cleanup",       make_db_cleanup_job(),                 86400)
    sched.add("portfolio_snap",   make_portfolio_snapshot_job(portfolio), 3600)
    return sched
