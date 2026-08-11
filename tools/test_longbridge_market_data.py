from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.vendors.longbridge import (  # noqa: E402
    LongbridgeCredentials,
    LongbridgeQuoteClient,
    LongbridgeQuoteError,
)
from src.alpaca_executor import (  # noqa: E402
    OrderInstruction,
    _build_decision_symbol_universe_snapshot,
    _build_execution_symbol_universe_snapshot,
    _live_marketable_reference_price,
    _submit_and_track_orders,
)


class _SubType:
    Quote = "quote"
    Depth = "depth"


class _FakeContext:
    sub_type = _SubType

    def __init__(
        self,
        *,
        bid: float = 100.0,
        ask: float = 100.1,
        stream_trade_status: str = "TradeStatus.Normal",
        static_quotes: dict[str, tuple[float, str]] | None = None,
        snapshot_depth_error: bool = False,
        snapshot_depth_delay_seconds: float = 0.0,
        candlestick_delay_seconds: float = 0.0,
    ) -> None:
        self.bid = bid
        self.ask = ask
        self.stream_trade_status = stream_trade_status
        self.static_quotes = static_quotes or {}
        self.snapshot_depth_error = bool(snapshot_depth_error)
        self.snapshot_depth_delay_seconds = max(0.0, float(snapshot_depth_delay_seconds))
        self.candlestick_delay_seconds = max(0.0, float(candlestick_delay_seconds))
        self.on_quote = None
        self.on_depth = None
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.quote_calls: list[list[str]] = []
        self.depth_calls: list[str] = []
        self.candlestick_calls: list[dict[str, object]] = []
        self.closed = False

    def set_on_quote(self, callback):
        self.on_quote = callback

    def set_on_depth(self, callback):
        self.on_depth = callback

    def quote_level(self):
        return "USAA:OpenAPI|USAA|Global|NBBO"

    def quote_package_details(self):
        return []

    def subscribe(self, symbols, sub_types):
        assert set(sub_types) == {_SubType.Quote, _SubType.Depth}
        self.subscribed.extend(symbols)
        for symbol in symbols:
            self.on_quote(
                symbol,
                SimpleNamespace(
                    last_done=(self.bid + self.ask) / 2.0,
                    current_volume=10,
                    timestamp=datetime.now(timezone.utc),
                    trade_status=self.stream_trade_status,
                    trade_session="TradeSession.Normal",
                ),
            )
            self.on_depth(
                symbol,
                SimpleNamespace(
                    bids=[SimpleNamespace(price=self.bid, volume=100)],
                    asks=[SimpleNamespace(price=self.ask, volume=200)],
                ),
            )

    def unsubscribe(self, symbols, sub_types):
        self.unsubscribed.extend(symbols)

    def close(self):
        self.closed = True

    def quote(self, symbols):
        self.quote_calls.append(list(symbols))
        rows = []
        for symbol in symbols:
            if self.static_quotes and symbol not in self.static_quotes:
                continue
            last_done, trade_status = self.static_quotes.get(
                symbol,
                ((self.bid + self.ask) / 2.0, "TradeStatus.Normal"),
            )
            rows.append(
                SimpleNamespace(
                    symbol=symbol,
                    last_done=last_done,
                    timestamp=datetime.now(timezone.utc),
                    trade_status=trade_status,
                )
            )
        return rows

    def depth(self, symbol):
        self.depth_calls.append(str(symbol))
        if self.snapshot_depth_delay_seconds > 0.0:
            time.sleep(self.snapshot_depth_delay_seconds)
        if self.snapshot_depth_error:
            raise RuntimeError("snapshot depth unavailable")
        return SimpleNamespace(
            bids=[SimpleNamespace(price=self.bid, volume=100)],
            asks=[SimpleNamespace(price=self.ask, volume=200)],
        )

    def history_candlesticks_by_date(
        self,
        symbol,
        period,
        adjust_type,
        start,
        end,
        trade_sessions,
    ):
        self.candlestick_calls.append(
            {
                "symbol": str(symbol),
                "period": str(period),
                "adjust_type": str(adjust_type),
                "start": start,
                "end": end,
                "trade_sessions": str(trade_sessions),
            }
        )
        if self.candlestick_delay_seconds > 0.0:
            time.sleep(self.candlestick_delay_seconds)
        return [
            SimpleNamespace(
                timestamp=datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.5,
                close=100.5,
                volume=1000,
                turnover=100250.0,
                trade_session="TradeSession.Normal",
            )
        ]


