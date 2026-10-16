"""
performance.py — Live Performance Analytics
=============================================
Computes professional-grade metrics from the trades table:
  Sharpe · Sortino · Calmar · Win rate · Profit factor
  Max drawdown · Edge decay · ML calibration · Per-sector breakdown
  Equity curve for charting
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

import db
from config import Config

log = logging.getLogger(__name__)
TRADING_DAYS = 252  # US stock market


@dataclass
class TradeStats:
    total_trades:  int   = 0
    wins:          int   = 0
    losses:        int   = 0
    win_rate:      float = 0.0
    avg_win_pct:   float = 0.0
    avg_loss_pct:  float = 0.0
    profit_factor: float = 0.0
    total_pnl_usd: float = 0.0
    avg_trade_pnl: float = 0.0


@dataclass
class RiskMetrics:
    sharpe:           float = 0.0
    sortino:          float = 0.0
    calmar:           float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    current_dd_pct:   float = 0.0


@dataclass
class FullReport:
    generated_at:   datetime
    lookback_days:  int
    trade_stats:    TradeStats
    risk_metrics:   RiskMetrics
    by_sector:      Dict[str, TradeStats] = field(default_factory=dict)
    by_exit_reason: Dict[str, int]        = field(default_factory=dict)
    equity_curve:   List[Tuple[str, float]] = field(default_factory=list)


class PerformanceAnalytics:

    def generate_report(self, lookback_days: int = 30) -> FullReport:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = db.fetchall(
            """
            SELECT trade_id, symbol, side, size_usd, entry_price, exit_price,
                   ml_probability, entry_quality, kelly_pct, pnl_usd, pnl_pct,
                   entry_time, exit_time, exit_reason, sector_code
            FROM trades
            WHERE status='closed' AND entry_time IS NOT NULL
              AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL
              AND julianday(entry_time) IS NOT NULL
              AND julianday(entry_time) >= julianday(?)
            ORDER BY entry_time ASC
            """,
            (cutoff,),
        )
        # Legacy rows can contain NULL/text/NaN P&L even when status says closed.
        # Exclude those rows rather than letting one corrupt record abort the
        # entire dashboard report or poison numpy metrics.
        valid_rows = []
        for row in rows:
            try:
                if (math.isfinite(float(row["pnl_pct"]))
                        and math.isfinite(float(row["pnl_usd"]))):
                    valid_rows.append(row)
            except (TypeError, ValueError):
                continue
        rows = valid_rows
        if not rows:
            return FullReport(
                generated_at=datetime.now(timezone.utc),
                lookback_days=lookback_days,
                trade_stats=TradeStats(),
                risk_metrics=RiskMetrics(),
            )

        pnl_pct = [float(r["pnl_pct"]) for r in rows]
        pnl_usd = [float(r["pnl_usd"]) for r in rows]
        win_mask = [p > 0 for p in pnl_usd]

        report = FullReport(
            generated_at=datetime.now(timezone.utc),
            lookback_days=lookback_days,
            trade_stats=self._trade_stats(rows, pnl_pct, pnl_usd, win_mask),
            risk_metrics=self._risk_metrics(pnl_pct, pnl_usd),
            by_sector=self._by_sector(rows),
            by_exit_reason=self._by_exit_reason(rows),
            equity_curve=self._equity_curve(rows),
        )
        self._log_report(report)
        return report

    def _trade_stats(self, rows, pnl_pct, pnl_usd, win_mask) -> TradeStats:
        n = len(rows)
        if not n:
            return TradeStats()
        wins   = [p for p, w in zip(pnl_usd, win_mask) if w]
        losses = [p for p, w in zip(pnl_usd, win_mask) if not w]
        gross_profit = sum(p for p in pnl_usd if p > 0)
        gross_loss   = abs(sum(p for p in pnl_usd if p < 0))
        return TradeStats(
            total_trades=n, wins=len(wins), losses=len(losses),
            win_rate=len(wins)/n,
            avg_win_pct=float(np.mean([p for p,w in zip(pnl_pct,win_mask) if w])) if wins else 0.0,
            avg_loss_pct=float(np.mean([p for p,w in zip(pnl_pct,win_mask) if not w])) if losses else 0.0,
            profit_factor=gross_profit/gross_loss if gross_loss > 0 else float("inf"),
            total_pnl_usd=sum(pnl_usd),
            avg_trade_pnl=float(np.mean(pnl_usd)),
        )

    def _risk_metrics(self, pnl_pct, pnl_usd) -> RiskMetrics:
        if len(pnl_pct) < 3:
            return RiskMetrics()
        arr = np.array(pnl_pct)
        mean_r = float(np.mean(arr))
        std_r  = float(np.std(arr, ddof=1))
        sharpe = mean_r / std_r * np.sqrt(TRADING_DAYS) if std_r > 0 else 0.0
        downside = arr[arr < 0]
        down_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else std_r
        sortino  = mean_r / down_std * np.sqrt(TRADING_DAYS) if down_std > 0 else 0.0
        # Drawdown is measured on the equity curve (starting capital + cumulative
        # PnL), not on cumulative PnL alone. The old code divided the ₹ drawdown by
        # max(cumulative_pnl) + 10000 — an arbitrary constant with no financial
        # meaning that systematically understated the drawdown percentage.
        cumul   = np.cumsum(pnl_usd)
        try:
            base_equity = float(Config.BANKROLL)
        except (TypeError, ValueError):
            base_equity = 0.0
        if not math.isfinite(base_equity) or base_equity < 0:
            base_equity = 0.0
        equity  = base_equity + cumul
        run_max = np.maximum.accumulate(equity)
        dds     = run_max - equity                       # ₹ drawdown from running peak equity
        # Do not divide by a zero/negative peak if corrupted data exceeds the
        # configured bankroll.  Such a path is already a complete loss; report a
        # bounded 100% drawdown instead of NaN/negative percentages.
        dd_pct = np.zeros_like(dds, dtype=float)
        positive_peak = run_max > 0
        dd_pct[positive_peak] = dds[positive_peak] / run_max[positive_peak]
        dd_pct[~positive_peak] = np.where(dds[~positive_peak] > 0, 1.0, 0.0)
        max_dd  = float(np.max(dds)) if len(dds) else 0.0
        calmar  = sum(pnl_usd) / max_dd if max_dd > 0 else 0.0
        return RiskMetrics(
            sharpe=round(sharpe, 3), sortino=round(sortino, 3), calmar=round(calmar, 3),
            max_drawdown_pct=round(float(np.max(dd_pct)), 4) if len(dd_pct) else 0.0,
            max_drawdown_usd=round(max_dd, 2),
            current_dd_pct=round(float(dd_pct[-1]), 4) if len(dd_pct) else 0.0,
        )

    def _by_sector(self, rows) -> Dict[str, TradeStats]:
        codes = {0:"technology",1:"finance",2:"healthcare",3:"consumer",4:"energy",5:"etf",6:"other"}
        data: Dict[str, dict] = {}
        for r in rows:
            try:
                code = int(r["sector_code"] or 6)
            except (TypeError, ValueError):
                code = 6
            sec = codes.get(code, "other")
            if sec not in data:
                data[sec] = {"pnl_pct":[],"pnl_usd":[],"win":[]}
            pnl = float(r["pnl_usd"] or 0)
            data[sec]["pnl_pct"].append(float(r["pnl_pct"] or 0))
            data[sec]["pnl_usd"].append(pnl)
            data[sec]["win"].append(pnl > 0)
        result = {}
        for sec, d in data.items():
            n = len(d["pnl_usd"]); wins = sum(d["win"])
            result[sec] = TradeStats(
                total_trades=n, wins=wins, losses=n-wins,
                win_rate=wins/n if n else 0,
                total_pnl_usd=sum(d["pnl_usd"]),
                avg_trade_pnl=float(np.mean(d["pnl_usd"])) if d["pnl_usd"] else 0,
            )
        return result

    def _by_exit_reason(self, rows) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in rows:
            k = str(r["exit_reason"] or "UNKNOWN").split("_")[0]
            counts[k] = counts.get(k, 0) + 1
        return counts

    def _equity_curve(self, rows) -> List[Tuple[str, float]]:
        curve, cumul = [], 0.0
        for r in rows:
            cumul += float(r["pnl_usd"] or 0)
            date = str(r["exit_time"] or r["entry_time"] or "")[:10]
            curve.append((date, round(cumul, 2)))
        return curve

    def _log_report(self, r: FullReport) -> None:
        ts = r.trade_stats; rm = r.risk_metrics
        log.info(
            f"\n{'='*60}\n  PERFORMANCE — last {r.lookback_days}d\n"
            f"  Trades: {ts.total_trades}  WR: {ts.win_rate:.1%}  PF: {ts.profit_factor:.2f}  "
            f"PnL: ${ts.total_pnl_usd:+,.2f}\n"
            f"  Sharpe: {rm.sharpe:.3f}  Sortino: {rm.sortino:.3f}  Calmar: {rm.calmar:.3f}\n"
            f"  MaxDD: {rm.max_drawdown_pct:.1%} (${rm.max_drawdown_usd:.2f})\n{'='*60}"
        )

    def print_summary(self, lookback_days: int = 7) -> None:
        r = self.generate_report(lookback_days)
        ts = r.trade_stats; rm = r.risk_metrics
        print(f"\n── Last {lookback_days}d Performance ──────────────────────")
        print(f"  Trades: {ts.total_trades} | WR: {ts.win_rate:.1%} | P/F: {ts.profit_factor:.2f}")
        print(f"  PnL: ${ts.total_pnl_usd:+,.2f} | Avg: ${ts.avg_trade_pnl:+,.2f}/trade")
        print(f"  Sharpe: {rm.sharpe:.3f} | Sortino: {rm.sortino:.3f} | Calmar: {rm.calmar:.3f}")
        print(f"  MaxDD: {rm.max_drawdown_pct:.1%} (${rm.max_drawdown_usd:.2f})")
        if r.by_sector:
            print("\n── By Sector ────────────────────────────────────────")
            for sec, s in sorted(r.by_sector.items(), key=lambda x: -x[1].total_pnl_usd):
                print(f"  {sec:12s}: {s.total_trades:3d} trades | WR {s.win_rate:.0%} | ${s.total_pnl_usd:+,.2f}")
        if r.by_exit_reason:
            print("\n── Exit Reasons ─────────────────────────────────────")
            for reason, count in sorted(r.by_exit_reason.items(), key=lambda x: -x[1]):
                print(f"  {reason:20s}: {count}")
        print("─────────────────────────────────────────────────────\n")
