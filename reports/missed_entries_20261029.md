# Missed stock entries — 29 October 2026

All times IST. Production server: 187.127.187.5, application /opt/options-execution. Evidence collected after market close, approximately 15:40–15:47 IST. This is an operational diagnosis from server records and deployed code; proposed entries are conditional on a fresh preview and all checks passing. No orders, configuration changes, restarts, or deployments were made.

## Main finding

The final entry pass at 11:59 attempted 19 enabled stocks. **SBIN and ULTRACEMCO entered; 17 did not.** Of those 17, **11 failed quote freshness, three exceeded the per-stock capital limit, two could not obtain spot, and one hit an incorrect tick-rounding rule.** The maximum-position limit was not reached. The 25% minimum premium filter did not produce a rejection in this final pass.

A temporary failure became a missed entry for the rest of the day because the runner makes one entry pass at startup, then monitors existing positions. It does not periodically retry rejected candidates. Last recorded entry attempt: 11:59:51; monitoring continued through 15:30. The configured entry window ended at 14:00, so another entry today is no longer available within the current rules.

## WAAREEENER — detailed example

WAAREEENER is the stock referred to as “wareener.” Its final attempt was at **11:59:51**, for the **23 November 2026** expiry, 25 calendar days away, with **one lot** configured.

| Leg | Intended action | Strike | Timestamp age at check |
|---|---|---:|---:|
| WAAREEENER26NOV2200PE | Sell | 2200 | 11.2 seconds |
| WAAREEENER26NOV2160PE | Buy hedge | 2160 | 27.2 seconds |
| WAAREEENER26NOV2380CE | Sell | 2380 | 27.2 seconds |
| WAAREEENER26NOV2440CE | Buy hedge | 2440 | 27.2 seconds |

**Recorded rejection:** QuoteGuard requires each quote to be no more than 10 seconds old. All four failed. InstrumentGuard, OrderGuard, PositionGuard, and CapitalGuard passed. Only two positions were open against a ten-position limit. The guard recorded available margin of ₹935,959.85, so this attempt did not fail for insufficient funds. Its ₹9,628.50 guard margin figure is an internal estimate, not a broker-confirmed basket requirement.

The broker adapter reads the timestamp supplied in the REST quote response. A fresh HTTP request therefore does not itself guarantee that this timestamp is recent. The saved logs do not establish whether the underlying cause was a quiet contract, upstream quote delay, or another timestamp issue. Also, QuoteGuard skips spread and LTP checks for stale legs: passing freshness on a later attempt would still require those additional checks to run.

**How to make entry possible:** during a permitted entry window, request all four legs again and rerun the complete preview when every timestamp is within 10 seconds. Add bounded automatic retries for transient freshness failures, with current spot, strikes, premium, depth, capital, duplicate-order and position checks each time. If the same contracts stay stale, inspect live depth and consider another complete candidate basket that satisfies the strategy; the logs cannot prove another basket was executable. Do not merely overwrite the exchange timestamp with local request time or assume raising the age limit guarantees execution.

These strikes describe the rejected historical basket. A future session must recalculate the basket and sizing from current market data.

## Every stock in the final entry pass

For quote failures, ages below refer to rejected legs; unlisted legs did not fail freshness in that snapshot. “Refresh and retry” always means rerun all checks inside the entry window, using current data.

