"""Offline regression tests for the high-risk trading paths.

The production application can use optional market-data/ML dependencies, but
these tests deliberately exercise only stdlib code and deterministic fakes.
Missing optional imports are stubbed before importing the affected modules;
no network, broker credentials, or model downloads are needed.
"""

from __future__ import annotations

import datetime as datetime_module
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_optional_imports() -> None:
    """Make imports harmless on a minimal Python installation.

    The application requirements include these packages, but a regression run
    should remain offline and should not need to install the ML stack merely to
    test risk, persistence, and broker boundary behavior.
    """

    try:
        importlib.import_module("dotenv")
    except ImportError:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = dotenv

    try:
        importlib.import_module("requests")
    except ImportError:
        requests = types.ModuleType("requests")

        class Session:
            def __init__(self, *args, **kwargs):
                self.headers = {}

            def mount(self, *args, **kwargs):
                return None

        requests.Session = Session
        adapters = types.ModuleType("requests.adapters")
        adapters.HTTPAdapter = type("HTTPAdapter", (), {})
        requests.adapters = adapters
        sys.modules["requests"] = requests
        sys.modules["requests.adapters"] = adapters

    try:
        importlib.import_module("urllib3.util.retry")
    except ImportError:
        urllib3 = types.ModuleType("urllib3")
        urllib3.__path__ = []
        util = types.ModuleType("urllib3.util")
        util.__path__ = []
        retry = types.ModuleType("urllib3.util.retry")
        retry.Retry = type("Retry", (), {})
        urllib3.util = util
        util.retry = retry
        sys.modules["urllib3"] = urllib3
        sys.modules["urllib3.util"] = util
        sys.modules["urllib3.util.retry"] = retry


_ensure_optional_imports()

from brokers.base import IST, BaseBroker  # noqa: E402
from config import Config  # noqa: E402
from models import FeatureVector, Portfolio, Signal, Trade  # noqa: E402


UTC = timezone.utc


def _feature(symbol: str = "INFY", sector_code: int = 6) -> FeatureVector:
    return FeatureVector(
        trade_id=f"feature-{symbol}",
        symbol=symbol,
        timestamp=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
        sector_code=sector_code,
    )


def _signal(
    symbol: str = "INFY",
    *,
    analyst_score: float = 0.6,
    sector_code: int = 6,
    entry_price: float = 100.0,
) -> Signal:
    return Signal(
        symbol=symbol,
        side="BUY",
        entry_price=entry_price,
        ml_prob=0.62,
        ml_conf=0.8,
        edge=0.12,
        features=_feature(symbol, sector_code),
        stop_loss=96.0,
        take_profit=110.0,
        analyst_score=analyst_score,
        thesis="deterministic regression fixture",
    )


def _trade(
    symbol: str = "INFY",
    *,
    shares: float = 10.0,
    size_usd: float = 1000.0,
    entry_price: float = 100.0,
    entry_time: datetime | None = None,
    sector_code: int = 6,
    trim_shares: float = 0.0,
) -> Trade:
    return Trade(
        trade_id=f"trade-{symbol}",
        symbol=symbol,
        side="BUY",
        shares=shares,
        size_usd=size_usd,
        entry_price=entry_price,
        stop_loss=96.0,
        take_profit=110.0,
        ml_probability=0.62,
        entry_quality=0.8,
        kelly_pct=0.1,
        features=_feature(symbol, sector_code),
        entry_time=entry_time or datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
        trim_shares=trim_shares,
    )


class StubBroker(BaseBroker):
    """Deterministic broker double used for BaseBroker and risk tests."""

    name = "stub"

    def __init__(
        self,
        *,
        now: datetime | None = None,
        account: dict | None = None,
        account_error: Exception | None = None,
        quote=None,
        quote_error: Exception | None = None,
        order_error: Exception | None = None,
        flatten_error: Exception | None = None,
        positions=None,
    ):
        super().__init__()
        self.now = now or datetime(2026, 8, 17, 10, 0, tzinfo=IST)
        self.account = account or {"cash": 100000.0, "portfolio_value": 100000.0}
        self.account_error = account_error
        self.quote = quote
        self.quote_error = quote_error
        self.order_error = order_error
        self.flatten_error = flatten_error
        self.positions = positions or []
        self.order_calls = []

    def _fetch_bars(self, symbol, timeframe="1Day", limit=100, start=None):
        return []

    def _fetch_quote(self, symbol):
        if self.quote_error:
            raise self.quote_error
        return self.quote

    def _account_info(self):
        if self.account_error:
            raise self.account_error
        return self.account

    def _place_entry(self, symbol, side, qty, client_order_id=""):
        self.order_calls.append((symbol, side, qty, client_order_id))
        if self.order_error:
            raise self.order_error
        return {"id": "stub-order", "qty": qty, "filled_avg_price": 100.0}

    def _flatten(self, symbol):
        if self.flatten_error:
            raise self.flatten_error
        return {"symbol": symbol, "qty": 0}

    def _positions(self):
        return self.positions

    def _now_ist(self):
        return self.now


class BrokerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._dry_run = Config.DRY_RUN
        Config.DRY_RUN = False
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        Config.DRY_RUN = self._dry_run

    def test_account_and_data_failures_are_safe(self):
        broker = StubBroker(
            account_error=RuntimeError("auth failed"),
            quote_error=RuntimeError("feed failed"),
        )

        self.assertEqual(broker.get_portfolio_value(), 0.0)
        self.assertIsNone(broker.get_quote("INFY", force=True))
        self.assertEqual(broker.get_bars("INFY"), [])
        self.assertEqual(broker.get_positions(), [])

    def test_order_and_close_adapter_errors_are_contained(self):
        from models import Quote

        quote = Quote("INFY", 99.0, 101.0, 1, 1, datetime.now(UTC))
        broker = StubBroker(
            quote=quote,
            order_error=RuntimeError("order rejected"),
            flatten_error=RuntimeError("flatten rejected"),
        )

        self.assertIsNone(broker.place_market_order("INFY", "BUY", 1000.0))
        self.assertIsNone(broker.close_position("INFY"))

    def test_position_fetch_exposes_health_bit_for_reconciliation(self):
        class FailingPositionsBroker(StubBroker):
            def _positions(self):
                raise RuntimeError("positions unavailable")

        broker = FailingPositionsBroker()
        self.assertEqual(broker.get_positions(), [])
        self.assertFalse(broker._last_positions_ok)

        broker._positions = lambda: []
        self.assertEqual(broker.get_positions(), [])
        self.assertTrue(broker._last_positions_ok)

    def test_bulk_quotes_drop_malformed_values_before_caching(self):
        from models import Quote

        class BulkBroker(StubBroker):
            def _fetch_quotes_bulk(self, symbols):
                return {
                    "INFY": Quote("INFY", 99.0, 101.0, 1, 1, datetime.now(UTC)),
                    "BAD": Quote("BAD", float("nan"), 101.0, 1, 1, datetime.now(UTC)),
                }

        broker = BulkBroker()
        quotes = broker.get_quotes_bulk(["INFY", "BAD"])
        self.assertEqual(set(quotes), {"INFY"})
        self.assertIn("INFY", broker._quote_cache)
        self.assertNotIn("BAD", broker._quote_cache)

    def test_quote_without_exchange_timestamp_is_stale(self):
        from brokers.data_provider import YahooDataProvider

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"chart": {"result": [{
                    "meta": {"regularMarketPrice": 100.0}
                }]}}

        provider = object.__new__(YahooDataProvider)
        provider.session = mock.Mock()
        provider.session.get.return_value = Response()
        quote = provider.get_quote("INFY")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.timestamp, datetime.fromtimestamp(0, tz=UTC))

    def test_exit_monitor_rejects_stale_quotes(self):
        from brokers.base import quote_is_fresh
        from models import Quote

        fresh = Quote("INFY", 99.0, 101.0, 1, 1, datetime.now(UTC))
        stale = Quote("INFY", 99.0, 101.0, 1, 1, datetime.fromtimestamp(0, tz=UTC))

        self.assertTrue(quote_is_fresh(fresh))
        self.assertFalse(quote_is_fresh(stale))

    def test_market_order_uses_whole_shares_and_client_order_id(self):
        from models import Quote

        broker = StubBroker(
            quote=Quote("INFY", 99.0, 101.0, 1, 1, datetime.now(UTC)),
        )
        result = broker.place_market_order("INFY", "BUY", 1050.0)

        self.assertEqual(result["qty"], 10)  # floor(1050 / ₹100 mid)
        self.assertEqual(len(broker.order_calls), 1)
        symbol, side, qty, client_order_id = broker.order_calls[0]
        self.assertEqual((symbol, side, qty), ("INFY", "buy", 10))
        self.assertRegex(client_order_id, r"^tj_[0-9a-f]{16}$")

    def test_all_live_adapters_accept_the_shared_order_id_keyword(self):
        import inspect

        adapters = (
            ("brokers.zerodha", "ZerodhaBroker"),
            ("brokers.upstox", "UpstoxBroker"),
            ("brokers.angelone", "AngelOneBroker"),
            ("brokers.dhan", "DhanBroker"),
            ("brokers.fyers", "FyersBroker"),
        )
        for module_name, class_name in adapters:
            cls = getattr(importlib.import_module(module_name), class_name)
            self.assertIn(
                "client_order_id",
                inspect.signature(cls._place_entry).parameters,
                msg=f"{class_name} does not accept BaseBroker's order id",
            )

    def test_live_adapter_status_payloads_normalize_to_confirmed_fills(self):
        cases = (
            ("brokers.zerodha", "ZerodhaBroker", {
                "data": [{"order_id": "1", "status": "COMPLETE",
                           "filled_quantity": 3, "average_price": 100}],
            }),
            ("brokers.upstox", "UpstoxBroker", {
                "data": {"order_id": "1", "status": "complete",
                          "filled_quantity": 3, "average_price": 100},
            }),
            ("brokers.angelone", "AngelOneBroker", {
                "data": {"orderid": "1", "orderstatus": "complete",
                          "filledshares": "3", "averageprice": "100"},
            }),
            ("brokers.dhan", "DhanBroker", {
                "orderId": "1", "orderStatus": "TRADED",
                "filledQty": 3, "averageTradedPrice": 100,
            }),
            ("brokers.fyers", "FyersBroker", {
                "orderBook": [{"id": "1", "status": 2,
                                "filledQty": 3, "avgPrice": 100}],
            }),
        )
        for module_name, class_name, payload in cases:
            cls = getattr(importlib.import_module(module_name), class_name)
            adapter = object.__new__(cls)
            adapter._get = mock.Mock(return_value=payload)
            status = adapter._order_status("1", "tj_test")
            self.assertEqual(status["status"], "filled", msg=class_name)
            self.assertEqual(status["filled_qty"], 3, msg=class_name)
            self.assertEqual(status["filled_avg_price"], 100, msg=class_name)

    def test_live_order_is_not_booked_until_terminal_fill_is_confirmed(self):
        from brokers.live_base import LiveBrokerBase

        broker = object.__new__(LiveBrokerBase)
        BaseBroker.__init__(broker)
        broker.ORDER_STATUS_POLL_INTERVAL = 0
        broker.ORDER_STATUS_POLL_TIMEOUT = 1
        broker.ORDER_STATUS_MAX_POLLS = 3
        broker._order_status = mock.Mock(side_effect=[
            {"id": "live-1", "status": "open", "filled_quantity": 0},
            {
                "id": "live-1",
                "status": "complete",
                "filled_quantity": 10,
                "average_price": 101.25,
            },
        ])

        result = broker._confirm_order("live-1", "INFY", "buy", 10, "tj_test")

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["qty"], 10)
        self.assertEqual(result["filled_avg_price"], 101.25)
        self.assertEqual(broker._order_status.call_count, 2)

    def test_live_rejection_and_invalid_fill_fail_closed(self):
        from brokers.live_base import LiveBrokerBase

        broker = object.__new__(LiveBrokerBase)
        BaseBroker.__init__(broker)
        broker.ORDER_STATUS_POLL_INTERVAL = 0
        broker.ORDER_STATUS_POLL_TIMEOUT = 0
        broker.ORDER_STATUS_MAX_POLLS = 1
        broker._order_status = mock.Mock(return_value={
            "id": "live-2",
            "status": "rejected",
            "filled_quantity": 10,
            "average_price": 101.25,
        })

        result = broker._confirm_order("live-2", "INFY", "buy", 10, "tj_test")

        self.assertTrue(result["unsafe"])
        self.assertEqual(result["qty"], 0)
        self.assertFalse(broker._validate_order_result(result, requested_qty=10))

    def test_unresolved_live_order_blocks_duplicate_until_status_is_known(self):
        from brokers.live_base import LiveBrokerBase

        broker = object.__new__(LiveBrokerBase)
        BaseBroker.__init__(broker)
        broker.ORDER_STATUS_POLL_INTERVAL = 0
        broker.ORDER_STATUS_POLL_TIMEOUT = 0
        broker.ORDER_STATUS_MAX_POLLS = 1
        broker._order_status = mock.Mock(side_effect=[
            {"id": "live-3", "status": "open", "filled_quantity": 0},
            {"id": "live-3", "status": "open", "filled_quantity": 0},
            {
                "id": "live-3",
                "status": "complete",
                "filled_quantity": 10,
                "average_price": 101.0,
            },
        ])

        first = broker._confirm_order("live-3", "INFY", "buy", 10, "tj_test")
        blocked = broker._resolve_unresolved_order("INFY", "buy", requested_qty=10)
        resolved = broker._resolve_unresolved_order("INFY", "buy", requested_qty=10)

        self.assertTrue(first["unsafe"])
        self.assertTrue(blocked["unsafe"])
        self.assertEqual(resolved["status"], "filled")
        self.assertEqual(broker._order_status.call_count, 3)

    def test_live_flatten_partial_fill_is_not_reported_as_flat(self):
        from brokers.live_base import LiveBrokerBase

        broker = object.__new__(LiveBrokerBase)
        BaseBroker.__init__(broker)
        broker._positions = mock.Mock(return_value=[{"symbol": "INFY", "qty": 10}])
        broker._place_entry = mock.Mock(return_value={
            "id": "close-1",
            "status": "filled",
            "qty": 4,
            "filled_avg_price": 99.0,
        })

        result = broker._flatten_position("INFY")

        self.assertTrue(result["unsafe"])
        self.assertEqual(result["qty"], 0)
        self.assertEqual(broker._place_entry.call_args.kwargs["client_order_id"][:3], "tj_")

    def test_live_flatten_rejects_fractional_position_payloads(self):
        from brokers.live_base import LiveBrokerBase

        broker = object.__new__(LiveBrokerBase)
        BaseBroker.__init__(broker)
        broker._positions = mock.Mock(return_value=[{"symbol": "INFY", "qty": 10.5}])
        broker._place_entry = mock.Mock()

        with self.assertRaises(RuntimeError):
            broker._flatten_position("INFY")
        broker._place_entry.assert_not_called()