def _credentials() -> LongbridgeCredentials:
    return LongbridgeCredentials("app-key", "app-secret", "access-token")


def test_credentials_are_loaded_without_repr_leak() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "longbridge.local.json"
        path.write_text(
            json.dumps(
                {
                    "app_key": "secret-key",
                    "app_secret": "secret-value",
                    "access_token": "secret-token",
                }
            ),
            encoding="utf-8",
        )
        credentials = LongbridgeCredentials.from_sources(path)
    rendered = repr(credentials)
    assert "secret-key" not in rendered
    assert "secret-value" not in rendered
    assert "secret-token" not in rendered


def test_stream_warmup_and_class_symbol_mapping() -> None:
    context = _FakeContext()
    client = LongbridgeQuoteClient(
        _credentials(),
        context_factory=lambda credentials: context,
    )
    health = client.start(["AAPL", "BRK.B"])
    quotes = client.get_latest_quotes(["AAPL", "BRK.B"], require_fresh=True)
    prices = client.get_reference_prices(["AAPL", "BRK.B"])
    assert context.subscribed == ["AAPL.US", "BRK.B.US"]
    assert health["status"] == "pass"
    assert len(quotes) == 2
    assert prices == {"AAPL": 100.05, "BRK.B": 100.05}
    assert quotes["AAPL"]["provider"] == "longbridge"
    assert quotes["AAPL"]["bp"] == 100.0
    assert quotes["AAPL"]["ap"] == 100.1
    client.close()
    assert context.unsubscribed == ["AAPL.US", "BRK.B.US"]


