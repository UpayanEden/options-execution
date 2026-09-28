# Operational Checklist

Use this checklist before every trading session and before every live entry.

---

## Pre-Session (before market opens)

- [ ] Confirm strategy expiry is correct in `config/strategy.yaml`
- [ ] Confirm strikes exist for the chosen expiry (check NSE website or Zerodha)
- [ ] Confirm lot size is 1 in `config/strategy.yaml`
- [ ] Confirm `TRADING_MODE=PAPER` in `.env` (only change to LIVE when ready)
- [ ] Check `risk.yaml` — capital limit, max loss, min credit are as intended
- [ ] Run daily token refresh:
      ```
      python3 -m brokers.token_helper
      ```
- [ ] Verify token works:
      ```
      python3 -m cli.main account-status
      ```
- [ ] Check available margin is sufficient:
      ```
      python3 -m cli.main check-margin
      ```
- [ ] Kill switch is NOT active:
      ```
      python3 -m cli.main status
      ```
- [ ] No unexpected open positions:
      ```
      python3 -m cli.main account-status
      ```
- [ ] No unexpected open orders:
      ```
      python3 -m cli.main account-status
      ```
- [ ] Run unit tests:
      ```
      python3 -m pytest tests/ -q
      ```

---

## Pre-Entry (before placing the iron condor)

- [ ] Market is open and within entry window (09:20–14:30 IST by default)
- [ ] Run `resolve-instruments --live` — all four legs resolve with correct tokens
- [ ] Run `fetch-quotes` — all quotes fresh, spreads acceptable
- [ ] Net credit shown by `fetch-quotes` is above `min_net_credit` in `risk.yaml`
- [ ] Margin shown by `check-margin` is within `capital_limit`
- [ ] Run `preview-entry` — review all details
- [ ] For paper mode: run `paper-enter`, confirm state = ENTRY_COMPLETE
- [ ] For live mode: understand the pre-flight table will be shown; you must type CONFIRM

---

## Entry (live only)

- [ ] `.env` has `TRADING_MODE=LIVE`
- [ ] `.env` has `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED`
- [ ] Fresh access token (generated today)
- [ ] No other instance of this system is running
- [ ] Kill switch file does NOT exist (`logs/KILL_SWITCH_ACTIVE`)
- [ ] Run:
      ```
      python3 -m cli.main live-enter --confirm
      ```
- [ ] Review the pre-flight table carefully
- [ ] Type `CONFIRM` only when satisfied
- [ ] After entry: verify positions on Zerodha console match expected
- [ ] Note the Run ID printed — needed for `live-exit`

---

## During Monitoring

- [ ] Check `account-status` at least once per day
- [ ] Monitor underlying spot price relative to short strikes
- [ ] Check DTE (days to expiry) — plan exit before expiry if required
- [ ] Verify no unexpected orders or position changes

---

## Pre-Exit

- [ ] Confirm exit is intentional (profit target, stop loss, or scheduled)
- [ ] Run `fetch-quotes` — confirm quotes are live and spreads are acceptable
- [ ] Review cost to close vs entry credit

---

## Exit (live only)

- [ ] Run:
      ```
      python3 -m cli.main live-exit --confirm --run-id <id>
      ```
- [ ] Type `CONFIRM` when prompted
- [ ] After exit: verify all positions are zero on Zerodha console
- [ ] Check for any open orders and cancel if needed:
      ```
      python3 -m cli.main cancel-open-orders --confirm
      ```

---

## Post-Session

- [ ] All positions are closed or as expected
- [ ] No open orders remain
- [ ] Review `logs/trading.db` for any anomalies
- [ ] Set `TRADING_MODE=PAPER` in `.env` until next session
- [ ] Remove `LIVE_TRADING_CONFIRMATION` from `.env` until next session