| Stock | Final outcome / evidence | What would allow entry |
|---|---|---|
| ANGELONE | ANGELONE26NOV280PE: quote age 65.2s > 10s; ANGELONE26NOV270PE: quote age 47.2s > 10s; ANGELONE26NOV310CE: quote age 64.2s > 10s; ANGELONE26NOV315CE: quote age 58.2s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| ASIANPAINT | ASIANPAINT26NOV2360CE: quote age 12.4s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| AXISBANK | AXISBANK26NOV1170PE: quote age 32.6s > 10s; AXISBANK26NOV1150PE: quote age 17.6s > 10s; AXISBANK26NOV1270CE: quote age 32.6s > 10s; AXISBANK26NOV1290CE: quote age 32.6s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| BAJAJ-AUTO | BAJAJ-AUTO26NOV9800PE: quote age 61.8s > 10s; BAJAJ-AUTO26NOV9600PE: quote age 47.8s > 10s; BAJAJ-AUTO26NOV11100CE: quote age 16.8s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| COALINDIA | COALINDIA26NOV380PE: quote age 21.2s > 10s; COALINDIA26NOV370PE: quote age 21.2s > 10s; COALINDIA26NOV420CE: quote age 21.2s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| HEROMOTOCO | HEROMOTOCO26NOV4800PE: quote age 20.4s > 10s; HEROMOTOCO26NOV5550CE: quote age 10.4s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| ICICIBANK | ICICIBANK26NOV1320PE: quote age 20.6s > 10s; ICICIBANK26NOV1300PE: quote age 20.6s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| MARUTI | MARUTI26NOV10900PE: quote age 33.8s > 10s; MARUTI26NOV10700PE: quote age 33.8s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| MUTHOOTFIN | MUTHOOTFIN26NOV2350PE: quote age 28.0s > 10s; MUTHOOTFIN26NOV2300PE: quote age 12.0s > 10s; MUTHOOTFIN26NOV2550CE: quote age 21.0s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| SUNPHARMA | SUNPHARMA26NOV1840CE: quote age 15.0s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| SUZLON | SUZLON26NOV39CE: limit price 1.00 not within bid=0.97 / ask=0.99 | Use the instrument’s ₹0.01 tick instead of hardcoded ₹0.05, rebuild the full basket and rerun guards. |
| WAAREEENER | WAAREEENER26NOV2200PE: quote age 11.2s > 10s; WAAREEENER26NOV2160PE: quote age 27.2s > 10s; WAAREEENER26NOV2380CE: quote age 27.2s > 10s; WAAREEENER26NOV2440CE: quote age 27.2s > 10s | Refresh all four quotes and retry only after freshness and remaining guards pass. |
| BHARTIARTL | Guards passed; capital allocator rejected approximately ₹350,193.75 at 3 lots, above ₹250,000 per stock. | 2 lots would estimate ₹233,462.50 under the same allocator; validate actual basket margin and every guard. |
| OBEROIRLTY | Guards passed; allocator rejected approximately ₹280,245 at 4 lots. | 3 lots would estimate ₹210,183.75; validate the full basket. |
| TITAN | Guards passed; allocator rejected approximately ₹288,986.25 at 3 lots. | 2 lots would estimate ₹192,657.50; validate the full basket. |
| BANKINDIA | Spot API failed around 11:59:20; no_spot_price. | Obtain a valid current NSE spot with bounded retry/backoff before strike selection. |
| M&M | Spot API failed around 11:59:24; no_spot_price. | Same: resolve spot acquisition first, then rerun the complete entry path. |
| SBIN | Entered at 11:59:37; one lot, 750 units, credit ₹8.45/share. | Successful; no missed entry. |
| ULTRACEMCO | Entered at 11:59:51; one lot, 50 units, credit ₹64.30/share. | Successful; no missed entry. |

The three reduced lot counts above are calculated from the allocator used at that time, not promises that orders would fill or broker margin would pass. Other stocks could encounter a capital failure after freshness is fixed because freshness currently stops their path before allocation.

## Code issues that explain the outcomes

### 1. Two different capital estimates

CapitalGuard calculates `spot × 2% × lot_size × lots × 1.20`. CapitalManager actually allocates `spot × lot_size × lots × 12.5%`. At the current wing setting, allocation is approximately **5.208 times** the guard estimate. This explains why BHARTIARTL, OBEROIRLTY and TITAN showed GUARD_PASS and then failed allocation.

| Stock | Guard estimate | Allocator estimate | Configured lots |
|---|---:|---:|---:|
| BHARTIARTL | ₹67,237.20 | ₹350,193.75 | 3 |
| OBEROIRLTY | ₹53,807.04 | ₹280,245.00 | 4 |
| TITAN | ₹55,485.36 | ₹288,986.25 | 3 |

Fix the disagreement by using one margin calculation for preview, guard, lot sizing and allocation, preferably informed by a broker basket-margin check. Neither present approximation establishes actual margin. If fixed lots exceed the configured per-stock cap, produce an explicit sizing rejection or an approved automatic reduction; the current code simply abandons entry. There is no need to raise the per-stock risk cap to explain or address this defect.

### 2. SUZLON rounds with the wrong tick

The entry builder hardcodes ₹0.05 increments. The server’s instrument cache records **₹0.01** for SUZLON26NOV39CE. Its bid/ask was **₹0.97/₹0.99**; the builder rounded the ₹0.99 hedge ask to **₹1.00**, and OrderGuard rejected it as outside that bid/ask range. Use each resolved instrument’s tick size, appropriate side-aware price handling, and validate against refreshed depth. At that snapshot ₹0.99 is the quoted ask and a valid ₹0.01 increment; it is not a guaranteed fill.

A further SUZLON issue warrants a preview check: the guard report lists only three distinct contracts, including one 39CE shared by the call side. This is consistent with nearest-strike snapping collapsing short and long call targets to the same strike. Enforce distinct, correctly ordered short/hedge strikes on both sides and validate the resulting width and premium before considering entry.

