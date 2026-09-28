# Failure Recovery Guide

This guide covers what to do when something goes wrong during entry, monitoring, or exit.
**When in doubt: activate the kill switch first, investigate second.**

---

## General Principles

1. **Kill switch first.** If anything is uncertain, run:
   ```bash
   touch logs/KILL_SWITCH_ACTIVE
   ```
   This halts all loops and blocks new orders. It does NOT cancel existing open orders.

2. **Cancel open orders.** After activating the kill switch:
   ```bash
   python3 -m cli.main cancel-open-orders --confirm
   ```

3. **Check broker directly.** Always verify positions and orders on the Zerodha console
   (kite.zerodha.com) — do not rely solely on local state.

4. **Never assume.** Confirm fills from the broker, not from local logs.

5. **Do not re-enter to recover losses.** No averaging, no revenge trades.

---

## Scenario 1: Process crashes during entry

**Symptoms:** System exits unexpectedly after submitting some orders. Partial fills possible.

**Steps:**
1. Activate kill switch immediately.
2. Open Zerodha console — check open positions and open orders.
3. Cancel any open (unfilled) orders manually or via:
   ```bash
   python3 -m cli.main cancel-open-orders --confirm
   ```
4. If any legs are filled but the position is incomplete (e.g., hedges filled but no shorts):
   - Assess risk of the current open position.
   - If short options are missing, the position has limited risk (long options only).
   - Exit the long options via Zerodha console or `live-exit`.
5. If short options filled but corresponding hedge did not:
   - **This is naked short risk.** Close the short immediately via Zerodha console.
   - Do not wait for the system — act directly on Zerodha.
6. After resolving positions, deactivate kill switch:
   ```bash
   rm logs/KILL_SWITCH_ACTIVE
   ```
7. Remove the process lock if it is stale:
   ```bash
   rm logs/strategy.lock
   ```

---

## Scenario 2: Order placed but no fill after a long time

**Symptoms:** `live-enter` is waiting, repricing is happening, but no fills.

**Check:**
- Is the limit price far from the market? Run `fetch-quotes` in a separate terminal.
- Is the market moving against you? Credit may be deteriorating.
- Is the option thinly traded (very low volume/OI)?

**Action:**
1. Let the system run until `max_repricing_attempts` is exhausted.
2. If you want to abort sooner:
   ```bash
   touch logs/KILL_SWITCH_ACTIVE
   ```
3. System will cancel open orders and abort. Check broker console to confirm cancellations.

---

## Scenario 3: System reports entry complete but Zerodha shows different positions

**Symptoms:** Local logs say ENTRY_COMPLETE but broker positions do not match.

**Steps:**
1. Do NOT rely on local state. Trust the broker.
2. Open Zerodha console — note actual positions and quantities.
3. Run:
   ```bash
   python3 -m cli.main account-status
   ```
4. Manually reconcile:
   - For any extra open orders: cancel them.
   - For any position that should not exist: close it.
   - For any position that should exist but does not: assess the risk before re-entering.
5. If reconciliation cannot be done via CLI, use the Zerodha app directly.

---

## Scenario 4: API error during exit (position left open)

**Symptoms:** Exit engine aborts, some legs not closed, state is EXIT_PARTIALLY_FILLED.

**This is the highest-risk scenario — short options may remain open.**

**Immediate steps:**
1. Activate kill switch.
2. Open Zerodha console immediately.
3. Close remaining short legs **first** (buying them back).
4. Then close remaining long legs.
5. Use Zerodha console directly — do not wait for the CLI.

**After resolving:**
1. Cancel any stale open orders.
2. Confirm all four legs are zero.
3. Deactivate kill switch.

---

## Scenario 5: Access token expired mid-session

**Symptoms:** API calls return 403 or "Invalid token" errors.

**Steps:**
1. Positions are safe — token expiry does not affect existing positions.
2. Renew token:
   ```bash
   python3 -m brokers.token_helper
   ```
3. Restart monitoring or exit commands after token is renewed.

---

## Scenario 6: Process lock prevents starting

**Symptoms:** `Cannot start: Another strategy instance is running`

**Steps:**
1. Check if another instance is actually running:
   ```bash
   cat logs/strategy.lock     # shows PID
   ps aux | grep cli          # check if that PID is alive
   ```
2. If the process is genuinely running, wait for it to finish or kill it cleanly.
3. If it is a stale lock (process dead), remove it:
   ```bash
   rm logs/strategy.lock
   ```

---

## Scenario 7: Kill switch left active accidentally

**Symptoms:** All operations blocked, commands abort immediately.

**Steps:**
```bash
# Verify it is active
python3 -m cli.main status

# Remove it
rm logs/KILL_SWITCH_ACTIVE

# Verify
python3 -m cli.main status
```

---

## Scenario 8: Wrong expiry or strikes in config

**Symptoms:** `resolve-instruments --live` returns `InstrumentNotFoundError`.

**Steps:**
1. Do NOT proceed. A wrong instrument is worse than no trade.
2. Check NSE website or Zerodha for the correct expiry date and available strikes.
3. Update `config/strategy.yaml` with the correct values.
4. Re-run `resolve-instruments --live` until all four legs resolve.

---

## Scenario 9: Margin call or insufficient funds

**Symptoms:** `check-margin` shows required > available, or Zerodha rejects the order.

**Steps:**
1. Do NOT enter. Abort the session.
2. Review position sizing: reduce `lots` in `strategy.yaml`.
3. Review capital: add funds to your account if appropriate.
4. Review the strategy: wider spreads require less margin.

---

## Contacts and Support

- **Zerodha support:** support.zerodha.com
- **Kite Connect API issues:** kite.trade/forum
- **Exchange trading halts:** NSE website (nseindia.com)

---

## Logs and Audit Trail

All events are stored in `logs/trading.db`. To inspect:

```bash
sqlite3 logs/trading.db

# Recent state changes
SELECT event_time, event_type, strategy_id, details
FROM system_events
ORDER BY event_time DESC
LIMIT 20;

# All orders
SELECT order_id, trading_symbol, side, quantity, limit_price,
       status, filled_quantity, average_fill_price
FROM orders
ORDER BY submitted_at DESC;

# All fills
SELECT f.fill_id, o.trading_symbol, f.quantity, f.price, f.timestamp
FROM fills f
JOIN orders o ON f.order_id = o.order_id
ORDER BY f.timestamp DESC;

# P&L snapshots
SELECT snapshot_time, entry_credit, cost_to_close,
       unrealized_pnl, pct_of_max_profit
FROM pnl_snapshots
ORDER BY snapshot_time DESC;
```
