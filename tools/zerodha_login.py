#!/usr/bin/env python3
"""
tools/zerodha_login.py — Zerodha Kite Connect daily login helper.

Zerodha access tokens expire every day at 06:00 IST.
Run this script each morning before starting the trading session.

Flow:
  1. Enter your api_key and api_secret (or load from credentials.env)
  2. Script prints the login URL — open it in your browser
  3. After login Zerodha redirects to your redirect_url with ?request_token=...
  4. Paste the full redirect URL (or just the request_token)
  5. Script exchanges it for an access_token and writes it to credentials.env

Usage:
    python tools/zerodha_login.py
    python tools/zerodha_login.py --env-file config/credentials.env
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Ensure project root is importable ─────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import dotenv_values, set_key
except ImportError:
    print("[ERROR] python-dotenv not installed. Run: pip install python-dotenv")
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("[ERROR] kiteconnect not installed. Run: pip install kiteconnect")
    sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_request_token(raw: str) -> str:
    """
    Extract request_token from a full redirect URL or a bare token string.

    Args:
        raw: Either the full redirect URL or just the request_token value.

    Returns:
        The request_token string.
    """
    raw = raw.strip()
    if raw.startswith("http"):
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        tokens = params.get("request_token") or params.get("request-token")
        if not tokens:
            print(f"[ERROR] No request_token found in URL: {raw}")
            sys.exit(1)
        return tokens[0]
    # Bare token — validate it looks like a hex/alphanum string
    if re.fullmatch(r"[A-Za-z0-9]{10,}", raw):
        return raw
    print(f"[ERROR] Unrecognised input — expected a URL or a request_token string.")
    sys.exit(1)


def _load_env(env_file: Path) -> dict[str, str]:
    """Load existing env file, returning empty dict if missing."""
    if env_file.exists():
        return dict(dotenv_values(env_file))
    return {}


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt the user for input, showing a masked hint for secrets."""
    hint = ""
    if default:
        if secret and len(default) > 6:
            hint = f" [{default[:4]}{'*' * (len(default) - 6)}{default[-2:]}]"
        else:
            hint = f" [{default}]"
    try:
        val = input(f"  {label}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Zerodha Kite Connect daily access token generator."
    )
    parser.add_argument(
        "--env-file",
        default=str(_ROOT / "config" / "credentials.env"),
        help="Path to credentials env file (default: config/credentials.env)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Override ZERODHA_API_KEY (skips prompt if provided)",
    )
    parser.add_argument(
        "--api-secret",
        default=None,
        help="Override ZERODHA_API_SECRET (skips prompt if provided)",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_env(env_file)

    print()
    print("=" * 60)
    print("  Zerodha Kite Connect — Daily Login")
    print("=" * 60)
    print()

    # ── Step 1: Credentials ────────────────────────────────────────────────
    api_key = (
        args.api_key
        or existing.get("ZERODHA_API_KEY", "")
        or os.environ.get("ZERODHA_API_KEY", "")
    )
    api_secret = (
        args.api_secret
        or existing.get("ZERODHA_API_SECRET", "")
        or os.environ.get("ZERODHA_API_SECRET", "")
    )

    print("Step 1 — Enter your Kite Connect app credentials")
    print("  (Find them at: https://developers.kite.trade/apps)")
    print()
    api_key    = _prompt("API Key   ", api_key)
    api_secret = _prompt("API Secret", api_secret, secret=True)

    if not api_key or not api_secret:
        print("\n[ERROR] API key and secret are required.")
        sys.exit(1)

    # ── Step 2: Login URL ──────────────────────────────────────────────────
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print()
    print("Step 2 — Open this URL in your browser and log in with your Zerodha account:")
    print()
    print(f"  {login_url}")
    print()
    print("  After login, Zerodha will redirect you to your app's redirect URL.")
    print("  The redirect URL will look like:")
    print("    https://your-redirect-url.com/?request_token=XXXXXX&action=login&status=success")
    print()

    # ── Step 3: Request token ──────────────────────────────────────────────
    print("Step 3 — Paste the full redirect URL (or just the request_token) below:")
    print()
    raw_input = _prompt("Redirect URL or request_token")

    request_token = _extract_request_token(raw_input)
    print(f"\n  Extracted request_token: {request_token[:6]}{'*' * max(0, len(request_token)-8)}{request_token[-2:]}")

    # ── Step 4: Generate session ───────────────────────────────────────────
    print("\nStep 4 — Exchanging request_token for access_token...")
    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as e:
        print(f"\n[ERROR] Failed to generate session: {e}")
        print("  Common causes:")
        print("  • Wrong api_secret")
        print("  • request_token already used (each token is one-use)")
        print("  • Token expired (tokens expire after a few minutes)")
        sys.exit(1)

    access_token = session_data["access_token"]
    user_name    = session_data.get("user_name", "unknown")
    user_id      = session_data.get("user_id", "unknown")

    print(f"\n  ✓ Logged in as: {user_name} ({user_id})")
    print(f"  ✓ Access token: {access_token[:6]}{'*' * max(0, len(access_token)-8)}{access_token[-2:]}")

    # ── Step 5: Write to env file ──────────────────────────────────────────

    # If file doesn't exist, create it from the example template
    if not env_file.exists():
        example = _ROOT / "config" / "credentials.env.example"
        if example.exists():
            env_file.write_text(example.read_text())
        else:
            env_file.write_text(
                "ZERODHA_API_KEY=\nZERODHA_API_SECRET=\nZERODHA_ACCESS_TOKEN=\nTRADING_MODE=paper\n"
            )

    set_key(str(env_file), "ZERODHA_API_KEY",      api_key,      quote_mode="never")
    set_key(str(env_file), "ZERODHA_API_SECRET",   api_secret,   quote_mode="never")
    set_key(str(env_file), "ZERODHA_ACCESS_TOKEN", access_token, quote_mode="never")
    set_key(str(env_file), "TRADING_MODE",         "paper",      quote_mode="never")

    print()
    print(f"  ✓ Credentials saved to: {env_file}")
    print()

    # ── Step 6: Quick connectivity test ───────────────────────────────────
    print("Step 5 — Running connectivity test...")
    kite.set_access_token(access_token)
    try:
        profile = kite.profile()
        print(f"  ✓ Profile OK   : {profile.get('user_name')} | {profile.get('email')}")
    except Exception as e:
        print(f"  ✗ Profile fetch failed: {e}")

    try:
        margins = kite.margins("equity")
        available = margins.get("net", 0)
        print(f"  ✓ Margins OK   : Available ₹{float(available):,.2f}")
    except Exception as e:
        print(f"  ✗ Margins fetch failed: {e}")

    try:
        positions = kite.positions()
        net_count = len(positions.get("net", []))
        print(f"  ✓ Positions OK : {net_count} open position(s)")
    except Exception as e:
        print(f"  ✗ Positions fetch failed: {e}")

    print()
    print("=" * 60)
    print("  Login complete. You can now run:")
    print("    python run_live.py --mode paper")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
