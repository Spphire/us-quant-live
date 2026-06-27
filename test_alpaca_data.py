"""
Direct test of Alpaca API data access to diagnose subscription/feed issues.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from vendors.alpaca import AlpacaHttpClient, AlpacaCredentials
import json

# Read config
with open("configs/alpaca_acounts/alpaca_accounts.local.json") as f:
    config = json.load(f)["ALPACA_US_FULL"]

creds = AlpacaCredentials(
    api_key_id=config["api_key"],
    api_secret_key=config["secret_key"],
    trading_base_url="https://paper-api.alpaca.markets",
    data_base_url="https://data.alpaca.markets",
    request_timeout_seconds=60,
    max_retries=3
)

client = AlpacaHttpClient(creds)

print("=" * 60)
print("ALPACA DATA ACCESS TEST")
print("=" * 60)

print("\n[1/4] Testing account access...")
try:
    account = client.get_account()
    print(f"  [OK] Account: {account.get('account_number')}")
    print(f"       Status: {account.get('status')}")
    print(f"       Equity: ${account.get('equity')}")
    print(f"       Base URL: {creds.trading_base_url}")
except Exception as e:
    print(f"  [FAIL] {e}")
    sys.exit(1)

print("\n[2/4] Testing IEX feed - 1 week ago...")
try:
    bars = client.get_stock_bars(
        symbols=["AAPL"],
        start="2026-06-15",
        end="2026-06-20",
        timeframe="1Day",
        feed="iex",
        limit=100
    )
    print(f"  [OK] Got {len(bars)} bars for AAPL with IEX feed")
    if bars:
        print(f"       First bar: {bars[0].get('t')} close=${bars[0].get('c')}")
except Exception as e:
    print(f"  [FAIL] {e}")

print("\n[3/4] Testing IEX feed - today (most restrictive)...")
try:
    bars = client.get_stock_bars(
        symbols=["AAPL"],
        start="2026-06-26",
        end="2026-06-27",
        timeframe="1Day",
        feed="iex",
        limit=100
    )
    print(f"  [OK] Got {len(bars)} bars for AAPL with IEX feed (today)")
    if bars:
        print(f"       Latest bar: {bars[-1].get('t')} close=${bars[-1].get('c')}")
except Exception as e:
    print(f"  [FAIL] {e}")
    print(f"       This is the error blocking full execution.")

print("\n[4/4] Testing SIP feed (should fail on free paper)...")
try:
    bars = client.get_stock_bars(
        symbols=["AAPL"],
        start="2026-06-15",
        end="2026-06-20",
        timeframe="1Day",
        feed="sip",
        limit=100
    )
    print(f"  [OK] Got {len(bars)} bars - you have SIP access!")
except Exception as e:
    print(f"  [EXPECTED FAIL] {e}")
    print(f"       (This is normal for free paper accounts)")

print("\n" + "=" * 60)
print("DIAGNOSIS:")
print("=" * 60)

print("""
If test 2 passed but test 3 failed:
  -> Your account can access HISTORICAL IEX data (> 1 week ago)
  -> But NOT recent data (today/yesterday)
  -> Solution: Use --date with dates > 1 week ago for testing
     OR wait until your paper account gets more permissions
     OR use a live account

If both test 2 and 3 failed:
  -> Your paper account has NO historical bar access at all
  -> Solution: Upgrade Alpaca data subscription ($99/mo)
     OR switch to live account (even with small balance)

If test 3 passed:
  -> Your account has FULL data access!
  -> The earlier error might have been transient or config issue
  -> Try running the full executor again
""")
