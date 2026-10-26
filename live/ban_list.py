"""
NSE F&O ban list fetcher and cache.

URL: https://nsearchives.nseindia.com/content/fo/fo_secban.csv
Requires browser-like User-Agent + warm-up request to nseindia.com.

Usage::

    from live.ban_list import is_banned, refresh_if_stale
    refresh_if_stale()
    if is_banned("AXISBANK"):
        ...
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date
from threading import Lock
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Module-level cache: (cache_date, set_of_banned_symbols)
_BAN_CACHE: tuple[Optional[date], Set[str]] = (None, set())
_BAN_LOCK = Lock()
_LAST_FETCH: float = 0.0

_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
_WARMUP_URL = "https://www.nseindia.com"
_CACHE_TTL_SECONDS = 1800  # 30 minutes
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"


def fetch_ban_list() -> Set[str]:
    """
    Fetch and return the current F&O ban list as a set of uppercase symbols.

    Performs a warm-up GET to nseindia.com first to acquire session cookies,
    then fetches the ban CSV.

    Returns:
        Set of uppercase symbol strings currently on the ban list.
        Returns the last cached set on failure.
    """
    global _BAN_CACHE, _LAST_FETCH

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com",
        "Connection": "keep-alive",
    }

    try:
        session = __import__("requests").Session()
        session.headers.update(headers)

        # Warm-up request to get cookies
        session.get(_WARMUP_URL, timeout=10)
        time.sleep(0.5)

        resp = session.get(_BAN_URL, timeout=10)
        resp.raise_for_status()

        banned: Set[str] = set()
        reader = csv.reader(io.StringIO(resp.text))
        next(reader, None)  # skip header
        for row in reader:
            if row:
                symbol = row[0].strip().upper()
                if symbol:
                    banned.add(symbol)

        _LAST_FETCH = time.time()
        with _BAN_LOCK:
            _BAN_CACHE = (date.today(), banned)

        logger.info("ban_list_fetched", extra={"count": len(banned)})
        return banned

    except Exception as exc:
        logger.warning("ban_list_fetch_failed", extra={"error": str(exc)})
        # Return last cached set
        with _BAN_LOCK:
            return _BAN_CACHE[1]


def is_banned(symbol: str) -> bool:
    """
    Return True if symbol is currently in the F&O ban list.

    Args:
        symbol: NSE symbol to check (case-insensitive).

    Returns:
        True if symbol is on the ban list, False otherwise.
    """
    with _BAN_LOCK:
        _, banned_set = _BAN_CACHE
    return symbol.upper() in banned_set


def refresh_if_stale() -> None:
    """
    Re-fetch the ban list if the cache is from a previous date or older than 30 minutes.

    Safe to call frequently — will skip the fetch if cache is still fresh.
    """
    with _BAN_LOCK:
        cache_date, _ = _BAN_CACHE

    now = time.time()
    today = date.today()

    # Stale if: never fetched, date changed, or TTL expired
    if (cache_date is None
            or cache_date != today
            or (now - _LAST_FETCH) >= _CACHE_TTL_SECONDS):
        fetch_ban_list()
