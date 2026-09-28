# Setup and First-Run Guide

## Step 1: Install Python dependencies

```bash
cd /media/upayan/Work/options_execution
pip install -r requirements.txt
```

Required packages: pydantic, python-dotenv, pyyaml, click, sqlalchemy, kiteconnect, tzdata, pytest.

## Step 2: Create your environment file

```bash
cp config/settings.example.env .env
```

Edit `.env`:

```
ZERODHA_API_KEY=your_key_here
ZERODHA_API_SECRET=your_secret_here
TRADING_MODE=PAPER
```

Leave `ZERODHA_ACCESS_TOKEN` blank for now. Leave `TRADING_MODE=PAPER`.

## Step 3: Run the tests

```bash
python3 -m pytest tests/ -v
```

All 77 tests should pass. No Zerodha connection is required.

## Step 4: Validate your strategy config

```bash
python3 -m cli.main validate-config
```

Adjust `config/strategy.yaml` or `config/risk.yaml` if any check fails.

## Step 5: Get your daily access token

Zerodha access tokens expire at midnight. Run this each trading day:

```bash
python3 -m brokers.token_helper
```

This will:
1. Print a login URL
2. Ask you to paste the `request_token` from the redirect URL after login
3. Generate the access token and write it to `.env` automatically

## Step 6: Test the Zerodha connection

```bash
python3 -m cli.main account-status
```

You should see your account margins and any open positions.

## Step 7: Resolve instruments

```bash
python3 -m cli.main resolve-instruments --live
```

All four legs of your strategy should resolve to specific instrument tokens and lot sizes.
If any leg fails, the strike or expiry is wrong — check NSE website and update `strategy.yaml`.

## Step 8: Fetch live quotes

```bash
python3 -m cli.main fetch-quotes
```

Review:
- All four legs show a bid and ask
- Spread % is below your `max_bid_ask_spread_pct` setting
- Net credit is above `min_net_credit`

## Step 9: Check margin

```bash
python3 -m cli.main check-margin
```

Required margin should be within your `capital_limit`.

## Step 10: Paper entry

```bash
python3 -m cli.main paper-enter
```

If net credit is below `min_net_credit` (common early in the day), temporarily lower it
in `risk.yaml` to test the full entry flow. Restore it afterwards.

## Step 11: (When ready) Live entry

Only proceed when all paper tests pass and quotes are satisfactory.

Edit `.env`:
```
TRADING_MODE=LIVE
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED
```

Then:
```bash
python3 -m cli.main live-enter --confirm
```

Read the pre-flight table. Type `CONFIRM` only when satisfied.

After entry, note the Run ID printed. You will need it for exit:

```bash
python3 -m cli.main live-exit --confirm --run-id <your-run-id>
```

## Daily Token Renewal Reminder

Access tokens expire at midnight IST. Add this to your morning routine:

```bash
python3 -m brokers.token_helper
python3 -m cli.main account-status   # verify token works
```

## Upgrading to PostgreSQL (future)

When you outgrow SQLite, change one line in `storage/database.py`:

```python
# SQLite (current)
engine = create_engine(f"sqlite:///{db_path}", echo=False)

# PostgreSQL (future)
engine = create_engine("postgresql://user:pass@localhost/options_db", echo=False)
```

All repositories work unchanged.
