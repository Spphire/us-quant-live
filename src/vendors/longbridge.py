from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class LongbridgeQuoteError(RuntimeError):
    """Raised when Longbridge cannot provide an execution-safe quote."""


@dataclass(slots=True, frozen=True, repr=False)
class LongbridgeCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    enable_overnight: bool = False

    @classmethod
    def from_sources(cls, config_path: str | Path | None = None) -> "LongbridgeCredentials":
        env_values = {
            "app_key": os.getenv("LONGPORT_APP_KEY", "").strip(),
            "app_secret": os.getenv("LONGPORT_APP_SECRET", "").strip(),
            "access_token": os.getenv("LONGPORT_ACCESS_TOKEN", "").strip(),
        }
        if all(env_values.values()):
            return cls(
                **env_values,
                enable_overnight=_parse_bool(os.getenv("LONGPORT_ENABLE_OVERNIGHT", "false")),
            )

        path = Path(config_path) if config_path else None
        if path is None or not path.exists():
            raise LongbridgeQuoteError(
                "Missing Longbridge credentials. Set LONGPORT_APP_KEY/LONGPORT_APP_SECRET/"
                "LONGPORT_ACCESS_TOKEN or provide --longbridge-config-path."
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LongbridgeQuoteError(f"Unable to read Longbridge config {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise LongbridgeQuoteError(f"Longbridge config must be a JSON object: {path}")

        values = {
            "app_key": str(payload.get("app_key") or "").strip(),
            "app_secret": str(payload.get("app_secret") or "").strip(),
            "access_token": str(payload.get("access_token") or "").strip(),
        }
        missing = sorted(key for key, value in values.items() if not value)
        if missing:
            raise LongbridgeQuoteError(
                f"Longbridge config {path} is missing required field(s): {', '.join(missing)}"
            )
        return cls(
            **values,
            enable_overnight=_parse_bool(payload.get("enable_overnight", False)),
        )


class LongbridgeQuoteClient:
    """Streaming US level-1 quote cache used for sizing and order submission."""

    provider_name = "longbridge"
    feed_name = "us_lv1_nbbo"

    def __init__(
        self,
        credentials: LongbridgeCredentials,
        *,
        warmup_timeout_seconds: float = 8.0,
        max_quote_age_seconds: float = 5.0,
        max_spread_bps: float = 150.0,
        max_subscriptions: int = 500,
        context_factory: Callable[[LongbridgeCredentials], Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._warmup_timeout_seconds = max(0.1, float(warmup_timeout_seconds))
        self._max_quote_age_seconds = max(0.1, float(max_quote_age_seconds))
        self._max_spread_bps = max(0.0, float(max_spread_bps))
        self._max_subscriptions = max(1, int(max_subscriptions))
        self._context_factory = context_factory
        self._context: Any | None = None
        self._sub_type: Any | None = None
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._quotes: dict[str, dict[str, Any]] = {}
        self._depths: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._quote_level = ""
        self._packages: list[dict[str, Any]] = []
        self._started_at_utc: str | None = None
        self._snapshot_refresh_attempt_count = 0
        self._snapshot_refresh_symbol_count = 0
        self._snapshot_refresh_failure_count = 0
        self._snapshot_refresh_elapsed_seconds = 0.0
        self._snapshot_refresh_depth_elapsed_seconds = 0.0
        self._snapshot_refresh_depth_work_seconds = 0.0
        self._snapshot_refresh_multi_symbol_count = 0
        self._snapshot_refresh_multi_symbol_depth_elapsed_seconds = 0.0
        self._snapshot_refresh_multi_symbol_depth_work_seconds = 0.0
        self._snapshot_refresh_max_requested_symbols = 0
        self._snapshot_refresh_max_depth_workers = 0
        self._snapshot_refresh_failure_history: list[dict[str, Any]] = []
        self._last_snapshot_refresh: dict[str, Any] = {}

    def check_symbol_coverage(
        self,
        symbols: Sequence[str],
        *,
        chunk_size: int = 500,
    ) -> dict[str, Any]:
        """Return a non-streaming coverage snapshot for a candidate universe."""

        requested = _normalize_symbols(symbols)
        effective_chunk_size = max(1, min(500, int(chunk_size)))
        self._ensure_context()
        rows_by_symbol: dict[str, dict[str, Any]] = {
            symbol: {
                "symbol": symbol,
                "provider_symbol": _provider_symbol(symbol),
                "returned": False,
                "covered": False,
                "permanently_unavailable": False,
                "coverage_reason": "missing_quote",
                "last_price": None,
                "quote_timestamp_utc": None,
                "trade_status": "",
            }
            for symbol in requested
        }
        errors: list[dict[str, Any]] = []
        chunks = [
            requested[offset : offset + effective_chunk_size]
            for offset in range(0, len(requested), effective_chunk_size)
        ]
        for chunk_index, chunk in enumerate(chunks, start=1):
            provider_symbols = [_provider_symbol(symbol) for symbol in chunk]
            try:
                quotes = list(self._context.quote(provider_symbols) or [])
            except Exception as exc:
                errors.append(
                    {
                        "chunk_index": chunk_index,
                        "requested_symbol_count": len(chunk),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                for symbol in chunk:
                    rows_by_symbol[symbol]["coverage_reason"] = "quote_request_error"
                continue

            for quote in quotes:
                provider_symbol = str(getattr(quote, "symbol", "") or "").strip().upper()
                symbol = _base_symbol(provider_symbol)
                if symbol not in rows_by_symbol:
                    continue
                last_price = _positive_float(getattr(quote, "last_done", None))
                trade_status = str(getattr(quote, "trade_status", "") or "")
                status_name = trade_status.rsplit(".", 1)[-1].strip().lower()
                permanently_unavailable = status_name in {"delisted", "codemoved"}
                covered = last_price is not None and not permanently_unavailable
                if permanently_unavailable:
                    coverage_reason = f"trade_status_{status_name}"
                elif last_price is None:
                    coverage_reason = "invalid_last_price"
                else:
                    coverage_reason = "covered"
                rows_by_symbol[symbol].update(
                    {
                        "returned": True,
                        "covered": bool(covered),
                        "permanently_unavailable": bool(permanently_unavailable),
                        "coverage_reason": coverage_reason,
                        "last_price": last_price,
                        "quote_timestamp_utc": _datetime_to_utc(
                            getattr(quote, "timestamp", None)
                        ),
                        "trade_status": trade_status,
                    }
                )

        rows = [rows_by_symbol[symbol] for symbol in requested]
        covered_symbols = [row["symbol"] for row in rows if row["covered"]]
        permanently_unavailable_symbols = [
            row["symbol"] for row in rows if row["permanently_unavailable"]
        ]
        uncovered_symbols = [row["symbol"] for row in rows if not row["covered"]]
        returned_symbols = [row["symbol"] for row in rows if row["returned"]]
        return {
            "schema_version": "1.0",
            "artifact_type": "longbridge_symbol_coverage",
            "collected_at_utc": _utc_now(),
            "provider": self.provider_name,
            "feed": self.feed_name,
            "quote_level": self._quote_level,
            "quote_packages": list(self._packages),
            "chunk_size": effective_chunk_size,
            "chunk_count": len(chunks),
            "requested_count": len(requested),
            "returned_count": len(returned_symbols),
            "covered_count": len(covered_symbols),
            "permanently_unavailable_count": len(permanently_unavailable_symbols),
            "uncovered_count": len(uncovered_symbols),
            "requested_symbols": requested,
            "returned_symbols": returned_symbols,
            "covered_symbols": covered_symbols,
            "permanently_unavailable_symbols": permanently_unavailable_symbols,
            "uncovered_symbols": uncovered_symbols,
            "errors": errors,
            "rows": rows,
            "status": "error" if errors else "pass",
            "coverage_rule": (
                "quote returned with last_done > 0 and trade_status not in "
                "{Delisted, CodeMoved}; temporary Halted status remains covered"
            ),
        }

    def start(self, symbols: Sequence[str]) -> dict[str, Any]:
        requested = _normalize_symbols(symbols)
        if len(requested) > self._max_subscriptions:
            raise LongbridgeQuoteError(
                f"Longbridge execution subscription requires {len(requested)} symbols; "
                f"configured maximum is {self._max_subscriptions}."
            )
        self._ensure_context()
        provider_symbols = [_provider_symbol(symbol) for symbol in requested]
        new_provider_symbols = [
            symbol for symbol in provider_symbols if _base_symbol(symbol) not in self._subscribed
        ]
        if new_provider_symbols:
            self._context.subscribe(
                new_provider_symbols,
                [self._sub_type.Quote, self._sub_type.Depth],
            )
            with self._lock:
                self._subscribed.update(_base_symbol(symbol) for symbol in new_provider_symbols)

        deadline = time.monotonic() + self._warmup_timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                depth_ready = requested and all(symbol in self._depths for symbol in requested)
                quote_ready = requested and all(symbol in self._quotes for symbol in requested)
            if not requested or (depth_ready and quote_ready):
                break
            time.sleep(0.05)

        missing_depth, missing_quote = self._missing_cache_symbols(requested)
        if missing_depth or missing_quote:
            raise LongbridgeQuoteError(
                "Longbridge quote warmup incomplete: "
                f"missing_depth={missing_depth}, missing_quote={missing_quote}"
            )
        return self.health_snapshot(requested_symbols=requested)

    def get_latest_quotes(
        self,
        symbols: Sequence[str],
        *,
        feed: str | None = None,
        require_fresh: bool = False,
        allow_wide_spread: bool = False,
    ) -> dict[str, dict[str, Any]]:
        del feed
        requested = _normalize_symbols(symbols)
        if require_fresh and requested:
            with self._refresh_lock:
                refresh_history: list[dict[str, Any]] = []
                for refresh_round in range(1, 4):
                    refresh_symbols = self._snapshot_refresh_candidates(requested)
                    if not refresh_symbols:
                        break
                    refresh_result = self._refresh_snapshots(refresh_symbols)
                    refresh_result["refresh_round"] = int(refresh_round)
                    refresh_history.append(refresh_result)
                if refresh_history:
                    latest = dict(refresh_history[-1])
                    latest["refresh_round_count"] = int(len(refresh_history))
                    latest["refresh_history"] = refresh_history
                    self._last_snapshot_refresh = latest
        out: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        now_monotonic = time.monotonic()
        with self._lock:
            for symbol in requested:
                depth = dict(self._depths.get(symbol) or {})
                quote = dict(self._quotes.get(symbol) or {})
                if not depth:
                    errors.append(f"{symbol}: missing depth")
                    continue
                local_age_ms = max(
                    0.0,
                    (now_monotonic - float(depth.get("received_monotonic") or 0.0)) * 1000.0,
                )
                row = {
                    "bp": depth.get("bid_price"),
                    "bs": depth.get("bid_size"),
                    "ap": depth.get("ask_price"),
                    "as": depth.get("ask_size"),
                    "bx": "LONGPORT",
                    "ax": "LONGPORT",
                    "t": depth.get("received_at_utc"),
                    "provider": self.provider_name,
                    "feed": self.feed_name,
                    "provider_symbol": _provider_symbol(symbol),
                    "depth_received_at_utc": depth.get("received_at_utc"),
                    "depth_local_age_ms": round(local_age_ms, 3),
                    "depth_source": str(depth.get("source") or "stream"),
                    "depth_snapshot_refreshed_at_utc": depth.get("snapshot_refreshed_at_utc"),
                    "last_trade_price": quote.get("last_trade_price"),
                    "last_trade_timestamp_utc": quote.get("last_trade_timestamp_utc"),
                    "quote_received_at_utc": quote.get("received_at_utc"),
                    "quote_source": str(quote.get("source") or "stream"),
                    "quote_snapshot_refreshed_at_utc": quote.get("snapshot_refreshed_at_utc"),
                    "trade_status": quote.get("trade_status"),
                    "trade_session": quote.get("trade_session"),
                    "c": [],
                    "z": "",
                }
                validation_error = self._validation_error(
                    row,
                    allow_wide_spread=bool(allow_wide_spread),
                )
                if validation_error:
                    row["validation_error"] = validation_error
                    if require_fresh:
                        errors.append(f"{symbol}: {validation_error}")
                out[symbol] = row
        if require_fresh and errors:
            raise LongbridgeQuoteError("; ".join(errors))
        return out

    def get_latest_trades(
        self,
        symbols: Sequence[str],
        *,
        feed: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        del feed
        requested = _normalize_symbols(symbols)
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for symbol in requested:
                quote = self._quotes.get(symbol)
                if not quote:
                    continue
                price = _positive_float(quote.get("last_trade_price"))
                if price is None:
                    continue
                out[symbol] = {
                    "p": price,
                    "t": quote.get("last_trade_timestamp_utc"),
                    "s": quote.get("last_trade_size"),
                    "provider": self.provider_name,
                    "feed": self.feed_name,
                    "provider_symbol": _provider_symbol(symbol),
                    "received_at_utc": quote.get("received_at_utc"),
                    "trade_status": quote.get("trade_status"),
                    "trade_session": quote.get("trade_session"),
                }
        return out

    def get_reference_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        quotes = self.get_latest_quotes(
            symbols,
            require_fresh=True,
            allow_wide_spread=True,
        )
        out: dict[str, float] = {}
        for symbol, quote in quotes.items():
            bid = _positive_float(quote.get("bp"))
            ask = _positive_float(quote.get("ap"))
            if bid is not None and ask is not None and ask >= bid:
                out[symbol] = (bid + ask) / 2.0
        return out

    def get_marketable_quote(self, symbol: str) -> dict[str, Any]:
        normalized = _normalize_symbols([symbol])
        if not normalized:
            raise LongbridgeQuoteError("Empty symbol requested for execution quote.")
        quotes = self.get_latest_quotes(normalized, require_fresh=True)
        return quotes[normalized[0]]

    def health_snapshot(self, *, requested_symbols: Sequence[str] | None = None) -> dict[str, Any]:
        requested = _normalize_symbols(requested_symbols or sorted(self._subscribed))
        missing_depth, missing_quote = self._missing_cache_symbols(requested)
        quotes = self.get_latest_quotes(requested, require_fresh=False) if requested else {}
        validation_errors = {
            symbol: str(row.get("validation_error"))
            for symbol, row in quotes.items()
            if row.get("validation_error")
        }
        spreads = [_spread_bps(row) for row in quotes.values()]
        spreads = [value for value in spreads if value is not None]
        ages = [
            float(row.get("depth_local_age_ms"))
            for row in quotes.values()
            if row.get("depth_local_age_ms") is not None
        ]
        nbbo_reported = "USAA" in self._quote_level and "NBBO" in self._quote_level
        us_quote_packages = [
            item
            for item in self._packages
            if str(item.get("key") or "").startswith("US_QBBO")
        ]
        package_remaining_days = [
            value
            for value in (_remaining_days(item.get("end_at_utc")) for item in us_quote_packages)
            if value is not None
        ]
        min_package_remaining_days = min(package_remaining_days, default=None)
        entitlement_attention = (
            not nbbo_reported
            or (
                min_package_remaining_days is not None
                and min_package_remaining_days <= 7.0
            )
        )
        validation_ok = not missing_depth and not missing_quote and not validation_errors
        return {
            "schema_version": "1.0",
            "collected_at_utc": _utc_now(),
            "provider": self.provider_name,
            "feed": self.feed_name,
            "started_at_utc": self._started_at_utc,
            "quote_level": self._quote_level,
            "quote_packages": list(self._packages),
            "us_nbbo_reported": nbbo_reported,
            "us_quote_package_end_at_utc": [
                item.get("end_at_utc") for item in us_quote_packages
            ],
            "us_quote_package_min_remaining_days": min_package_remaining_days,
            "entitlement_attention": entitlement_attention,
            "requested_symbol_count": len(requested),
            "subscribed_symbol_count": len(self._subscribed),
            "depth_cache_count": len(self._depths),
            "quote_cache_count": len(self._quotes),
            "missing_depth_symbols": missing_depth,
            "missing_quote_symbols": missing_quote,
            "validation_errors": validation_errors,
            "valid_quote_count": len(quotes) - len(validation_errors),
            "max_depth_local_age_ms": max(ages, default=None),
            "snapshot_refresh_attempt_count": int(self._snapshot_refresh_attempt_count),
            "snapshot_refresh_symbol_count": int(self._snapshot_refresh_symbol_count),
            "snapshot_refresh_failure_count": int(self._snapshot_refresh_failure_count),
            "snapshot_refresh_elapsed_seconds": round(
                float(self._snapshot_refresh_elapsed_seconds), 6
            ),
            "snapshot_refresh_depth_elapsed_seconds": round(
                float(self._snapshot_refresh_depth_elapsed_seconds), 6
            ),
            "snapshot_refresh_depth_work_seconds": round(
                float(self._snapshot_refresh_depth_work_seconds), 6
            ),
            "snapshot_refresh_depth_parallel_speedup_ratio": (
                float(
                    self._snapshot_refresh_depth_work_seconds
                    / self._snapshot_refresh_depth_elapsed_seconds
                )
                if self._snapshot_refresh_depth_elapsed_seconds > 0.0
                else None
            ),
            "snapshot_refresh_multi_symbol_count": int(
                self._snapshot_refresh_multi_symbol_count
            ),
            "snapshot_refresh_multi_symbol_depth_elapsed_seconds": round(
                float(self._snapshot_refresh_multi_symbol_depth_elapsed_seconds), 6
            ),
            "snapshot_refresh_multi_symbol_depth_work_seconds": round(
                float(self._snapshot_refresh_multi_symbol_depth_work_seconds), 6
            ),
            "snapshot_refresh_multi_symbol_parallel_speedup_ratio": (
                float(
                    self._snapshot_refresh_multi_symbol_depth_work_seconds
                    / self._snapshot_refresh_multi_symbol_depth_elapsed_seconds
                )
                if self._snapshot_refresh_multi_symbol_depth_elapsed_seconds > 0.0
                else None
            ),
            "snapshot_refresh_max_requested_symbols": int(
                self._snapshot_refresh_max_requested_symbols
            ),
            "snapshot_refresh_max_depth_workers": int(
                self._snapshot_refresh_max_depth_workers
            ),
            "snapshot_refresh_failure_history": [
                dict(item) for item in self._snapshot_refresh_failure_history
            ],
            "last_snapshot_refresh": dict(self._last_snapshot_refresh),
            "max_spread_bps_observed": max(spreads, default=None),
            "warmup_timeout_seconds": self._warmup_timeout_seconds,
            "max_quote_age_seconds": self._max_quote_age_seconds,
            "snapshot_proactive_refresh_age_seconds": max(
                0.05,
                self._max_quote_age_seconds
                - min(3.0, self._max_quote_age_seconds * 0.30),
            ),
            "max_spread_bps": self._max_spread_bps,
            "max_subscriptions": self._max_subscriptions,
            "status": (
                "pass" if validation_ok and not entitlement_attention else "attention"
            ),
        }

    def close(self) -> None:
        context = self._context
        if context is None:
            return
        provider_symbols = [_provider_symbol(symbol) for symbol in sorted(self._subscribed)]
        if provider_symbols:
            try:
                context.unsubscribe(provider_symbols, [self._sub_type.Quote, self._sub_type.Depth])
            except Exception:
                pass
        self._context = None

    def _ensure_context(self) -> None:
        if self._context is not None:
            return
        if self._context_factory is not None:
            context = self._context_factory(self._credentials)
            sub_type = getattr(context, "sub_type", None)
            if sub_type is None:
                raise LongbridgeQuoteError("Test/context factory must expose sub_type.")
        else:
            try:
                from longport.openapi import Config, QuoteContext, SubType
            except ImportError as exc:
                raise LongbridgeQuoteError(
                    "The longport package is required for Longbridge execution quotes."
                ) from exc
            config = Config.from_apikey(
                self._credentials.app_key,
                self._credentials.app_secret,
                self._credentials.access_token,
                enable_overnight=bool(self._credentials.enable_overnight),
                enable_print_quote_packages=False,
            )
            context = QuoteContext(config)
            sub_type = SubType
        context.set_on_quote(self._on_quote)
        context.set_on_depth(self._on_depth)
        self._context = context
        self._sub_type = sub_type
        self._started_at_utc = _utc_now()
        try:
            self._quote_level = str(context.quote_level())
        except Exception as exc:
            raise LongbridgeQuoteError(f"Unable to read Longbridge quote entitlement: {exc}") from exc
        if "USAA" not in self._quote_level or "NBBO" not in self._quote_level:
            raise LongbridgeQuoteError(
                "Longbridge account does not report the required US NBBO quote entitlement."
            )
        try:
            self._packages = [_package_payload(item) for item in context.quote_package_details()]
        except Exception:
            self._packages = []

    def _on_quote(self, provider_symbol: str, event: Any) -> None:
        symbol = _base_symbol(provider_symbol)
        received_at_utc = _utc_now(milliseconds=True)
        payload = {
            "last_trade_price": _positive_float(getattr(event, "last_done", None)),
            "last_trade_size": _positive_float(getattr(event, "current_volume", None)),
            "last_trade_timestamp_utc": _datetime_to_utc(getattr(event, "timestamp", None)),
            "trade_status": str(getattr(event, "trade_status", "")),
            "trade_session": str(getattr(event, "trade_session", "")),
            "received_at_utc": received_at_utc,
            "received_monotonic": time.monotonic(),
            "source": "stream",
        }
        with self._lock:
            self._quotes[symbol] = payload

    def _on_depth(self, provider_symbol: str, event: Any) -> None:
        symbol = _base_symbol(provider_symbol)
        bids = list(getattr(event, "bids", None) or [])
        asks = list(getattr(event, "asks", None) or [])
        bid = _first_positive_level(bids)
        ask = _first_positive_level(asks)
        payload = {
            "bid_price": _positive_float(getattr(bid, "price", None)) if bid else None,
            "bid_size": _positive_float(getattr(bid, "volume", None)) if bid else None,
            "ask_price": _positive_float(getattr(ask, "price", None)) if ask else None,
            "ask_size": _positive_float(getattr(ask, "volume", None)) if ask else None,
            "received_at_utc": _utc_now(milliseconds=True),
            "received_monotonic": time.monotonic(),
            "source": "stream",
        }
        with self._lock:
            self._depths[symbol] = payload

    def _validation_error(
        self,
        quote: Mapping[str, Any],
        *,
        allow_wide_spread: bool = False,
    ) -> str | None:
        bid = _positive_float(quote.get("bp"))
        ask = _positive_float(quote.get("ap"))
        if bid is None or ask is None:
            return "missing_positive_bid_or_ask"
        if ask < bid:
            return "crossed_quote"
        age_ms = _positive_float(quote.get("depth_local_age_ms")) or 0.0
        if age_ms > self._max_quote_age_seconds * 1000.0:
            return f"stale_quote_age_ms={age_ms:.3f}"
        spread_bps = _spread_bps(quote)
        if spread_bps is None:
            return "invalid_spread"
        if (
            not allow_wide_spread
            and self._max_spread_bps > 0.0
            and spread_bps > self._max_spread_bps
        ):
            return f"spread_bps={spread_bps:.3f}_exceeds_{self._max_spread_bps:.3f}"
        trade_status = str(quote.get("trade_status") or "")
        if trade_status and trade_status != "TradeStatus.Normal":
            return f"trade_status={trade_status}"
        return None

    def _missing_cache_symbols(self, requested: Sequence[str]) -> tuple[list[str], list[str]]:
        with self._lock:
            missing_depth = sorted(set(requested) - set(self._depths))
            missing_quote = sorted(set(requested) - set(self._quotes))
        return missing_depth, missing_quote

    def _snapshot_refresh_candidates(self, requested: Sequence[str]) -> list[str]:
        now_monotonic = time.monotonic()
        freshness_headroom_seconds = min(
            3.0,
            self._max_quote_age_seconds * 0.30,
        )
        proactive_refresh_age_seconds = max(
            0.05,
            self._max_quote_age_seconds - freshness_headroom_seconds,
        )
        out: list[str] = []
        with self._lock:
            for symbol in requested:
                depth = self._depths.get(symbol)
                quote = self._quotes.get(symbol)
                if not depth or not quote:
                    out.append(symbol)
                    continue
                received_monotonic = float(depth.get("received_monotonic") or 0.0)
                if now_monotonic - received_monotonic > proactive_refresh_age_seconds:
                    out.append(symbol)
        return out

    def _refresh_snapshots(self, symbols: Sequence[str]) -> dict[str, Any]:
        requested = _normalize_symbols(symbols)
        provider_symbols = [_provider_symbol(symbol) for symbol in requested]
        started_at_utc = _utc_now(milliseconds=True)
        started_monotonic = time.monotonic()
        errors: list[dict[str, str]] = []
        quote_refreshed: set[str] = set()
        depth_refreshed: set[str] = set()
        self._snapshot_refresh_attempt_count += 1
        self._snapshot_refresh_symbol_count += len(requested)

        quote_batch_started_monotonic = time.monotonic()
        try:
            static_quotes = list(self._context.quote(provider_symbols) or [])
            for event in static_quotes:
                provider_symbol = str(getattr(event, "symbol", "") or "").strip().upper()
                symbol = _base_symbol(provider_symbol)
                if symbol not in requested:
                    continue
                self._on_quote(provider_symbol, event)
                with self._lock:
                    self._quotes[symbol]["source"] = "snapshot_refresh"
                    self._quotes[symbol]["snapshot_refreshed_at_utc"] = started_at_utc
                quote_refreshed.add(symbol)
        except Exception as exc:
            errors.append(
                {
                    "scope": "quote_batch",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        quote_batch_elapsed_seconds = time.monotonic() - quote_batch_started_monotonic

        def fetch_depth(
            symbol: str,
            provider_symbol: str,
        ) -> tuple[str, str, Any, Exception | None, float]:
            depth_started_monotonic = time.monotonic()
            try:
                event = self._context.depth(provider_symbol)
                return (
                    symbol,
                    provider_symbol,
                    event,
                    None,
                    time.monotonic() - depth_started_monotonic,
                )
            except Exception as exc:
                return (
                    symbol,
                    provider_symbol,
                    None,
                    exc,
                    time.monotonic() - depth_started_monotonic,
                )

        depth_worker_count = min(8, max(1, len(provider_symbols)))
        depth_started_monotonic = time.monotonic()
        depth_request_elapsed_seconds: list[float] = []
        with ThreadPoolExecutor(max_workers=depth_worker_count) as executor:
            futures = [
                executor.submit(fetch_depth, symbol, provider_symbol)
                for symbol, provider_symbol in zip(requested, provider_symbols)
            ]
            for future in as_completed(futures):
                symbol, provider_symbol, event, error, request_elapsed = future.result()
                depth_request_elapsed_seconds.append(float(request_elapsed))
                if error is not None:
                    errors.append(
                        {
                            "scope": "depth",
                            "symbol": symbol,
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    )
                    continue
                self._on_depth(provider_symbol, event)
                with self._lock:
                    self._depths[symbol]["source"] = "snapshot_refresh"
                    self._depths[symbol]["snapshot_refreshed_at_utc"] = started_at_utc
                depth_refreshed.add(symbol)
        depth_elapsed_seconds = time.monotonic() - depth_started_monotonic
        depth_work_seconds = float(sum(depth_request_elapsed_seconds))

        missing_quote = sorted(set(requested) - quote_refreshed)
        missing_depth = sorted(set(requested) - depth_refreshed)
        if missing_quote:
            errors.append({"scope": "quote_missing", "symbols": ",".join(missing_quote)})
        if missing_depth:
            errors.append({"scope": "depth_missing", "symbols": ",".join(missing_depth)})
        elapsed_seconds = time.monotonic() - started_monotonic
        self._snapshot_refresh_elapsed_seconds += float(elapsed_seconds)
        self._snapshot_refresh_depth_elapsed_seconds += float(depth_elapsed_seconds)
        self._snapshot_refresh_depth_work_seconds += float(depth_work_seconds)
        self._snapshot_refresh_max_requested_symbols = max(
            int(self._snapshot_refresh_max_requested_symbols), len(requested)
        )
        self._snapshot_refresh_max_depth_workers = max(
            int(self._snapshot_refresh_max_depth_workers), int(depth_worker_count)
        )
        if len(requested) > 1:
            self._snapshot_refresh_multi_symbol_count += 1
            self._snapshot_refresh_multi_symbol_depth_elapsed_seconds += float(
                depth_elapsed_seconds
            )
            self._snapshot_refresh_multi_symbol_depth_work_seconds += float(
                depth_work_seconds
            )
        result = {
            "attempted_at_utc": started_at_utc,
            "requested_symbols": requested,
            "quote_refreshed_symbols": sorted(quote_refreshed),
            "depth_refreshed_symbols": sorted(depth_refreshed),
            "errors": errors,
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "quote_batch_elapsed_seconds": round(
                float(quote_batch_elapsed_seconds), 6
            ),
            "depth_phase_elapsed_seconds": round(float(depth_elapsed_seconds), 6),
            "depth_aggregate_work_seconds": round(float(depth_work_seconds), 6),
            "depth_parallel_speedup_ratio": (
                float(depth_work_seconds / depth_elapsed_seconds)
                if depth_elapsed_seconds > 0.0
                else None
            ),
            "depth_request_min_seconds": (
                round(min(depth_request_elapsed_seconds), 6)
                if depth_request_elapsed_seconds
                else None
            ),
            "depth_request_max_seconds": (
                round(max(depth_request_elapsed_seconds), 6)
                if depth_request_elapsed_seconds
                else None
            ),
            "depth_worker_count": int(depth_worker_count),
            "status": "pass" if not errors else "error",
        }
        if errors:
            self._snapshot_refresh_failure_count += 1
            self._snapshot_refresh_failure_history.append(
                {
                    "attempted_at_utc": started_at_utc,
                    "requested_symbols": requested,
                    "errors": [dict(item) for item in errors],
                    "elapsed_seconds": result["elapsed_seconds"],
                    "depth_worker_count": int(depth_worker_count),
                }
            )
            self._snapshot_refresh_failure_history = self._snapshot_refresh_failure_history[-20:]
        return result


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    return sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})


def _provider_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    return normalized if normalized.endswith(".US") else f"{normalized}.US"


def _base_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    return normalized[:-3] if normalized.endswith(".US") else normalized


def _first_positive_level(levels: Sequence[Any]) -> Any | None:
    for level in levels:
        if _positive_float(getattr(level, "price", None)) is not None:
            return level
    return None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None


def _spread_bps(quote: Mapping[str, Any]) -> float | None:
    bid = _positive_float(quote.get("bp"))
    ask = _positive_float(quote.get("ap"))
    if bid is None or ask is None or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 10000.0 if mid > 0.0 else None


def _datetime_to_utc(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _package_payload(item: Any) -> dict[str, Any]:
    return {
        "key": str(getattr(item, "key", "")),
        "name": str(getattr(item, "name", "")),
        "description": str(getattr(item, "description", "")),
        "start_at_utc": _datetime_to_utc(getattr(item, "start_at", None)),
        "end_at_utc": _datetime_to_utc(getattr(item, "end_at", None)),
    }


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _remaining_days(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _utc_now(*, milliseconds: bool = False) -> str:
    timespec = "milliseconds" if milliseconds else "seconds"
    return datetime.now(timezone.utc).isoformat(timespec=timespec)
