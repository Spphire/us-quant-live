from __future__ import annotations

import json
import os
import ssl
import time
from dataclasses import dataclass
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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

    def get_clock(self) -> dict[str, Any]:
        payload = self._get_trading("/v2/clock", {})
        if not isinstance(payload, dict):
            raise AlpacaRequestError("Unexpected Alpaca clock payload shape.")
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
    ) -> list[dict[str, Any]]:
        payload = self._get_trading(
            "/v2/orders",
            {
                "status": str(status),
                "limit": int(limit),
                "direction": str(direction),
                "nested": str(bool(nested)).lower(),
            },
        )
        if not isinstance(payload, list):
            raise AlpacaRequestError("Unexpected Alpaca orders payload shape.")
        return payload

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
                    if not raw.strip():
                        return {}
                    return json.loads(raw)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                last_error = AlpacaRequestError(
                    f"Alpaca request failed with HTTP {exc.code}: {detail}"
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