class MarketTimeTests(unittest.TestCase):
    def setUp(self):
        self._ignore_hours = Config.IGNORE_MARKET_HOURS
        Config.IGNORE_MARKET_HOURS = False
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        Config.IGNORE_MARKET_HOURS = self._ignore_hours

    def test_nse_open_close_boundaries_and_minutes(self):
        broker = StubBroker(now=datetime(2026, 8, 17, 9, 15, tzinfo=IST))
        self.assertTrue(broker.is_market_open())
        self.assertEqual(broker.minutes_since_open(), 0.0)
        self.assertEqual(broker.minutes_to_close(), 375.0)

        broker.now = datetime(2026, 8, 17, 15, 29, tzinfo=IST)
        self.assertTrue(broker.is_market_open())
        self.assertEqual(broker.minutes_to_close(), 1.0)

        broker.now = datetime(2026, 8, 17, 15, 30, tzinfo=IST)
        self.assertFalse(broker.is_market_open())
        self.assertEqual(broker.minutes_to_close(), -1.0)
        self.assertEqual(broker.minutes_since_open(), -1.0)

    def test_weekends_and_declared_holidays_are_closed(self):
        saturday = StubBroker(now=datetime(2026, 8, 22, 10, 0, tzinfo=IST))
        holiday = StubBroker(now=datetime(2026, 10, 2, 10, 0, tzinfo=IST))
        self.assertFalse(saturday.is_market_open())
        self.assertFalse(holiday.is_market_open())

    def test_ignore_market_hours_is_an_explicit_test_override(self):
        Config.IGNORE_MARKET_HOURS = True
        broker = StubBroker(now=datetime(2026, 8, 22, 10, 0, tzinfo=IST))
        self.assertTrue(broker.is_market_open())


