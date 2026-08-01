"""Read-only deployment readiness checks for Alpaca and Longbridge."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dynamic_symbol_pool import _load_credentials_from_accounts_json  # noqa: E402
from vendors import AlpacaHttpClient  # noqa: E402
from vendors.longbridge import LongbridgeCredentials, LongbridgeQuoteClient  # noqa: E402


DEPENDENCIES = {
    "longport": "longport",
    "numpy": "numpy",
    "pandas": "pandas",
    "Pillow": "PIL",
    "pystray": "pystray",
    "requests": "requests",
    "scipy": "scipy",
    "tzdata": "tzdata",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate local configuration and read-only provider access."
    )
    parser.add_argument(
        "--accounts-json-path",
        default=str(PROJECT_ROOT / "configs" / "alpaca_acounts" / "alpaca_accounts.local.json"),
    )
    parser.add_argument("--account-name", default="ALPACA_US_FULL")
    parser.add_argument(
        "--longbridge-config-path",
        default=str(PROJECT_ROOT / "configs" / "longbridge.local.json"),
    )
    parser.add_argument("--symbols", default="AAPL,MSFT")
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Validate files and dependencies without contacting either provider.",
    )
    return parser.parse_args(argv)


def _dependency_snapshot() -> tuple[dict[str, Any], list[str]]:
    rows: dict[str, Any] = {}
    issues: list[str] = []
    for distribution, module in DEPENDENCIES.items():
        available = importlib.util.find_spec(module) is not None
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
        rows[distribution] = {"available": available, "version": version}
        if not available or version is None:
            issues.append(f"missing_dependency:{distribution}")
    return rows, issues


def _alpaca_check(credentials: Any) -> tuple[dict[str, Any], list[str]]:
    client = AlpacaHttpClient(credentials)
    account = client.get_account()
    clock = client.get_clock()
    today = date.today()
    bars = client.get_stock_bars(
        symbols=["AAPL"],
        start=(today - timedelta(days=14)).isoformat(),
        end=(today - timedelta(days=2)).isoformat(),
        timeframe="1Day",
        adjustment="raw",
        feed="sip",
        limit=100,
    )
    status = str(account.get("status") or "").upper()
    blocked_flags = {
        key: bool(account.get(key, False))
        for key in ("account_blocked", "trading_blocked", "trade_suspended_by_user")
    }
    shorting_enabled = bool(account.get("shorting_enabled", False))
    issues: list[str] = []
    if status != "ACTIVE":
        issues.append(f"alpaca_account_status:{status or 'missing'}")
    issues.extend(key for key, value in blocked_flags.items() if value)
    if not shorting_enabled:
        issues.append("alpaca_shorting_disabled")
    if not bars:
        issues.append("alpaca_historical_sip_bars_empty")
    return {
        "status": status,
        "paper": "paper-api.alpaca.markets" in credentials.trading_base_url,
        "shorting_enabled": shorting_enabled,
        "blocked_flags": blocked_flags,
        "market_is_open": bool(clock.get("is_open", False)),
        "historical_sip_bar_count": len(bars),
    }, issues


def _longbridge_check(
    credentials: LongbridgeCredentials,
    symbols: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    client = LongbridgeQuoteClient(credentials)
    try:
        coverage = client.check_symbol_coverage(symbols)
    finally:
        client.close()
    quote_level = str(coverage.get("quote_level") or "")
    quote_level_entries = [entry.strip() for entry in quote_level.split(";") if entry.strip()]
    us_nbbo_entry = next(
        (
            entry
            for entry in quote_level_entries
            if entry.startswith("USAA:") and "NBBO" in entry
        ),
        "",
    )
    us_nbbo_reported = bool(us_nbbo_entry)
    covered_count = int(coverage.get("covered_count") or 0)
    issues: list[str] = []
    if not us_nbbo_reported:
        issues.append("longbridge_us_nbbo_entitlement_missing")
    if coverage.get("status") != "pass":
        issues.append("longbridge_coverage_request_failed")
    if covered_count != len(symbols):
        issues.append("longbridge_sample_coverage_incomplete")
    return {
        "us_nbbo_reported": us_nbbo_reported,
        "us_nbbo_quote_level": us_nbbo_entry,
        "requested_count": len(symbols),
        "covered_count": covered_count,
        "uncovered_symbols": list(coverage.get("uncovered_symbols") or []),
        "request_error_count": len(coverage.get("errors") or []),
    }, issues


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = sorted(
        {token.strip().upper() for token in str(args.symbols).split(",") if token.strip()}
    )
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "config_only" if args.config_only else "live_read_only",
        "python": sys.version.split()[0],
        "checks": {},
        "issues": [],
    }

    dependencies, dependency_issues = _dependency_snapshot()
    result["checks"]["dependencies"] = dependencies
    result["issues"].extend(dependency_issues)

    try:
        alpaca_credentials = _load_credentials_from_accounts_json(
            path=Path(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url="https://data.alpaca.markets",
            request_timeout_seconds=60.0,
            max_retries=2,
        )
        result["checks"]["alpaca_config"] = {
            "valid": True,
            "account_name": str(args.account_name),
            "trading_base_url": alpaca_credentials.trading_base_url,
        }
    except Exception as exc:
        result["checks"]["alpaca_config"] = {
            "valid": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        result["issues"].append("alpaca_config_invalid")
        alpaca_credentials = None

    try:
        longbridge_credentials = LongbridgeCredentials.from_sources(
            args.longbridge_config_path
        )
        result["checks"]["longbridge_config"] = {"valid": True}
    except Exception as exc:
        result["checks"]["longbridge_config"] = {
            "valid": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        result["issues"].append("longbridge_config_invalid")
        longbridge_credentials = None

    if not args.config_only and alpaca_credentials is not None:
        try:
            snapshot, issues = _alpaca_check(alpaca_credentials)
            result["checks"]["alpaca_access"] = snapshot
            result["issues"].extend(issues)
        except Exception as exc:
            result["checks"]["alpaca_access"] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            result["issues"].append("alpaca_access_failed")

    if not args.config_only and longbridge_credentials is not None:
        try:
            snapshot, issues = _longbridge_check(longbridge_credentials, symbols)
            result["checks"]["longbridge_access"] = snapshot
            result["issues"].extend(issues)
        except Exception as exc:
            result["checks"]["longbridge_access"] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            result["issues"].append("longbridge_access_failed")

    result["issues"] = sorted(set(str(issue) for issue in result["issues"]))
    result["status"] = "pass" if not result["issues"] else "fail"
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
