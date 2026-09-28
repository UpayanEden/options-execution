"""
Zerodha OAuth token helper.
Run this once per trading day to generate a fresh access token.
Stores it in .env or prints it for manual entry.
NEVER logs the token value.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def get_login_url(api_key: str) -> str:
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    return kite.login_url()


def generate_access_token(api_key: str, api_secret: str, request_token: str) -> str:
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    session = kite.generate_session(request_token, api_secret=api_secret)
    return session["access_token"]


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ZERODHA_API_KEY", "")
    api_secret = os.environ.get("ZERODHA_API_SECRET", "")

    if not api_key or not api_secret:
        print("\nZERODHA_API_KEY and ZERODHA_API_SECRET must be set in your .env file.")
        sys.exit(1)

    url = get_login_url(api_key)
    print(f"\nStep 1: Open this URL in your browser and log in:")
    print(f"\n  {url}\n")
    print("Step 2: After login, Zerodha redirects to your callback URL.")
    print("        Copy the 'request_token' from the URL parameters.\n")

    request_token = input("Paste the request_token here: ").strip()
    if not request_token:
        print("No token entered. Exiting.")
        sys.exit(1)

    try:
        access_token = generate_access_token(api_key, api_secret, request_token)
    except Exception as exc:
        print(f"\nFailed to generate access token: {exc}")
        sys.exit(1)

    print("\nAccess token generated successfully.")
    print("Add this to your .env file as:")
    print(f"\n  ZERODHA_ACCESS_TOKEN=<your token>\n")
    print("(Token value not printed here for security. It was returned by the API.)")
    print("Check your .env file — write it there now before it expires.\n")

    # Optionally write to .env
    env_path = Path(".env")
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
        updated = []
        found = False
        for line in lines:
            if line.startswith("ZERODHA_ACCESS_TOKEN="):
                updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}\n")
                found = True
            else:
                updated.append(line)
        if not found:
            updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}\n")
        env_path.write_text("".join(updated))
        print(f".env updated with new access token.")
    else:
        print(f"No .env file found. Create one from settings.example.env and add the token manually.")


if __name__ == "__main__":
    main()
