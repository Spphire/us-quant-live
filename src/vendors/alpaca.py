from __future__ import annotations

import json
import os
import ssl
import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen


class AlpacaRequestError(RuntimeError):
    """Raised when an Alpaca API request fails."""


@dataclass(slots=True, frozen=True)
class AlpacaCredentials:
    """Credentials and base URLs required for Alpaca requests."""

    api_key_id: str
    api_secret_key: str
    trading_base_url: str = "https://paper-api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"
    request_timeout_seconds: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "AlpacaCredentials":
        _load_local_env_file()
        api_key_id = os.getenv("ALPACA_API_KEY_ID")
        api_secret_key = os.getenv("ALPACA_API_SECRET_KEY")
        if not api_key_id or not api_secret_key:
            raise AlpacaRequestError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY_ID and "
                "ALPACA_API_SECRET_KEY before running collectors."
            )

        return cls(
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            trading_base_url=os.getenv(
                "ALPACA_TRADING_BASE_URL",
                "https://paper-api.alpaca.markets",
            ).rstrip("/"),
            data_base_url=os.getenv(
                "ALPACA_DATA_BASE_URL",
                "https://data.alpaca.markets",
            ).rstrip("/"),
            request_timeout_seconds=float(os.getenv("ALPACA_HTTP_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("ALPACA_HTTP_MAX_RETRIES", "2")),
        )


class AlpacaHttpClient:
    """Small HTTP client for Alpaca trading and market data endpoints."""

    def __init__(self, credentials: AlpacaCredentials) -> None:
        self._credentials = credentials
        self._audit_log_path: Path | None = None
        self._audit_lock = threading.Lock()
        self._audit_seq = 0

    def set_audit_log_path(self, path: str | Path | None) -> None:
        """Enable per-request JSONL audit logging.

        The log intentionally excludes authentication headers.  Large response
        bodies are recorded by byte size, sha256, and a short preview so the
        trading path does not duplicate large market-data payloads.
        """
        self._audit_log_path = None if path is None else Path(path)
        if self._audit_log_path is not None:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def list_assets(
        self,
        *,
        status: str = "active",
        asset_class: str = "us_equity",
    ) -> list[dict[str, Any]]:
        payload = self._get_trading(
            "/v2/assets",
            {"status": status, "asset_class": asset_class},
        )
        if not isinstance(payload, list):
            raise AlpacaRequestError("Unexpected Alpaca assets payload shape.")
        return payload

    def get_account(self) -> dict[str, Any]:
        payload = self._get_trading("/v2/account", {})
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca account payload shape.")
        return payload

    def get_account_configurations(self) -> dict[str, Any]:
        payload = self._get_trading("/v2/account/configurations", {})
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca account-configurations payload shape.")
        return payload

    def get_clock(self) -> dict[str, Any]:
        payload = self._get_trading("/v2/clock", {})
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca clock payload shape.")
        return payload

    def get_calendar(self, *, start: str, end: str) -> list[dict[str, Any]]:
        payload = self._get_trading("/v2/calendar", {"start": str(start), "end": str(end)})
        if not isinstance(payload, list):
            raise AlpacaRequestError("Unexpected Alpaca calendar payload shape.")
        return payload

    def get_portfolio_history(
        self,
        *,
        period: str | None = None,
        timeframe: str | None = None,
        intraday_reporting: str | None = None,
        pnl_reset: str | None = None,
        start: str | None = None,
        end: str | None = None,
        cashflow_types: str | None = None,
        extended_hours: bool | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        for key, value in {
            "period": period,
            "timeframe": timeframe,
            "intraday_reporting": intraday_reporting,
            "pnl_reset": pnl_reset,
            "start": start,
            "end": end,
            "cashflow_types": cashflow_types,
        }.items():
            if value not in (None, ""):
                query[key] = str(value)
        if extended_hours is not None:
            query["extended_hours"] = str(bool(extended_hours)).lower()
        payload = self._get_trading("/v2/account/portfolio/history", query)
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca portfolio-history payload shape.")
        return payload

    def list_positions(self) -> list[dict[str, Any]]:
        payload = self._get_trading("/v2/positions", {})
        if not isinstance(payload, list):
            raise AlpacaRequestError("Unexpected Alpaca positions payload shape.")
        return payload

    def list_orders(
        self,
        *,
        status: str = "open",
        limit: int = 500,
        direction: str = "desc",
        nested: bool = False,
        after: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "status": str(status),
            "limit": int(limit),
            "direction": str(direction),
            "nested": str(bool(nested)).lower(),
        }
        if after:
            query["after"] = str(after)
        if until:
            query["until"] = str(until)
        payload = self._get_trading("/v2/orders", query)
        if not isinstance(payload, list):
            raise AlpacaRequestError("Unexpected Alpaca orders payload shape.")
        return payload

    def list_orders_all_pages(
        self,
        *,
        status: str = "all",
        limit: int = 500,
        direction: str = "desc",
        nested: bool = False,
        after: str | None = None,
        until: str | None = None,
        max_pages: int = 20,
        overlap_rows: int = 3,
    ) -> dict[str, Any]:
        """Return order pages plus de-duplicated orders.

        Alpaca caps list_orders at 500 rows.  For descending scans, page by
        moving the exclusive ``until`` boundary back to an older submitted_at
        value and de-duplicate order ids to avoid gaps around equal timestamps.
        """
        page_limit = max(1, min(int(limit), 500))
        pages = max(1, int(max_pages))
        overlap = max(1, int(overlap_rows))
        current_until = str(until).strip() if until else None
        orders: list[dict[str, Any]] = []
        seen_order_ids: set[str] = set()
        page_meta: list[dict[str, Any]] = []
        seen_boundaries: set[str] = set()
        truncated = False
        for page_no in range(1, pages + 1):
            page = self.list_orders(
                status=status,
                limit=page_limit,
                direction=direction,
                nested=nested,
                after=after,
                until=current_until,
            )
            submitted_at_values = [
                str(item.get("submitted_at") or item.get("created_at") or "")
                for item in page
                if isinstance(item, dict) and str(item.get("submitted_at") or item.get("created_at") or "")
            ]
            new_rows = 0
            for item in page:
                if not isinstance(item, dict):
                    continue
                order_id = str(item.get("id") or "").strip()
                dedupe_key = order_id or json.dumps(item, sort_keys=True, default=str)
                if dedupe_key in seen_order_ids:
                    continue
                seen_order_ids.add(dedupe_key)
                orders.append(item)
                new_rows += 1

            next_until = None
            if page:
                boundary_item = page[-min(overlap, len(page))]
                if isinstance(boundary_item, dict):
                    next_until = str(boundary_item.get("submitted_at") or boundary_item.get("created_at") or "").strip()
            page_meta.append(
                {
                    "page_no": page_no,
                    "request": {
                        "status": str(status),
                        "limit": int(page_limit),
                        "direction": str(direction),
                        "nested": bool(nested),
                        "after": after,
                        "until": current_until,
                    },
                    "row_count": len(page),
                    "new_row_count": new_rows,
                    "first_order_id": str(page[0].get("id") or "") if page and isinstance(page[0], dict) else "",
                    "last_order_id": str(page[-1].get("id") or "") if page and isinstance(page[-1], dict) else "",
                    "min_submitted_at": min(submitted_at_values) if submitted_at_values else "",
                    "max_submitted_at": max(submitted_at_values) if submitted_at_values else "",
                    "next_until": next_until,
                }
            )
            if len(page) < page_limit:
                break
            if not next_until or next_until in seen_boundaries or new_rows == 0:
                truncated = len(page) >= page_limit
                break
            seen_boundaries.add(next_until)
            current_until = next_until
        else:
            truncated = True

        return {
            "schema_version": "1.0",
            "status": str(status),
            "direction": str(direction),
            "nested": bool(nested),
            "page_limit": int(page_limit),
            "max_pages": int(pages),
            "overlap_rows": int(overlap),
            "page_count": len(page_meta),
            "order_count": len(orders),
            "truncated": bool(truncated),
            "pages": page_meta,
            "orders": orders,
        }

    def list_account_activities(
        self,
        *,
        activity_types: str | None = None,
        date: str | None = None,
        until: str | None = None,
        after: str | None = None,
        direction: str = "desc",
        page_size: int = 100,
        page_token: str | None = None,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Return Alpaca account activities, typically FILL activities.

        Alpaca exposes fills through /v2/account/activities/FILL.  We keep this
        generic so the executor can persist the exact broker payload for later
        audit/replay without having to infer all execution details from the
        final order snapshot.  Alpaca paginates this endpoint by passing the
        last activity id from the current page as the next page_token.
        """
        path = "/v2/account/activities"
        if activity_types:
            path = f"{path}/{str(activity_types).strip()}"
        query: dict[str, Any] = {
            "direction": str(direction),
            "page_size": int(page_size),
        }
        if date:
            query["date"] = str(date)
        if until:
            query["until"] = str(until)
        if after:
            query["after"] = str(after)
        next_page_token = str(page_token).strip() if page_token else None
        pages = max(1, int(max_pages))
        out: list[dict[str, Any]] = []
        seen_activity_ids: set[str] = set()
        seen_page_tokens: set[str] = set()
        for _ in range(pages):
            page_query = dict(query)
            if next_page_token:
                page_query["page_token"] = next_page_token
            payload = self._get_trading(path, page_query)
            if not isinstance(payload, list):
                raise AlpacaRequestError("Unexpected Alpaca account-activities payload shape.")
            if not payload:
                break

            last_activity_id = ""
            new_rows = 0
            for item in payload:
                if not isinstance(item, dict):
                    continue
                activity_id = str(item.get("id") or "").strip()
                last_activity_id = activity_id or last_activity_id
                dedupe_key = activity_id or json.dumps(item, sort_keys=True, default=str)
                if dedupe_key in seen_activity_ids:
                    continue
                seen_activity_ids.add(dedupe_key)
                out.append(item)
                new_rows += 1

            if len(payload) < int(page_size):
                break
            if not last_activity_id or last_activity_id in seen_page_tokens or new_rows == 0:
                break
            seen_page_tokens.add(last_activity_id)
            next_page_token = last_activity_id
        return out

    def cancel_all_orders(self) -> Any:
        return self._get_trading("/v2/orders", {}, method="DELETE")

    def cancel_order(self, order_id: str) -> Any:
        return self._get_trading(f"/v2/orders/{order_id}", {}, method="DELETE")

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        type: str = "market",
        time_in_force: str = "day",
        qty: str | float | int | None = None,
        notional: str | float | int | None = None,
        client_order_id: str | None = None,
        extended_hours: bool | None = None,
        limit_price: str | float | int | None = None,
    ) -> dict[str, Any]:
        if (qty is None and notional is None) or (qty is not None and notional is not None):
            raise ValueError("Exactly one of qty or notional must be provided.")
        payload: dict[str, Any] = {
            "symbol": str(symbol).strip().upper(),
            "side": str(side).strip().lower(),
            "type": str(type).strip().lower(),
            "time_in_force": str(time_in_force).strip().lower(),
        }
        if qty is not None:
            payload["qty"] = str(qty)
        if notional is not None:
            payload["notional"] = str(notional)
        if client_order_id:
            payload["client_order_id"] = str(client_order_id)
        if extended_hours is not None:
            payload["extended_hours"] = bool(extended_hours)
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        response = self._get_trading("/v2/orders", {}, method="POST", payload=payload)
        if not isinstance(response, dict):
            raise AlpacaRequestError("Unexpected Alpaca submit-order payload shape.")
        return response

    def get_order(self, order_id: str) -> dict[str, Any]:
        payload = self._get_trading(f"/v2/orders/{order_id}", {})
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca order payload shape.")
        return payload

    def get_latest_trades(
        self,
        *,
        symbols: list[str],
        feed: str = "iex",
    ) -> dict[str, dict[str, Any]]:
        payload = self._get_data(
            "/v2/stocks/trades/latest",
            {
                "symbols": ",".join(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})),
                "feed": str(feed),
            },
        )
        trades = payload.get("trades", {})
        if not isinstance(trades, dict):
            raise AlpacaRequestError("Unexpected Alpaca latest trades payload shape.")
        return {str(symbol).upper(): trade for symbol, trade in trades.items() if isinstance(trade, dict)}

    def get_latest_quotes(
        self,
        *,
        symbols: list[str],
        feed: str = "iex",
    ) -> dict[str, dict[str, Any]]:
        payload = self._get_data(
            "/v2/stocks/quotes/latest",
            {
                "symbols": ",".join(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})),
                "feed": str(feed),
            },
        )
        quotes = payload.get("quotes", {})
        if not isinstance(quotes, dict):
            raise AlpacaRequestError("Unexpected Alpaca latest quotes payload shape.")
        return {str(symbol).upper(): quote for symbol, quote in quotes.items() if isinstance(quote, dict)}

    def get_stock_bars(
        self,
        *,
        symbols: list[str],
        start: str,
        end: str,
        timeframe: str = "1Day",
        adjustment: str = "raw",
        feed: str = "iex",
        limit: int = 10000,
        asof: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "timeframe": timeframe,
            "adjustment": adjustment,
            "feed": feed,
            "limit": limit,
            "sort": "asc",
        }
        if asof:
            query["asof"] = asof

        bars: list[dict[str, Any]] = []
        next_page_token: str | None = None

        while True:
            page_query = dict(query)
            if next_page_token:
                page_query["page_token"] = next_page_token
            payload = self._get_data("/v2/stocks/bars", page_query)
            bar_map = payload.get("bars", {})
            for symbol, symbol_bars in bar_map.items():
                if not isinstance(symbol_bars, list):
                    continue
                for bar in symbol_bars:
                    if isinstance(bar, dict):
                        bars.append({"symbol": symbol, **bar})

            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break

        return bars

    def get_corporate_actions(
        self,
        *,
        symbols: list[str],
        start: str,
        end: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": start,
            "end": end,
            "limit": limit,
        }

        actions: list[dict[str, Any]] = []
        next_page_token: str | None = None

        while True:
            page_query = dict(query)
            if next_page_token:
                page_query["page_token"] = next_page_token
            payload = self._get_data("/v1/corporate-actions", page_query)
            action_groups = payload.get("corporate_actions", {})
            if not isinstance(action_groups, dict):
                raise AlpacaRequestError("Unexpected Alpaca corporate actions payload shape.")

            for action_type, items in action_groups.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        actions.append({"action_type": action_type, **item})

            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break

        return actions

    def _get_trading(
        self,
        path: str,
        query: dict[str, Any],
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return self._request_trading(path=path, query=query, method=method, payload=payload)

    def _request_trading(
        self,
        *,
        path: str,
        query: dict[str, Any] | None = None,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = self._compose_url(
            base_url=self._credentials.trading_base_url,
            path=path,
            query=query or {},
        )
        return self._request(url, method=method, payload=payload)

    def _get_data(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        url = self._compose_url(
            base_url=self._credentials.data_base_url,
            path=path,
            query=query,
        )
        payload = self._request(url)
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca market data payload shape.")
        return payload

    def _compose_url(self, *, base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
        base = str(base_url).rstrip("/")
        normalized_path = path if str(path).startswith("/") else f"/{path}"

        # Be tolerant to account configs that already include /v1 or /v2.
        for version_prefix in ("/v1", "/v2"):
            if base.endswith(version_prefix) and normalized_path.startswith(f"{version_prefix}/"):
                normalized_path = normalized_path[len(version_prefix) :]
                break

        url = f"{base}{normalized_path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def _next_audit_seq(self) -> int:
        with self._audit_lock:
            self._audit_seq += 1
            return self._audit_seq

    def _write_audit_event(self, event: dict[str, Any]) -> None:
        if self._audit_log_path is None:
            return
        try:
            with self._audit_lock:
                with self._audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Request auditing is diagnostic only; never let it affect trading.
            return

    @staticmethod
    def _body_digest(raw: str) -> dict[str, Any]:
        encoded = raw.encode("utf-8", errors="replace")
        preview = raw[:4000]
        return {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "preview": preview,
            "preview_truncated": len(raw) > len(preview),
        }

    @staticmethod
    def _redact_payload_value(key: str, value: Any) -> Any:
        key_l = str(key).lower()
        if any(token in key_l for token in ("secret", "password", "token", "api_key", "key_id")):
            if value in (None, ""):
                return value
            return f"<redacted:{len(str(value))} chars>"
        return value

    @classmethod
    def _redacted_payload(cls, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        return {str(key): cls._redact_payload_value(str(key), value) for key, value in sorted(payload.items())}

    @staticmethod
    def _url_audit_shape(url: str) -> dict[str, Any]:
        try:
            parts = urlsplit(str(url))
            query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        except Exception:
            return {"path": "", "query_keys": [], "query_count": 0}
        return {
            "scheme": parts.scheme,
            "host": parts.netloc,
            "path": parts.path,
            "query_keys": sorted({str(key) for key, _ in query_pairs}),
            "query_count": len(query_pairs),
        }

    @classmethod
    def _request_digest(cls, *, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        redacted_payload = cls._redacted_payload(payload)
        payload_text = json.dumps(redacted_payload, ensure_ascii=False, sort_keys=True, default=str)
        payload_digest = cls._body_digest(payload_text) if redacted_payload is not None else None
        return {
            "url_shape": cls._url_audit_shape(url),
            "payload_shape": cls._payload_shape(redacted_payload),
            "payload_body": payload_digest,
            "payload_preview": redacted_payload,
        }

    @staticmethod
    def _payload_shape(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {"type": "dict", "keys": sorted(str(k) for k in list(value)[:50]), "key_count": len(value)}
        if isinstance(value, list):
            return {"type": "list", "length": len(value)}
        return {"type": type(value).__name__}

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        for attempt in range(self._credentials.max_retries + 1):
            audit_seq = self._next_audit_seq()
            attempt_started = time.monotonic()
            audit_base = {
                "seq": audit_seq,
                "attempt": int(attempt),
                "max_retries": int(self._credentials.max_retries),
                "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "method": str(method).upper(),
                "url": url,
                "request": self._request_digest(url=url, payload=payload),
            }
            headers: dict[str, str] = {
                "accept": "application/json",
                "APCA-API-KEY-ID": self._credentials.api_key_id,
                "APCA-API-SECRET-KEY": self._credentials.api_secret_key,
            }
            if body is not None:
                headers["content-type"] = "application/json"
            request = Request(
                url=url,
                headers=headers,
                method=str(method).upper(),
                data=body,
            )
            try:
                with urlopen(request, timeout=self._credentials.request_timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                    elapsed_ms = round((time.monotonic() - attempt_started) * 1000.0, 3)
                    status_code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
                    response_headers = dict(response.headers.items()) if getattr(response, "headers", None) else {}
                    if not raw.strip():
                        self._write_audit_event(
                            {
                                **audit_base,
                                "ok": True,
                                "elapsed_ms": elapsed_ms,
                                "status_code": status_code,
                                "response_headers": response_headers,
                                "response_body": {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "preview": ""},
                                "response_shape": {"type": "empty"},
                            }
                        )
                        return {}
                    decoded = json.loads(raw)
                    self._write_audit_event(
                        {
                            **audit_base,
                            "ok": True,
                            "elapsed_ms": elapsed_ms,
                            "status_code": status_code,
                            "response_headers": response_headers,
                            "response_body": self._body_digest(raw),
                            "response_shape": self._payload_shape(decoded),
                        }
                    )
                    return decoded
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                elapsed_ms = round((time.monotonic() - attempt_started) * 1000.0, 3)
                retryable = exc.code == 429 or 500 <= exc.code < 600
                last_error = AlpacaRequestError(
                    f"Alpaca request failed with HTTP {exc.code}: {detail}"
                )
                self._write_audit_event(
                    {
                        **audit_base,
                        "ok": False,
                        "elapsed_ms": elapsed_ms,
                        "status_code": int(exc.code),
                        "retryable": bool(retryable),
                        "error_type": "HTTPError",
                        "error": str(last_error),
                        "response_body": self._body_digest(detail),
                    }
                )
                if not retryable or attempt >= self._credentials.max_retries:
                    raise last_error from exc
            except (
                TimeoutError,
                URLError,
                IncompleteRead,
                RemoteDisconnected,
                ConnectionResetError,
                ssl.SSLError,
            ) as exc:
                reason = exc.reason if isinstance(exc, URLError) else "read timeout"
                if isinstance(exc, IncompleteRead):
                    reason = "incomplete read"
                elif isinstance(exc, RemoteDisconnected):
                    reason = "remote disconnected"
                elif isinstance(exc, ConnectionResetError):
                    reason = "connection reset"
                elif isinstance(exc, ssl.SSLError):
                    reason = str(exc)
                last_error = AlpacaRequestError(f"Failed to reach Alpaca: {reason}")
                elapsed_ms = round((time.monotonic() - attempt_started) * 1000.0, 3)
                self._write_audit_event(
                    {
                        **audit_base,
                        "ok": False,
                        "elapsed_ms": elapsed_ms,
                        "status_code": None,
                        "retryable": bool(attempt < self._credentials.max_retries),
                        "error_type": type(exc).__name__,
                        "error": str(last_error),
                    }
                )
                if attempt >= self._credentials.max_retries:
                    raise last_error from exc

            time.sleep(min(2 ** attempt, 5))

        if last_error is not None:
            raise last_error
        raise AlpacaRequestError("Alpaca request failed without a captured error.")


def _load_local_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
