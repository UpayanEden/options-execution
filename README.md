# Options Execution System

A safe, semi-automated iron condor execution and monitoring system for NSE F&O.
Built on Zerodha Kite Connect. Defaults to paper trading mode.
Live orders require explicit double confirmation.

## Features

- Four-leg iron condor entry and exit via Zerodha Kite Connect
- Adaptive limit-order repricing — no market orders ever
- Hedge-first execution to prevent naked short exposure
- Paper trading mode with conservative/midpoint/touch fill simulation
- 16-state strategy state machine with audited transitions
- Pre-trade validation: quote freshness, bid-ask spread, net credit, margin, capital limits
- Runtime exit conditions: stop-loss, profit target, DTE exit, time-based exit
- Kill switch (file-based, process-safe)
- Process lock — prevents duplicate instances
- SQLite audit trail: every quote, decision, order, fill, P&L, alert
- Configurable via YAML — no hard-coded strategy parameters
- Zerodha instrument master cached locally (refreshes every 8 hours)
- Full test suite: 77 tests, zero live broker calls required

## Safety Model

```
TRADING_MODE=PAPER          ← default, always safe
```

Live trading requires **both**:

```
TRADING_MODE=LIVE
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED
```

Even with both set, `live-enter` prints a pre-flight table and asks you to type `CONFIRM` before touching the exchange. The broker layer checks the env vars again as a second defence.

## Directory Structure

```
options_execution/
├── config/
│   ├── strategy.yaml          Strategy definition (legs, underlying, expiry)
│   ├── risk.yaml              Risk limits, timing, exit rules
│   └── settings.example.env  Environment variable template
├── brokers/
│   ├── base.py                Abstract BrokerAdapter interface
│   ├── zerodha.py             Zerodha Kite Connect adapter (read + live orders)
│   ├── paper.py               Paper broker (simulated fills from live quotes)
│   └── token_helper.py        Daily OAuth token helper
├── execution/
│   ├── models.py              Pydantic models: Strategy, Quote, Order, Fill, Position
│   ├── state_machine.py       16-state machine with validated transitions
│   ├── quote_engine.py        Quote fetch, staleness check, basket credit calculation
│   ├── pricing.py             Adaptive limit price: round_to_tick, initial, next
│   ├── order_manager.py       Paper order lifecycle with adaptive repricing
│   ├── live_order_manager.py  Live order lifecycle with poll-based fill confirmation
│   ├── fill_tracker.py        Per-leg fill recording and net credit calculation
│   ├── entry_engine.py        Hedge-first entry orchestration
│   ├── exit_engine.py         Shorts-first controlled exit
│   ├── process_lock.py        File-based PID lock
│   └── reconciliation.py      Local vs broker position comparison
├── risk/
│   ├── payoff.py              Max profit, max loss, breakevens, cost-to-close
│   ├── validator.py           Pre-trade validation checks
│   ├── limits.py              Runtime exit condition checks
│   └── kill_switch.py         Emergency halt via file flag
├── monitoring/
│   ├── position_monitor.py    MTM polling loop with exit condition checks
│   ├── alerts.py              Console + log alerts (extensible to Telegram)
│   └── journal.py             Structured audit trail
├── storage/
│   ├── database.py            SQLite schema (13 tables)
│   └── repositories.py        CRUD layer (portable to PostgreSQL)
├── cli/
│   └── main.py                Click CLI — all commands
├── tests/                     77 unit tests, zero live API calls
├── logs/                      SQLite DB, rotating logs, lock file
└── reports/                   Generated P&L reports
```

## Setup

### Requirements

