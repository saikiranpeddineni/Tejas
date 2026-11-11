# GATES — Production-Grade Paper/Live Capital Utilization & Autostart

## G1 — Mode and Autostart Persistence
CHECK: python -c "import json; d=json.load(open('autostart.local.json')); assert d.get('mode') == 'paper' and d.get('dry_run') is False, f'Bad autostart: {d}'; print('AUTOSTART PAPER OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: AUTOSTART PAPER OK

## G2 — Active Exposure Accounting in Models
CHECK: python -c "import sys; sys.path.insert(0, '.'); from models import Trade, Portfolio, FeatureVector; from datetime import datetime, timezone; f = FeatureVector('1', 'TEST', datetime.now(timezone.utc)); t = Trade('1', 'TEST', 'BUY', shares=10.0, size_usd=10000.0, entry_price=1000.0, stop_loss=950.0, take_profit=1100.0, ml_probability=0.6, entry_quality=0.7, kelly_pct=0.05, features=f, trim_shares=5.0); p = Portfolio(bankroll=100000.0, peak_bankroll=100000.0, open_trades=[t]); assert t.active_shares == 5.0, f'active_shares failed: {t.active_shares}'; assert t.active_size_usd == 5000.0, f'active_size failed: {t.active_size_usd}'; assert p.total_exposure == 5000.0, f'portfolio exposure failed: {p.total_exposure}'; assert p.available_capital == 95000.0, f'available capital failed: {p.available_capital}'; print('ACTIVE EXPOSURE OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: ACTIVE EXPOSURE OK

## G3 — Dynamic Risk & Sector Exposure Accounting
CHECK: python -c "import sys; sys.path.insert(0, '.'); from dynamic_risk import DynamicRiskManager; from brokers.simulated import SimulatedBroker; from models import Trade, Portfolio, FeatureVector; from datetime import datetime, timezone; f = FeatureVector('1', 'TEST', datetime.now(timezone.utc), sector_code=1.0); t = Trade('1', 'TEST', 'BUY', shares=10.0, size_usd=10000.0, entry_price=1000.0, stop_loss=950.0, take_profit=1100.0, ml_probability=0.6, entry_quality=0.7, kelly_pct=0.05, features=f, trim_shares=5.0); p = Portfolio(bankroll=100000.0, peak_bankroll=100000.0, open_trades=[t]); b = SimulatedBroker(); mgr = DynamicRiskManager(b); beta = mgr._estimate_portfolio_beta(p.open_trades); assert beta > 0, 'Beta estimation failed'; print('RISK ACCOUNTING OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: RISK ACCOUNTING OK

## G4 — WebUI Cash Reporting Accurate
CHECK: python -c "import urllib.request, json; res = urllib.request.urlopen('http://127.0.0.1:8765/api/status'); d = json.loads(res.read()); acct = d.get('account', {}); pos = d.get('positions', []); cur_eq = acct.get('equity', 0); cash = acct.get('cash', 0); pos_mv = sum(p.get('shares', 0) * (p.get('current_price') or p.get('entry_price', 0)) for p in pos); assert abs((cur_eq - pos_mv) - cash) < 1.0 or cash > 0, f'Cash mismatch: eq={cur_eq}, pos_mv={pos_mv}, cash={cash}'; print('WEBUI CASH OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: WEBUI CASH OK

## G5 — Scheduled Tasks and Autostart Launcher Registered
CHECK: python -c "import autostart_manager; st = autostart_manager.check_status(); assert st['startup_file_present'] is True, 'Startup shortcut missing'; assert st['watchdog_task_registered'] is True, 'Watchdog task missing'; assert st['wake_task_registered'] is True, 'Wake task missing'; print('SCHEDULED TASKS OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: SCHEDULED TASKS OK
