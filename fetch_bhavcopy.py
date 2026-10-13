#!/usr/bin/env python3
"""
Fetch missing NSE F&O Bhavcopy files into the cache directory.
Usage:  python fetch_bhavcopy.py --from 2026-07-25 --to 2026-10-10
"""
import io
import sys
import time
import zipfile
import argparse
from datetime import date, timedelta
from pathlib import Path

CACHE_DIR = Path("/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache")

try:
    import requests
except ImportError:
    sys.exit("pip install requests")


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def fetch_one(session, dt: date) -> bytes | None:
    ds = dt.strftime("%Y%m%d")
    urls = [
        f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip",
        f"https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 500:
                return r.content
        except Exception:
            pass
        time.sleep(0.3)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", default="2026-07-25")
    parser.add_argument("--to",   dest="end",   default="2026-10-10")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    # warm up cookies
    try:
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1.5)
    except Exception:
        pass

    days = list(trading_days(start, end))
    need = [d for d in days if not (CACHE_DIR / f"fo_{d.strftime('%Y%m%d')}.csv").exists()]

    print(f"Cache dir : {CACHE_DIR}")
    print(f"Period    : {start} → {end}  ({len(days)} trading days)")
    print(f"Already cached : {len(days) - len(need)}")
    print(f"Need to fetch  : {len(need)}")

    ok = 0
    fail = []

    for dt in need:
        dest = CACHE_DIR / f"fo_{dt.strftime('%Y%m%d')}.csv"
        print(f"  Fetching {dt} ...", end=" ", flush=True)
        raw = fetch_one(session, dt)
        if raw is None:
            print("SKIP (no data / holiday)")
            fail.append(dt)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                fname = z.namelist()[0]
                with z.open(fname) as f:
                    content = f.read()
            dest.write_bytes(content)
            size_kb = len(content) // 1024
            print(f"OK  ({size_kb} KB)")
            ok += 1
        except Exception as e:
            print(f"ERROR: {e}")
            fail.append(dt)
        time.sleep(0.5)

    print(f"\nDone: {ok} downloaded, {len(fail)} failed/holiday.")
    if fail:
        print("  Failed dates:", [str(d) for d in fail])


if __name__ == "__main__":
    main()
