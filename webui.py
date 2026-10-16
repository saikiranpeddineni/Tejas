"""
webui.py — Pro browser dashboard for Tejas.
===========================================
Stdlib only (no extra dependency). Launch with the "Start Tejas" shortcut or
`python webui.py`; it auto-opens http://127.0.0.1:8765.

  • Start / Stop / Quit the bot from the browser (rich toggles, dark mode).
  • Live status (polled): equity + run P&L, equity sparkline, regime/VIX,
    open positions with live P&L, and a reasoning log.
  • "Top Picks" runs the analyst over the NSE universe and shows ranked stocks.

Default settings are paper-safe (simulated broker, fake money, no API key).
"""
import base64
import hmac
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import socket

# NSE session calendar — single source of truth shared with the trading loop.
from brokers.base import IST, SESSION_OPEN, SESSION_CLOSE, NSE_HOLIDAYS

import logging
log = logging.getLogger("webui")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "tejas.log")
DB_PATH  = os.path.join(BASE_DIR, "tejas.db")
# The dashboard can start/stop the trader and save broker credentials, so it
# must not be reachable from the network by default.  Opt into a remote bind
# only together with TEJAS_UI_TOKEN (HTTP Basic auth; see README.md).
HOST = os.environ.get("TEJAS_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 8765
UI_TOKEN = os.environ.get("TEJAS_UI_TOKEN", "").strip()
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
if HOST not in _LOOPBACK_HOSTS and not UI_TOKEN:
    raise SystemExit(
        "Refusing to expose the Tejas dashboard without TEJAS_UI_TOKEN; "
        "use the default loopback bind or configure a token."
    )


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


_LAN_URL = None
def lan_url() -> str:
    global _LAN_URL
    if _LAN_URL is None:
        # A loopback listener cannot be reached from a phone.  Do not advertise
        # a misleading LAN URL in the status payload/QR dialog.
        host = "127.0.0.1" if HOST in _LOOPBACK_HOSTS else lan_ip()
        _LAN_URL = f"http://{host}:{PORT}"
    return _LAN_URL

_proc_lock = threading.Lock()
bot_proc: subprocess.Popen | None = None
bot_cfg: dict = {}

_CYCLE_RE = re.compile(r"Cycle (\d+) \| ₹([\d,]+\.\d+) dd=([\d.]+)% open=(\d+) regime=(\w+)")
_VIX_RE   = re.compile(r"VIX≈([\d.]+)")

_price_cache: dict = {}
cand: dict = {"computing": False, "updated": None, "items": [], "regime": None}
_eng: dict = {}

# ── Auto-start supervisor state ────────────────────────────────────────────────
# The dashboard stays up whenever the PC is on; the supervisor thread starts the
# bot only inside NSE market windows and stops it after close — so the heavy
# scan/ML work is gated to trading hours, not running 24/7.
AUTOSTART_PATH = os.path.join(BASE_DIR, "autostart.local.json")  # last-used Start opts
PAUSE_PATH     = os.path.join(BASE_DIR, "tejas.paused")          # set by manual Stop
PRE_OPEN_MINS  = 10   # spin up this many minutes before the bell
POST_CLOSE_MINS = 15  # keep alive this long after close so EOD exits settle
# When the watchdog launches us in the background it sets TEJAS_NO_BROWSER=1 so
# no tab ever pops to the foreground (safe during screen sharing).
_no_browser = os.environ.get("TEJAS_NO_BROWSER") == "1"


def in_trading_window(now: datetime | None = None) -> bool:
    """True on an NSE trading day from (open − PRE_OPEN_MINS) to (close + POST_CLOSE_MINS)."""
    now = now or datetime.now(IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    mins  = now.hour * 60 + now.minute
    start = SESSION_OPEN[0] * 60 + SESSION_OPEN[1] - PRE_OPEN_MINS
    end   = SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1] + POST_CLOSE_MINS
    return start <= mins < end


def is_paused() -> bool:
    """Paused only for the IST day it was set. A flag left over from a previous
    day is stale → treated as not paused (and cleaned up), so a Stop never
    blocks future days: the bot auto-resumes next trading morning by the clock."""
    try:
        with open(PAUSE_PATH, encoding="utf-8") as f:
            day = f.read().strip()[:10]   # YYYY-MM-DD
    except OSError:
        return False
    if day == datetime.now(IST).strftime("%Y-%m-%d"):
        return True
    try:
        os.remove(PAUSE_PATH)   # stale — clear it
    except OSError:
        pass
    return False


def set_paused(paused: bool) -> None:
    try:
        if paused:
            with open(PAUSE_PATH, "w", encoding="utf-8") as f:
                f.write(datetime.now(IST).strftime("%Y-%m-%d"))   # today only
        elif os.path.exists(PAUSE_PATH):
            os.remove(PAUSE_PATH)
    except OSError:
        pass


def load_autostart_opts() -> dict:
    """Last opts used from the UI, or paper-safe defaults if never started here."""
    try:
        with open(AUTOSTART_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"broker": "simulated", "dry_run": True, "bankroll": "100000", "scan": "30"}


def save_autostart_opts(opts: dict) -> None:
    keep = {k: opts.get(k) for k in ("broker", "dry_run", "ignore_hours", "bankroll", "scan")}
    try:
        with open(AUTOSTART_PATH, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=2)
    except OSError:
        pass


def _auto_supervisor() -> None:
    """Background: keep the bot running through NSE market windows and stopped
    outside them — unless the user manually paused it via the Stop button.
    The DASHBOARD itself stays up 24/7 (it's a tiny idle server and holds no
    keep-awake) so P&L and trade history are viewable after the close; only the
    BOT is lifecycle-managed."""
    import logging
    log = logging.getLogger("supervisor")
    while True:
        try:
            if not is_paused():
                in_win  = in_trading_window()
                running = is_running()
                if in_win and not running:
                    res = start_bot(load_autostart_opts(), source="auto")
                    if res.get("ok"):
                        log.info("[Auto] Market window open — bot started (pid=%s)", res.get("pid"))
                    else:
                        log.warning("[Auto] Could not auto-start bot: %s", res.get("error"))
                elif (not in_win) and running:
                    stop_bot(source="auto")
                    log.info("[Auto] Outside market window — bot stopped")
        except Exception as e:
            log.warning("[Auto] supervisor error: %s", e)
        time.sleep(45)

# ── Broker credentials ─────────────────────────────────────────────────────────
# Which env vars each broker needs (entered from the UI, stored locally, injected
# into the bot subprocess on Start). 'opt' fields are optional.
KEYS_PATH = os.path.join(BASE_DIR, "keys.local.json")
KEY_SPEC = {
    "simulated": [],
    "zerodha":  [("KITE_API_KEY", "API Key", False), ("KITE_ACCESS_TOKEN", "Access Token (regenerate daily)", False)],
    "upstox":   [("UPSTOX_ACCESS_TOKEN", "Access Token (daily)", False), ("UPSTOX_INSTRUMENTS", "Instrument map JSON/path", True)],
    "angelone": [("SMARTAPI_API_KEY", "API Key", False), ("SMARTAPI_JWT", "Session JWT (daily)", False), ("SMARTAPI_INSTRUMENTS", "Token map JSON/path", True)],
    "dhan":     [("DHAN_ACCESS_TOKEN", "Access Token", False), ("DHAN_CLIENT_ID", "Client ID", False), ("DHAN_INSTRUMENTS", "Security map JSON/path", True)],
    "fyers":    [("FYERS_APP_ID", "App ID", False), ("FYERS_ACCESS_TOKEN", "Access Token (daily)", False)],
}
_ALL_KEY_NAMES = frozenset(env for fields in KEY_SPEC.values() for env, _label, _optional in fields)
_keys_lock = threading.Lock()
_MAX_BODY_BYTES = 64 * 1024
_MAX_KEY_VALUE_BYTES = 4096


def load_keys() -> dict:
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        try:
            os.chmod(KEYS_PATH, 0o600)
        except OSError:
            pass
        if not isinstance(raw, dict):
            return {}
        # Ignore unknown entries from old/corrupt files.  Otherwise a value such
        # as PYTHONPATH or LD_PRELOAD would be copied into the trading child.
        clean = {}
        for key, value in raw.items():
            if key not in _ALL_KEY_NAMES or isinstance(value, bool):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            text = str(value)
            if text.strip() and "\x00" not in text and len(text) <= _MAX_KEY_VALUE_BYTES:
                clean[key] = text
        return clean
    except (OSError, ValueError, TypeError):
        return {}


def save_keys(values: dict, broker: str | None = None) -> None:
    allowed = {
        env for env, _label, _optional in KEY_SPEC.get(broker, [])
    } if broker else _ALL_KEY_NAMES
    clean = {}
    for key, value in values.items():
        if (key not in allowed or isinstance(value, bool)
                or not isinstance(value, (str, int, float))):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        text = str(value).strip()
        if text and len(text) <= _MAX_KEY_VALUE_BYTES and "\x00" not in text:
            clean[key] = text
    if not clean:
        return
    with _keys_lock:
        keys = load_keys()
        keys.update(clean)
        tmp_path = f"{KEYS_PATH}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(keys, f, indent=2)
                f.write("\n")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, KEYS_PATH)
            try:
                os.chmod(KEYS_PATH, 0o600)
            except OSError:
                pass
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


