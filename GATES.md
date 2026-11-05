# GATES — P&L Blink Reset Fix + Heartbeat Write Error

## G1 — P&L reset root cause identified
MANUAL: `autostart.local.json` has `"bankroll": null` and `"scan": null`.
When the bot is NOT running, `status()` reads `bot_cfg` which is empty `{}`,
so `start = float(bot_cfg.get("BANKROLL", 100000) or 100000)` → returns 100000.
When the bot IS running, `bot_cfg["BANKROLL"]` is set correctly from BANKROLL env var.
The blink occurs because:
1. Status poll fetches equity=₹1,102,613 and start=correct_bankroll → shows real P&L
2. Log rotates or the 128KB tail_log window misses the [Main] Cycle line briefly
3. equity becomes None → cur_equity = start = 100000 → P&L shown as ₹0 (reset flash)
EVIDENCE: Log shows `equity=₹1,102,613` correctly on each cycle line.

## G2 — Heartbeat write error identified
MANUAL: Log shows `ERROR main — [Main] Heartbeat write failed — live entries remain blocked`
at `2026-11-05 14:50:50`. This is an `OSError` in `_write_heartbeat()`. Likely a file
lock contention between bot process writing `tejas.heartbeat.tmp.<pid>` and the webui
reading `tejas.heartbeat` simultaneously, or a transient disk error.

## G3 — Fix applied to webui.py status() — P&L fallback uses DB not hardcoded 100000
CHECK: python -c "import ast, sys; ast.parse(open('webui.py').read()); print('SYNTAX OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: SYNTAX OK

## G4 — autostart.local.json bankroll fixed (not null)
CHECK: python -c "import json; d=json.load(open('autostart.local.json')); assert d['bankroll'] is not None and float(d['bankroll']) > 0, f'bankroll bad: {d[\"bankroll\"]}'; print('BANKROLL OK')"
CWD: c:\Users\peddi\Desktop\Bots\OLD BOTS\Tejas
EXPECT: BANKROLL OK