class RiskGateTests(unittest.TestCase):
    def setUp(self):
        self.dynamic_risk = importlib.import_module("dynamic_risk")
        self._config = {
            name: getattr(Config, name)
            for name in (
                "DRY_RUN",
                "MAX_DRAWDOWN_HALT",
                "MAX_OPEN_POSITIONS",
                "MIN_POSITION_USD",
                "MAX_POSITION_USD",
                "MAX_POSITION_PCT",
                "MAX_PORTFOLIO_BETA",
                "REENTRY_COOLDOWN_HOURS",
            )
        }
        Config.DRY_RUN = False
        Config.MAX_DRAWDOWN_HALT = 0.10
        Config.MAX_OPEN_POSITIONS = 20
        Config.MIN_POSITION_USD = 5000.0
        Config.MAX_POSITION_USD = 25000.0
        Config.MAX_POSITION_PCT = 0.12
        Config.MAX_PORTFOLIO_BETA = 1.30
        Config.REENTRY_COOLDOWN_HOURS = 24.0
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        for name, value in self._config.items():
            setattr(Config, name, value)

    def test_drawdown_and_extreme_regime_are_hard_rejects(self):
        risk = self.dynamic_risk.DynamicRiskManager(StubBroker())
        signal = _signal("INFY")
        portfolio = Portfolio(bankroll=85000.0, peak_bankroll=100000.0)

        self.assertIsNone(
            risk.approve(signal, 0.1, 10000.0, portfolio, {"size_mult": 1.0})
        )
        healthy = Portfolio(bankroll=100000.0, peak_bankroll=100000.0)
        self.assertIsNone(
            risk.approve(
                signal,
                0.1,
                10000.0,
                healthy,
                {"is_extreme": True, "size_mult": 1.0},
            )
        )

    def test_nonfinite_portfolio_state_fails_closed(self):
        portfolio = Portfolio(bankroll=float("nan"), peak_bankroll=100000.0)
        self.assertEqual(portfolio.drawdown, 1.0)
        portfolio = Portfolio(bankroll=100000.0, peak_bankroll=float("inf"))
        self.assertEqual(portfolio.drawdown, 1.0)

    def test_reserve_tier_rejects_weak_signals_but_allows_strong_signals(self):
        risk = self.dynamic_risk.DynamicRiskManager(StubBroker())
        held = _trade("INFY", size_usd=70000.0)
        portfolio = Portfolio(
            bankroll=100000.0,
            peak_bankroll=100000.0,
            open_trades=[held],
        )
        regime = {"size_mult": 1.0}

        self.assertIsNone(
            risk.approve(
                _signal("ZOMATO", analyst_score=0.39),
                0.1,
                10000.0,
                portfolio,
                regime,
            )
        )
        approved = risk.approve(
            _signal("ZOMATO", analyst_score=0.50),
            0.1,
            10000.0,
            portfolio,
            regime,
        )
        self.assertEqual(approved, 10000.0)

    def test_beta_cap_rejects_residual_size_below_minimum(self):
        risk = self.dynamic_risk.DynamicRiskManager(StubBroker())
        portfolio = Portfolio(bankroll=100000.0, peak_bankroll=100000.0)

        # The beta cap leaves ₹4,444.44 of room. The old max(MIN_POSITION)
        # floor inflated this back to ₹5,000 and defeated the cap.
        with mock.patch.object(risk, "_estimate_portfolio_beta", return_value=1.22):
            approved = risk.approve(
                _signal("ZOMATO", analyst_score=0.8),
                0.1,
                10000.0,
                portfolio,
                {"size_mult": 1.0},
            )
        self.assertIsNone(approved)

    def test_restart_restore_seeds_daily_loss_and_reentry_cooldown(self):
        # Freeze the clock because the restore policy is explicitly based on
        # the current IST day and a 24-hour UTC cooldown.
        fixed_now = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)

        class FixedDateTime(datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        fake_datetime = types.ModuleType("datetime")
        fake_datetime.datetime = FixedDateTime
        fake_datetime.time = datetime_module.time
        fake_datetime.timezone = datetime_module.timezone
        broker = StubBroker(now=fixed_now.astimezone(IST))
        risk = self.dynamic_risk.DynamicRiskManager(broker)
        # Keep the realistic row shape so this remains a guard for the restart
        # breaker and the timestamp-aware cooldown policy.
        def fetchall(sql):
            if "GROUP BY symbol" in sql:
                return [{"symbol": "INFY", "ex": fixed_now.isoformat()}]
            # Mirror the columns selected by the production query.
            if sql.lstrip().startswith("SELECT pnl_usd,"):
                return [{"pnl_usd": -250.0, "exit_time": fixed_now.isoformat()}]
            return [{"pnl_usd": -250.0}]

        with mock.patch.dict(sys.modules, {"datetime": fake_datetime}), \
             mock.patch("db.fetchall", side_effect=fetchall):
            risk.restore_state_from_db()

        self.assertEqual(risk._daily_pnl, -250.0)
        self.assertEqual(risk._daily_pnl_date, broker.now.date())
        self.assertIn("INFY", risk._recent_exits)


class LiveSafetyGateTests(unittest.TestCase):
    def setUp(self):
        self._values = {
            name: getattr(Config, name)
            for name in (
                "BROKER",
                "DRY_RUN",
                "ENABLE_LIVE_TRADING",
                "LIVE_TRADING_ACK",
                "IGNORE_MARKET_HOURS",
                "USE_EARNINGS_GATE",
                "USE_SENTIMENT_GATE",
                "USE_FUNDAMENTALS_GATE",
                "PAPER_TRADING",
            )
        }
        Config.BROKER = "zerodha"
        Config.DRY_RUN = False
        Config.PAPER_TRADING = False
        Config.ENABLE_LIVE_TRADING = False
        Config.LIVE_TRADING_ACK = ""
        Config.IGNORE_MARKET_HOURS = False
        Config.USE_EARNINGS_GATE = True
        Config.USE_SENTIMENT_GATE = True
        Config.USE_FUNDAMENTALS_GATE = True
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._values.items():
            setattr(Config, name, value)

    def test_live_entries_require_explicit_acknowledgement(self):
        allowed, reason = Config.live_entry_status()
        self.assertFalse(allowed)
        self.assertIn("ENABLE_LIVE_TRADING", reason)

        Config.ENABLE_LIVE_TRADING = True
        Config.LIVE_TRADING_ACK = Config.LIVE_TRADING_ACK_TEXT
        allowed, reason = Config.live_entry_status()
        self.assertTrue(allowed)
        self.assertEqual(reason, "enabled")

    def test_live_entries_reject_test_mode_or_disabled_data_gates(self):
        Config.ENABLE_LIVE_TRADING = True
        Config.LIVE_TRADING_ACK = Config.LIVE_TRADING_ACK_TEXT

        Config.IGNORE_MARKET_HOURS = True
        allowed, reason = Config.live_entry_status()
        self.assertFalse(allowed)
        self.assertIn("IGNORE_MARKET_HOURS", reason)

        Config.IGNORE_MARKET_HOURS = False
        Config.USE_SENTIMENT_GATE = False
        allowed, reason = Config.live_entry_status()
        self.assertFalse(allowed)
        self.assertIn("data gates", reason)


class DatabaseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.db = importlib.import_module("db")
        self._old_path = Config.DB_PATH
        self._close_thread_connection()
        self.tempdir = tempfile.TemporaryDirectory()
        Config.DB_PATH = str(Path(self.tempdir.name) / "regression.sqlite")
        self.db.init_db()
        self.addCleanup(self._cleanup)

    def _close_thread_connection(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None

    def _cleanup(self):
        self._close_thread_connection()
        Config.DB_PATH = self._old_path
        self.tempdir.cleanup()

    def test_schema_migrations_and_failed_write_rollback_leave_connection_usable(self):
        columns = {
            row[1]
            for row in self.db._get_conn().execute("PRAGMA table_info(trades)")
        }
        self.assertTrue({"highest_price", "trim_shares", "trim_price", "rationale"} <= columns)
        broker_order_columns = {
            row[1]
            for row in self.db._get_conn().execute("PRAGMA table_info(broker_orders)")
        }
        self.assertTrue({"order_id", "status", "filled_avg_price"} <= broker_order_columns)

    def test_unresolved_live_order_state_survives_restart(self):
        from brokers.live_base import LiveBrokerBase

        def make_broker():
            broker = object.__new__(LiveBrokerBase)
            BaseBroker.__init__(broker)
            broker._order_persistence_enabled = True
            broker._unresolved_orders = {}
            broker._unresolved_orders_loaded = False
            broker.ORDER_STATUS_POLL_INTERVAL = 0
            broker.ORDER_STATUS_POLL_TIMEOUT = 0
            broker.ORDER_STATUS_MAX_POLLS = 1
            return broker

        first = make_broker()
        first._order_status = mock.Mock(return_value={
            "id": "persisted-1", "status": "open", "filled_quantity": 0,
        })
        result = first._confirm_order("persisted-1", "INFY", "buy", 10, "tj_persist")
        self.assertTrue(result["unsafe"])

        row = self.db.fetchone(
            "SELECT status, symbol, requested_qty FROM broker_orders WHERE order_id=?",
            ("persisted-1",),
        )
        self.assertEqual((row["status"], row["symbol"], row["requested_qty"]),
                         ("timeout", "INFY", 10))

        restored = make_broker()
        restored._load_unresolved_orders()
        self.assertIn("persisted-1", restored._unresolved_orders)

        self.db.execute(
            "INSERT INTO portfolio_snapshots(snap_date, bankroll, open_trades, total_pnl) "
            "VALUES (?, ?, ?, ?)",
            ("2026-08-17", 100000.0, 0, 0.0),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO portfolio_snapshots(snap_date, bankroll, open_trades, total_pnl) "
                "VALUES (?, ?, ?, ?)",
                ("2026-08-17", 90000.0, 1, -10000.0),
            )

        # execute() rolled back the failed statement and kept the connection
        # usable for a subsequent committed write.
        self.db.execute(
            "INSERT INTO portfolio_snapshots(snap_date, bankroll, open_trades, total_pnl) "
            "VALUES (?, ?, ?, ?)",
            ("2026-08-18", 101000.0, 0, 1000.0),
        )
        self.assertEqual(
            self.db.fetchone("SELECT COUNT(*) AS n FROM portfolio_snapshots")["n"],
            2,
        )

    def test_thread_local_connections_commit_from_independent_workers(self):
        connection_ids = []
        errors = []
        first_done = threading.Event()

        def writer(symbol: str, wait_for_first: bool):
            try:
                if wait_for_first:
                    self.assertTrue(first_done.wait(timeout=5))
                connection_ids.append(id(self.db._get_conn()))
                self.db.execute(
                    "INSERT INTO rejected_signals(symbol, reason, edge, quality) "
                    "VALUES (?, ?, ?, ?)",
                    (symbol, "fixture", 0.1, 0.8),
                )
                first_done.set()
            except Exception as exc:  # surfaced by the assertions below
                errors.append(exc)

        first = threading.Thread(target=writer, args=("A", False))
        second = threading.Thread(target=writer, args=("B", True))
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(set(connection_ids)), 2)
        self.assertEqual(
            self.db.fetchone("SELECT COUNT(*) AS n FROM rejected_signals")["n"],
            2,
        )


class ExecutionAndPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.exec_mod = types.ModuleType("ml_engine_gold")
        self.exec_mod.MLEngineGold = object
        self._old_ml_module = sys.modules.get("ml_engine_gold")
        sys.modules["ml_engine_gold"] = self.exec_mod
        sys.modules.pop("execution_engine", None)
        self.execution_engine = importlib.import_module("execution_engine")
        self.old_dry_run = Config.DRY_RUN
        self.old_paper = Config.PAPER_TRADING
        Config.DRY_RUN = False
        Config.PAPER_TRADING = True
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        Config.DRY_RUN = self.old_dry_run
        Config.PAPER_TRADING = self.old_paper
        sys.modules.pop("execution_engine", None)
        if self._old_ml_module is None:
            sys.modules.pop("ml_engine_gold", None)
        else:
            sys.modules["ml_engine_gold"] = self._old_ml_module

    def test_execute_books_broker_fill_quantity_and_price(self):
        class FillBroker:
            def place_bracket_order(self, **kwargs):
                return {
                    "id": "broker-order",
                    "qty": 15,
                    "filled_avg_price": 101.0,
                }

        ml = mock.Mock()
        engine = self.execution_engine.ExecutionEngine(FillBroker(), ml)
        signal = _signal("INFY", entry_price=100.0)

        with mock.patch.object(self.execution_engine.db, "execute") as execute:
            trade = engine.execute(signal, approved_size=1599.0, kelly_pct=0.1, entry_quality=0.8)

        self.assertIsNotNone(trade)
        self.assertEqual(trade.shares, 15.0)
        self.assertEqual(trade.entry_price, 101.0)
        self.assertEqual(trade.size_usd, 1515.0)
        self.assertEqual(trade.highest_price, 101.0)
        persisted = execute.call_args.args[1]
        self.assertEqual(persisted[3:6], (15.0, 1515.0, 101.0))
        ml.log_entry.assert_called_once()

    def test_live_flatten_failure_keeps_trade_open(self):
        class FailingBroker:
            def close_position(self, symbol):
                return None

        Config.PAPER_TRADING = False
        trade = _trade("INFY")
        engine = self.execution_engine.ExecutionEngine(FailingBroker(), mock.Mock())

        with mock.patch.object(self.execution_engine.db, "execute") as execute:
            closed = engine.close_trade(trade, 105.0, 0.05, "TAKE_PROFIT")

        self.assertFalse(closed)
        self.assertEqual(trade.status, "open")
        self.assertIsNone(trade.exit_time)
        execute.assert_not_called()

    def test_dry_run_does_not_create_hypothetical_trade_rows(self):
        class DryRunBroker:
            def place_bracket_order(self, **kwargs):
                return {"dry_run": True, "id": "dry-order"}

        Config.DRY_RUN = True
        engine = self.execution_engine.ExecutionEngine(DryRunBroker(), mock.Mock())
        signal = _signal("INFY")

        with mock.patch.object(self.execution_engine.db, "execute") as execute:
            trade = engine.execute(signal, approved_size=10000.0, kelly_pct=0.1,
                                   entry_quality=0.8)

        self.assertIsNone(trade)
        self.assertTrue(engine.last_was_dry_run)
        execute.assert_not_called()


class SimulatedRestoreTests(unittest.TestCase):
    def test_restore_uses_untrimmed_shares_and_actual_cost_basis(self):
        simulated_mod = importlib.import_module("brokers.simulated")
        broker = object.__new__(simulated_mod.SimulatedBroker)
        broker.book = {}
        broker.cash = 10000.0
        rows = [
            {"symbol": "INFY", "shares": 10, "size_usd": 1000, "entry_price": 100,
             "trim_shares": 3, "trim_price": 105},
            # -1 means "partial close unsupported", not one extra share trimmed.
            {"symbol": "TCS", "shares": 2, "size_usd": 100, "entry_price": 50,
             "trim_shares": -1, "trim_price": None},
        ]
        with mock.patch("db.fetchall", return_value=rows):
            broker._restore_from_db()

        self.assertEqual(broker.book["INFY"], {"qty": 7.0, "avg": 100.0})
        self.assertEqual(broker.book["TCS"], {"qty": 2.0, "avg": 50.0})
        # ₹1,000 entry cost less ₹315 trim proceeds and ₹100 TCS cost.
        self.assertEqual(broker.cash, 9215.0)

    def test_restore_carries_closed_realized_pnl_into_cash(self):
        simulated_mod = importlib.import_module("brokers.simulated")
        broker = object.__new__(simulated_mod.SimulatedBroker)
        broker.book = {}
        broker.cash = 10000.0

        def fetchall(sql):
            if "status='open'" in sql:
                return [{"symbol": "INFY", "shares": 10, "entry_price": 100,
                         "trim_shares": 0, "trim_price": None}]
            return [{"pnl_usd": 250.0}, {"pnl_usd": float("nan")}, {"pnl_usd": "bad"}]

        with mock.patch("db.fetchall", side_effect=fetchall):
            broker._restore_from_db()

        self.assertEqual(broker.cash, 9250.0)


class ReconciliationTests(unittest.TestCase):
    def test_empty_fetch_is_not_flat_and_duplicate_drift_is_suppressed(self):
        telegram = types.ModuleType("telegram_alerts")
        telegram.alert_system_message = mock.Mock()
        old_telegram = sys.modules.get("telegram_alerts")
        old_recon = sys.modules.pop("reconciliation_engine", None)
        sys.modules["telegram_alerts"] = telegram
        try:
            recon_mod = importlib.import_module("reconciliation_engine")
            trade = _trade("INFY", shares=10, trim_shares=2)
            portfolio = Portfolio(100000.0, 100000.0, open_trades=[trade])

            class Positions:
                values = []
                _last_positions_ok = True

                def get_positions(self):
                    return list(self.values)

            broker = Positions()
            engine = recon_mod.ReconciliationEngine(broker, portfolio)
            engine._running = True

            # 8.4 is within the half-share reconciliation tolerance for 8.
            broker.values = [{"symbol": "INFY", "qty": 8.4}]
            engine.reconcile()
            telegram.alert_system_message.assert_not_called()
            self.assertTrue(engine.entries_safe())

            broker.values = [{"symbol": "INFY", "qty": 7}]
            engine.reconcile()
            engine.reconcile()
            self.assertEqual(telegram.alert_system_message.call_count, 1)
            self.assertFalse(engine.entries_safe())

            # [] is treated as an unavailable fetch, not as a confirmed flat book
            # when the adapter reports that the fetch failed.
            broker.values = []
            broker._last_positions_ok = False
            engine.reconcile()
            self.assertEqual(telegram.alert_system_message.call_count, 1)
            self.assertFalse(engine.entries_safe())
        finally:
            sys.modules.pop("reconciliation_engine", None)
            if old_recon is not None:
                sys.modules["reconciliation_engine"] = old_recon
            if old_telegram is None:
                sys.modules.pop("telegram_alerts", None)
            else:
                sys.modules["telegram_alerts"] = old_telegram


class WebApiSerializationTests(unittest.TestCase):
    def test_manual_live_start_requires_non_persisted_acknowledgement(self):
        webui = importlib.import_module("webui")
        old_proc = webui.bot_proc
        webui.bot_proc = None
        try:
            with mock.patch.object(webui, "_bot_lock_busy", return_value=False), \
                 mock.patch.object(webui.subprocess, "Popen") as popen:
                result = webui.start_bot(
                    {"broker": "zerodha", "dry_run": False, "bankroll": "100000"},
                    source="ui",
                )
            self.assertFalse(result["ok"])
            self.assertIn("I_UNDERSTAND_LIVE_TRADING", result["error"])
            popen.assert_not_called()
        finally:
            webui.bot_proc = old_proc

    def test_start_injects_only_selected_broker_credentials(self):
        webui = importlib.import_module("webui")
        old_proc = webui.bot_proc
        webui.bot_proc = None
        fake_proc = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
        try:
            with mock.patch.object(webui, "_bot_lock_busy", return_value=False), \
                 mock.patch.object(webui.subprocess, "Popen", return_value=fake_proc) as popen, \
                 mock.patch.object(webui, "load_keys", return_value={
                     "KITE_API_KEY": "kite", "DHAN_ACCESS_TOKEN": "dhan"}), \
                 mock.patch.object(webui, "save_autostart_opts"), \
                 mock.patch.object(webui, "set_paused"):
                result = webui.start_bot({"broker": "zerodha", "dry_run": True,
                                          "bankroll": "100000", "scan": "30"})
            self.assertTrue(result["ok"])
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["KITE_API_KEY"], "kite")
            self.assertNotIn("DHAN_ACCESS_TOKEN", env)
            self.assertNotIn("TEJAS_UI_TOKEN", env)
        finally:
            webui.bot_proc = old_proc

    def test_auto_start_downgrades_missing_live_credentials_to_dry_run(self):
        webui = importlib.import_module("webui")
        old_proc = webui.bot_proc
        webui.bot_proc = None
        fake_proc = mock.Mock(pid=124, poll=mock.Mock(return_value=None))
        try:
            with mock.patch.object(webui, "_bot_lock_busy", return_value=False), \
                 mock.patch.object(webui.subprocess, "Popen", return_value=fake_proc) as popen, \
                 mock.patch.object(webui, "load_keys", return_value={}):
                result = webui.start_bot(
                    {"broker": "zerodha", "dry_run": False, "bankroll": "100000"},
                    source="auto",
                )
            self.assertTrue(result["ok"])
            self.assertEqual(popen.call_args.kwargs["env"]["DRY_RUN"], "true")
        finally:
            webui.bot_proc = old_proc

    def test_duration_normalizes_legacy_naive_timestamps(self):
        from telegram_alerts import _duration

        trade = _trade("INFY", entry_time=datetime(2026, 8, 17, 10, 0))
        trade.exit_time = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
        self.assertEqual(_duration(trade), "2h 30m")

    def test_trade_history_round_trips_special_characters_as_json_data(self):
        webui = importlib.import_module("webui")
        old_path = webui.DB_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            webui.DB_PATH = str(Path(tempdir) / "history.sqlite")
            conn = sqlite3.connect(webui.DB_PATH)
            conn.execute(
                "CREATE TABLE trades ("
                "symbol TEXT, entry_price REAL, exit_price REAL, pnl_usd REAL, "
                "pnl_pct REAL, exit_reason TEXT, rationale TEXT, entry_time TEXT, "
                "exit_time TEXT, trim_shares REAL, trim_price REAL, status TEXT)"
            )
            rationale = '"><script>alert("x")</script>&'
            conn.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "INFY",
                    100.0,
                    105.0,
                    50.0,
                    0.05,
                    "TAKE_PROFIT",
                    rationale,
                    "2026-08-17T10:00:00+00:00",
                    "2026-08-18T10:00:00+00:00",
                    0.0,
                    None,
                    "closed",
                ),
            )
            conn.commit()
            conn.close()
            try:
                rows = webui.trade_history()
                decoded = json.loads(json.dumps(rows))
            finally:
                webui.DB_PATH = old_path

        self.assertEqual(decoded[0]["rationale"], rationale)
        self.assertFalse(decoded[0]["trimmed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