# ── bot process control ──────────────────────────────────────────────────────
def start_bot(opts: dict, source: str = "ui") -> dict:
    global bot_proc, bot_cfg
    if not isinstance(opts, dict):
        return {"ok": False, "error": "start options must be a JSON object"}

    def _flag(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    broker = str(opts.get("broker", "simulated") or "simulated").strip().lower()
    if broker not in KEY_SPEC:
        return {"ok": False, "error": f"unsupported broker: {broker}"}
    dry_run = _flag(opts.get("dry_run"))
    ignore_hours = _flag(opts.get("ignore_hours"))
    # Auto-start is never allowed to arm live orders from persisted UI state.
    # Apply that downgrade before credential validation so a saved live-broker
    # selection can still run safely in dry-run mode when credentials are absent.
    if (source == "auto" and broker != "simulated" and not dry_run
            and not _flag(opts.get("auto_live"))):
        dry_run = True

    with _proc_lock:
        if bot_proc and bot_proc.poll() is None:
            return {"ok": False, "error": "already running"}
        if _bot_lock_busy():
            return {"ok": False, "error": "A Tejas bot is already running (another window or device). Stop it there first."}
        # Safety: don't allow a LIVE broker to start without its credentials —
        # otherwise the UI shows a misleading "LIVE" state that can't trade.
        if broker != "simulated" and not dry_run:
            keys = load_keys()
            missing = [e for e, l, o in KEY_SPEC.get(broker, []) if not o and not keys.get(e)]
            if missing:
                return {"ok": False, "error":
                        f"Can't go LIVE on {broker} — add API keys first "
                        f"({', '.join(missing)}). Click 🔑 API Keys, or enable Dry-run."}
        env = dict(os.environ)
        # The UI token authenticates this dashboard only; do not copy it into the
        # trading child process environment unnecessarily.
        env.pop("TEJAS_UI_TOKEN", None)
        env["BROKER"]                = broker
        env["DRY_RUN"]               = "true" if dry_run else "false"
        env["IGNORE_MARKET_HOURS"]   = "true" if ignore_hours else "false"
        # ★ VALIDATE numerics before injecting: the child parses these with
        #   float()/int() at import time with stderr discarded, so one bad value
        #   from the (unauthenticated, LAN-reachable) API or a corrupt
        #   autostart.local.json crash-loops every auto-start with zero trace.
        #   (A persisted `"scan": null` did exactly this once — int("None").)
        def _num(v, cast, default, lo, hi):
            try:
                x = cast(v)
                return str(x) if lo <= x <= hi else str(default)
            except (TypeError, ValueError):
                return str(default)
        env["BANKROLL"]              = _num(opts.get("bankroll"), float, 100000, 1000, 1e10)
        env["SCAN_INTERVAL_SECONDS"] = _num(opts.get("scan"), int, 30, 5, 3600)
        env.setdefault("ALPACA_API_KEY", "")
        # Inject only the selected adapter's credentials. Passing every saved
        # broker token to each child needlessly widens the blast radius of a
        # process compromise and can confuse third-party SDK auto-discovery.
        allowed_keys = {env_name for env_name, _label, _optional in KEY_SPEC[broker]}
        for k, v in load_keys().items():
            if k in allowed_keys and v:
                env[k] = str(v)
        startup_log_path = os.path.join(BASE_DIR, "tejas.startup.log")
        try:
            # Import-time failures happen before tejas.py can configure its
            # rotating logger.  Keep stderr only (the normal stdout stream is
            # discarded so this file cannot duplicate the full trading log) and
            # cap the diagnostic trail used by crash-loop recovery.
            if os.path.exists(startup_log_path) and os.path.getsize(startup_log_path) > 1024 * 1024:
                with open(startup_log_path, "wb"):
                    pass
            startup_log = open(startup_log_path, "ab")
            try:
                bot_proc = subprocess.Popen(
                    [sys.executable, "-u", os.path.join(BASE_DIR, "tejas.py")],
                    cwd=BASE_DIR, env=env,
                    stdout=subprocess.DEVNULL, stderr=startup_log,
                )
            finally:
                startup_log.close()
        except OSError:
            log.error("[Start] Could not launch Tejas child", exc_info=True)
            bot_proc = None
            return {"ok": False, "error": "could not launch Tejas"}
        bot_cfg = {k: env[k] for k in
                   ("BROKER", "DRY_RUN", "IGNORE_MARKET_HOURS", "BANKROLL")}
        if source == "ui":
            # Remember these for the auto-supervisor, and clear any manual pause.
            save_autostart_opts(opts)
            set_paused(False)
        return {"ok": True, "pid": bot_proc.pid}


def _request_graceful_stop() -> bool:
    """Ask the bot to finish its cycle and exit cleanly by sending TEJAS_STOP to
    its single-instance lock socket. Works even for a bot we didn't spawn."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", 8766))
        s.sendall(b"TEJAS_STOP")
        return True
    except OSError:
        return False
    finally:
        s.close()


def _finish_stop(proc, requested: bool) -> None:
    """Background: give the graceful stop a short window, then hard-kill.
    Runs OFF the request thread and WITHOUT _proc_lock — stop_bot used to hold
    both for up to 40s, which froze the Stop/Quit response AND blocked Start
    (same lock). The bot's sleeps are interruptible now, so a graceful exit
    normally lands within a second or two."""
    if proc is not None:
        try:
            proc.wait(timeout=15 if requested else 0.1)
            return
        except subprocess.TimeoutExpired:
            # ★ Escalate via a SECOND stop request first: the bot force-exits on
            # a repeat STOP even when wedged mid-cycle (slow feed crawl), which
            # still beats TerminateProcess landing mid-DB-write.
            log.warning("[Stop] Graceful window expired — escalating")
            _request_graceful_stop()
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
    else:
        # External bot (started outside this dashboard): watch the lock only.
        deadline = time.time() + (15 if requested else 0)
        while time.time() < deadline and _bot_lock_busy():
            time.sleep(1)
        if _bot_lock_busy():
            _request_graceful_stop()   # repeat STOP → bot force-exits itself


def stop_bot(source: str = "ui") -> dict:
    global bot_proc
    with _proc_lock:   # held only for the quick state checks — never for waits
        if source == "ui":
            # Manual stop = stay down for the rest of today (don't let the
            # auto-supervisor relaunch). Auto-resumes next trading morning.
            set_paused(True)
        ours = bot_proc is not None and bot_proc.poll() is None
        if not ours and not _bot_lock_busy():
            return {"ok": False, "error": "not running"}
        # ★ Graceful first: on Windows terminate() is TerminateProcess (hard
        # kill) — it can land mid-DB-write inside close_trade and leave phantom
        # open rows that get restored (and re-sold) the next morning. The STOP
        # message lets the bot finish its cycle and run its shutdown path.
        requested = _request_graceful_stop()
        proc = bot_proc if ours else None
    threading.Thread(target=_finish_stop, args=(proc, requested), daemon=True).start()
    return {"ok": True, "stopping": True}


def _bot_lock_busy() -> bool:
    """True if a Tejas bot process holds its single-instance lock (port 8766)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", 8766))
        return True
    except OSError:
        return False
    finally:
        s.close()


def is_running() -> bool:
    # Truthful across the whole machine: our child OR any Tejas bot holding the lock.
    return bool(bot_proc and bot_proc.poll() is None) or _bot_lock_busy()


# ── live price (stdlib urllib, cached) ────────────────────────────────────────
def live_price(symbol: str) -> float | None:
    hit = _price_cache.get(symbol)
    if hit and time.time() - hit[1] < 12:
        return hit[0]
    ysym = {"NIFTY": "^NSEI"}.get(symbol, f"{symbol}.NS")
    try:
        req = urllib.request.Request(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range=1d&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        px = float(d["chart"]["result"][0]["meta"].get("regularMarketPrice") or 0)
        if px > 0:
            _price_cache[symbol] = (px, time.time())
            return px
    except Exception:
        pass
    return None


def cached_price(symbol: str) -> float | None:
    """Non-blocking read of the price cache (no network) — keeps status() fast."""
    hit = _price_cache.get(symbol)
    return hit[0] if hit else None


def _price_refresher():
    """Background: keep prices for open positions warm so status() never blocks."""
    while True:
        try:
            c = sqlite3.connect(DB_PATH, timeout=5)
            syms = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM trades WHERE status='open'").fetchall()]
            c.close()
            for s in syms:
                live_price(s)   # fills _price_cache via network
        except Exception:
            pass
        time.sleep(8)


# ── status snapshot ───────────────────────────────────────────────────────────
def tail_log(n: int = 150) -> list[str]:
    """Last n lines WITHOUT reading the whole file — tejas.log grows to many MB
    and /api/status polls every 3s from every open tab; readlines() on the full
    file made every status (and by contention start/stop) request crawl."""
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 131072))   # last 128 KB ≫ 150 lines
            data = f.read().decode("utf-8", errors="replace")
        return [l.rstrip() for l in data.splitlines()[-n:]]
    except FileNotFoundError:
        return []