def test_stale_stream_quote_is_refreshed_from_snapshot() -> None:
    context = _FakeContext()
    client = LongbridgeQuoteClient(
        _credentials(),
        max_quote_age_seconds=0.01,
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    time.sleep(0.12)
    quote = client.get_marketable_quote("AAPL")
    assert quote["depth_source"] == "snapshot_refresh", quote
    assert context.depth_calls == ["AAPL.US"], context.depth_calls
    health = client.health_snapshot(requested_symbols=["AAPL"])
    assert health["snapshot_refresh_attempt_count"] == 1, health
    assert health["snapshot_refresh_failure_count"] == 0, health


def test_stale_depth_snapshots_are_refreshed_concurrently() -> None:
    symbols = [f"S{index}" for index in range(16)]
    context = _FakeContext(snapshot_depth_delay_seconds=0.05)
    client = LongbridgeQuoteClient(
        _credentials(),
        max_quote_age_seconds=0.2,
        context_factory=lambda credentials: context,
    )
    client.start(symbols)
    time.sleep(0.22)
    started = time.monotonic()
    quotes = client.get_latest_quotes(symbols, require_fresh=True)
    elapsed = time.monotonic() - started
    assert len(quotes) == len(symbols), quotes
    assert elapsed < 0.45, elapsed
    health = client.health_snapshot(requested_symbols=symbols)
    refresh = health["last_snapshot_refresh"]
    assert refresh["depth_worker_count"] == 8, refresh
    assert refresh["refresh_round_count"] == 1, refresh
    assert refresh["depth_aggregate_work_seconds"] >= 0.7, refresh
    assert refresh["depth_phase_elapsed_seconds"] < 0.2, refresh
    assert refresh["depth_parallel_speedup_ratio"] >= 3.0, refresh
    assert health["snapshot_refresh_multi_symbol_count"] == 1, health
    assert health["snapshot_refresh_max_requested_symbols"] == len(symbols), health
    assert health["snapshot_refresh_max_depth_workers"] == 8, health
    assert health["snapshot_refresh_multi_symbol_parallel_speedup_ratio"] >= 3.0, health


def test_stale_depth_snapshots_are_sharded_across_contexts() -> None:
    symbols = [f"S{index}" for index in range(32)]
    main_context = _FakeContext(snapshot_depth_delay_seconds=0.05)
    auxiliary_contexts: list[_FakeContext] = []

    def new_snapshot_context(credentials):
        del credentials
        context = _FakeContext(snapshot_depth_delay_seconds=0.05)
        auxiliary_contexts.append(context)
        return context

    client = LongbridgeQuoteClient(
        _credentials(),
        max_quote_age_seconds=0.2,
        snapshot_context_count=4,
        context_factory=lambda credentials: main_context,
        snapshot_context_factory=new_snapshot_context,
    )
    client.start(symbols)
    assert len(auxiliary_contexts) == 3, auxiliary_contexts
    time.sleep(0.22)
    started = time.monotonic()
    quotes = client.get_latest_quotes(symbols, require_fresh=True)
    elapsed = time.monotonic() - started
    assert len(quotes) == len(symbols), quotes
    assert elapsed < 0.75, elapsed
    assert sorted(
        len(context.depth_calls) for context in [main_context, *auxiliary_contexts]
    ) == [8, 8, 8, 8]
    health = client.health_snapshot(requested_symbols=symbols)
    refresh = health["last_snapshot_refresh"]
    assert refresh["depth_context_count"] == 4, refresh
    assert refresh["depth_worker_count"] == 4, refresh
    assert refresh["depth_execution_mode"] == "sharded_quote_contexts", refresh
    assert refresh["snapshot_context_creation_errors"] == [], refresh
    assert health["snapshot_refresh_max_contexts"] == 4, health
    assert health["snapshot_aux_context_count"] == 3, health
    client.close()
    assert main_context.closed
    assert all(context.closed for context in auxiliary_contexts)


def test_near_expiry_depth_is_refreshed_before_hard_limit() -> None:
    context = _FakeContext()
    client = LongbridgeQuoteClient(
        _credentials(),
        max_quote_age_seconds=0.4,
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    time.sleep(0.32)
    quote = client.get_marketable_quote("AAPL")
    assert quote["depth_source"] == "snapshot_refresh", quote
    assert context.depth_calls == ["AAPL.US"], context.depth_calls
    health = client.health_snapshot(requested_symbols=["AAPL"])
    assert health["max_quote_age_seconds"] == 0.4, health
    assert abs(health["snapshot_proactive_refresh_age_seconds"] - 0.28) < 1e-9, health


def test_stale_quote_is_rejected_when_snapshot_refresh_fails() -> None:
    context = _FakeContext(snapshot_depth_error=True)
    client = LongbridgeQuoteClient(
        _credentials(),
        max_quote_age_seconds=0.01,
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    time.sleep(0.12)
    try:
        client.get_marketable_quote("AAPL")
    except LongbridgeQuoteError as exc:
        assert "stale_quote_age_ms" in str(exc)
    else:
        raise AssertionError("stale Longbridge quote was accepted")
    health = client.health_snapshot(requested_symbols=["AAPL"])
    assert health["snapshot_refresh_failure_count"] == 3, health
    assert len(health["snapshot_refresh_failure_history"]) == 3, health
    assert health["snapshot_refresh_failure_history"][0]["requested_symbols"] == ["AAPL"], health


def test_wide_quote_is_rejected() -> None:
    context = _FakeContext(bid=100.0, ask=102.0)
    client = LongbridgeQuoteClient(
        _credentials(),
        max_spread_bps=150.0,
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    try:
        client.get_marketable_quote("AAPL")
    except LongbridgeQuoteError as exc:
        assert "spread_bps" in str(exc)
    else:
        raise AssertionError("over-wide Longbridge quote was accepted")


def test_wide_quote_can_value_portfolio_but_cannot_price_order() -> None:
    context = _FakeContext(bid=100.0, ask=102.0)
    client = LongbridgeQuoteClient(
        _credentials(),
        max_spread_bps=150.0,
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    assert client.get_reference_prices(["AAPL"]) == {"AAPL": 101.0}
    try:
        client.get_marketable_quote("AAPL")
    except LongbridgeQuoteError as exc:
        assert "spread_bps" in str(exc)
    else:
        raise AssertionError("wide quote reached order pricing")


def test_subscription_limit_is_enforced() -> None:
    client = LongbridgeQuoteClient(
        _credentials(),
        max_subscriptions=1,
        context_factory=lambda credentials: _FakeContext(),
    )
    try:
        client.start(["AAPL", "MSFT"])
    except LongbridgeQuoteError as exc:
        assert "configured maximum is 1" in str(exc)
    else:
        raise AssertionError("subscription limit was not enforced")


def test_coverage_batches_and_classifies_static_statuses() -> None:
    symbols = [f"S{index:04d}" for index in range(1001)]
    static_quotes = {
        f"{symbol}.US": (100.0, "TradeStatus.Normal") for symbol in symbols
    }
    static_quotes["S0001.US"] = (100.0, "TradeStatus.Delisted")
    static_quotes["S0002.US"] = (100.0, "TradeStatus.CodeMoved")
    static_quotes["S0003.US"] = (100.0, "TradeStatus.Halted")
    static_quotes["S0004.US"] = (0.0, "TradeStatus.Normal")
    context = _FakeContext(static_quotes=static_quotes)
    client = LongbridgeQuoteClient(
        _credentials(),
        context_factory=lambda credentials: context,
    )
    coverage = client.check_symbol_coverage(symbols, chunk_size=500)
    assert [len(chunk) for chunk in context.quote_calls] == [500, 500, 1]
    assert coverage["covered_count"] == 998
    assert coverage["permanently_unavailable_symbols"] == ["S0001", "S0002"]
    assert "S0003" in coverage["covered_symbols"]
    assert "S0004" in coverage["uncovered_symbols"]
    assert "app-key" not in json.dumps(coverage)
    assert "app-secret" not in json.dumps(coverage)
    assert "access-token" not in json.dumps(coverage)


def test_halted_is_covered_but_rejected_for_execution() -> None:
    context = _FakeContext(
        stream_trade_status="TradeStatus.Halted",
        static_quotes={"AAPL.US": (100.0, "TradeStatus.Halted")},
    )
    client = LongbridgeQuoteClient(
        _credentials(),
        context_factory=lambda credentials: context,
    )
    coverage = client.check_symbol_coverage(["AAPL"])
    assert coverage["covered_symbols"] == ["AAPL"]
    client.start(["AAPL"])
    try:
        client.get_marketable_quote("AAPL")
    except LongbridgeQuoteError as exc:
        assert "TradeStatus.Halted" in str(exc)
    else:
        raise AssertionError("halted symbol was accepted for execution")


def test_decision_intersection_and_execute_exit_scope() -> None:
    coverage = {
        "status": "pass",
        "errors": [],
        "covered_symbols": ["AAPL", "HALT"],
        "rows": [
            {
                "symbol": "AAPL",
                "returned": True,
                "covered": True,
                "permanently_unavailable": False,
                "coverage_reason": "covered",
                "last_price": 100.0,
                "trade_status": "TradeStatus.Normal",
            },
            {
                "symbol": "HALT",
                "returned": True,
                "covered": True,
                "permanently_unavailable": False,
                "coverage_reason": "covered",
                "last_price": 25.0,
                "trade_status": "TradeStatus.Halted",
            },
            {
                "symbol": "OLD",
                "returned": True,
                "covered": False,
                "permanently_unavailable": True,
                "coverage_reason": "trade_status_delisted",
                "last_price": 10.0,
                "trade_status": "TradeStatus.Delisted",
            },
        ],
    }
    assets = [
        {"symbol": symbol, "name": f"{symbol} Inc", "status": "active", "class": "us_equity", "tradable": True}
        for symbol in ["AAPL", "HALT", "OLD"]
    ]
    with TemporaryDirectory() as tmp:
        candidate_path = Path(tmp) / "candidates.txt"
        candidate_path.write_text("AAPL\nHALT\nOLD\n", encoding="utf-8")
        decision = _build_decision_symbol_universe_snapshot(
            candidate_symbols_path=candidate_path,
            candidate_symbols=["AAPL", "HALT", "OLD"],
            alpaca_assets=assets,
            longbridge_coverage=coverage,
            decision_date=datetime.now(timezone.utc).date(),
        )
        assert decision["final_intersection_symbols"] == ["AAPL", "HALT"]
        decision_path = Path(tmp) / "symbol_universe_intersection.json"
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        current_coverage = {
            "status": "pass",
            "errors": [],
            "covered_symbols": ["AAPL", "LEGACY"],
            "rows": [
                {"symbol": "AAPL", "covered": True, "coverage_reason": "covered"},
                {"symbol": "HALT", "covered": False, "coverage_reason": "missing_quote"},
                {"symbol": "LEGACY", "covered": True, "coverage_reason": "covered"},
            ],
        }
        execute = _build_execution_symbol_universe_snapshot(
            decision_snapshot_path=decision_path,
            target_signed_weights={"AAPL": 0.4, "LEGACY": 0.1},
            broker_weights={"LEGACY": 0.2},
            current_longbridge_coverage=current_coverage,
            decision_date=datetime.now(timezone.utc).date(),
        )
        assert execute["target_scope"]["exit_only_symbols"] == ["LEGACY"]
        assert execute["target_scope"]["invalid_target_scope_symbols"] == []
        assert execute["coverage_lost_since_decision_symbols"] == ["HALT"]
        assert execute["status"] == "pass"
        blocked = _build_execution_symbol_universe_snapshot(
            decision_snapshot_path=decision_path,
            target_signed_weights={"AAPL": 0.4, "HALT": -0.2},
            broker_weights={},
            current_longbridge_coverage=current_coverage,
            decision_date=datetime.now(timezone.utc).date(),
        )
        assert blocked["required_symbols_without_coverage"] == ["HALT"]
        assert blocked["blocking_symbols"] == ["HALT"]
        assert blocked["status"] == "error"


def test_execution_uses_directional_longbridge_quote() -> None:
    context = _FakeContext(bid=99.9, ask=100.2)
    client = LongbridgeQuoteClient(
        _credentials(),
        context_factory=lambda credentials: context,
    )
    client.start(["AAPL"])
    instruction = OrderInstruction(
        symbol="AAPL",
        side="buy",
        qty=1.0,
        reference_price=100.0,
        sizing_price=100.0,
        current_notional=0.0,
        target_notional=100.0,
        delta_notional=100.0,
        opening_short=False,
    )
    price, source, quote, error = _live_marketable_reference_price(
        client=client,
        instruction=instruction,
        execution_price_feed="us_lv1_nbbo",
        strict=True,
    )
    assert price == 100.2
    assert source == "longbridge.latest_quote.ap"
    assert quote["bp"] == 99.9
    assert error is None


def test_intraday_bars_are_unadjusted_and_sharded() -> None:
    primary = _FakeContext(candlestick_delay_seconds=0.01)
    auxiliary = _FakeContext(candlestick_delay_seconds=0.01)
    auxiliary_contexts = [auxiliary]
    client = LongbridgeQuoteClient(
        _credentials(),
        snapshot_context_count=2,
        context_factory=lambda credentials: primary,
        snapshot_context_factory=lambda credentials: auxiliary_contexts.pop(0),
    )
    symbols = [f"TEST{index}" for index in range(9)]
    payload = client.get_intraday_bars(
        symbols=symbols,
        session_date=date(2026, 8, 10),
    )
    assert payload["adjustment"] == "raw"
    assert payload["trade_sessions"] == "all"
    assert payload["missing_bar_symbols"] == []
    assert payload["errors"] == []
    assert len(payload["bars"]) == len(symbols)
    assert payload["metrics"]["worker_count"] == 2
    assert payload["metrics"]["context_count"] == 2
    assert payload["metrics"]["api_call_count"] == len(symbols)
    assert payload["metrics"]["rate_limit_error_count"] == 0
    assert payload["metrics"]["positive_volume_bar_count"] == len(symbols)
    assert payload["metrics"]["zero_volume_bar_count"] == 0
    assert payload["metrics"]["positive_volume_bar_symbol_count"] == len(symbols)
    assert payload["metrics"]["trade_session_counts"] == {
        "TradeSession.Normal": len(symbols)
    }
    assert payload["missing_positive_volume_bar_symbols"] == []
    assert payload["metrics"]["parallel_speedup_ratio"] > 1.0
    calls = [*primary.candlestick_calls, *auxiliary.candlestick_calls]
    assert len(calls) == len(symbols)
    assert all("NoAdjust" in str(call["adjust_type"]) for call in calls)
    assert all(row["provider"] == "longbridge" for row in payload["bars"])
    assert all(row["capture_source"] == "primary" for row in payload["bars"])
    client.close()


def test_quote_failure_never_reaches_broker_submit() -> None:
    class _Broker:
        def __init__(self) -> None:
            self.submit_count = 0

        def submit_order(self, **kwargs):
            self.submit_count += 1
            raise AssertionError("broker submit must not be called without an execution quote")

    class _RejectingQuoteClient:
        def get_marketable_quote(self, symbol):
            raise LongbridgeQuoteError(f"{symbol}: stale quote")

    broker = _Broker()
    records = _submit_and_track_orders(
        client=broker,
        execution_quote_client=_RejectingQuoteClient(),
        instructions=[
            OrderInstruction(
                symbol="AAPL",
                side="buy",
                qty=1.0,
                reference_price=100.0,
                sizing_price=100.0,
                current_notional=0.0,
                target_notional=100.0,
                delta_notional=100.0,
                opening_short=False,
            )
        ],
        session_token="quote-guard",
        timeout_seconds=1.0,
        poll_seconds=0.1,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.1,
    )
    assert broker.submit_count == 0
    assert records[0]["status_latest"] == "quote_unavailable"
    assert records[0]["submit_error_class"] == "execution_quote_unavailable"


def main() -> int:
    tests = [
        test_credentials_are_loaded_without_repr_leak,
        test_stream_warmup_and_class_symbol_mapping,
        test_stale_stream_quote_is_refreshed_from_snapshot,
        test_stale_depth_snapshots_are_refreshed_concurrently,
        test_stale_depth_snapshots_are_sharded_across_contexts,
        test_near_expiry_depth_is_refreshed_before_hard_limit,
        test_stale_quote_is_rejected_when_snapshot_refresh_fails,
        test_wide_quote_is_rejected,
        test_wide_quote_can_value_portfolio_but_cannot_price_order,
        test_subscription_limit_is_enforced,
        test_coverage_batches_and_classifies_static_statuses,
        test_halted_is_covered_but_rejected_for_execution,
        test_decision_intersection_and_execute_exit_scope,
        test_execution_uses_directional_longbridge_quote,
        test_intraday_bars_are_unadjusted_and_sharded,
        test_quote_failure_never_reaches_broker_submit,
    ]
    for test in tests:
        test()
        print(f"[OK] {test.__name__}")
    print(f"[PASS] {len(tests)} Longbridge market-data tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
