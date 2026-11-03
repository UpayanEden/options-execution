# Stale Quotes Diagnosis

Examined files: `brokers/zerodha.py`, `execution/models.py`, `execution/quote_engine.py`,
`live/guard_layers.py`, `live/shift_monitor.py`, `live/condor_runner.py`.

---

## Cause 1 — Timezone bug

**CLEAR.** Server runs `Asia/Kolkata` (`/etc/timezone` = `Asia/Kolkata`, `time.tzname` = `('IST', 'IST')`).
All freshness comparisons use `datetime.now(ZoneInfo("Asia/Kolkata"))`.

`_parse_quote` (`zerodha.py:324–337`) forces `tzinfo=IST` on any naive string it gets from Kite:

```python
ts = datetime.fromisoformat(ts).replace(tzinfo=IST)   # zerodha.py:331
```

`Quote.is_stale` (`execution/models.py:235`) uses `now_ist()` which is `datetime.now(IST)`.

No timezone bug present.

---

## Cause 2 — Wrong freshness field (PRESENT — PRIMARY CAUSE)

**`zerodha.py:327`** — `_parse_quote` bases its `timestamp` on `data.get("timestamp")`,
which is Zerodha's `"timestamp"` field in the REST `kite.quote()` response.
That field is the **last time the exchange updated this instrument's quote packet**, not
the top-of-book change time.

For illiquid far-OTM options (e.g. `COALINDIA26NOV365PE`, `COALINDIA26NOV410CE`) that haven't
traded in several minutes, the exchange timestamp is genuinely 5–15 minutes old even though
the order book depth may have live resting orders.

QuoteGuard (`guard_layers.py:267–270`) then fires:

```python
age_seconds = (now - quote.timestamp).total_seconds()
if age_seconds > 10:
    failures.append(f"{sym}: quote age {age_seconds:.1f}s > 10s")
```

This is the **direct cause of every `QuoteGuard` failure seen in the logs** (e.g.
`COALINDIA26NOV365PE: quote age 17.6s > 10s`). The exchange hasn't printed a new tick for
that strike, so the timestamp is old — but the book is alive and tradeable.

**Fix needed:** for entry-guard age checks, use `received_at` (local monotonic time of the
REST call) rather than the exchange timestamp. The exchange timestamp should only gate
whether a fill price is trustworthy, not whether a REST response is fresh.

---

## Cause 3 — Price taken from LTP, not the book (PRESENT — TWO LOCATIONS)

### 3a. `zerodha.py:358–370` — LTP fallback when depth is absent

```python
if depth_bids:
    best_bid = depth_bids[0].price
else:
    best_bid = Decimal(str(data.get("last_price", 0)))   # zerodha.py:362 ← LTP used as bid

if depth_asks:
    best_ask = depth_asks[0].price
else:
    best_ask = Decimal(str(data.get("last_price", 0)))   # zerodha.py:369 ← LTP used as ask
```

