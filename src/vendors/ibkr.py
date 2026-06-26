from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from http.client import IncompleteRead
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class IbkrRequestError(RuntimeError):
    """Raised when an IBKR Client Portal API request fails."""


@dataclass(slots=True, frozen=True)
class IbkrCredentials:
    """Connection settings for IBKR Client Portal API."""

    base_url: str = "https://127.0.0.1:5000/v1/api"
    account_id: str | None = None
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    verify_tls: bool = False


class IbkrHttpClient:
    """Small HTTP client for IBKR Client Portal API trading endpoints."""

    def __init__(self, credentials: IbkrCredentials) -> None:
        self._credentials = credentials
        self._active_account_id: str | None = None
        self._account_cache: dict[str, Any] | None = None
        self._conid_by_symbol: dict[str, int] = {}
        self._portfolio_primed = False

    def ensure_authenticated(self, *, init_if_needed: bool = True, compete: bool = True) -> dict[str, Any]:
        status = self._auth_status()
        authenticated = bool(status.get("authenticated", False))
        if authenticated or not init_if_needed:
            return status

        init_payload = {"publish": True, "compete": bool(compete)}
        try:
            self._request(path="/iserver/auth/ssodh/init", method="POST", payload=init_payload)
        except IbkrRequestError:
            # Fallback for gateways that expose /iserver/reauthenticate only.
            self._request(path="/iserver/reauthenticate", method="POST", payload={})
        status = self._auth_status()
        if not bool(status.get("authenticated", False)):
            raise IbkrRequestError(
                "IBKR brokerage session is not authenticated. "
                "Please log into Client Portal Gateway and retry."
            )
        return status

    def get_account(self) -> dict[str, Any]:
        account_id = self.get_account_id()
        ledger = self.get_portfolio_ledger(account_id)
        summary = self.get_portfolio_summary(account_id)
        accounts_payload = self._account_cache or {}
        acct_props = {}
        if isinstance(accounts_payload, Mapping):
            acct_props_root = accounts_payload.get("acctProps")
            if isinstance(acct_props_root, Mapping):
                raw = acct_props_root.get(account_id)
                if isinstance(raw, Mapping):
                    acct_props = dict(raw)

        base_ledger = {}
        if isinstance(ledger, Mapping):
            maybe_base = ledger.get("BASE")
            if isinstance(maybe_base, Mapping):
                base_ledger = dict(maybe_base)

        account: dict[str, Any] = {
            "account_id": account_id,
            "selected_account": account_id,
            "supports_fractions": _coerce_bool(acct_props.get("supportsFractions"), default=False),
            "supports_cash_qty": _coerce_bool(acct_props.get("supportsCashQty"), default=False),
            "allow_customer_time": _coerce_bool(acct_props.get("allowCustomerTime"), default=False),
            "shorting_enabled": _coerce_bool(acct_props.get("allowShorting"), default=True),
            "netliquidationvalue": _safe_float(base_ledger.get("netliquidationvalue")),
            "cashbalance": _safe_float(base_ledger.get("cashbalance")),
            "stockmarketvalue": _safe_float(base_ledger.get("stockmarketvalue")),
            "summary": summary,
            "ledger": ledger,
        }
        return account

    def list_positions(self) -> list[dict[str, Any]]:
        account_id = self.get_account_id()
        self._prime_portfolio()

        payload = self._request(path=f"/portfolio2/{account_id}/positions", method="GET")
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]

        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            page_payload = self._request(path=f"/portfolio/{account_id}/positions/{page}", method="GET")
            if not isinstance(page_payload, list):
                break
            chunk = [row for row in page_payload if isinstance(row, dict)]
            if not chunk:
                break
            rows.extend(chunk)
            page += 1
        return rows

    def list_orders(
        self,
        *,
        status: str = "open",
        limit: int = 500,
        direction: str = "desc",
        nested: bool = False,
    ) -> list[dict[str, Any]]:
        _ = status, limit, direction, nested  # Kept for Alpaca interface compatibility.
        self.get_account_id()  # Ensure /iserver/accounts has been called.
        payload = self._request(path="/iserver/account/orders", method="GET", query={"force": "true"})
        if not isinstance(payload, Mapping):
            return []
        orders = payload.get("orders")
        if not isinstance(orders, list):
            return []
        return [row for row in orders if isinstance(row, dict)]

    def cancel_all_orders(self) -> list[dict[str, Any]]:
        account_id = self.get_account_id()
        response = self._request(
            path=f"/iserver/account/{account_id}/order/-1",
            method="DELETE",
            query={"manualIndicator": "false"},
        )
        if isinstance(response, list):
            return [row for row in response if isinstance(row, dict)]
        if isinstance(response, dict):
            return [response]
        return []

    def cancel_order(self, order_id: str) -> Any:
        account_id = self.get_account_id()
        return self._request(
            path=f"/iserver/account/{account_id}/order/{order_id}",
            method="DELETE",
            query={"manualIndicator": "false"},
        )

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
        if qty is None:
            raise ValueError("IBKR submit_order currently requires qty.")
        if notional is not None:
            raise ValueError("IBKR submit_order does not support notional in this adapter.")

        account_id = self.get_account_id()
        conid = self.resolve_conid(str(symbol).strip().upper())
        order_type = "MKT" if str(type).strip().lower() == "market" else "LMT"
        order_side = str(side).strip().upper()
        tif = str(time_in_force).strip().upper()
        quantity = float(qty)
        if quantity <= 0:
            raise ValueError("qty must be > 0")

        order_payload: dict[str, Any] = {
            "conid": int(conid),
            "secType": f"{int(conid)}:STK",
            "orderType": order_type,
            "side": order_side,
            "tif": tif,
            "quantity": quantity,
            "outsideRTH": bool(extended_hours) if extended_hours is not None else False,
            "listingExchange": "SMART",
            "referrer": "SM",
        }
        if order_type == "LMT":
            if limit_price is None:
                raise ValueError("limit_price is required for limit orders")
            order_payload["price"] = float(limit_price)
        if client_order_id:
            order_payload["cOID"] = str(client_order_id)

        response = self._request(
            path=f"/iserver/account/{account_id}/orders",
            method="POST",
            payload={"orders": [order_payload]},
        )
        resolved = self._resolve_order_submit_response(response)
        if not isinstance(resolved, Mapping):
            raise IbkrRequestError(f"Unexpected IBKR submit-order response: {resolved!r}")

        order_id = str(resolved.get("order_id") or resolved.get("orderId") or "")
        order_status = str(resolved.get("order_status") or resolved.get("status") or "")
        return {
            "id": order_id,
            "order_id": order_id,
            "status": order_status,
            "order_status": order_status,
            "raw": dict(resolved),
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        payload = self._request(path=f"/iserver/account/order/status/{order_id}", method="GET")
        if isinstance(payload, dict):
            status = str(payload.get("order_status") or payload.get("status") or "")
            out = dict(payload)
            out.setdefault("status", status)
            out.setdefault("order_status", status)
            out.setdefault("id", str(payload.get("order_id") or payload.get("orderId") or order_id))
            return out
        raise IbkrRequestError(f"Unexpected IBKR order-status payload shape: {type(payload).__name__}")

    def get_portfolio_summary(self, account_id: str | None = None) -> dict[str, Any]:
        acct = self.get_account_id(account_id)
        self._prime_portfolio()
        payload = self._request(path=f"/portfolio/{acct}/summary", method="GET")
        if isinstance(payload, dict):
            return payload
        raise IbkrRequestError("Unexpected IBKR portfolio summary payload shape.")

    def get_portfolio_ledger(self, account_id: str | None = None) -> dict[str, Any]:
        acct = self.get_account_id(account_id)
        self._prime_portfolio()
        payload = self._request(path=f"/portfolio/{acct}/ledger", method="GET")
        if isinstance(payload, dict):
            return payload
        raise IbkrRequestError("Unexpected IBKR portfolio ledger payload shape.")

    def get_account_id(self, preferred: str | None = None) -> str:
        if preferred:
            preferred = str(preferred).strip().upper() or None
        requested = preferred or self._active_account_id or self._credentials.account_id
        payload = self._request(path="/iserver/accounts", method="GET")
        if not isinstance(payload, dict):
            raise IbkrRequestError("Unexpected IBKR accounts payload shape.")
        self._account_cache = payload

        available = payload.get("accounts")
        available_accounts = [
            str(item).strip().upper() for item in available if isinstance(item, str) and str(item).strip()
        ] if isinstance(available, list) else []
        selected_raw = payload.get("selectedAccount")
        selected = str(selected_raw).strip().upper() if isinstance(selected_raw, str) and str(selected_raw).strip() else None
        target = requested or selected or (available_accounts[0] if available_accounts else None)
        if not target:
            raise IbkrRequestError("No IBKR account is available from /iserver/accounts.")
        if available_accounts and target not in available_accounts:
            raise IbkrRequestError(
                f"Requested IBKR account {target!r} not in accessible accounts: {available_accounts}"
            )

        if selected and selected != target:
            switched = self._request(path="/iserver/account", method="POST", payload={"acctId": target})
            if isinstance(switched, Mapping):
                switched_id = str(switched.get("acctId") or "").strip().upper()
                if switched_id and switched_id != target:
                    raise IbkrRequestError(
                        f"IBKR account switch returned {switched_id!r}, expected {target!r}."
                    )

        self._active_account_id = target
        return target

    def resolve_conid(self, symbol: str) -> int:
        normalized = str(symbol).strip().upper()
        if not normalized:
            raise IbkrRequestError("symbol is empty when resolving conid")
        cached = self._conid_by_symbol.get(normalized)
        if cached is not None:
            return int(cached)

        payload = self._request(path="/trsrv/stocks", method="GET", query={"symbols": normalized})
        if not isinstance(payload, Mapping):
            raise IbkrRequestError(f"Unexpected /trsrv/stocks payload for {normalized}")
        matches = payload.get(normalized)
        if not isinstance(matches, list):
            raise IbkrRequestError(f"No stock contract found for symbol={normalized}")

        candidates: list[dict[str, Any]] = []
        for item in matches:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("assetClass") or "").upper() != "STK":
                continue
            contracts = item.get("contracts")
            if not isinstance(contracts, list):
                continue
            for contract in contracts:
                if isinstance(contract, Mapping):
                    candidates.append(dict(contract))
        if not candidates:
            raise IbkrRequestError(f"No STK contracts found for symbol={normalized}")

        def score(contract: Mapping[str, Any]) -> tuple[int, int, int]:
            exchange = str(contract.get("exchange") or "").upper()
            is_us = bool(contract.get("isUS", False))
            exchange_score = 1 if exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "SMART", "IEX"} else 0
            return (1 if is_us else 0, exchange_score, 1)

        best = max(candidates, key=score)
        conid = _safe_int(best.get("conid"))
        if conid is None:
            raise IbkrRequestError(f"Cannot resolve numeric conid for symbol={normalized}")
        self._conid_by_symbol[normalized] = int(conid)
        return int(conid)

    def _auth_status(self) -> dict[str, Any]:
        try:
            payload = self._request(path="/iserver/auth/status", method="POST", payload={})
        except IbkrRequestError:
            payload = self._request(path="/iserver/auth/status", method="GET")
        if not isinstance(payload, dict):
            raise IbkrRequestError("Unexpected IBKR auth/status payload shape.")
        return payload

    def _prime_portfolio(self) -> None:
        if self._portfolio_primed:
            return
        payload = self._request(path="/portfolio/accounts", method="GET")
        if not isinstance(payload, list):
            raise IbkrRequestError("Unexpected IBKR /portfolio/accounts payload shape.")
        self._portfolio_primed = True

    def _resolve_order_submit_response(self, payload: Any, *, _max_depth: int = 8) -> Mapping[str, Any]:
        current = payload
        depth = 0
        while depth < _max_depth:
            if isinstance(current, Mapping):
                if "error" in current:
                    raise IbkrRequestError(str(current.get("error") or "IBKR order rejected"))
                if current.get("order_id") or current.get("orderId"):
                    return current
                if "id" in current and "message" in current:
                    reply_id = str(current.get("id") or "").strip()
                    if not reply_id:
                        raise IbkrRequestError(f"IBKR order reply confirmation id is empty: {current!r}")
                    current = self._request(
                        path=f"/iserver/reply/{reply_id}",
                        method="POST",
                        payload={"confirmed": True},
                    )
                    depth += 1
                    continue
                if "orders" in current and isinstance(current.get("orders"), list):
                    current = current.get("orders")
                    depth += 1
                    continue
                raise IbkrRequestError(f"Unexpected IBKR order response object: {current!r}")

            if isinstance(current, list):
                if not current:
                    raise IbkrRequestError("IBKR order response is an empty list.")
                head = current[0]
                if not isinstance(head, Mapping):
                    raise IbkrRequestError(f"Unexpected IBKR order response entry: {head!r}")
                if "error" in head:
                    raise IbkrRequestError(str(head.get("error") or "IBKR order rejected"))
                if head.get("order_id") or head.get("orderId"):
                    return head
                if "id" in head and "message" in head:
                    reply_id = str(head.get("id") or "").strip()
                    if not reply_id:
                        raise IbkrRequestError(f"IBKR order reply confirmation id is empty: {head!r}")
                    current = self._request(
                        path=f"/iserver/reply/{reply_id}",
                        method="POST",
                        payload={"confirmed": True},
                    )
                    depth += 1
                    continue
                raise IbkrRequestError(f"Unexpected IBKR order response list entry: {head!r}")

            raise IbkrRequestError(f"Unexpected IBKR order response payload type: {type(current).__name__}")
        raise IbkrRequestError("IBKR order confirmation exceeded max reply depth.")

    def _compose_url(self, *, path: str, query: Mapping[str, Any] | None = None) -> str:
        base = str(self._credentials.base_url).rstrip("/")
        normalized = path if str(path).startswith("/") else f"/{path}"
        url = f"{base}{normalized}"
        if query:
            mapped = {str(key): str(value) for key, value in query.items() if value is not None}
            if mapped:
                url = f"{url}?{urlencode(mapped)}"
        return url

    def _request(
        self,
        *,
        path: str,
        method: str = "GET",
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        url = self._compose_url(path=path, query=query)
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self._credentials.max_retries + 1):
            headers = {"accept": "application/json"}
            if body is not None:
                headers["content-type"] = "application/json"
            request = Request(
                url=url,
                headers=headers,
                method=str(method).upper(),
                data=body,
            )
            try:
                ssl_context = None
                if str(url).startswith("https://") and not bool(self._credentials.verify_tls):
                    ssl_context = ssl._create_unverified_context()
                with urlopen(
                    request,
                    timeout=self._credentials.request_timeout_seconds,
                    context=ssl_context,
                ) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    if not raw.strip():
                        return {}
                    return json.loads(raw)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if exc.code == 401:
                    hint = (
                        "IBKR gateway unauthorized (401). "
                        "Open https://localhost:5000, login paper account, finish 2FA, then retry."
                    )
                    last_error = IbkrRequestError(f"{hint} detail={detail}")
                else:
                    last_error = IbkrRequestError(f"IBKR request failed with HTTP {exc.code}: {detail}")
                if not retryable or attempt >= self._credentials.max_retries:
                    raise last_error from exc
            except (TimeoutError, URLError, IncompleteRead, ssl.SSLError, json.JSONDecodeError) as exc:
                if isinstance(exc, URLError):
                    reason = exc.reason
                elif isinstance(exc, IncompleteRead):
                    reason = "incomplete read"
                elif isinstance(exc, ssl.SSLError):
                    reason = f"ssl error: {exc}"
                elif isinstance(exc, json.JSONDecodeError):
                    reason = f"invalid json response: {exc}"
                else:
                    reason = "request timeout"
                last_error = IbkrRequestError(f"Failed to reach IBKR gateway: {reason}")
                if attempt >= self._credentials.max_retries:
                    raise last_error from exc
            time.sleep(min(2**attempt, 5))

        if last_error is not None:
            raise last_error
        raise IbkrRequestError("IBKR request failed without a captured error.")


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"true", "t", "yes", "y", "1"}:
        return True
    if token in {"false", "f", "no", "n", "0"}:
        return False
    return bool(default)