- Python 3.11+
- Zerodha Kite Connect API key and secret (from [kite.trade](https://kite.trade))
- Active Zerodha trading account with F&O segment enabled

### Install dependencies

```bash
cd /media/upayan/Work/options_execution
pip install -r requirements.txt
```

### Configure

```bash
cp config/settings.example.env .env
# Edit .env with your API key and secret
```

### Get a daily access token

```bash
python3 -m brokers.token_helper
# Follow the printed instructions
# Token is written to .env automatically
```

### Verify setup

```bash
python3 -m pytest tests/ -v                          # 77 tests, all pass
python3 -m cli.main validate-config                  # config files OK
python3 -m cli.main resolve-instruments --live       # instruments resolve
python3 -m cli.main fetch-quotes                     # live bid/ask shown
python3 -m cli.main check-margin                     # margin within limits
```

## CLI Reference

| Command | Description | Orders? |
|---|---|---|
| `validate-config` | Validate strategy.yaml and risk.yaml | No |
| `resolve-instruments` | Show estimated symbols from config | No |
| `resolve-instruments --live` | Resolve real tokens from Zerodha | No |
| `fetch-quotes` | Live bid/ask/LTP/spread/OI for all legs | No |
| `check-margin` | Basket margin estimate via Zerodha API | No |
| `account-status` | Open positions, orders, available funds | No |
| `preview-entry` | Full entry preview table | No |
| `paper-enter` | Simulated entry with paper broker | No |
| `status` | System status: mode, kill switch, DB | No |
| `live-enter --confirm` | Live entry (double confirmation required) | **YES** |
| `live-exit --confirm --run-id ID` | Live exit for an open position | **YES** |
| `cancel-open-orders --confirm` | Cancel all open orders on Zerodha | **YES** |
| `emergency-exit --confirm` | Activate kill switch | No orders |

Global options (pass before the command):

```bash
python3 -m cli.main --strategy config/strategy.yaml --risk config/risk.yaml --log-level DEBUG <command>
```

## Strategy Configuration

Edit `config/strategy.yaml`:

```yaml
strategy:
  name: expected_move_iron_condor
  underlying: OBEROIRLTY
  exchange: NFO
  expiry: "2026-08-25"
  lots: 1
  legs:
    - side: BUY
      option_type: PE
      strike: 1700
    - side: SELL
      option_type: PE
      strike: 1720
    - side: SELL
      option_type: CE
      strike: 1920
    - side: BUY
      option_type: CE
      strike: 1940
```

Rules:
- `lots` must be 1 for the first live run
- Strikes must exist in the NFO instrument master for the given expiry
- Expiry format: `"YYYY-MM-DD"` (use the actual exchange expiry date)

## Risk Configuration

Key parameters in `config/risk.yaml`:

| Parameter | Default | Meaning |
|---|---|---|
| `capital_limit` | 200000 | Maximum capital to deploy (INR) |
| `max_strategy_loss` | 5000 | Abort entry if max loss exceeds this |
| `min_net_credit` | 15.0 | Minimum acceptable net credit (points) |
| `max_bid_ask_spread_pct` | 8.0 | Reject quotes with spread > this % |
| `quote_staleness_seconds` | 30 | Reject quotes older than this |
| `entry_start_time` | 09:20 | No entry before this time (IST) |
| `entry_end_time` | 14:30 | No entry after this time (IST) |
| `max_repricing_attempts` | 5 | Give up after this many reprice cycles |
| `order_wait_seconds` | 15 | Wait between reprice attempts |

## Entry Execution Logic

1. Validate instruments resolve from NFO master
2. Fetch live quotes, check staleness and spread
3. Calculate conservative executable basket credit (BUY pays ask, SELL receives bid)
4. Compare credit vs `min_net_credit` — wait or abort if too low
5. Run pre-trade risk validation (capital, max loss, timing window)
6. Print pre-flight table and require typed `CONFIRM`
7. Submit BUY legs (hedges) first — wait for fills
8. If hedges fill, submit SELL legs (shorts)
9. If any short fails: cancel all open orders, close filled positions, abort cleanly
10. Record actual net credit, transition to MONITORING

## Paper Trading vs Live Trading

| Behaviour | Paper | Live |
|---|---|---|
| Quote source | Live Zerodha quotes | Live Zerodha quotes |
| Fill simulation | CONSERVATIVE/MIDPOINT/TOUCH | Real exchange fills |
| Orders sent to Zerodha | No | Yes |
| State machine | Same | Same |
| Risk checks | Same | Same |
| SQLite audit | Same | Same |

`PAPER_FILL_MODE=CONSERVATIVE` — BUY fills only if limit ≥ ask; SELL fills only if limit ≤ bid.

## Emergency Procedures

```bash
# Immediate halt — stops all loops, blocks new orders
touch logs/KILL_SWITCH_ACTIVE

# Cancel all open Zerodha orders
python3 -m cli.main cancel-open-orders --confirm

# Close all legs
python3 -m cli.main live-exit --confirm --run-id <id>

# Remove kill switch when safe
rm logs/KILL_SWITCH_ACTIVE
```

## Extending the System

- **New alert channel** (Telegram, email): subclass `AlertChannel` in `monitoring/alerts.py`
- **New exit rule**: add a check in `risk/limits.py` `check_exit_conditions()`
- **PostgreSQL**: swap the engine URL in `storage/database.py` — repositories are SQL-agnostic
- **New underlying**: edit `config/strategy.yaml` — no code changes needed
- **Multiple strategies**: run separate processes with separate strategy.yaml and lock files

## Licence

Private. Not for redistribution.