### 3. No automatic retry of missed candidates

`start_session()` loops through enabled stocks once, then starts ShiftMonitor. Failed candidates are not revisited during monitoring. Add a candidate queue that retries transient data failures with bounded backoff until 14:00, stopping for filled/pending exposure, hard failures, position limits, kill switch or expiry restrictions. A runner restart currently initiates another entry scan, but it should not be the normal retry mechanism for a live system.

### 4. Important failure details disappear from normal logs

The stdout formatter saves event names but omits `extra` fields containing symbol and exception details. BANKINDIA and M&M emitted `equity_spot_failed`, but the stored line does not reveal whether it was timeout, rate limiting, authentication or another API error. Those causes cannot be assigned conclusively from this evidence. Persist structured ENTRY_FAILED records for spot failures, no expiry, allocation failures and other early returns, including reason, symbol, requested expiry, sizing and exception class, while excluding credentials.

## Earlier attempts today

| Entry pass | Recorded result |
|---|---|
| 10:23:58–10:24:03 | 36 stocks attempted: 35 no_suitable_expiry and one no_spot_price. |
| 11:42:25–11:42:26 | All 19 attempted stocks failed spot acquisition. |
| 11:49:54–11:49:55 | All 19 attempted stocks again failed spot acquisition. |
| 11:55:19 | All 19 attempted stocks returned no_suitable_expiry. |
| 11:59:16–11:59:51 | Expiry resolution succeeded for 17 stocks; BANKINDIA and M&M stopped at spot acquisition; final outcomes above. |

The initial 36-symbol universe additionally included AMBUJACEM, BIOCON, BRITANNIA, CDSL, COCHINSHIP, DLF, DIVISLAB, DRREDDY, FEDERALBNK, GODREJPROP, HDFCAMC, HYUNDAI, PIDILITIND, PRESTIGE, RELIANCE, VBL and VEDL. They were absent from the final 19-symbol configuration, so there was no final-pass opportunity for them. Their original rejection was no_suitable_expiry. ICICIBANK was the original no_spot_price case, identified by correlating its attempt timestamp with the subsequent stdout warning.

The current deployed selector uses the last broker-listed expiry per month. The successful baskets use 23 November, which is 25 days from today and inside the configured 20–45 day window. Earlier “no suitable expiry” events were therefore an earlier selection/data/configuration problem, not proof that November contracts were unavailable for the final pass. The logs do not preserve earlier code/config snapshots, so the precise historical change that cleared this failure cannot be proven. Recheck expiry selection against the instrument master before the next session; do not hardcode weekday assumptions.

The two all-stock spot-failure passes suggest a shared service/data issue, but that is an inference. The exception text is absent from stdout, so expired credentials and API throttling remain unconfirmed possibilities.

## Recommended work order

1. Correct instrument-specific ticks and prevent collapsed call/put wings, starting with SUZLON.
2. Unify capital estimates; preview the proposed smaller sizes for BHARTIARTL, OBEROIRLTY and TITAN under the existing ₹250,000 cap.
3. Add bounded retries for stale quotes and spot fetch failures, with complete revalidation and duplicate-exposure protection.
4. Persist full structured rejection reasons and validate spot, expiry and candidate baskets before the next entry session.
5. For WAAREEENER specifically, preview a freshly calculated one-lot basket during the next permitted window and enter only if freshness, depth, premium, sizing and broker-margin checks all pass.

## Evidence and limits

- `entry_events.json`: all 161 structured events dated 29 October retained from server `logs/live/trades.jsonl`, with original source line numbers. Includes 112 entry attempts across five passes, 12 guard failures and two entry successes.
- `runner_today.log`: dated stdout lines from server `logs/live/runner_stdout.log`, covering the day through the 15:30 monitoring event. The formatter does not retain exception `extra` fields.
- Read-only inspection of server `data/live_state.db`: SBIN and ULTRACEMCO persisted OPEN; NIFTY closed today at its profit target.
- Read-only inspection of deployed `live/condor_runner.py`, `live/guard_layers.py`, `live/capital_manager.py`, `brokers/zerodha.py`, configuration and instrument cache.
- Today's configuration at collection: live mode, ₹2,000,000 total capital, ₹250,000 per stock, ten maximum positions, 09:30–14:00 entry window, 20–45 DTE, 25% minimum premium/width ratio.

This report diagnoses the conditions observed at each attempt. It does not reconstruct an executable historical order book, establish that every blocked trade could have filled later, or calculate hypothetical missed profit.
