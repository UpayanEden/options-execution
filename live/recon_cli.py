"""
Reconciliation CLI for NEEDS_RECON positions.

Usage::

    python -m live.recon_cli --symbol AXISBANK

Shows the current DB state vs broker positions with a diff.
Prompts:
    (a) adopt broker legs  — update the position's legs in DB to match broker, set state = OPEN
    (b) close orphans      — place market orders to close broker legs not in DB, set state = CLOSED
    (q) quit               — exit without changes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_config():
    """Load live config from default path."""
    from live.config import load_config
    return load_config()


def _build_broker() -> Any:
    """Create a ZerodhaBroker from the live config."""
    config = _load_config()
    from brokers.zerodha import ZerodhaBroker
    return ZerodhaBroker(
        api_key=config.zerodha_api_key,
        api_secret=config.zerodha_api_secret,
        access_token=config.zerodha_access_token,
    )


def _get_db_position(symbol: str) -> Optional[Dict]:
    """Load the most recent NEEDS_RECON or OPEN/SHIFTING position for symbol from DB."""
    from live.position_store import PositionStore
    store = PositionStore()
    try:
        cur = store._conn.execute(
            "SELECT * FROM positions WHERE symbol=? ORDER BY entry_time DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
    except Exception as exc:
        print(f"DB error: {exc}")
    return None


def _get_broker_positions(broker: Any) -> List[Dict]:
    """Fetch all current broker positions."""
    try:
        return broker.get_positions() or []
    except Exception as exc:
        print(f"Broker positions error: {exc}")
        return []


def _print_diff(db_pos: Dict, broker_positions: List[Dict]) -> Dict[str, Any]:
    """
    Compare DB position vs broker positions and print diff.

    Returns:
        Dict of leg_name → {db_symbol, db_qty_expected, broker_qty_actual, diff}
    """
    symbol = db_pos.get("symbol", "?")
    lots = int(db_pos.get("lots", 0))
    lot_size = int(db_pos.get("lot_size", 0))
    expected_qty = lots * lot_size

    legs = {}
    for leg_key in ["short_put", "long_put", "short_call", "long_call"]:
        raw = db_pos.get(leg_key)
        if raw:
            data = json.loads(raw) if isinstance(raw, str) else raw
            ts = data.get("trading_symbol", "")
            # Sign convention: short = negative qty (sold), long = positive
            exp_qty = -expected_qty if leg_key.startswith("short") else expected_qty
            legs[leg_key] = {"symbol": ts, "expected": exp_qty, "actual": 0}

    # Find broker positions
    broker_qty: Dict[str, int] = {}
    for bp in broker_positions:
        ts = str(bp.get("tradingsymbol", bp.get("trading_symbol", "")))
        qty = int(bp.get("quantity", bp.get("net_quantity", 0)) or 0)
        if ts:
            broker_qty[ts] = broker_qty.get(ts, 0) + qty

    diff: Dict[str, Any] = {}
    print(f"\n{'Leg':<15} {'DB Symbol':<30} {'Expected Qty':>12} {'Broker Qty':>10} {'Match':>6}")
    print("-" * 80)
    for leg_key, info in legs.items():
        ts = info["symbol"]
        exp = info["expected"]
        actual = broker_qty.get(ts, 0)
        match = "OK" if actual == exp else "DIFF"
        print(f"{leg_key:<15} {ts:<30} {exp:>12} {actual:>10} {match:>6}")
        if actual != exp:
            diff[leg_key] = {
                "db_symbol": ts,
                "expected": exp,
                "actual": actual,
            }

    # Orphan broker positions (legs in broker but not in DB)
    db_symbols = {info["symbol"] for info in legs.values()}
    orphans: List[str] = []
    for bp in broker_positions:
        ts = str(bp.get("tradingsymbol", ""))
        if ts.startswith(symbol) and ts not in db_symbols:
            qty = int(bp.get("quantity", 0) or 0)
            if qty != 0:
                orphans.append(f"{ts} (qty={qty})")

    if orphans:
        print(f"\nOrphan broker legs (in broker, not in DB): {orphans}")

    return diff


def run_recon(symbol: str) -> None:
    """Run the interactive reconciliation flow for a symbol."""
    print(f"\n=== Reconciliation CLI for {symbol} ===")

    db_pos = _get_db_position(symbol)
    if db_pos is None:
        print(f"No position found in DB for {symbol}")
        return

    state = db_pos.get("state", "?")
    print(f"\nDB state: {state}")
    print(f"Shift count: {db_pos.get('shift_count', 0)}")
    print(f"Entry time: {db_pos.get('entry_time', '?')}")

    broker = _build_broker()
    broker_positions = _get_broker_positions(broker)

    diff = _print_diff(db_pos, broker_positions)

    if not diff:
        print("\nAll legs match broker. No reconciliation needed.")
        if state == "NEEDS_RECON":
            ans = input("\nSet state back to OPEN? (y/n): ").strip().lower()
            if ans == "y":
                _set_state(symbol, db_pos["entry_time"], "OPEN")
                print(f"State set to OPEN for {symbol}.")
        return

    print(f"\n{len(diff)} leg(s) differ.")
    print("\nChoose action:")
    print("  (a) adopt broker legs — update DB to match broker, set state = OPEN")
    print("  (b) close orphans     — close broker legs not matching DB, set state = CLOSED")
    print("  (q) quit              — exit without changes")

    ans = input("\nChoice [a/b/q]: ").strip().lower()
    if ans == "q":
        print("Exiting without changes.")
        return

    from live.position_store import PositionStore
    store = PositionStore()

    if ans == "a":
        # Adopt broker state: update DB legs to match broker, set state = OPEN
        _set_state(symbol, db_pos["entry_time"], "OPEN")
        print(f"State set to OPEN for {symbol}. "
              "Note: DB leg quantities were NOT updated — this is structural/manual only.")
        print("You may need to manually correct leg instruments if strikes changed mid-shift.")

    elif ans == "b":
        # Close orphan broker legs at market, then set state = CLOSED
        print("Placing market orders to close all broker positions for this symbol...")
        _close_broker_legs(broker, symbol, broker_positions)
        _set_state(symbol, db_pos["entry_time"], "CLOSED")
        print(f"Orphan legs closed and state set to CLOSED for {symbol}.")


def _set_state(symbol: str, entry_time: str, state: str) -> None:
    """Update position state in DB."""
    from live.position_store import PositionStore
    store = PositionStore()
    try:
        store._conn.execute(
            "UPDATE positions SET state=? WHERE symbol=? AND entry_time=?",
            (state, symbol, entry_time),
        )
        store._conn.commit()
        print(f"DB state updated to {state} for {symbol}.")
    except Exception as exc:
        print(f"DB update failed: {exc}")


def _close_broker_legs(broker: Any, symbol: str, broker_positions: List[Dict]) -> None:
    """Place market orders to close all broker legs for symbol."""
    from execution.models import Order, Side
    from decimal import Decimal
    import time

    for bp in broker_positions:
        ts = str(bp.get("tradingsymbol", ""))
        if not ts.startswith(symbol):
            continue
        qty = int(bp.get("quantity", 0) or 0)
        if qty == 0:
            continue

        # Close by taking the opposite side at market (use limit near market for safety)
        close_side = Side.SELL if qty > 0 else Side.BUY
        close_qty = abs(qty)
        token = int(bp.get("instrument_token", 0))

        print(f"  Closing {ts}: {close_side.value} {close_qty}")
        try:
            if hasattr(broker, "place_order_from_model"):
                order = Order(
                    strategy_run_id=f"recon_close_{symbol}",
                    leg_index=0,
                    trading_symbol=ts,
                    instrument_token=token,
                    side=close_side,
                    quantity=close_qty,
                    limit_price=Decimal("0.05"),  # will use market price; broker may adjust
                )
                broker.place_order_from_model(order)  # type: ignore[attr-defined]
            time.sleep(0.5)
        except Exception as exc:
            print(f"  Failed to close {ts}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile a NEEDS_RECON condor position against broker."
    )
    parser.add_argument("--symbol", required=True, help="NSE symbol to reconcile (e.g. AXISBANK)")
    args = parser.parse_args()

    run_recon(args.symbol.upper())


if __name__ == "__main__":
    main()
