# Tejas

Tejas is an NSE equity research and execution bot. It defaults to the simulated
broker and dry-run mode; real broker orders require an explicit `DRY_RUN=false`,
valid broker credentials, and a deliberate manual start. Stops are software
managed, so the bot must remain running while positions are open.

## Setup

Requirements: Python 3.11 (the pinned runtime is in `.python-version`). The
Dockerfile provides the same runtime in a clean Linux image.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
cp .env.example .env
```

Put broker credentials in the environment or in the local dashboard's
`keys.local.json` store. Never put tokens in `.env.example`, source files, logs,
local JSON files, or the repository. `keys.local.json`, databases, logs,
heartbeats, retrained models, and local configuration are ignored by Git.

### Operating modes

- `DRY_RUN=true` is observation-only: it logs eligible orders but does not create
  hypothetical ledger fills or P&L.
- `BROKER=simulated` with `DRY_RUN=false` is the full paper broker: it fills in
  memory, persists trades, restores state after restart, and marks to real NSE
  data without sending orders to an exchange.
- A live broker requires `DRY_RUN=false`, valid credentials, and a deliberate
  manual start. Live adapters are not considered production-ready until their
  broker-specific asynchronous fill/status behavior has been validated.

## Run

For a local paper dashboard:

```bash
. .venv/bin/activate
python webui.py
# open http://127.0.0.1:8765
```

For the trading loop directly (paper/dry-run):

```bash
. .venv/bin/activate
python tejas.py
```

The dashboard is loopback-only by default. To make it reachable on a trusted
LAN, set both `TEJAS_UI_HOST` and `TEJAS_UI_TOKEN` (the token is used as the
HTTP Basic password):

```bash
TEJAS_UI_HOST=0.0.0.0 TEJAS_UI_TOKEN='use-a-long-random-value' python webui.py
```

Do not expose this plain HTTP dashboard directly to the public internet.
`watchdog.py` is a one-shot local supervisor for Windows Task Scheduler; the
provided `setup_autostart.bat` registers it without requiring administrator
privileges.

## Test

The one supported offline check is:

```bash
make test
```

It compiles the project and runs deterministic regression tests. Tests do not
contact Yahoo Finance, Telegram, a broker, or a model service.

## Docker

Build and run the paper trading loop from scratch:

```bash
docker build -t tejas:local .
docker run --rm tejas:local
```

To run the dashboard in a container, opt into its authenticated network bind:

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -e TEJAS_UI_HOST=0.0.0.0 -e TEJAS_UI_TOKEN='use-a-long-random-value' \
  --entrypoint python tejas:local webui.py
```

The container has no credentials by default. For a live deployment, provide
secrets through the runtime environment/secret manager, mount persistent storage
for `tejas.db` and `tejas.log`, and keep the dashboard behind authenticated
network access.

## Recovery and operations

- The bot uses a localhost single-instance lock on port `8766`; a second process
  exits instead of trading the same account and database.
- `Stop` requests a graceful cycle stop; the dashboard records startup failures
  in `tejas.startup.log` so import/config errors are diagnosable.
- Open trades are restored from SQLite on restart. Reconciliation compares the
  broker's net positions with the local ledger and does not treat a failed
  position fetch as a flat account.
- Software SL/TP cannot protect a position while the process or host is down.
  Treat any offline-gap alert as an operational incident and verify positions
  with the broker before resuming.
- After an intentional emergency stop, check the local log, broker positions,
  and the database before restarting. Do not delete runtime state while trades
  are open.
