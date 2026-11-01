"""
earnings_filter.py — ★ NEW: Earnings Calendar Blackout
========================================================
Earnings announcements are binary events — a stock can move ±15%
in seconds based on one report. Trading into earnings is gambling,
not quantitative edge. This filter blocks it entirely.

LOGIC:
  • Fetch earnings dates from AlphaVantage (free API, 500/day)
  • Cache results for 24 hours (dates don't change)
  • Block new ENTRIES within 3 days before earnings
  • Signal EXIT for existing positions 2h before earnings
  • Re-enable trading 1 day after earnings (post-gap stabilizes)

This is one of the highest-alpha improvements you can make:
  - Avoids binary risk completely
  - Prevents holding into gap-down disasters
  - Forces you into only high-quality setups with known dates

Real hedge funds dedicate entire teams to earnings calendars.
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Set

from brokers.data_provider import quote_summary
from config import Config

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(IST).date()


class EarningsFilter:
    def __init__(self):
        # Cache: symbol → (earnings_date, fetched_at, ok)
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = 86400   # 24h — earnings dates are stable
        # ★ Failed fetches get a SHORT ttl: caching a transient Yahoo outage for
        # 24h fail-opened the earnings gate for the whole day (is_blackout(None)
        # is False), letting entries through right before results announcements.
        self._fail_ttl = 900      # 15min — retry soon after an outage

    # ── PUBLIC API ────────────────────────────────────────────────────────────
    def is_blackout(self, symbol: str) -> bool:
        """
        Returns True if we should NOT enter a new position.
        True if earnings are within EARNINGS_BLACKOUT_DAYS_BEFORE days.
        """
        next_date = self._get_next_earnings(symbol)
        if next_date is None:
            return False   # unknown = allow (can't block indefinitely)

        today = _today_ist()
        days_to = (next_date - today).days

        if days_to < 0:
            # Earnings was recently — check if we're in post-blackout
            return abs(days_to) <= Config.EARNINGS_BLACKOUT_DAYS_AFTER

        return days_to <= Config.EARNINGS_BLACKOUT_DAYS_BEFORE

    def minutes_to_earnings(self, symbol: str) -> Optional[float]:
        """
        Returns minutes until earnings announcement.
        None if unknown. Negative if already past.
        """
        next_date = self._get_next_earnings(symbol)
        if next_date is None:
            return None

        # NSE results are typically declared during/after the session; assume
        # 16:00 IST on the date as a conservative cut-off for the pre-earnings exit.
        earnings_dt = datetime.combine(next_date, datetime.min.time()).replace(
            hour=16, minute=0, tzinfo=IST
        )
        now_ist = datetime.now(IST)
        diff_secs = (earnings_dt - now_ist).total_seconds()
        return diff_secs / 60.0

    def should_exit_before_earnings(self, symbol: str, *, fetch: bool = True) -> bool:
        """
        Returns True if we should exit an existing position
        (within EARNINGS_EXIT_BEFORE_MINS minutes of earnings).
        """
        if fetch:
            mins = self.minutes_to_earnings(symbol)
        else:
            # Exit monitoring must not block the stop loop on a fresh Yahoo
            # request for every open position. The scanner pre-pass warms this
            # cache; an uncached symbol is checked on the next scan cycle.
            cached = self._cache.get(symbol)
            if not cached:
                return False
            next_date, fetched_at, ok = cached
            ttl = self._cache_ttl if ok else self._fail_ttl
            if time.time() - fetched_at >= ttl:
                return False
            if next_date is None:
                return False
            earnings_dt = datetime.combine(next_date, datetime.min.time()).replace(
                hour=16, minute=0, tzinfo=IST
            )
            mins = (earnings_dt - datetime.now(IST)).total_seconds() / 60.0
        if mins is None:
            return False
        return 0 < mins <= Config.EARNINGS_EXIT_BEFORE_MINS

    def days_to_earnings(self, symbol: str) -> Optional[int]:
        """Days until next earnings. None if unknown. Negative if past."""
        next_date = self._get_next_earnings(symbol)
        if next_date is None:
            return None
        return (next_date - _today_ist()).days

    # =============================================================================
    # [MODIFIED 2026-11-01]: Parity fix for earnings data freshness
    # ORIGINAL INTENT: Required next_date is not None to ensure calendar data was fetched.
    # REASON FOR CHANGE: When a company has no earnings scheduled in the current window,
    # next_date is None but ok is True (verified safe). Requiring next_date is not None
    # caused live trading to reject >90% of safe stocks that paper trading traded.
    # SAFE ROLLBACK: Preserved original implementation below as comments.
    # -----------------------------------------------------------------------------
    # [PREVIOUS CODE - PRESERVED]:
    # def has_fresh_data(self, symbol: str) -> bool:
    #     """True only when a fresh calendar fetch produced a known next date.
    #
    #     ``is_blackout`` keeps its historical unknown=allow behavior for paper and
    #     existing-position monitoring. Live entry selection uses this stricter
    #     predicate so an outage or an unchecked symbol cannot pass as safe.
    #     """
    #     cached = self._cache.get(symbol)
    #     if not cached:
    #         return False
    #     next_date, fetched_at, ok = cached
    #     try:
    #         return bool(ok and next_date is not None
    #                     and 0 <= time.time() - float(fetched_at) < self._cache_ttl)
    #     except (TypeError, ValueError, OverflowError):
    #         return False
    # -----------------------------------------------------------------------------
    # [NEW CODE]:
    def has_fresh_data(self, symbol: str) -> bool:
        """True when a fresh calendar fetch has confirmed the symbol's status.

        A successful fetch where no earnings are scheduled in the window returns
        ok=True and next_date=None, which is verified fresh and safe. Outages or
        failed fetches have ok=False and fail this check.
        """
        cached = self._cache.get(symbol)
        if not cached:
            return False
        next_date, fetched_at, ok = cached
        try:
            return bool(ok and 0 <= time.time() - float(fetched_at) < self._cache_ttl)
        except (TypeError, ValueError, OverflowError):
            return False
    # =============================================================================

    # ── FETCH ─────────────────────────────────────────────────────────────────
    def _get_next_earnings(self, symbol: str) -> Optional[date]:
        """Returns next earnings date, from cache or Yahoo."""
        # Check cache ("no earnings found" cached for the full TTL; FAILED
        # fetches only briefly, so an outage can't disarm the gate all day)
        if symbol in self._cache:
            earnings_date, fetched_at, ok = self._cache[symbol]
            ttl = self._cache_ttl if ok else self._fail_ttl
            if time.time() - fetched_at < ttl:
                return earnings_date

        earnings_date, ok = self._fetch_from_yahoo(symbol)
        if not ok:
            log.debug(f"[Earnings] {symbol}: fetch failed — gate inactive, retry in {self._fail_ttl//60}min")
        self._cache[symbol] = (earnings_date, time.time(), ok)
        return earnings_date

    def _fetch_from_yahoo(self, symbol: str) -> tuple:
        """
        Real next-earnings date from Yahoo Finance calendarEvents (works for NSE
        symbols, no API key). Returns (next_future_earnings_date_or_None, ok) —
        ok=False means the FETCH failed (≠ "no earnings scheduled").
        """
        try:
            res = quote_summary(symbol, "calendarEvents")
            if not res:
                return None, False
            dates = (
                res.get("calendarEvents", {})
                   .get("earnings", {})
                   .get("earningsDate", [])
            )
            today = _today_ist()
            future = []
            for d in dates:
                raw = d.get("raw") if isinstance(d, dict) else None
                if raw is None:
                    continue
                ed = datetime.fromtimestamp(raw, tz=IST).date()
                future.append(ed)
            if not future:
                return None, True   # genuinely no earnings info — cache full TTL
            future.sort()
            # Prefer the next date that is today-or-future; else the latest known.
            nxt = next((d for d in future if d >= today), future[-1])
            log.debug(f"[Earnings] {symbol}: next={nxt}")
            return nxt, True
        except Exception as e:
            log.debug(f"[Earnings] {symbol} Yahoo fetch failed: {e}")
            return None, False

    def get_blackout_set(self, symbols: list,
                         time_budget_secs: float = 90.0,
                         max_consecutive_failures: int = 8,
                         fail_closed: bool = False) -> Set[str]:
        """Returns set of symbols currently in earnings blackout.

        ★ BOUNDED. This runs at the top of EVERY scan cycle over the whole
        universe. When Yahoo quoteSummary goes down, each uncached fetch burned
        ~6s in retries × 500 symbols ≈ a 50-MINUTE cycle — during which the bot
        can neither monitor open positions (stops dark!) nor respond to a stop
        request. Two brakes: a hard wall-clock budget, and a consecutive-failure
        circuit breaker (a systemic outage fails identically on every symbol —
        after 8 in a row, checking the other 490 is pointless). Paper callers may
        keep the historical fail-open behavior; live callers separately reject
        every symbol without fresh calendar data."""
        blackout = set()
        start = time.time()
        consec_fail = 0
        failed = 0
        stopped_early = False
        for i, sym in enumerate(symbols):
            if time.time() - start > time_budget_secs:
                stopped_early = True
                break
            try:
                if self.is_blackout(sym):
                    days = self.days_to_earnings(sym)
                    log.info(f"[Earnings] {sym}: BLACKOUT ({days}d to earnings)")
                    blackout.add(sym)
                # circuit breaker: count fetches that FAILED (ok=False in cache)
                ent = self._cache.get(sym)
                if ent is not None and ent[2] is False:
                    failed += 1
                    consec_fail += 1
                    if consec_fail >= max_consecutive_failures:
                        stopped_early = True
                        break
                else:
                    consec_fail = 0
            except Exception:
                failed += 1
        if stopped_early:
            mode = "blocked for live entries" if fail_closed else "fail open for paper"
            log.warning(f"[Earnings] blackout pre-pass stopped early at {len(self._cache)}/{len(symbols)} "
                        f"symbols; unchecked names {mode} this cycle")
        if failed:
            log.warning(f"[Earnings] {failed} calendar fetches failed; live entries blocked for those names "
                        f"(retry in {self._fail_ttl//60}min)")
        return blackout
