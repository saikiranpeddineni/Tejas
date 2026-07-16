"""
telegram_alerts.py — Telegram Notifications for Tejas (NSE)
============================================================
Telegram alerts for every trade event, timestamped in IST (NSE session tz).
"""
import logging
import threading
from datetime import datetime, timezone

import requests

from config import Config
from models import Portfolio, Signal, Trade

log = logging.getLogger(__name__)


def _send(msg: str, parse_mode: str = "HTML") -> bool:
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        log.debug("[Telegram] No token/chat configured")
        return False
    try:
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": parse_mode},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"[Telegram] Send failed: {e}")
        return False


def _async_send(msg: str) -> None:
    threading.Thread(target=_send, args=(msg,), daemon=True).start()


def ist_time() -> str:
    """Current time formatted for IST (UTC+5:30, the NSE session tz)."""
    from datetime import timedelta
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_tz).strftime("%H:%M IST")


def alert_system_start(bankroll: float, paper: bool) -> None:
    mode = "📋 PAPER TRADING" if paper else "🔴 LIVE TRADING"
    msg = (
        f"<b>🚀 StockBot Started | {mode}</b>\n"
        f"💵 Capital: ₹{bankroll:,.2f}\n"
        f"📊 Universe: {len(Config.WATCHLIST)} NSE stocks\n"
        f"🎯 Min edge: {Config.MIN_EDGE:.1%} | Min quality: {Config.MIN_ENTRY_QUALITY:.2f}\n"
        f"🛡️ Stop loss: {Config.STOP_LOSS_PCT:.1%} | Take profit: {Config.TAKE_PROFIT_PCT:.1%}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_offline_gap(minutes: float, symbols: list) -> None:
    """Bot was down during market hours with open positions — software stops
    could not fire in that window. Sent once by the restart that detects it."""
    msg = (
        f"<b>⚠️ Tejas was OFFLINE {minutes:.0f} min during market hours</b>\n"
        f"Open positions were UNMONITORED (no stop protection):\n"
        f"📦 {', '.join(symbols) if symbols else 'none'}\n"
        f"SL/TP re-armed — any gapped-through stop exits at the current price.\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_market_open() -> None:
    msg = (
        f"<b>🔔 NSE Market Open</b>\n"
        f"NSE equity: 09:15–15:30 IST\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_trade_entry(signal: Signal, trade: Trade) -> None:
    mode = "📋" if Config.PAPER_TRADING else "🔴"
    # Stops/targets are computed per-trade (ATR-based), so show the trade's actual
    # distances — not the fixed Config.STOP_LOSS_PCT/TAKE_PROFIT_PCT constants,
    # which generally don't match trade.stop_loss / trade.take_profit.
    ep = trade.entry_price or 0.0
    sl_pct = (ep - trade.stop_loss) / ep if ep > 0 else 0.0
    tp_pct = (trade.take_profit - ep) / ep if ep > 0 else 0.0
    msg = (
        f"<b>{mode} BUY | {trade.symbol}</b>\n"
        f"💵 Size: <b>₹{trade.size_usd:,.2f}</b> @ ₹{trade.entry_price:.2f}\n"
        f"🛡️ Stop: ₹{trade.stop_loss:.2f} ({sl_pct:.1%})\n"
        f"🎯 Target: ₹{trade.take_profit:.2f} ({tp_pct:.1%})\n"
        f"🤖 ML Prob: {signal.ml_prob:.1%} | Edge: {signal.edge:+.3f}\n"
        f"📊 RSI: {signal.features.rsi_14*100:.0f} | "
        f"Vol: {signal.features.volume_ratio:.1f}x avg\n"
        + (f"💡 Why: {trade.rationale}\n" if getattr(trade, "rationale", "") else "")
        + f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_partial_tp(trade: Trade) -> None:
    """★ Staged exit leg 1: half booked at first target, stop moved to breakeven."""
    gain = ((trade.trim_price or 0) - trade.entry_price) / max(trade.entry_price, 0.01)
    msg = (
        f"<b>✂️ TRIM | {trade.symbol}</b>\n"
        f"💰 Sold {trade.trim_shares:.0f}/{trade.shares:.0f} @ ₹{trade.trim_price:.2f} "
        f"({gain:+.2%})\n"
        f"🛡️ Stop moved to breakeven: ₹{trade.stop_loss:.2f}\n"
        f"🎯 Rest rides to ₹{trade.take_profit:.2f}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_trade_exit(trade: Trade) -> None:
    emoji = "✅ WIN" if trade.pnl_usd >= 0 else "❌ LOSS"
    dur = _duration(trade)
    msg = (
        f"<b>{emoji} | {trade.symbol}</b>\n"
        f"💰 PnL: <b>₹{trade.pnl_usd:+,.2f} ({trade.pnl_pct:+.2%})</b>\n"
        f"📌 Exit: ₹{trade.exit_price:.2f} | Reason: {trade.exit_reason}\n"
        f"⏱ Held: {dur}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_drawdown_warning(portfolio: Portfolio) -> None:
    msg = (
        f"⚠️ <b>DRAWDOWN WARNING</b>\n"
        f"📉 Current DD: <b>{portfolio.drawdown:.1%}</b>\n"
        f"💵 Capital: ₹{portfolio.bankroll:,.2f} (peak: ₹{portfolio.peak_bankroll:,.2f})\n"
        f"🛑 Halt at: {Config.MAX_DRAWDOWN_HALT:.0%}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_daily_summary(portfolio: Portfolio, ml) -> None:
    audit = ml.audit()
    msg = (
        f"<b>📊 Daily Summary</b>\n"
        f"💵 Capital: <b>₹{portfolio.bankroll:,.2f}</b>\n"
        f"📈 Peak: ₹{portfolio.peak_bankroll:,.2f} | DD: {portfolio.drawdown:.1%}\n"
        f"🔓 Open positions: {len(portfolio.open_trades)}\n"
        f"🤖 ML AUC: {audit.get('val_auc', 0):.3f} | Win Rate: {audit.get('win_rate', 0):.1%}\n"
        f"📚 Resolved trades: {audit['resolved']}/{audit['total']}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def alert_error(context: str, error: Exception) -> None:
    msg = (
        f"🔴 <b>ERROR</b>\n"
        f"📍 {context}\n"
        f"💬 {str(error)[:200]}\n"
        f"⏰ {ist_time()}"
    )
    _async_send(msg)


def _duration(trade: Trade) -> str:
    if trade.exit_time and trade.entry_time:
        delta = trade.exit_time - trade.entry_time
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m}m"
    return "—"