def trade_history(limit: int = 50) -> list[dict]:
    """Closed trades with exit reason + entry rationale — the after-close P&L
    review view (readable any time; the dashboard runs 24/7)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT symbol, entry_price, exit_price, pnl_usd, pnl_pct, exit_reason, "
            "rationale, entry_time, exit_time, trim_shares, trim_price "
            "FROM trades WHERE status='closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        c.close()
    except Exception:
        return []
    return [{
        "symbol": r["symbol"],
        "entry_price": r["entry_price"], "exit_price": r["exit_price"],
        "pnl_usd": r["pnl_usd"], "pnl_pct": r["pnl_pct"],
        "exit_reason": r["exit_reason"] or "",
        "rationale": r["rationale"] or "",
        "trimmed": bool(r["trim_shares"] and r["trim_shares"] > 0),
        "entry_time": r["entry_time"], "exit_time": r["exit_time"],
    } for r in rows]


def open_positions() -> list[dict]:
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT symbol, size_usd, shares, entry_price, stop_loss, take_profit, entry_time "
            "FROM trades WHERE status='open' ORDER BY entry_time DESC"
        ).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for r in rows:
        p = dict(r)
        cur = cached_price(p["symbol"])   # cache-only — never blocks the poll
        p["current_price"] = cur
        p["pnl_pct"] = ((cur - p["entry_price"]) / p["entry_price"] * 100) if cur and p["entry_price"] else None
        out.append(p)
    return out


def status() -> dict:
    log = tail_log(150)
    equity = regime = vix = cycle = dd = open_n = None
    for line in reversed(log):
        m = _CYCLE_RE.search(line)
        if m:
            cycle  = int(m.group(1)); equity = float(m.group(2).replace(",", ""))
            dd = float(m.group(3)); open_n = int(m.group(4)); regime = m.group(5)
            break
    for line in reversed(log):
        v = _VIX_RE.search(line)
        if v:
            vix = float(v.group(1)); break
    start = float(bot_cfg.get("BANKROLL", 100000) or 100000)
    run_pnl = (equity - start) if equity is not None else None
    run_pct = (run_pnl / start * 100) if run_pnl is not None and start else None
    feed = [l for l in log if any(t in l for t in
            ("[Analyst]", "[Decision]", "[Risk]", "ENTRY", "EXIT", "⤫",
             "[Regime]", "Scan:", "DRAWDOWN"))][-32:]
    from config import Config
    return {
        "running": is_running(), "config": bot_cfg,
        "equity": equity, "start": start, "run_pnl": run_pnl, "run_pct": run_pct,
        "drawdown": dd, "cycle": cycle, "regime": regime, "vix": vix,
        "open_count": open_n, "max_positions": Config.MAX_OPEN_POSITIONS,
        "positions": open_positions(), "log": feed,
        "lan_url": lan_url(),
    }


# ── live analyst preview ("Top Picks") ─────────────────────────────────────────
def compute_candidates():
    cand["computing"] = True
    try:
        os.environ.setdefault("BROKER", "simulated")
        os.environ.setdefault("ALPACA_API_KEY", "")
        if not _eng:
            import db; db.init_db()
            from brokers.simulated import SimulatedBroker
            from ml_engine_gold import MLEngineGold
            from feature_builder_gold import FeatureBuilderGold
            from earnings_filter import EarningsFilter
            from sentiment_engine import SentimentEngine
            from signal_engine_gold import SignalEngineGold
            from regime_engine import RegimeEngine
            b = SimulatedBroker()
            _eng["sig"] = SignalEngineGold(b, MLEngineGold(), FeatureBuilderGold(b),
                                           EarningsFilter(), SentimentEngine())
            _eng["regime"] = RegimeEngine(b)
        reg = _eng["regime"].detect()
        sigs = _eng["sig"].scan(regime=reg)
        items = [{
            "symbol": s.symbol, "score": round(s.analyst_score, 3),
            "edge": round(s.edge, 4), "conf": round(s.ml_conf, 2),
            "price": round(s.entry_price, 2), "thesis": s.thesis,
            "factors": s.factors,
        } for s in sigs[:15]]
        cand["items"] = items
        cand["regime"] = {"regime": reg["regime"], "vix": round(reg["vix_level"], 1)}
        cand["updated"] = time.strftime("%H:%M:%S")
    except Exception as e:
        cand["error"] = str(e)
    finally:
        cand["computing"] = False


# ── forecast model training ─────────────────────────────────────────────────
MODEL_INFO = os.path.join(BASE_DIR, "model.local.json")
train_state = {"running": False, "msg": ""}


def model_info() -> dict:
    info = {}
    try:
        with open(MODEL_INFO, encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        pass
    # The dashboard's Train action builds the forward forecaster, which is
    # stored separately from the live-outcome model. Checking only lgb_gold.txt
    # made a successful forecast training run look like it never happened.
    info["exists"] = any(
        os.path.exists(os.path.join(BASE_DIR, "models", filename))
        for filename in ("lgb_forecast.txt", "lgb_gold.txt")
    )
    return info


def _do_train():
    train_state.update(running=True, msg="Building dataset & training… (several minutes)")
    try:
        os.environ.setdefault("BROKER", "simulated")
        os.environ.setdefault("ALPACA_API_KEY", "")
        import db; db.init_db()
        from brokers.simulated import SimulatedBroker
        from ml_engine_gold import MLEngineGold
        from feature_builder_gold import FeatureBuilderGold
        from forecast_trainer import train_forecast_model
        b = SimulatedBroker(); ml = MLEngineGold(); bld = FeatureBuilderGold(b)
        res = train_forecast_model(b, bld, ml, max_symbols=150)
        res["trained_at"] = time.strftime("%Y-%m-%d %H:%M")
        try:
            with open(MODEL_INFO, "w", encoding="utf-8") as f:
                json.dump(res, f)
        except Exception:
            pass
        train_state["msg"] = (f"Done — val AUC {res.get('val_auc')}, {res.get('n')} samples. "
                              f"(Re)start the bot to use it.") if res.get("status") == "ok" \
                              else f"Training: {res.get('status')}"
    except Exception as e:
        train_state["msg"] = f"Training failed: {e}"
    finally:
        train_state["running"] = False


# ── HTTP request helpers ────────────────────────────────────────────────────────
def _json_body(handler: BaseHTTPRequestHandler):
    """Read a bounded JSON object from a mutating API request."""
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return None, 400, "invalid Content-Length"
    if length < 0 or length > _MAX_BODY_BYTES:
        return None, 413, "request body too large"
    try:
        raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 400, "invalid JSON"
    if not isinstance(value, dict):
        return None, 400, "request body must be a JSON object"
    return value, None, None


# ── HTTP handler ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        if not UI_TOKEN:
            return True
        expected = "Basic " + base64.b64encode(
            f"tejas:{UI_TOKEN}".encode("utf-8")
        ).decode("ascii")
        supplied = self.headers.get("Authorization", "")
        if hmac.compare_digest(supplied, expected):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Tejas dashboard"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _mutation_allowed(self) -> bool:
        # A custom header makes cross-origin form/fetch requests fail the
        # browser's simple-request path (and triggers preflight for fetch).
        return self.headers.get("X-Tejas-Request") == "1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, json.dumps(status()))
        elif path == "/api/candidates":
            self._send(200, json.dumps(cand))
        elif path == "/api/history":
            self._send(200, json.dumps(trade_history()))
        elif path == "/api/train":
            self._send(200, json.dumps({"running": train_state["running"],
                                        "msg": train_state["msg"], "info": model_info()}))
        elif path == "/api/keys":
            query = parse_qs(urlparse(self.path).query)
            broker = (query.get("broker", ["simulated"])[0] or "simulated").strip().lower()
            saved = load_keys()
            fields = [{"env": env, "label": lbl, "opt": opt, "set": bool(saved.get(env))}
                      for env, lbl, opt in KEY_SPEC.get(broker, [])]
            self._send(200, json.dumps({"broker": broker, "fields": fields}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if not self._authorized():
            return
        if not self._mutation_allowed():
            self._send(403, json.dumps({"ok": False, "error": "missing request header"}))
            return
        path = urlparse(self.path).path
        if path == "/api/start":
            opts, status_code, error = _json_body(self)
            if error:
                self._send(status_code, json.dumps({"ok": False, "error": error}))
                return
            self._send(200, json.dumps(start_bot(opts)))
        elif path == "/api/stop":
            self._send(200, json.dumps(stop_bot()))
        elif path == "/api/keys":
            body, status_code, error = _json_body(self)
            if error:
                self._send(status_code, json.dumps({"ok": False, "error": error}))
                return
            broker = str(body.get("broker", "") or "").strip().lower()
            values = body.get("values")
            if broker not in KEY_SPEC:
                self._send(400, json.dumps({"ok": False, "error": "unsupported broker"}))
                return
            if not isinstance(values, dict):
                self._send(400, json.dumps({"ok": False, "error": "values must be an object"}))
                return
            allowed = {env for env, _label, _optional in KEY_SPEC[broker]}
            unknown = sorted(set(values) - allowed)
            if unknown:
                self._send(400, json.dumps({"ok": False, "error": "unknown credential field"}))
                return
            # only overwrite fields the user actually filled in (non-empty)
            save_keys({k: v for k, v in values.items() if str(v).strip()}, broker=broker)
            self._send(200, json.dumps({"ok": True}))
        elif path == "/api/candidates/refresh":
            if not cand["computing"]:
                threading.Thread(target=compute_candidates, daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        elif path == "/api/train":
            if not train_state["running"]:
                threading.Thread(target=_do_train, daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        elif path == "/api/reset":
            if is_running():
                self._send(200, json.dumps({"ok": False, "error": "Stop the bot before resetting."}))
            else:
                try:
                    c = sqlite3.connect(DB_PATH, timeout=5)
                    for t in ("trades", "training_data", "portfolio_snapshots"):
                        try: c.execute(f"DELETE FROM {t}")
                        except Exception: pass
                    c.commit(); c.close()
                    self._send(200, json.dumps({"ok": True}))
                except Exception as e:
                    self._send(200, json.dumps({"ok": False, "error": str(e)}))
        elif path == "/api/quit":
            # Quit shuts the dashboard down for now, but must NOT sticky-pause the
            # bot — the watchdog relaunches the dashboard and it should resume
            # auto-trading on schedule. Only the Stop button pauses (source="ui").
            stop_bot(source="auto")
            self._send(200, json.dumps({"ok": True}))

            def _quit():
                # Give the bot's graceful exit a moment (its sleeps are
                # interruptible, so this normally clears in ~1s), then go.
                deadline = time.time() + 8
                while time.time() < deadline and _bot_lock_busy():
                    time.sleep(0.5)
                os._exit(0)
            threading.Thread(target=_quit, daemon=True).start()
        else:
            self._send(404, "{}")


PAGE = r"""<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tejas · NSE Trader</title>
<style>
 html[data-theme=light]{
   --bg:#eef1f7;--card:#ffffff;--bd:#e6eaf2;--fg:#0f1b2d;--mut:#6b7a90;--soft:#f2f5fb;
   --grn:#12a150;--red:#e5484d;--acc:#3b6fff;--accsoft:#eaf0ff;--shadow:0 1px 2px #1a2b470a,0 10px 30px #1a2b470d}
 html[data-theme=dark]{
   --bg:#0b0f17;--card:#141b27;--bd:#222d3d;--fg:#e8eef6;--mut:#8a98ad;--soft:#1b2433;
   --grn:#2ecc71;--red:#ff5d5d;--acc:#5b8cff;--accsoft:#1b2740;--shadow:0 1px 2px #0006,0 12px 30px #0004}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.55 -apple-system,Segoe UI,Roboto,system-ui,sans-serif;background:var(--bg);
   color:var(--fg);-webkit-font-smoothing:antialiased;transition:background .25s,color .25s}
 header{display:flex;align-items:center;gap:12px;padding:12px 22px;background:var(--card);
   border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:9}
 .logo{font-size:18px;font-weight:800;letter-spacing:-.3px}.logo b{color:var(--acc)}
 .chip{font-size:10px;font-weight:800;letter-spacing:1px;color:var(--mut);
   border:1px solid var(--bd);border-radius:6px;padding:2px 6px;background:var(--soft)}
 .pill{padding:4px 11px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.3px}
 .on{background:#e7f8ee;color:#0d8a44}.off{background:#fdecec;color:#d23}
 html[data-theme=dark] .on{background:#10301f;color:#37d07f}html[data-theme=dark] .off{background:#3a1414;color:#ff6b6b}
 .mode{padding:4px 10px;border-radius:8px;font-size:11px;font-weight:800;background:var(--accsoft);color:var(--acc)}
 .mode.live{background:#fdecec;color:#d23}
 .spacer{margin-left:auto}.muted{color:var(--mut);font-size:13px}
 .iconbtn{background:var(--soft);border:1px solid var(--bd);border-radius:9px;width:38px;height:34px;
   font-size:15px;cursor:pointer;color:var(--fg)}
 .dot{width:9px;height:9px;border-radius:50%;background:#c5ccd6;display:inline-block}
 .dot.live{background:var(--grn);animation:pulse 1.4s infinite}
 @keyframes pulse{0%{box-shadow:0 0 0 0 #12a15066}70%{box-shadow:0 0 0 7px #12a15000}100%{box-shadow:0 0 0 0 #12a15000}}
 main{padding:22px;max-width:1200px;margin:0 auto}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:16px;box-shadow:var(--shadow)}
 .controls{display:flex;align-items:center;gap:20px;flex-wrap:wrap;padding:16px 18px;margin-bottom:8px}
 .fld label{display:block;font-size:10px;color:var(--mut);font-weight:700;letter-spacing:.6px;
   text-transform:uppercase;margin-bottom:5px}
 select,input{background:var(--card);border:1px solid var(--bd);color:var(--fg);border-radius:10px;
   padding:9px 12px;font-size:14px;font-weight:600}
 select:focus,input:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px var(--accsoft)}
 .toggle{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:600;user-select:none}
 .switch{position:relative;width:40px;height:23px;flex:0 0 auto}
 .switch input{opacity:0;width:0;height:0}
 .slider{position:absolute;inset:0;background:#cdd5e2;border-radius:999px;transition:.2s;cursor:pointer}
 html[data-theme=dark] .slider{background:#33405460}
 .slider:before{content:"";position:absolute;height:17px;width:17px;left:3px;top:3px;background:#fff;
   border-radius:50%;transition:.2s;box-shadow:0 1px 3px #0004}
 .switch input:checked+.slider{background:var(--acc)}
 .switch input:checked+.slider:before{transform:translateX(17px)}
 button{border:0;border-radius:10px;padding:10px 18px;font-size:14px;font-weight:700;cursor:pointer;transition:.15s}
 button:hover:not(:disabled){transform:translateY(-1px)}
 .start{background:var(--grn);color:#fff;box-shadow:0 3px 10px #12a15040}
 .stop{background:var(--red);color:#fff}.ghost{background:var(--soft);color:var(--fg);border:1px solid var(--bd)}
 button:disabled{opacity:.4;cursor:not-allowed}
 .grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:14px;margin:14px 0}
 .stat{padding:16px 18px;min-height:128px;display:flex;flex-direction:column}
 .stat .k{font-size:10px;color:var(--mut);font-weight:800;letter-spacing:.7px;text-transform:uppercase}
 .stat .v{font-size:27px;font-weight:800;letter-spacing:-.6px;margin-top:4px}
 .stat .s{font-size:13px;font-weight:700;margin-top:auto}
 .pos{color:var(--grn)}.neg{color:var(--red)}
 canvas{width:100%;height:42px;display:block;margin-top:8px}
 .cols{display:grid;grid-template-columns:1.15fr 1fr;gap:14px}
 .sec{padding:16px 18px}
 h2{font-size:15px;font-weight:800;margin:0 0 12px;display:flex;align-items:center;gap:8px;letter-spacing:-.2px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--bd)}
 tr:last-child td{border-bottom:0}
 th{color:var(--mut);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
 tbody tr:hover{background:var(--soft)}td.num{text-align:right;font-variant-numeric:tabular-nums}
 .bar{height:8px;border-radius:5px;background:var(--soft);overflow:hidden;min-width:70px}
 .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--grn))}
 .tag{font-size:11px;color:var(--mut)}.rank{color:var(--mut);font-weight:700}
 .vd{font-size:10px;font-weight:800;padding:1px 6px;border-radius:5px;background:var(--accsoft);color:var(--acc)}
 #log{background:var(--soft);border:1px solid var(--bd);border-radius:12px;padding:13px;height:240px;
   overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;color:var(--mut)}
 [title]{cursor:help}.note{font-size:12px;color:var(--mut);padding:0 18px 14px}
 .overlay{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;z-index:50}
 .overlay.show{display:flex}
 .modal{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:22px;width:460px;max-width:92vw;box-shadow:var(--shadow)}
 .modal h3{margin:0 0 4px;font-size:16px}
 .kf{margin:12px 0}.kf label{display:block;font-size:11px;color:var(--mut);margin-bottom:4px;font-weight:700}
 .kf input{width:100%}.set{font-size:10px;color:var(--grn);font-weight:800;margin-left:6px}
 .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
 .warn{font-size:11px;color:var(--mut);background:var(--soft);border-radius:8px;padding:9px 11px;margin-top:8px}
 @media(max-width:880px){.grid,.cols{grid-template-columns:1fr}}
</style></head><body>
<header>
 <span class="logo"><b>Tejas</b></span><span class="chip">NSE</span>
 <span id="badge" class="pill off">stopped</span>
 <span id="modepill" class="mode" style="display:none"></span>
 <span class="muted" id="cfg"></span>
 <span class="spacer"></span>
 <span class="dot" id="dot" title="Live — refreshes every 3s"></span>
 <span class="muted" id="clock"></span>
 <button class="iconbtn" id="qrBtn" onclick="openQR()" title="Open this dashboard on your phone (scan a QR code)">📱</button>
 <button class="iconbtn" id="themeBtn" title="Toggle dark / light mode">🌙</button>
</header>
<main>
 <div class="panel controls">
  <div class="fld" title="simulated = paper (fake money, real NSE data, no API key). Others place REAL orders and need that broker's API key.">
   <label>Broker</label>
   <select id="broker">
    <option value="simulated">Paper (simulated)</option>
    <option value="zerodha">Zerodha</option><option value="upstox">Upstox</option>
    <option value="angelone">Angel One</option><option value="dhan">Dhan</option><option value="fyers">Fyers</option>
   </select></div>
  <div class="fld" title="Starting capital in ₹. Fake money in paper mode."><label>Capital ₹</label>
   <input id="bankroll" type="number" value="100000" style="width:120px"></div>
  <button class="ghost" onclick="openKeys()" style="align-self:end"
    title="Enter & save this broker's API credentials (needed only for live trading; stored locally on this machine).">🔑 API Keys</button>
  <button class="ghost" onclick="train()" style="align-self:end"
    title="Train the forward-return forecast model on historical NSE data (a few minutes). (Re)start the bot afterward to use it.">🧠 Train</button>
  <label class="toggle" title="ON = log-only: prints the trades it WOULD make but fills nothing, so equity stays flat. OFF = paper-trade with simulated fills (P&L moves).">
   <span class="switch"><input id="dry" type="checkbox"><span class="slider"></span></span> Dry-run (log only)</label>
  <label class="toggle" title="ON = ignore NSE hours (09:15–15:30 IST) so it runs on the latest data even on weekends — for testing. OFF for live use.">
   <span class="switch"><input id="ign" type="checkbox" checked><span class="slider"></span></span> Test mode · ignore hours</label>
  <span class="spacer"></span>
  <button class="start" id="startBtn" onclick="start()">▶ Start</button>
  <button class="stop"  id="stopBtn"  onclick="stop()" disabled>■ Stop</button>
  <button class="ghost" onclick="quit()" title="Stop the bot and shut down this dashboard.">⏻ Quit</button>
 </div>
 <div class="note">Data: real NSE prices via Yahoo Finance (free tier, intraday ~15&nbsp;min delayed). Paper mode = fake money on real data.
   &nbsp;·&nbsp; <span id="modelStat">🧠 forecast model: …</span></div>

 <div class="grid">
  <div class="panel stat" title="Total account value = cash + current value of holdings.">
   <div class="k">Equity</div><div class="v" id="equity">—</div>
   <div class="s" id="pnl">—</div><canvas id="spark"></canvas></div>
  <div class="panel stat" title="Market state from NIFTY trend + India VIX. It scales position sizes.">
   <div class="k">Market Regime</div><div class="v" id="regime" style="font-size:21px">—</div>
   <div class="s muted" id="vix">India VIX —</div></div>
  <div class="panel stat" title="Open positions currently held & managed.">
   <div class="k">Open Positions</div><div class="v" id="openc">—</div>
   <div class="s muted" id="openmax">of — max</div></div>
  <div class="panel stat" title="Scan cycles since start. dd = drawdown from peak.">
   <div class="k">Scan Cycle</div><div class="v" id="cycle">—</div>
   <div class="s muted" id="dd">drawdown —</div></div>
 </div>

 <div class="cols">
  <div class="panel sec">
   <h2 title="The analyst ranks the whole NSE universe now (trend, momentum, relative strength, volume, risk). Auto-refreshes ~90s while running.">
     🎯 Top Picks <span class="muted" style="font-weight:500">— live analyst view</span>
     <button class="ghost" id="refBtn" onclick="refresh()" style="margin-left:auto;padding:6px 13px">↻ Scan</button></h2>
   <table><thead><tr><th>#</th><th>Stock</th>
     <th title="Strength of the factor case (longer = stronger).">Conviction</th>
     <th class="num" title="Modelled probability edge over a coin flip.">Edge</th><th>Thesis</th></tr></thead>
   <tbody id="cands"><tr><td colspan="5" class="muted">Scanning…</td></tr></tbody></table>
   <div class="muted" id="candmeta" style="margin-top:8px;font-size:12px"></div>
  </div>
  <div class="panel sec">
   <h2>📌 Open Positions
     <button class="ghost" onclick="resetData()" style="margin-left:auto;padding:5px 11px;font-size:12px"
       title="Clear all recorded paper trades from the local DB (stop the bot first).">Reset</button></h2>
   <table><thead><tr><th>Stock</th><th class="num">Size ₹</th><th class="num">Entry</th>
     <th>In</th><th class="num">LTP</th><th class="num">P&L</th></tr></thead>
   <tbody id="postable"><tr><td colspan="6" class="muted">none</td></tr></tbody></table>
  </div>
 </div>

 <div class="panel sec" style="margin-top:14px">
  <h2>📜 Trade History <span class="muted" style="font-weight:400;font-size:12px">— hover a row for the entry rationale</span></h2>
  <table><thead><tr><th>Stock</th><th class="num">Entry</th><th>In</th><th class="num">Exit</th>
    <th>Out</th><th class="num">P&amp;L</th><th>Reason</th></tr></thead>
  <tbody id="histtable"><tr><td colspan="7" class="muted">none yet</td></tr></tbody></table>
 </div>

 <div class="panel sec" style="margin-top:14px"><h2>🧠 Reasoning &amp; Activity</h2><div id="log" class="muted">waiting…</div></div>
</main>
<div class="overlay" id="keysModal">
 <div class="modal">
  <h3>🔑 API Keys — <span id="keysBroker"></span></h3>
  <div class="muted" style="font-size:12px">Needed only for live trading. Stored locally in <code>keys.local.json</code> on this machine and injected into the bot when you Start.</div>
  <div id="keysFields"></div>
  <div class="warn">⚠ Most Indian brokers' access tokens expire daily — re-enter the token before market open. Keys are saved in plain text on this PC only and never leave it.</div>
  <div class="actions"><button class="ghost" onclick="closeKeys()">Cancel</button><button class="start" onclick="saveKeys()">Save</button></div>
 </div>
</div>
<div class="overlay" id="qrModal">
 <div class="modal" style="text-align:center;width:340px">
  <h3>📱 Open on another device</h3>
  <div class="muted" style="font-size:12px">Scan with your phone's camera — it must be on the <b>same Wi-Fi</b>.</div>
  <img id="qrImg" alt="QR code" style="width:240px;height:240px;background:#fff;border-radius:12px;margin:14px auto;display:block;padding:8px"
    onerror="this.style.display='none'">
  <div class="muted" style="font-size:12px">…or type this URL into the phone's browser:</div>
  <div id="qrUrl" style="font-weight:800;word-break:break-all;margin:4px 0;font-size:15px"></div>
  <div class="warn">If the phone can't connect, allow Python through Windows Firewall (Private networks).</div>
  <div class="actions"><button class="ghost" onclick="closeQR()">Close</button></div>
 </div>
</div>
<script>
const hist=[];let running=false;let lanUrl='';
const $=id=>document.getElementById(id);
const POST_HEADERS={'Content-Type':'application/json','X-Tejas-Request':'1'};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function openQR(){const u=lanUrl||location.origin;$('qrUrl').textContent=u;
 // Keep the dashboard URL local. Sending it to a third-party QR service would
 // disclose the LAN address (and any future access-token-bearing URL).
 const img=$('qrImg');img.style.display='none';
 $('qrModal').classList.add('show');}
function closeQR(){$('qrModal').classList.remove('show');}
async function train(){if(!confirm('Train the forecast model on historical NSE data?\nTakes several minutes. (Re)start the bot afterward to use the new model.'))return;
 await fetch('/api/train',{method:'POST',headers:POST_HEADERS});setTimeout(pollTrain,1000);}
async function pollTrain(){let t;try{t=await(await fetch('/api/train')).json();}catch(e){return;}
 const el=$('modelStat');
 const i=t.info||{};
 if(t.running){el.textContent='🧠 training forecast model… (several minutes)';setTimeout(pollTrain,4000);}
 else if(i.val_auc!=null){el.textContent=`🧠 forecast model: trained · val-AUC ${i.val_auc} · ${i.trained_at||''}`;}
 else if(i.exists){el.textContent='🧠 forecast model: trained (blending with analyst)';}
 else{el.textContent='🧠 forecast model: not trained — click 🧠 Train';}
 if(t.msg&&!t.running&&t._shown!==1){/* keep msg available */}
}
const BNAME={simulated:'Paper (simulated)',zerodha:'Zerodha',upstox:'Upstox',angelone:'Angel One',dhan:'Dhan',fyers:'Fyers'};
function money(n){return '₹'+Math.round(n).toLocaleString('en-IN');}
function pretty(r){if(!r)return '—';let t=r.replace('_low_vol',' Low-Vol').replace('_high_vol',' High-Vol').replace('_fear',' Fear');
 return t.split(/[ _]/).map(w=>w?w[0].toUpperCase()+w.slice(1):w).join(' ');}
// theme
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.setItem('tejas-theme',t);
 $('themeBtn').textContent=t==='dark'?'☀':'☾';}
$('themeBtn').onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');
setTheme(localStorage.getItem('tejas-theme')||'light');
// actions
async function start(){const r=await(await fetch('/api/start',{method:'POST',headers:POST_HEADERS,body:JSON.stringify(
 {broker:broker.value,bankroll:bankroll.value,dry_run:dry.checked,ignore_hours:ign.checked})})).json();
 if(r&&r.ok===false)alert('⚠ '+(r.error||'could not start'));setTimeout(poll,400);}
async function resetData(){if(!confirm('Clear all recorded paper trades from the local database?'))return;
 const r=await(await fetch('/api/reset',{method:'POST',headers:POST_HEADERS})).json();
 if(r&&r.ok===false)alert('⚠ '+(r.error||'failed'));setTimeout(poll,300);}
async function stop(){await fetch('/api/stop',{method:'POST',headers:POST_HEADERS});setTimeout(poll,400);}
async function quit(){if(!confirm('Stop the bot and shut down the dashboard?'))return;
 try{await fetch('/api/quit',{method:'POST',headers:POST_HEADERS});}catch(e){}
 document.body.innerHTML='<main><div class="panel sec">Server stopped. You can close this tab.</div></main>';}
async function refresh(){$('refBtn').textContent='… scanning';await fetch('/api/candidates/refresh',{method:'POST',headers:POST_HEADERS});setTimeout(pollCand,1500);}
// API keys modal
async function openKeys(){const r=await(await fetch('/api/keys?broker='+broker.value)).json();
 $('keysBroker').textContent=BNAME[r.broker]||r.broker;const f=$('keysFields');
 if(!r.fields.length){f.innerHTML='<div class="warn">No API keys needed for Paper (simulated). Pick a real broker to enter keys.</div>';}
 else{f.innerHTML=r.fields.map(x=>`<div class="kf"><label>${esc(x.label)}${x.opt?' · optional':''}${x.set?'<span class="set">● saved</span>':''}</label>
   <input type="password" data-env="${esc(x.env)}" placeholder="${x.set?'•••••• (leave blank to keep)':'enter value'}"></div>`).join('');}
 $('keysModal').classList.add('show');}
function closeKeys(){$('keysModal').classList.remove('show');}
async function saveKeys(){const vals={};document.querySelectorAll('#keysFields input').forEach(i=>{if(i.value.trim())vals[i.dataset.env]=i.value.trim();});
 await fetch('/api/keys',{method:'POST',headers:POST_HEADERS,body:JSON.stringify({broker:broker.value,values:vals})});closeKeys();
 if(Object.keys(vals).length)alert('Saved ✓ — used next time you Start the bot.');}
$('keysModal').addEventListener('click',e=>{if(e.target.id==='keysModal')closeKeys();});
$('qrModal').addEventListener('click',e=>{if(e.target.id==='qrModal')closeQR();});
broker.addEventListener('change',()=>{if($('keysModal').classList.contains('show'))openKeys();});
function spark(){const c=$('spark'),x=c.getContext('2d'),w=c.width=c.clientWidth,h=c.height=42;
 x.clearRect(0,0,w,h);if(hist.length<2)return;const mn=Math.min(...hist),mx=Math.max(...hist),r=(mx-mn)||1;
 const up=hist[hist.length-1]>=hist[0],col=up?'#12a150':'#e5484d';
 x.beginPath();hist.forEach((v,i)=>{const px=i/(hist.length-1)*w,py=h-((v-mn)/r)*(h-6)-3;i?x.lineTo(px,py):x.moveTo(px,py);});
 x.strokeStyle=col;x.lineWidth=2;x.stroke();x.lineTo(w,h);x.lineTo(0,h);x.closePath();x.fillStyle=col+'1e';x.fill();}
async function poll(){let s;try{s=await(await fetch('/api/status')).json();}catch(e){return;}
 const b=$('badge');b.textContent=s.running?'running':'stopped';b.className='pill '+(s.running?'on':'off');
 running=s.running;$('dot').className='dot'+(s.running?' live':'');
 if(s.lan_url)lanUrl=s.lan_url;
 $('startBtn').disabled=s.running;$('stopBtn').disabled=!s.running;
 const c=s.config||{};const mp=$('modepill');
 if(c.BROKER){mp.style.display='';const live=c.DRY_RUN!=='true'&&c.BROKER!=='simulated';
   mp.textContent=c.DRY_RUN==='true'?'DRY-RUN':(c.BROKER==='simulated'?'PAPER':'LIVE');
   mp.className='mode'+(live?' live':'');$('cfg').textContent=BNAME[c.BROKER]||c.BROKER;}else{mp.style.display='none';$('cfg').textContent='';}
 // Mirror the ACTUAL running config onto every device (laptop ⇄ phone in sync),
 // and lock the controls while running so they reflect reality, not local edits.
 if(s.running&&c.BROKER){broker.value=c.BROKER;if(c.BANKROLL)bankroll.value=c.BANKROLL;
   dry.checked=c.DRY_RUN==='true';ign.checked=c.IGNORE_MARKET_HOURS==='true';}
 [broker,bankroll,dry,ign].forEach(el=>{el.disabled=s.running;});
 $('equity').textContent=s.equity!=null?money(s.equity):'—';
 if(s.run_pnl!=null){const e=$('pnl');const up=s.run_pnl>=0;
   e.textContent=`${up?'▲':'▼'} ${money(Math.abs(s.run_pnl))} (${s.run_pct.toFixed(2)}%)`;e.className='s '+(up?'pos':'neg');}
 $('regime').textContent=pretty(s.regime);$('vix').textContent='India VIX '+(s.vix!=null?s.vix:'—');
 $('openc').textContent=s.open_count!=null?s.open_count:'—';
 if(s.max_positions!=null)$('openmax').textContent='of '+s.max_positions+' max';
 $('cycle').textContent=s.cycle!=null?s.cycle:'—';$('dd').textContent='drawdown '+(s.drawdown!=null?s.drawdown+'%':'—');
 if(s.equity!=null&&(!hist.length||hist[hist.length-1]!==s.equity)){hist.push(s.equity);if(hist.length>150)hist.shift();}
 spark();
 $('postable').innerHTML=(s.positions&&s.positions.length)?s.positions.map(p=>{const pl=p.pnl_pct;
   const cls=pl==null?'muted':(pl>=0?'pos':'neg');
   const when_in=p.entry_time?p.entry_time.slice(0,16).replace('T',' '):'—';
   return `<tr><td><b>${esc(p.symbol)}</b></td><td class="num">${money(+p.size_usd)}</td><td class="num">${(+p.entry_price).toFixed(2)}</td>
    <td class="muted">${esc(when_in)}</td><td class="num">${p.current_price?(+p.current_price).toFixed(2):'—'}</td>
    <td class="num ${cls}">${pl==null?'—':(pl>=0?'+':'')+pl.toFixed(2)+'%'}</td></tr>`;}).join('')
   :'<tr><td colspan="6" class="muted">none yet</td></tr>';
 const L=$('log');const atBottom=L.scrollTop+L.clientHeight>=L.scrollHeight-20;
 L.textContent=(s.log||[]).join('\n');if(atBottom)L.scrollTop=L.scrollHeight;
}
async function pollCand(){let c;try{c=await(await fetch('/api/candidates')).json();}catch(e){return;}
 $('refBtn').textContent=c.computing?'… scanning':'↻ Scan';
 if(c.items&&c.items.length){const mx=Math.max(...c.items.map(i=>Math.abs(Number(i.score)||0)))||1;
  $('cands').innerHTML=c.items.map((it,i)=>{const pct=Math.max(0,Math.min(100,Math.round((Number(it.score)||0)/mx*100)));
   const thesis=String(it.thesis||''); const vd=thesis.split(':')[0];
   return `<tr><td class="rank">${i+1}</td><td><b>${esc(it.symbol)}</b><div class="tag">@₹${esc(it.price)}</div></td>
    <td><div class="bar"><i style="width:${pct}%"></i></div></td>
    <td class="num">${(Number(it.edge||0)*100).toFixed(1)}%</td>
    <td style="max-width:320px"><span class="vd">${esc(vd)}</span> ${esc(thesis.split(':').slice(1).join(':'))}</td></tr>`;}).join('');
  $('candmeta').textContent=`Ranked ${c.items.length} · regime ${pretty(c.regime?c.regime.regime:'')} · VIX ${c.regime?c.regime.vix:''} · ${c.updated}`;}
 else if(c.computing){$('candmeta').textContent='Scanning NSE universe…';}
 if(c.computing)setTimeout(pollCand,1500);
}
async function pollHist(){let h;try{h=await(await fetch('/api/history')).json();}catch(e){return;}
 $('histtable').innerHTML=(h&&h.length)?h.map(t=>{const up=(t.pnl_pct||0)>=0;
  const when_in=t.entry_time?t.entry_time.slice(0,16).replace('T',' '):'—';
  const when_out=t.exit_time?t.exit_time.slice(0,16).replace('T',' '):'—';
  const reason=(t.exit_reason||'').split('_').slice(0,2).join(' ');
  const why=t.rationale||'';
  return `<tr title="${esc(why)}"><td><b>${esc(t.symbol)}</b>${t.trimmed?' <span class="tag">✂ trimmed</span>':''}</td>
   <td class="num">${(+t.entry_price).toFixed(2)}</td><td class="muted">${esc(when_in)}</td>
   <td class="num">${t.exit_price?(+t.exit_price).toFixed(2):'—'}</td><td class="muted">${esc(when_out)}</td>
   <td class="num ${up?'pos':'neg'}">${(up?'+':'')+((t.pnl_pct||0)*100).toFixed(2)}% (₹${Math.round(t.pnl_usd||0)})</td>
   <td>${esc(reason)}</td></tr>`;}).join('')
  :'<tr><td colspan="7" class="muted">none yet</td></tr>';
}
function updateClock(){$('clock').textContent=new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false})+' IST';}
setInterval(updateClock,1000);updateClock();
poll();setInterval(poll,3000);
pollHist();setInterval(pollHist,30000);
refresh();setInterval(()=>{if(running)refresh();},90000);
pollTrain();
</script></body></html>"""


def _already_serving() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


if __name__ == "__main__":
    local = f"http://127.0.0.1:{PORT}"
    if _already_serving():
        print(f"Tejas dashboard already running → opening {local}")
        try: webbrowser.open(local)
        except Exception: pass
        sys.exit(0)
    ip = lan_ip()
    print("=" * 52)
    print(f" Tejas dashboard")
    print(f"   This PC : {local}")
    print(f"   Phone   : http://{ip}:{PORT}   (same Wi-Fi)")
    print("   If the phone can't connect, allow Python through")
    print("   Windows Firewall (Private networks).")
    print("=" * 52)
    class Server(ThreadingHTTPServer):
        allow_reuse_address = False   # a 2nd instance must NOT share the port
        daemon_threads = True
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} already in use — dashboard already running → opening {local}")
        if not _no_browser:
            try: webbrowser.open(local)
            except Exception: pass
        sys.exit(0)
    # Auto-open the browser ONLY for interactive launches. When the watchdog
    # starts us in the background (TEJAS_NO_BROWSER=1) we never pop a tab — it
    # must not steal focus or appear during screen sharing. Open the dashboard
    # yourself at the URL above whenever you want to look.
    if not _no_browser:
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(local)), daemon=True).start()
    threading.Thread(target=_price_refresher, daemon=True).start()
    threading.Thread(target=_auto_supervisor, daemon=True).start()
    srv.serve_forever()