When the REST response has no depth (can happen for very illiquid strikes or when the
exchange hasn't published depth for that packet), both bid and ask are set to `last_price`.
This gives:
- A synthetic bid==ask (zero spread), which passes the spread check.
- A price that is potentially many minutes old (last trade price, not current book).
- No signal to the caller that depth was missing — the `Quote` looks normal.

### 3b. `shift_monitor.py:489–497` — LTP fallback in profit-target check

```python
def _leg_val(bid_key: str, ltp_key: str) -> float:
    bid = options_cache.get(bid_key, 0.0)
    ltp = options_cache.get(ltp_key, 0.0)
    return bid if bid > 0 else ltp          # shift_monitor.py:492
```

If `bid == 0` (empty book on the short leg), `ltp` is used to compute `current_credit`.
LTP can be the price from the last trade, potentially many minutes old, making the
profit-target calculation wrong.

### 3c. `condor_runner.py:932` — LTP used for long-leg exit price

```python
raw_price = max(q.bid, q.ltp) if q else Decimal("0")   # condor_runner.py:932
```

During exit, long legs use `max(bid, ltp)`. If bid is 0 and ltp is old, the exit order is
placed at a stale price.

---

## Cause 4 — WebSocket mode

**NOT PRESENT.** There is no `KiteTicker` usage anywhere in the codebase.
The entire system is **pure REST polling** via `kite.quote()`.

No WebSocket subscription, no `MODE_FULL`, no tick callbacks.

Consequence: every quote fetch is a synchronous REST round-trip. Quote freshness is
controlled by how frequently the strategy code calls `get_quotes()`, not by a live feed.
The "staleness" seen in the logs is exchange-timestamp staleness (Cause 2), not
a WebSocket mode problem.

---

## Cause 5 — WebSocket gives no signal when data stops

**NOT APPLICABLE** — no WebSocket. See Cause 4.

The equivalent REST risk is that `kite.quote()` can return stale data silently (the
exchange hasn't updated the packet but returns the last known value). This is covered
by Cause 2 and Cause 7.

---

## Cause 6 — REST polling and rate limits (PARTIALLY PRESENT)

### 6a. Batch logic is correct

`zerodha.py:292–299`: batches of 400 instruments with a 0.35 s inter-batch sleep.
On error, raises `QuoteError` — does **not** return cached data. ✓

### 6b. Equity/index spot calls swallow all exceptions

`zerodha.py:701–706` (`get_index_spot`) and `zerodha.py:742–747` (`get_equity_spots_batch`):

```python
except Exception as exc:
    logger.warning("equity_spot_failed", ...)
    return None   # or {s.upper(): None for s in symbols}
```

`shift_monitor.py:687`: when spot returns None, falls back to `pos.spot_at_entry`:

```python
"shift_monitor_using_stale_spot"
```

A 429, expired token, or network blip on a spot call causes silent fallback to a
potentially hours-old entry price for shift-trigger evaluation. If the stale spot
happens to be outside the trigger zone, a legitimate shift is missed entirely.

### 6c. No explicit 429 detection

Zerodha raises `kiteconnect.exceptions.NetworkException` for 429s.
The catch-all `except Exception` in `get_quotes` re-raises as `QuoteError`, which is
correct. But `get_equity_spots_batch` swallows it and returns None (6b above).

---

## Cause 7 — Caching (PRESENT — minor)

### 7a. `options_cache` in `shift_monitor` has no per-entry receive time

`shift_monitor.py:203–235`: one dict is built per monitor cycle from a single
`get_quotes()` batch. Entries have no `received_at` timestamp. The cycle processes
positions sequentially; if 10 positions take 5 s total, the last position's cache
entries are already 5 s old but appear fresh to any consumer.

There is no explicit TTL — the cache is rebuilt each cycle, so the maximum age equals
one full cycle duration (typically 5–30 s depending on position count).

### 7b. `Quote.timestamp` is set to `now` if absent

`zerodha.py:336–337`: if Kite's response has no `timestamp` field, the code sets
`ts = now` (current IST time). This makes the quote appear freshly received even if it
isn't, masking staleness for instruments where Kite omits the timestamp.

---

## Cause 8 — Market phases (PRESENT — no gate in quote layer)

There is no guard in the quote layer against pre-open (09:00–09:15) or post-close (>15:29)
quotes being used as tradeable prices.

The strategy config gates entries via `entry_start: "09:30"` but:
- The quote layer itself will return and accept quotes at any time.
- If the monitor runs at 09:10, options_cache is built from pre-open auction prices.
  Bid/ask during pre-open are indicative order-match prices, not live book prices.
- QuoteGuard's age check (10 s) does not protect here: the exchange timestamp is fresh
  (auction is running), so age passes, but the prices are not tradeable.

Additionally there is no holiday/exchange-halt detection.

---

## Summary table

| Cause | Present? | Severity | Primary file:line |
|-------|----------|----------|-------------------|
| 1 — Timezone bug | **No** | — | — |
| 2 — Wrong freshness field | **Yes** | HIGH | `zerodha.py:327`, `guard_layers.py:267` |
| 3 — LTP instead of book | **Yes** | MEDIUM | `zerodha.py:362/369`, `shift_monitor.py:492`, `condor_runner.py:932` |
| 4 — WebSocket mode | **N/A** | — | No WebSocket used |
| 5 — WS silent-feed / reconnect | **N/A** | — | No WebSocket used |
| 6 — REST 429 swallowed | **Partial** | MEDIUM | `zerodha.py:742` (spot only) |
| 7 — Quote cache has no receive-time | **Yes** | LOW | `shift_monitor.py:203` |
| 8 — Market-phase quotes accepted | **Yes** | LOW | `live/guard_layers.py:250` |

## Root cause of the log stale-quote errors

Every `QuoteGuard` failure with `quote age Xs > 10s` in the logs is Cause 2:
the exchange `timestamp` on the Kite REST response for illiquid far-OTM option
strikes is genuinely old (the option hasn't been touched by the exchange in > 10 s),
but the order book is live. The fix is to track `received_at = time.monotonic()` at
parse time and use that for the QuoteGuard age check, reserving exchange `timestamp`
for price-staleness checks only.
