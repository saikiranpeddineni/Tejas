# Tejas production audit

**Audit date:** 2026-10-17
**Branch:** `audit/production-audit-20261017`
**Scope:** full tracked Python application, broker adapters, persistence, ML/data paths, dashboard, alerts, operations, packaging, tests, and runtime-boundary behavior.

## Result

The audit traced the critical flows from configuration and market-data ingestion through feature construction, signal filtering, Kelly sizing, portfolio risk gates, broker submission, fills, ledger persistence, staged exits, restart restoration, reconciliation, dashboard control, and shutdown. Verified issues were fixed with focused changes rather than a framework rewrite.

### Trading, risk, and execution

- Broker adapters now share client-order IDs, reject malformed/error envelopes, avoid automatic POST retries, require broker order IDs, and fail closed on account/position/quote failures.
- Live flattening no longer treats a failed position lookup as an already-flat account.
- Orders and staged exits use whole-share quantities and broker-reported fills where available; malformed fill data cannot create a local position. `DRY_RUN=true` is now observation-only and cannot create hypothetical ledger trades or P&L.
- Risk sizing no longer inflates a capped order back to the minimum. Kelly sizing uses each signal's actual stop/target distances, preserves defensive regime multipliers, and rejects invalid probability/risk inputs.
- Exposure, sector, beta, drawdown, daily-loss, cooldown, and extreme-regime gates are restart-aware and fail closed on non-finite portfolio state.
- Open-trade restoration preserves trailing highs, staged-exit state, feature defaults, sector exposure, and malformed-row safety. Simulated-broker restoration also carries realized paper P&L across restarts.
- Exit monitoring validates prices, avoids stale/off-hours exits, wires the cached earnings pre-event exit, persists trailing highs, and keeps trades open when flattening fails.
- Reconciliation aggregates duplicate symbols, tolerates whole-share rounding, distinguishes broker outages from a confirmed flat account, rejects malformed quantities, and suppresses repeated identical alerts.

### Data, ML, and analytics

- OHLCV, quote, sentiment, fundamentals, forecast labels, training rows, timestamps, and inference vectors are validated for structure, finiteness, date alignment, and safe fallbacks.
- Historical market/sector context is date-aligned to training windows. Forecast and live-outcome model artifacts use separate files and model-specific trust metrics; mismatched legacy metrics disable the ML trust gate until retraining.
- Temporal ML holdout evaluation no longer early-stops against the scored test fold. Invalid datasets/timestamps are rejected without replacing a usable model.
- SQLite migrations are additive and recover independently after interrupted staged-exit migrations; worker connections roll back failed statements and remain usable.
- Performance/backtest queries normalize ISO timestamps and exclude invalid legacy P&L rows.

### Dashboard, security, and operations

- The dashboard binds to loopback by default. Deliberate remote binding requires a configured token and HTTP Basic authentication; mutating requests require a custom header and bounded JSON bodies.
- Dashboard output and Telegram messages escape external/model text. Credential files are allowlisted, atomically written with restrictive permissions, and only the selected broker's credentials are passed to its child process. Telegram errors do not log bot-token URLs.
- Dashboard polling tails only a bounded log window, startup diagnostics are capped, and scheduler/reconciliation shutdown is interruptible. Forecast-model status checks the forecast artifact, and unattended live-broker selections are downgraded to dry-run before credential validation.
- The old unbounded log handler is now rotating. Runtime state, local credentials, databases, logs, and generated model outputs are ignored for new commits.
- Reproducibility files were added: `.python-version`, pinned `requirements.lock`, `Dockerfile`, `.dockerignore`, `.env.example`, `Makefile`, and setup/run/test/deploy/recovery documentation in `README.md`.

## Verification performed

Commands run from the repository root:

- `make test` — passed; 27 deterministic offline regression tests.
- `python -m unittest discover -s tests -p 'test*.py' -v` — passed.
- `.venv/bin/python -m unittest discover -s tests -p 'test*.py' -v` — passed.
- `python -m compileall -q .` — passed.
- Targeted module import smoke test under the project environment — passed for all application modules available in the installed environment.
- `.venv/bin/python -m pip check` — passed for the packages installed in that environment.
- `git diff --check` — passed for source/test/documentation changes; a pre-existing generated model backup contains trailing whitespace.

The Docker executable is not available in this environment, so an actual image build/run could not be performed here. The Dockerfile uses Python 3.11.9 and installs the pinned lockfile plus `libgomp1` required by the ML stack.

## Remaining risks requiring deliberate operational or broker-specific work

1. **Asynchronous live fills:** the live adapters return submitted order IDs, but they do not yet poll each broker's order/trade endpoint for final fill status. Before live use, validate partial fills, rejections, and delayed fills in each broker's sandbox and add adapter-specific reconciliation/polling.
2. **Software-only stops:** NSE delivery stops are enforced by the running process, not an exchange-side bracket. A host/process/feed outage can leave positions unprotected; keep the watchdog/supervisor active and treat offline-gap alerts as incidents.
3. **External feed outages:** earnings, sentiment, fundamentals, and Yahoo market data have bounded retries/budgets but some gates intentionally fail open to avoid freezing all trading. Operators should disable new entries if those feeds remain degraded.
4. **Legacy artifacts/state:** existing local databases, logs, and model artifacts may contain historical schema/model versions. Legacy or untagged model metrics are intentionally untrusted until a fresh compatible training run writes model-specific metadata.
5. **Remote dashboard transport:** remote mode is authenticated but plain HTTP. Use loopback by default or put deliberate LAN access behind a trusted network and TLS/reverse proxy.
