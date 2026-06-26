from __future__ import annotations

import argparse
import ast
import concurrent.futures
import http.client
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dynamic_symbol_pool import (  # noqa: E402
    DEFAULT_CANDIDATE_SYMBOLS_PATH,
    DynamicSymbolPool,
    _load_candidate_symbols,
    _resolve_alpaca_credentials,
)
from vendors import AlpacaHttpClient, AlpacaRequestError  # noqa: E402


DEFAULT_INDUSTRY_PATH = PROJECT_ROOT / "configs" / "universe" / "static_industry_classification.csv"
DEFAULT_SEC_CACHE_ROOT_LIVE = PROJECT_ROOT / "data" / "raw" / "sec"
DEFAULT_SEC_CACHE_ROOT_BACKTEST = PROJECT_ROOT / "data" / "backtest" / "sec"
DEFAULT_SEC_TICKER_MAP_CACHE = DEFAULT_SEC_CACHE_ROOT_LIVE / "company_tickers.json"
DEFAULT_SEC_COMPANYFACTS_CACHE_DIR = DEFAULT_SEC_CACHE_ROOT_LIVE / "companyfacts"
DEFAULT_SEC_SUBMISSIONS_CACHE_DIR = DEFAULT_SEC_CACHE_ROOT_LIVE / "submissions"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "alpha_core"

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"

FACT_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}

DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "reversal_score": 0.25,
    "momentum_score": 0.10,
    "small_size_score": 0.30,
    "low_beta_score": 0.20,
    "cash_quality_score": 0.15,
}

FACTOR_COLUMNS = (
    "reversal_score",
    "momentum_score",
    "small_size_score",
    "low_beta_score",
    "cash_quality_score",
)


@dataclass(frozen=True)
class ShareFactSpec:
    feature: str
    namespace: str
    tags: tuple[str, ...]
    source_priority: int
    is_spot_shares: bool


SHARE_FACT_SPECS = (
    ShareFactSpec(
        feature="entity_common_stock_shares_outstanding",
        namespace="dei",
        tags=("EntityCommonStockSharesOutstanding",),
        source_priority=0,
        is_spot_shares=True,
    ),
    ShareFactSpec(
        feature="weighted_average_basic_shares",
        namespace="us-gaap",
        tags=(
            "WeightedAverageNumberOfSharesOutstandingBasic",
            "WeightedAverageSharesOutstandingBasic",
        ),
        source_priority=10,
        is_spot_shares=False,
    ),
    ShareFactSpec(
        feature="weighted_average_diluted_shares",
        namespace="us-gaap",
        tags=(
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageSharesOutstandingDiluted",
        ),
        source_priority=20,
        is_spot_shares=False,
    ),
)

FACT_SPECS: dict[str, tuple[str, ...]] = {
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "stockholders_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "cash_and_short_term_investments": (
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndCashEquivalentsAndShortTermInvestments",
    ),
    "current_debt": (
        "ShortTermBorrowings",
        "ShortTermDebt",
        "LongTermDebtCurrent",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
    ),
    "noncurrent_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    ),
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
}

RAW_FACT_COLUMNS = tuple(FACT_SPECS.keys())


class SecApiClient:
    """Small SEC client with local file cache for ticker map, companyfacts, and submissions."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_requests_per_second: float = 9.0,
        ticker_map_cache_path: Path = DEFAULT_SEC_TICKER_MAP_CACHE,
        companyfacts_cache_dir: Path = DEFAULT_SEC_COMPANYFACTS_CACHE_DIR,
        submissions_cache_dir: Path = DEFAULT_SEC_SUBMISSIONS_CACHE_DIR,
        refresh_ticker_map: bool = False,
        refresh_companyfacts: bool = False,
        refresh_submissions: bool = False,
        sleep_seconds: float = 0.12,
        cache_mode: str = "network",
        memory_cache_enabled: bool = True,
    ) -> None:
        cleaned_user_agent = str(user_agent).strip()
        if not cleaned_user_agent:
            raise ValueError(
                "SEC User-Agent is required. Set --sec-user-agent (for example: "
                "'YourName your_email@example.com')."
            )

        self._user_agent = cleaned_user_agent
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._max_requests_per_second = float(max_requests_per_second)
        if not np.isfinite(self._max_requests_per_second) or self._max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be a positive number.")
        self._ticker_map_cache_path = Path(ticker_map_cache_path)
        self._companyfacts_cache_dir = Path(companyfacts_cache_dir)
        self._submissions_cache_dir = Path(submissions_cache_dir)
        self._refresh_ticker_map = bool(refresh_ticker_map)
        self._refresh_companyfacts = bool(refresh_companyfacts)
        self._refresh_submissions = bool(refresh_submissions)
        self._sleep_seconds = float(sleep_seconds)
        self._cache_mode = str(cache_mode or "network").strip().lower()
        if self._cache_mode not in {"network", "prefer", "cache_only"}:
            raise ValueError(
                "cache_mode must be one of: network / prefer / cache_only"
            )
        self._memory_cache_enabled = bool(memory_cache_enabled)
        self._throttle_lock = threading.Lock()
        self._next_request_time = 0.0
        self._min_request_interval = 1.0 / self._max_requests_per_second
        self._payload_cache_lock = threading.Lock()
        self._ticker_map_payload_mem: Mapping[str, Any] | None = None
        self._companyfacts_payload_mem: dict[str, Mapping[str, Any]] = {}
        self._submissions_payload_mem: dict[str, Mapping[str, Any]] = {}

        self._ticker_map_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._companyfacts_cache_dir.mkdir(parents=True, exist_ok=True)
        self._submissions_cache_dir.mkdir(parents=True, exist_ok=True)

    def load_ticker_to_cik_map(self) -> dict[str, str]:
        payload: Mapping[str, Any] | None = None
        allow_cache = self._cache_mode in {"prefer", "cache_only"}
        allow_network = self._cache_mode in {"network", "prefer"}

        if self._memory_cache_enabled and not self._refresh_ticker_map:
            with self._payload_cache_lock:
                if self._ticker_map_payload_mem is not None:
                    payload = self._ticker_map_payload_mem

        if payload is None and allow_cache and not self._refresh_ticker_map:
            payload = self._read_cached_json(self._ticker_map_cache_path)

        if payload is None and allow_network:
            try:
                payload = self._request_json(SEC_TICKER_MAP_URL)
                self._write_cached_json(self._ticker_map_cache_path, payload)
            except RuntimeError:
                if allow_cache:
                    payload = self._read_cached_json(self._ticker_map_cache_path)
                if payload is None:
                    raise

        if payload is None:
            raise RuntimeError(
                f"SEC ticker map unavailable under cache_mode={self._cache_mode}. "
                f"cache_path={self._ticker_map_cache_path.as_posix()}"
            )

        if self._memory_cache_enabled:
            with self._payload_cache_lock:
                self._ticker_map_payload_mem = payload

        mapping: dict[str, str] = {}
        for value in payload.values():
            if not isinstance(value, Mapping):
                continue
            ticker = str(value.get("ticker") or "").strip().upper()
            cik = _normalize_cik(value.get("cik_str"))
            if ticker and cik:
                mapping[ticker] = cik
        return mapping

    def get_companyfacts_payload(self, cik: str) -> tuple[Mapping[str, Any] | None, str, str]:
        normalized_cik = str(cik).zfill(10)
        path = self._companyfacts_cache_dir / f"CIK{normalized_cik}.json"
        url = SEC_COMPANYFACTS_URL_TEMPLATE.format(cik=normalized_cik)
        allow_cache = self._cache_mode in {"prefer", "cache_only"}
        allow_network = self._cache_mode in {"network", "prefer"}

        if self._memory_cache_enabled and not self._refresh_companyfacts:
            with self._payload_cache_lock:
                cached = self._companyfacts_payload_mem.get(normalized_cik)
            if cached is not None:
                return cached, "memory_cache", ""

        if allow_cache and not self._refresh_companyfacts:
            cached = self._read_cached_json(path)
            if cached is not None:
                if self._memory_cache_enabled:
                    with self._payload_cache_lock:
                        self._companyfacts_payload_mem[normalized_cik] = cached
                return cached, "cache", ""

        if not allow_network:
            return None, "cache_miss", "cache_only_mode_miss"

        try:
            payload = self._request_json(url)
            self._write_cached_json(path, payload)
            if self._memory_cache_enabled:
                with self._payload_cache_lock:
                    self._companyfacts_payload_mem[normalized_cik] = payload
            if self._sleep_seconds > 0:
                time.sleep(self._sleep_seconds)
            return payload, "download", ""
        except RuntimeError as exc:
            if allow_cache:
                cached = self._read_cached_json(path)
                if cached is not None:
                    if self._memory_cache_enabled:
                        with self._payload_cache_lock:
                            self._companyfacts_payload_mem[normalized_cik] = cached
                    return cached, "cache_fallback_after_error", str(exc)
            return None, "failed", str(exc)

    def get_submissions_payload(self, cik: str) -> tuple[Mapping[str, Any] | None, str, str]:
        normalized_cik = str(cik).zfill(10)
        path = self._submissions_cache_dir / f"CIK{normalized_cik}.json"
        url = SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=normalized_cik)
        allow_cache = self._cache_mode in {"prefer", "cache_only"}
        allow_network = self._cache_mode in {"network", "prefer"}

        if self._memory_cache_enabled and not self._refresh_submissions:
            with self._payload_cache_lock:
                cached = self._submissions_payload_mem.get(normalized_cik)
            if cached is not None:
                return cached, "memory_cache", ""

        if allow_cache and not self._refresh_submissions:
            cached = self._read_cached_json(path)
            if cached is not None:
                if self._memory_cache_enabled:
                    with self._payload_cache_lock:
                        self._submissions_payload_mem[normalized_cik] = cached
                return cached, "cache", ""

        if not allow_network:
            return None, "cache_miss", "cache_only_mode_miss"

        try:
            payload = self._request_json(url)
            self._write_cached_json(path, payload)
            if self._memory_cache_enabled:
                with self._payload_cache_lock:
                    self._submissions_payload_mem[normalized_cik] = payload
            if self._sleep_seconds > 0:
                time.sleep(self._sleep_seconds)
            return payload, "download", ""
        except RuntimeError as exc:
            if allow_cache:
                cached = self._read_cached_json(path)
                if cached is not None:
                    if self._memory_cache_enabled:
                        with self._payload_cache_lock:
                            self._submissions_payload_mem[normalized_cik] = cached
                    return cached, "cache_fallback_after_error", str(exc)
            return None, "failed", str(exc)

    @staticmethod
    def _read_cached_json(path: Path) -> Mapping[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    @staticmethod
    def _write_cached_json(path: Path, payload: Mapping[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        except OSError:
            pass

    def _request_json(self, url: str) -> Mapping[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            conn: http.client.HTTPConnection | None = None
            try:
                self._acquire_request_slot()
                parts = urlsplit(url)
                if parts.scheme not in {"https", "http"}:
                    raise RuntimeError(f"Unsupported SEC URL scheme: {url}")
                host = parts.netloc
                path = parts.path or "/"
                if parts.query:
                    path = f"{path}?{parts.query}"

                conn_cls = (
                    http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
                )
                conn = conn_cls(host, timeout=self._timeout_seconds)
                conn.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": "application/json",
                        "Connection": "close",
                    },
                )
                response = conn.getresponse()
                body = response.read()
                status = int(response.status)
                if 200 <= status < 300:
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, Mapping):
                        raise RuntimeError(f"SEC response is not a mapping: {url}")
                    return payload

                detail = body.decode("utf-8", errors="replace")
                retryable = status == 429 or 500 <= status < 600
                last_error = RuntimeError(f"SEC HTTP {status}: {detail}")
                if not retryable or attempt >= self._max_retries:
                    raise last_error
            except (TimeoutError, OSError, http.client.HTTPException, json.JSONDecodeError, RuntimeError) as exc:
                last_error = RuntimeError(f"SEC request failed: {exc}")
                if attempt >= self._max_retries:
                    raise last_error from exc
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
            time.sleep(min(2 ** attempt, 5))

        raise RuntimeError(f"SEC request failed: {last_error}")

    def _acquire_request_slot(self) -> None:
        with self._throttle_lock:
            now = time.monotonic()
            slot_time = max(now, self._next_request_time)
            self._next_request_time = slot_time + self._min_request_interval
            wait_seconds = slot_time - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)


class AlphaCore:
    """Build single-day 5-factor panel aligned with Phase7K scoring style."""

    def __init__(
        self,
        *,
        alpaca_client: AlpacaHttpClient,
        sec_client: SecApiClient,
        industry_map: pd.DataFrame,
        sec_submissions_workers: int = 8,
        sec_companyfacts_workers: int = 8,
        feed: str = "iex",
        price_adjustment: str = "all",
        bars_window_calendar_days: int = 420,
        bars_chunk_size: int = 120,
        bars_workers: int = 4,
        benchmark_symbol: str = "SPY",
        beta_lookback_sessions: int = 252,
        beta_min_observations: int = 126,
        beta_shrinkage_target: float = 1.0,
        beta_shrinkage_strength: float = 0.10,
        beta_clip_low: float | None = 0.0,
        beta_clip_high: float | None = 3.0,
        max_price_staleness_days: int = 5,
        factor_weights: Mapping[str, float] = DEFAULT_FACTOR_WEIGHTS,
    ) -> None:
        self._alpaca_client = alpaca_client
        self._sec_client = sec_client
        self._industry_map = industry_map.copy()
        self._sec_submissions_workers = max(1, int(sec_submissions_workers))
        self._sec_companyfacts_workers = max(1, int(sec_companyfacts_workers))
        self._feed = str(feed)
        self._price_adjustment = str(price_adjustment)
        self._bars_window_calendar_days = int(bars_window_calendar_days)
        self._bars_chunk_size = int(bars_chunk_size)
        self._bars_workers = max(1, int(bars_workers))
        self._benchmark_symbol = str(benchmark_symbol).strip().upper()
        self._beta_lookback_sessions = int(beta_lookback_sessions)
        self._beta_min_observations = int(beta_min_observations)
        self._beta_shrinkage_target = float(beta_shrinkage_target)
        self._beta_shrinkage_strength = float(beta_shrinkage_strength)
        self._beta_clip_low = beta_clip_low
        self._beta_clip_high = beta_clip_high
        self._max_price_staleness_days = max(0, int(max_price_staleness_days))
        self._factor_weights = dict(factor_weights)

    def build_for_date(self, *, as_of_date: str | date | datetime, symbols: Sequence[str]) -> pd.DataFrame:
        target_date = _normalize_date(as_of_date)
        target_date_str = target_date.isoformat()
        data_cutoff_date = target_date - timedelta(days=1)
        data_cutoff_str = data_cutoff_date.isoformat()
        cleaned_symbols = _normalize_symbols(symbols)
        if not cleaned_symbols:
            raise ValueError("No symbols provided to AlphaCore.")

        print(
            f"[AlphaCore] decision_date={target_date_str}, data_cutoff_date={data_cutoff_str}",
            flush=True,
        )
        print(
            f"[AlphaCore] Step 1/4: pulling Alpaca bars for {len(cleaned_symbols)} symbols ...",
            flush=True,
        )
        price_frame = self._build_price_features(
            cleaned_symbols,
            target_date=target_date,
            data_cutoff_date=data_cutoff_date,
        )
        print("[AlphaCore] Step 1/4 done.", flush=True)

        print(
            f"[AlphaCore] Step 2/4: pulling SEC companyfacts for {len(cleaned_symbols)} symbols ...",
            flush=True,
        )
        sec_frame = self._build_sec_features(cleaned_symbols, data_cutoff_date=data_cutoff_date)
        print("[AlphaCore] Step 2/4 done.", flush=True)

        print("[AlphaCore] Step 3/4: merging with industry map ...", flush=True)
        base = pd.DataFrame({"symbol": cleaned_symbols})
        merged = base.merge(
            self._industry_map[["symbol", "sic2_sector", "sic4_industry"]],
            on="symbol",
            how="left",
        )
        merged = merged.merge(price_frame, on="symbol", how="left")
        merged = merged.merge(sec_frame, on="symbol", how="left")

        merged["session_date"] = target_date_str
        merged["sic2_sector"] = merged["sic2_sector"].fillna("UNKNOWN").astype(str)
        merged["sic4_industry"] = merged["sic4_industry"].fillna("UNKNOWN").astype(str)

        merged["market_cap_price"] = merged["lagged_raw_close"].where(
            merged["lagged_raw_close"].notna(),
            merged["close"],
        )
        merged["market_cap_price_source"] = np.where(
            merged["lagged_raw_close"].notna(),
            "lagged_raw_close",
            np.where(merged["close"].notna(), "close_fallback", ""),
        )
        merged["market_cap"] = pd.to_numeric(merged["market_cap_price"], errors="coerce") * pd.to_numeric(
            merged["shares_outstanding"],
            errors="coerce",
        )
        merged.loc[~np.isfinite(merged["market_cap"]) | merged["market_cap"].le(0.0), "market_cap"] = np.nan
        merged["market_cap_log"] = np.log(merged["market_cap"])

        merged["cash_or_st_investments"] = merged["cash_and_short_term_investments"].where(
            pd.to_numeric(merged["cash_and_short_term_investments"], errors="coerce").notna(),
            merged["cash"],
        )
        merged["cash_to_assets"] = _safe_div_series(merged["cash_or_st_investments"], merged["assets"])

        print("[AlphaCore] Step 3/4 done.", flush=True)

        print("[AlphaCore] Step 4/4: computing 5 raw factors + sector zscore + composite ...", flush=True)
        scored = self._score_factors(merged)
        print("[AlphaCore] Step 4/4 done.", flush=True)
        return scored

    def _build_price_features(
        self,
        symbols: Sequence[str],
        *,
        target_date: date,
        data_cutoff_date: date,
    ) -> pd.DataFrame:
        start = data_cutoff_date - timedelta(days=self._bars_window_calendar_days)
        end = data_cutoff_date
        all_symbols = sorted(set(symbols).union({self._benchmark_symbol}))

        bars = self._collect_bars_for_symbols(
            symbols=all_symbols,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        panel = _bars_to_price_panel(bars)
        if panel.empty:
            return pd.DataFrame(
                columns=[
                    "symbol",
                    "price_asof_session_date",
                    "close",
                    "lagged_raw_close",
                    "return_5d",
                    "momentum_l120_s20",
                    "beta_raw",
                    "beta",
                    "beta_obs",
                ]
            )

        benchmark = panel[panel["symbol"].eq(self._benchmark_symbol)].copy()
        benchmark = benchmark.sort_values("session_date")
        benchmark["benchmark_return"] = benchmark["close"].pct_change()
        benchmark_returns = benchmark[["session_date", "benchmark_return"]].copy()

        stocks = panel[panel["symbol"].isin(set(symbols))].copy()
        stocks = stocks.sort_values(["symbol", "session_date"])
        grouped = stocks.groupby("symbol", group_keys=False)
        stocks["symbol_return"] = grouped["close"].pct_change()
        stocks["return_5d"] = grouped["close"].pct_change(5)
        stocks["momentum_l120_s20"] = grouped["close"].shift(20) / grouped["close"].shift(140) - 1.0
        stocks["lagged_raw_close"] = grouped["close"].shift(1)

        return_pairs = stocks[["session_date", "symbol", "symbol_return"]].merge(
            benchmark_returns,
            on="session_date",
            how="left",
        )
        beta_estimates = _lagged_rolling_beta(
            return_pairs,
            lookback_sessions=self._beta_lookback_sessions,
            min_observations=self._beta_min_observations,
            shrinkage_target=self._beta_shrinkage_target,
            shrinkage_strength=self._beta_shrinkage_strength,
            beta_clip_low=self._beta_clip_low,
            beta_clip_high=self._beta_clip_high,
            asof_lag_sessions=1,
        )

        enriched = stocks.merge(
            beta_estimates[["session_date", "symbol", "beta_raw", "beta", "beta_obs"]],
            on=["session_date", "symbol"],
            how="left",
        )
        target_str = data_cutoff_date.isoformat()
        asof_rows = (
            enriched[enriched["session_date"].astype(str).le(target_str)]
            .sort_values(["symbol", "session_date"])
            .drop_duplicates("symbol", keep="last")
        )
        if asof_rows.empty:
            raise ValueError(
                f"No price rows are available on or before data_cutoff_date={target_str} "
                f"for decision_date={target_date.isoformat()}."
            )
        latest_price_session = pd.to_datetime(asof_rows["session_date"], errors="coerce").max()
        if pd.isna(latest_price_session):
            raise ValueError(
                f"Unable to determine latest price session on or before data_cutoff_date={target_str}."
            )
        staleness_days = (data_cutoff_date - latest_price_session.date()).days
        if staleness_days > self._max_price_staleness_days:
            raise ValueError(
                "Price data is stale for requested decision date. "
                f"decision_date={target_date.isoformat()}, "
                f"data_cutoff_date={target_str}, "
                f"latest_price_session={latest_price_session.date().isoformat()}, "
                f"staleness_days={staleness_days}, "
                f"max_price_staleness_days={self._max_price_staleness_days}."
            )

        out = pd.DataFrame({"symbol": list(symbols)})
        out = out.merge(
            asof_rows[
                [
                    "symbol",
                    "session_date",
                    "close",
                    "lagged_raw_close",
                    "return_5d",
                    "momentum_l120_s20",
                    "beta_raw",
                    "beta",
                    "beta_obs",
                ]
            ].rename(columns={"session_date": "price_asof_session_date"}),
            on="symbol",
            how="left",
        )
        return out

    def _collect_bars_for_symbols(self, *, symbols: Sequence[str], start: str, end: str) -> list[dict[str, Any]]:
        if not symbols:
            return []
        rows: list[dict[str, Any]] = []
        chunks = [list(chunk) for chunk in _chunks(symbols, self._bars_chunk_size)]
        total_chunks = max(1, len(chunks))
        worker_count = max(1, min(self._bars_workers, total_chunks))
        _print_progress(label="Alpaca bars", current=0, total=total_chunks)
        if worker_count == 1:
            for idx, chunk in enumerate(chunks, start=1):
                bars = self._alpaca_client.get_stock_bars(
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Day",
                    adjustment=self._price_adjustment,
                    feed=self._feed,
                    limit=10000,
                )
                rows.extend(bar for bar in bars if isinstance(bar, Mapping))
                _print_progress(label="Alpaca bars", current=idx, total=total_chunks)
            return rows

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        interrupted = False
        future_to_chunk: dict[concurrent.futures.Future[list[dict[str, Any]]], list[str]] = {}
        try:
            future_to_chunk = {
                executor.submit(
                    self._alpaca_client.get_stock_bars,
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Day",
                    adjustment=self._price_adjustment,
                    feed=self._feed,
                    limit=10000,
                ): chunk
                for chunk in chunks
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_to_chunk):
                bars = future.result()
                rows.extend(bar for bar in bars if isinstance(bar, Mapping))
                completed += 1
                _print_progress(label="Alpaca bars", current=completed, total=total_chunks)
        except KeyboardInterrupt:
            interrupted = True
            for future in future_to_chunk:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            print("\n[AlphaCore] interrupted by Ctrl+C during Alpaca bars fetch.", flush=True)
            raise
        finally:
            if not interrupted:
                executor.shutdown(wait=True)
        return rows

    def _build_sec_features(self, symbols: Sequence[str], *, data_cutoff_date: date) -> pd.DataFrame:
        target_str = data_cutoff_date.isoformat()
        try:
            ticker_to_cik = self._sec_client.load_ticker_to_cik_map()
        except RuntimeError as exc:
            print(f"[AlphaCore] warning: ticker->CIK map unavailable ({exc})", flush=True)
            ticker_to_cik = {}

        symbol_to_cik = {symbol: str(ticker_to_cik.get(symbol) or "").zfill(10) for symbol in symbols}
        unique_ciks = sorted({cik for cik in symbol_to_cik.values() if cik and cik != "0000000000"})
        payload_by_cik = _parallel_fetch_cik_payloads(
            ciks=unique_ciks,
            fetch_fn=self._sec_client.get_companyfacts_payload,
            label="SEC companyfacts",
            max_workers=self._sec_companyfacts_workers,
        )

        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            row: dict[str, Any] = {
                "symbol": symbol,
                "cik": "",
                "sec_status": "unknown",
                "sec_payload_source": "",
                "sec_error": "",
                "shares_outstanding": np.nan,
                "share_source": "",
                "share_source_tag": "",
                "share_source_priority": np.nan,
                "share_is_spot": False,
                "share_filed_date": "",
                "share_period_end": "",
                "share_form": "",
                "share_accession": "",
                "last_fundamental_filed_date": "",
                "last_fundamental_period_end": "",
            }
            for concept in RAW_FACT_COLUMNS:
                row[concept] = np.nan

            cik = symbol_to_cik.get(symbol, "")
            if not cik:
                row["sec_status"] = "missing_cik_mapping"
                rows.append(row)
                continue

            row["cik"] = cik
            payload, payload_source, payload_error = payload_by_cik.get(cik, (None, "missing", "payload_not_fetched"))
            row["sec_payload_source"] = payload_source
            row["sec_error"] = payload_error
            if payload is None:
                row["sec_status"] = "payload_unavailable"
                rows.append(row)
                continue

            share_snapshot = _extract_share_snapshot(payload, as_of_date=target_str)
            fundamental_snapshot = _extract_fundamental_snapshot(payload, as_of_date=target_str)
            row.update(share_snapshot)
            row.update(fundamental_snapshot)
            row["sec_status"] = "ok"
            rows.append(row)

        frame = pd.DataFrame(rows)
        for column in ("shares_outstanding", *RAW_FACT_COLUMNS):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    def _score_factors(self, frame: pd.DataFrame) -> pd.DataFrame:
        scored = frame.copy()

        raw_map = {
            "reversal_score": -pd.to_numeric(scored["return_5d"], errors="coerce"),
            "momentum_score": pd.to_numeric(scored["momentum_l120_s20"], errors="coerce"),
            "small_size_score": -pd.to_numeric(scored["market_cap_log"], errors="coerce"),
            "low_beta_score": -pd.to_numeric(scored["beta"], errors="coerce"),
            "cash_quality_score": pd.to_numeric(scored["cash_to_assets"], errors="coerce"),
        }

        for factor, raw_series in raw_map.items():
            raw_column = f"{factor}_raw"
            scored[raw_column] = pd.to_numeric(raw_series, errors="coerce").replace([np.inf, -np.inf], np.nan)
            scored[factor] = _sector_zscore(scored, raw_column).fillna(0.0)

        total_weight = sum(abs(float(weight)) for weight in self._factor_weights.values())
        if total_weight <= 0.0:
            raise ValueError("factor weights must contain at least one non-zero value")

        composite_raw = np.zeros(len(scored), dtype=float)
        for factor, weight in self._factor_weights.items():
            if factor not in scored.columns:
                continue
            composite_raw += float(weight) * pd.to_numeric(scored[factor], errors="coerce").fillna(0.0).to_numpy(
                dtype=float
            )
        scored["composite_score_raw"] = composite_raw / total_weight
        scored["composite_score"] = _single_date_zscore(scored["composite_score_raw"]).fillna(0.0)
        scored["composite_rank"] = scored["composite_score"].rank(method="average", ascending=False, pct=True)

        scored = scored.sort_values(["composite_score", "symbol"], ascending=[False, True]).reset_index(drop=True)
        return scored


def _load_industry_map(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Industry static file not found: {path.as_posix()}. "
            f"Please place it under configs (default: {DEFAULT_INDUSTRY_PATH.as_posix()})."
        )
    frame = pd.read_csv(path)
    if "symbol" not in frame.columns:
        raise ValueError(f"Industry file {path.as_posix()} must include column: symbol")

    sector_candidates = ("sic2_sector", "sector", "industry_sector", "gics_sector")
    industry_candidates = ("sic4_industry", "industry", "sub_industry")
    sector_column = next((name for name in sector_candidates if name in frame.columns), None)
    industry_column = next((name for name in industry_candidates if name in frame.columns), None)
    if sector_column is None:
        raise ValueError(
            "Industry file must include one sector column from: "
            "sic2_sector / sector / industry_sector / gics_sector"
        )

    out = pd.DataFrame(
        {
            "symbol": frame["symbol"].astype(str).str.strip().str.upper(),
            "sic2_sector": frame[sector_column].astype(str),
            "sic4_industry": frame[industry_column].astype(str) if industry_column else "UNKNOWN",
        }
    )
    out["sic2_sector"] = out["sic2_sector"].replace({"": "UNKNOWN", "nan": "UNKNOWN"}).fillna("UNKNOWN")
    out["sic4_industry"] = out["sic4_industry"].replace({"": "UNKNOWN", "nan": "UNKNOWN"}).fillna("UNKNOWN")
    out = out[out["symbol"].ne("")].drop_duplicates("symbol", keep="first").reset_index(drop=True)
    return out


def _resolve_industry_map_for_symbols(
    *,
    symbols: Sequence[str],
    sec_client: SecApiClient,
    industry_cache_output_path: Path | None,
    submissions_workers: int,
) -> pd.DataFrame:
    symbol_list = _normalize_symbols(symbols)
    print("[AlphaCore] industry source: SEC submissions (dynamic)", flush=True)
    resolved = _build_industry_map_from_sec(
        symbols=symbol_list,
        sec_client=sec_client,
        max_workers=submissions_workers,
    )
    resolved["sic2_sector"] = resolved["sic2_sector"].fillna("UNKNOWN").astype(str)
    resolved["sic4_industry"] = resolved["sic4_industry"].fillna("UNKNOWN").astype(str)
    resolved.loc[resolved["sic2_sector"].str.strip().eq(""), "sic2_sector"] = "UNKNOWN"
    resolved.loc[resolved["sic4_industry"].str.strip().eq(""), "sic4_industry"] = "UNKNOWN"

    known_rate = float(resolved["sic2_sector"].ne("UNKNOWN").mean()) if len(resolved) else 0.0
    print(
        f"[AlphaCore] industry coverage: {int(resolved['sic2_sector'].ne('UNKNOWN').sum())}/"
        f"{len(resolved)} ({known_rate * 100:.2f}%)",
        flush=True,
    )

    if industry_cache_output_path is not None:
        industry_cache_output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved.to_csv(industry_cache_output_path, index=False)
        print(
            f"[AlphaCore] wrote resolved industry map: {industry_cache_output_path.as_posix()}",
            flush=True,
        )
    return resolved


def _parallel_fetch_cik_payloads(
    *,
    ciks: Sequence[str],
    fetch_fn: Callable[[str], tuple[Mapping[str, Any] | None, str, str]],
    label: str,
    max_workers: int,
) -> dict[str, tuple[Mapping[str, Any] | None, str, str]]:
    normalized_ciks = sorted(
        {
            normalized
            for normalized in (_normalize_cik(cik) for cik in ciks)
            if normalized and normalized != "0000000000"
        }
    )
    if not normalized_ciks:
        return {}

    total = len(normalized_ciks)
    worker_count = max(1, min(int(max_workers), total))
    results: dict[str, tuple[Mapping[str, Any] | None, str, str]] = {}
    _print_progress(label=label, current=0, total=total)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    interrupted = False
    future_to_cik: dict[concurrent.futures.Future[tuple[Mapping[str, Any] | None, str, str]], str] = {}
    try:
        future_to_cik = {executor.submit(fetch_fn, cik): cik for cik in normalized_ciks}
        completed = 0
        for future in concurrent.futures.as_completed(future_to_cik):
            cik = future_to_cik[future]
            try:
                payload, payload_source, payload_error = future.result()
            except Exception as exc:  # defensive: thread worker should not crash whole loop
                payload, payload_source, payload_error = None, "failed", f"{type(exc).__name__}: {exc}"
            if not isinstance(payload, Mapping):
                payload = None
            results[cik] = (
                payload,
                str(payload_source or ""),
                str(payload_error or ""),
            )
            completed += 1
            _print_progress(label=label, current=completed, total=total)
    except KeyboardInterrupt:
        interrupted = True
        for future in future_to_cik:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        print(f"\n[AlphaCore] interrupted by Ctrl+C during {label}.", flush=True)
        raise
    finally:
        if not interrupted:
            executor.shutdown(wait=True)
    return results


def _build_industry_map_from_sec(
    *,
    symbols: Sequence[str],
    sec_client: SecApiClient,
    max_workers: int,
) -> pd.DataFrame:
    try:
        ticker_to_cik = sec_client.load_ticker_to_cik_map()
    except RuntimeError as exc:
        print(f"[AlphaCore] warning: ticker->CIK map unavailable for industries ({exc})", flush=True)
        ticker_to_cik = {}

    symbol_to_cik = {symbol: _normalize_cik(ticker_to_cik.get(symbol)) for symbol in symbols}
    unique_ciks = sorted({cik for cik in symbol_to_cik.values() if cik})
    payload_by_cik = _parallel_fetch_cik_payloads(
        ciks=unique_ciks,
        fetch_fn=sec_client.get_submissions_payload,
        label="SEC submissions",
        max_workers=max_workers,
    )

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        cik = symbol_to_cik.get(symbol, "")
        sic2_sector = "UNKNOWN"
        sic4_industry = "UNKNOWN"
        if cik:
            payload, _, _ = payload_by_cik.get(cik, (None, "missing", "payload_not_fetched"))
            if payload is not None:
                sic_str = _normalize_sic_string(payload.get("sic"))
                if sic_str:
                    sic2_sector = f"SIC{sic_str[:2]}"
                    sic4_industry = f"SIC{sic_str}"
        rows.append(
            {
                "symbol": symbol,
                "sic2_sector": sic2_sector,
                "sic4_industry": sic4_industry,
            }
        )
    return pd.DataFrame(rows)


def _normalize_sic_string(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(int(float(text))).zfill(4)
    except (TypeError, ValueError):
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return ""
        return digits[:4].zfill(4)


def _normalize_cik(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(int(float(text))).zfill(10)
    except (TypeError, ValueError):
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return ""
        return digits.zfill(10)[-10:]


def _resolve_symbols(
    *,
    args: argparse.Namespace,
    alpaca_client: AlpacaHttpClient,
) -> list[str]:
    if args.symbols_literal:
        symbols = _parse_symbols_literal(args.symbols_literal)
        if symbols:
            print(f"[AlphaCore] symbols source: --symbols-literal ({len(symbols)})", flush=True)
            return symbols
        raise ValueError("--symbols-literal is provided but no valid symbols were parsed.")

    if args.symbols_path:
        symbols = _load_symbols_file(Path(args.symbols_path))
        print(f"[AlphaCore] symbols source: --symbols-path ({len(symbols)})", flush=True)
        return symbols

    candidate_symbols = _load_candidate_symbols(Path(args.candidate_symbols_path))
    dynamic_pool = DynamicSymbolPool(
        client=alpaca_client,
        candidate_symbols=candidate_symbols,
        pool_size=int(args.pool_size),
        lookback_sessions=int(args.lookback_sessions),
        min_observations=int(args.min_observations),
        price_floor=float(args.price_floor),
        bars_window_calendar_days=int(args.dynamic_bars_window_calendar_days),
        bars_chunk_size=int(args.dynamic_bars_chunk_size),
        bars_workers=int(args.bars_workers),
        feed=str(args.feed),
        beta_full_observations=int(args.dynamic_beta_full_observations),
    )
    symbols = sorted(dynamic_pool.fresh(args.date))
    print(f"[AlphaCore] symbols source: DynamicSymbolPool ({len(symbols)})", flush=True)
    if args.dynamic_symbols_output_path:
        output_path = Path(args.dynamic_symbols_output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(symbols), encoding="utf-8")
        print(f"[AlphaCore] wrote dynamic symbols to: {output_path.as_posix()}", flush=True)
    return symbols


def _parse_symbols_literal(raw: str) -> list[str]:
    text = str(raw).strip()
    if not text:
        return []
    try:
        literal = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        literal = None
    if isinstance(literal, (list, set, tuple)):
        return _normalize_symbols(str(item) for item in literal)
    if isinstance(literal, str):
        return _normalize_symbols([literal])

    separators = text.replace("{", "").replace("}", "").replace("[", "").replace("]", "")
    pieces = [part.strip().strip("'").strip('"') for part in separators.replace(";", ",").split(",")]
    return _normalize_symbols(piece for piece in pieces if piece)


def _load_symbols_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"symbols file not found: {path.as_posix()}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _normalize_symbols(path.read_text(encoding="utf-8").splitlines())
    if suffix == ".csv":
        frame = pd.read_csv(path)
        if "symbol" not in frame.columns:
            raise ValueError(f"CSV {path.as_posix()} must include column: symbol")
        return _normalize_symbols(frame["symbol"].tolist())
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return _normalize_symbols(payload)
        if isinstance(payload, Mapping):
            symbols = payload.get("symbols")
            if isinstance(symbols, list):
                return _normalize_symbols(symbols)
        raise ValueError(f"JSON {path.as_posix()} must be list[str] or {{\"symbols\": [...]}}")
    return _parse_symbols_literal(path.read_text(encoding="utf-8"))


def _normalize_symbols(values: Iterable[Any]) -> list[str]:
    cleaned = sorted({str(value).strip().upper() for value in values if str(value).strip()})
    return cleaned


def _extract_share_snapshot(payload: Mapping[str, Any], *, as_of_date: str) -> dict[str, Any]:
    facts = payload.get("facts", {})
    if not isinstance(facts, Mapping):
        return _empty_share_snapshot()

    candidates: list[dict[str, Any]] = []
    for spec in SHARE_FACT_SPECS:
        namespace_payload = facts.get(spec.namespace, {})
        if not isinstance(namespace_payload, Mapping):
            continue
        for tag_offset, tag in enumerate(spec.tags):
            tag_payload = namespace_payload.get(tag)
            if not isinstance(tag_payload, Mapping):
                continue
            for unit, values in _iter_fact_units(tag_payload):
                if unit != "shares":
                    continue
                for fact in values:
                    if not isinstance(fact, Mapping):
                        continue
                    filed_date = _clean_date(fact.get("filed"))
                    period_end = _clean_date(fact.get("end"))
                    form = str(fact.get("form") or "")
                    if not filed_date or not period_end or form not in FACT_FORMS:
                        continue
                    if filed_date > as_of_date:
                        continue
                    value = _safe_float(fact.get("val"))
                    if value is None or not np.isfinite(value) or value <= 0:
                        continue
                    if value < 1_000 or value > 100_000_000_000_000:
                        continue
                    candidates.append(
                        {
                            "shares_outstanding": float(value),
                            "share_source": spec.feature,
                            "share_source_tag": tag,
                            "share_source_priority": int(spec.source_priority + tag_offset),
                            "share_is_spot": bool(spec.is_spot_shares),
                            "share_filed_date": filed_date,
                            "share_period_end": period_end,
                            "share_form": form,
                            "share_accession": str(fact.get("accn") or ""),
                        }
                    )

    if not candidates:
        return _empty_share_snapshot()

    best = max(
        candidates,
        key=lambda row: (
            row["share_filed_date"],
            -int(row["share_source_priority"]),
            row["share_period_end"],
            str(row["share_accession"]),
        ),
    )
    return best


def _extract_fundamental_snapshot(payload: Mapping[str, Any], *, as_of_date: str) -> dict[str, Any]:
    output: dict[str, Any] = {concept: np.nan for concept in RAW_FACT_COLUMNS}
    output["last_fundamental_filed_date"] = ""
    output["last_fundamental_period_end"] = ""

    facts = payload.get("facts", {})
    if not isinstance(facts, Mapping):
        return output
    us_gaap = facts.get("us-gaap", {})
    if not isinstance(us_gaap, Mapping):
        return output

    last_filed = ""
    last_period_end = ""
    for concept, tags in FACT_SPECS.items():
        concept_candidates: list[dict[str, Any]] = []
        for source_priority, tag in enumerate(tags):
            tag_payload = us_gaap.get(tag)
            if not isinstance(tag_payload, Mapping):
                continue
            for unit, values in _iter_fact_units(tag_payload):
                if unit != "USD":
                    continue
                for fact in values:
                    if not isinstance(fact, Mapping):
                        continue
                    filed_date = _clean_date(fact.get("filed"))
                    period_end = _clean_date(fact.get("end"))
                    form = str(fact.get("form") or "")
                    if not filed_date or not period_end or form not in FACT_FORMS:
                        continue
                    if filed_date > as_of_date:
                        continue
                    value = _safe_float(fact.get("val"))
                    if value is None or not np.isfinite(value):
                        continue
                    concept_candidates.append(
                        {
                            "value": float(value),
                            "filed_date": filed_date,
                            "period_end": period_end,
                            "source_priority": int(source_priority),
                            "accession": str(fact.get("accn") or ""),
                        }
                    )
        if not concept_candidates:
            continue
        best = max(
            concept_candidates,
            key=lambda row: (
                row["filed_date"],
                -int(row["source_priority"]),
                row["period_end"],
                str(row["accession"]),
            ),
        )
        output[concept] = float(best["value"])
        if best["filed_date"] > last_filed or (
            best["filed_date"] == last_filed and best["period_end"] > last_period_end
        ):
            last_filed = best["filed_date"]
            last_period_end = best["period_end"]

    output["last_fundamental_filed_date"] = last_filed
    output["last_fundamental_period_end"] = last_period_end
    return output


def _empty_share_snapshot() -> dict[str, Any]:
    return {
        "shares_outstanding": np.nan,
        "share_source": "",
        "share_source_tag": "",
        "share_source_priority": np.nan,
        "share_is_spot": False,
        "share_filed_date": "",
        "share_period_end": "",
        "share_form": "",
        "share_accession": "",
    }


def _iter_fact_units(tag_payload: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    units = tag_payload.get("units", {})
    if not isinstance(units, Mapping):
        return []
    ordered: list[tuple[str, list[Any]]] = []
    for unit in ("USD", "USD/shares", "shares"):
        values = units.get(unit)
        if isinstance(values, list):
            ordered.append((unit, values))
    for unit, values in units.items():
        if unit in {"USD", "USD/shares", "shares"}:
            continue
        if isinstance(values, list):
            ordered.append((str(unit), values))
    return ordered


def _bars_to_price_panel(bars: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        symbol = str(bar.get("symbol") or "").strip().upper()
        timestamp = str(bar.get("t") or bar.get("timestamp") or "")
        session_date = timestamp[:10] if len(timestamp) >= 10 else ""
        close = _safe_float(bar.get("c"))
        if close is None:
            close = _safe_float(bar.get("close"))
        if not symbol or not session_date or close is None or close <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "close": float(close),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["symbol", "session_date", "close"])
    frame = pd.DataFrame(rows)
    frame = (
        frame.sort_values(["symbol", "session_date"])
        .drop_duplicates(["symbol", "session_date"], keep="last")
        .reset_index(drop=True)
    )
    return frame


def _lagged_rolling_beta(
    return_pairs: pd.DataFrame,
    *,
    lookback_sessions: int,
    min_observations: int,
    shrinkage_target: float,
    shrinkage_strength: float,
    beta_clip_low: float | None,
    beta_clip_high: float | None,
    asof_lag_sessions: int,
) -> pd.DataFrame:
    if return_pairs.empty:
        return pd.DataFrame(columns=["session_date", "symbol", "beta_raw", "beta", "beta_obs"])
    pieces: list[pd.DataFrame] = []
    for symbol, group in return_pairs.groupby("symbol", sort=False):
        pieces.append(
            _lagged_rolling_beta_one_symbol(
                symbol=str(symbol),
                group=group,
                lookback_sessions=lookback_sessions,
                min_observations=min_observations,
                shrinkage_target=shrinkage_target,
                shrinkage_strength=shrinkage_strength,
                beta_clip_low=beta_clip_low,
                beta_clip_high=beta_clip_high,
                asof_lag_sessions=asof_lag_sessions,
            )
        )
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(
        columns=["session_date", "symbol", "beta_raw", "beta", "beta_obs"]
    )


def _lagged_rolling_beta_one_symbol(
    symbol: str,
    group: pd.DataFrame,
    *,
    lookback_sessions: int,
    min_observations: int,
    shrinkage_target: float,
    shrinkage_strength: float,
    beta_clip_low: float | None,
    beta_clip_high: float | None,
    asof_lag_sessions: int,
) -> pd.DataFrame:
    ordered = group.sort_values("session_date").copy()
    x = pd.to_numeric(ordered["benchmark_return"], errors="coerce").shift(asof_lag_sessions)
    y = pd.to_numeric(ordered["symbol_return"], errors="coerce").shift(asof_lag_sessions)
    xy = x * y
    x2 = x * x

    rolling = {
        "obs": x.rolling(lookback_sessions, min_periods=min_observations).count(),
        "sum_x": x.rolling(lookback_sessions, min_periods=min_observations).sum(),
        "sum_y": y.rolling(lookback_sessions, min_periods=min_observations).sum(),
        "sum_xy": xy.rolling(lookback_sessions, min_periods=min_observations).sum(),
        "sum_x2": x2.rolling(lookback_sessions, min_periods=min_observations).sum(),
    }
    obs = rolling["obs"]
    cov_num = rolling["sum_xy"] - (rolling["sum_x"] * rolling["sum_y"] / obs)
    var_num = rolling["sum_x2"] - (rolling["sum_x"] * rolling["sum_x"] / obs)
    beta_raw = cov_num / var_num
    beta_raw = beta_raw.where((obs >= min_observations) & (var_num > 0))
    beta_shrunk = (1.0 - shrinkage_strength) * beta_raw + shrinkage_strength * shrinkage_target
    beta = beta_shrunk.copy()
    if beta_clip_low is not None or beta_clip_high is not None:
        beta = beta.clip(lower=beta_clip_low, upper=beta_clip_high)

    return pd.DataFrame(
        {
            "session_date": ordered["session_date"].to_numpy(),
            "symbol": symbol,
            "beta_raw": beta_raw,
            "beta": beta,
            "beta_obs": obs.fillna(0).astype(int),
        }
    )


def _sector_zscore(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    grouped = values.groupby([frame["session_date"], frame["sic2_sector"]])
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    z = (values - mean) / std.where(std > 1e-12)
    fallback = _date_zscore(frame, column)
    return z.where(np.isfinite(z), fallback).clip(-3.0, 3.0)


def _date_zscore(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    grouped = values.groupby(frame["session_date"])
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    return ((values - mean) / std.where(std > 1e-12)).clip(-3.0, 3.0)


def _single_date_zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    mean = values.mean()
    std = values.std()
    if not np.isfinite(std) or std <= 1e-12:
        return pd.Series(np.zeros(len(values)), index=values.index)
    return ((values - mean) / std).clip(-3.0, 3.0)


def _safe_div_series(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denom = pd.to_numeric(denominator, errors="coerce").replace(0.0, np.nan)
    return pd.to_numeric(numerator, errors="coerce") / denom


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _clean_date(value: Any) -> str:
    text = str(value or "")
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else ""


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _print_progress(*, label: str, current: int, total: int, width: int = 28) -> None:
    safe_total = max(1, int(total))
    safe_current = min(max(0, int(current)), safe_total)
    ratio = safe_current / safe_total
    filled = int(round(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    print(
        f"\r[{label}] {safe_current}/{safe_total} |{bar}| {ratio * 100:6.2f}%",
        end="",
        flush=True,
    )
    if safe_current >= safe_total:
        print("", flush=True)


def _normalize_date(raw: str | date | datetime) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _build_summary(*, panel: pd.DataFrame, output_path: Path, symbols_count: int) -> dict[str, Any]:
    rows = int(len(panel))
    return {
        "ok": True,
        "symbols_input": int(symbols_count),
        "rows_output": rows,
        "coverage": {
            "beta_non_null_rate": float(panel["beta"].notna().mean()) if rows else 0.0,
            "market_cap_log_non_null_rate": float(panel["market_cap_log"].notna().mean()) if rows else 0.0,
            "cash_to_assets_non_null_rate": float(panel["cash_to_assets"].notna().mean()) if rows else 0.0,
            "composite_score_non_null_rate": float(panel["composite_score"].notna().mean()) if rows else 0.0,
        },
        "output_path": output_path.as_posix(),
    }


def _resolve_sec_cache_paths(
    *,
    sec_cache_profile: str,
    sec_cache_root: str | None,
    ticker_map_cache_path: str | None,
    companyfacts_cache_dir: str | None,
    submissions_cache_dir: str | None,
) -> tuple[Path, Path, Path, str]:
    profile = str(sec_cache_profile or "live").strip().lower()
    if profile not in {"live", "backtest"}:
        raise ValueError(f"Unsupported sec_cache_profile: {sec_cache_profile!r}. Use live/backtest.")

    root_text = str(sec_cache_root or "").strip()
    if root_text:
        root = Path(root_text)
        cache_source = f"root:{root.as_posix()}"
    elif profile == "backtest":
        root = DEFAULT_SEC_CACHE_ROOT_BACKTEST
        cache_source = "profile:backtest"
    else:
        root = DEFAULT_SEC_CACHE_ROOT_LIVE
        cache_source = "profile:live"

    default_ticker = root / "company_tickers.json"
    default_companyfacts = root / "companyfacts"
    default_submissions = root / "submissions"

    final_ticker = Path(str(ticker_map_cache_path).strip()) if str(ticker_map_cache_path or "").strip() else default_ticker
    final_companyfacts = (
        Path(str(companyfacts_cache_dir).strip()) if str(companyfacts_cache_dir or "").strip() else default_companyfacts
    )
    final_submissions = (
        Path(str(submissions_cache_dir).strip()) if str(submissions_cache_dir or "").strip() else default_submissions
    )
    return final_ticker, final_companyfacts, final_submissions, cache_source


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build AlphaCore daily 5-factor panel from DynamicSymbolPool symbols, "
            "SEC industry classification, Alpaca market data, and SEC companyfacts."
        )
    )
    parser.add_argument(
        "--date",
        required=True,
        help=(
            "Decision date in YYYY-MM-DD. "
            "Output alpha is intended for this date open; features use only data up to date-1."
        ),
    )

    parser.add_argument(
        "--accounts-json-path",
        default="configs/alpaca_acounts/alpaca_accounts.local.json",
        help="Alpaca account config json path.",
    )
    parser.add_argument(
        "--account-name",
        default="ALPACA_US_FULL",
        help="Account key inside accounts json.",
    )
    parser.add_argument("--data-base-url", default="https://data.alpaca.markets")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=2)

    parser.add_argument(
        "--symbols-path",
        default=None,
        help="Optional symbols file (.txt/.csv/.json). If omitted, DynamicSymbolPool is used.",
    )
    parser.add_argument(
        "--symbols-literal",
        default=None,
        help="Optional literal symbol list/set text, for example \"['AAPL','MSFT']\".",
    )
    parser.add_argument(
        "--dynamic-symbols-output-path",
        default=None,
        help="Optional output path to save generated dynamic symbol list (txt).",
    )
    parser.add_argument(
        "--candidate-symbols-path",
        default=str(DEFAULT_CANDIDATE_SYMBOLS_PATH),
        help="Fixed candidate symbol list for DynamicSymbolPool fallback.",
    )
    parser.add_argument("--pool-size", type=int, default=1000)
    parser.add_argument("--lookback-sessions", type=int, default=20)
    parser.add_argument("--min-observations", type=int, default=15)
    parser.add_argument("--price-floor", type=float, default=10.0)
    parser.add_argument("--dynamic-bars-window-calendar-days", type=int, default=420)
    parser.add_argument("--dynamic-bars-chunk-size", type=int, default=120)
    parser.add_argument("--dynamic-beta-full-observations", type=int, default=252)

    parser.add_argument(
        "--industry-path",
        default=str(DEFAULT_INDUSTRY_PATH),
        help=(
            "Deprecated. Industry is now resolved dynamically from SEC submissions; "
            "this argument is ignored."
        ),
    )
    parser.add_argument(
        "--industry-cache-output-path",
        default=None,
        help="Optional path to persist the resolved industry map csv.",
    )
    parser.add_argument(
        "--output-path",
        default=None,
        help="Output CSV path. Default: artifacts/alpha_core/alpha_core_panel_YYYYMMDD.csv",
    )

    parser.add_argument("--feed", default="iex")
    parser.add_argument("--price-adjustment", default="all", help="raw/split/dividend/all")
    parser.add_argument("--bars-window-calendar-days", type=int, default=420)
    parser.add_argument("--bars-chunk-size", type=int, default=120)
    parser.add_argument(
        "--bars-workers",
        type=int,
        default=8,
        help="Alpaca bars parallel workers shared by DynamicSymbolPool and AlphaCore.",
    )
    parser.add_argument(
        "--dynamic-bars-workers",
        dest="bars_workers",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--benchmark-symbol", default="SPY")

    parser.add_argument("--beta-lookback-sessions", type=int, default=252)
    parser.add_argument("--beta-min-observations", type=int, default=126)
    parser.add_argument("--beta-shrinkage-target", type=float, default=1.0)
    parser.add_argument("--beta-shrinkage-strength", type=float, default=0.10)
    parser.add_argument("--beta-clip-low", type=float, default=0.0)
    parser.add_argument("--beta-clip-high", type=float, default=3.0)
    parser.add_argument(
        "--max-price-staleness-days",
        type=int,
        default=5,
        help=(
            "Max allowed calendar-day gap between (date-1) and latest available price session. "
            "Raise error when data is too stale (prevents accidental future-date runs)."
        ),
    )

    parser.add_argument(
        "--sec-user-agent",
        default="aapricity@sjtu.edu.cn", # "1216401387@qq.com", # os.getenv("SEC_USER_AGENT", ""),
        help="SEC API User-Agent, for example: 'YourName your_email@example.com'.",
    )
    parser.add_argument("--sec-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--sec-max-retries", type=int, default=2)
    parser.add_argument("--sec-max-requests-per-second", type=float, default=10.0)
    parser.add_argument("--sec-sleep-seconds", type=float, default=0.0)
    parser.add_argument("--sec-submissions-workers", type=int, default=10)
    parser.add_argument("--sec-companyfacts-workers", type=int, default=10)
    parser.add_argument(
        "--sec-cache-mode",
        choices=("network", "prefer", "cache_only", "auto"),
        default="auto",
        help=(
            "SEC cache usage mode: "
            "network=always fetch online, "
            "prefer=read cache first then network, "
            "cache_only=cache required, "
            "auto=prefer for backtest profile and network for live profile."
        ),
    )
    parser.add_argument(
        "--sec-memory-cache-enabled",
        action="store_true",
        default=True,
        help=(
            "Enable in-process SEC payload memory cache for faster repeated runs "
            "(especially multi-day backtests)."
        ),
    )
    parser.add_argument(
        "--no-sec-memory-cache",
        action="store_false",
        dest="sec_memory_cache_enabled",
        help="Disable in-process SEC payload memory cache.",
    )
    parser.add_argument("--sec-refresh-ticker-map", action="store_true")
    parser.add_argument("--sec-refresh-companyfacts", action="store_true")
    parser.add_argument("--sec-refresh-submissions", action="store_true")
    parser.add_argument(
        "--sec-cache-profile",
        choices=("live", "backtest"),
        default="live",
        help=(
            "SEC cache namespace. live uses data/raw/sec, "
            "backtest uses data/backtest/sec."
        ),
    )
    parser.add_argument(
        "--sec-cache-root",
        default=None,
        help=(
            "Optional SEC cache root. When set, defaults become "
            "<root>/company_tickers.json, <root>/companyfacts, <root>/submissions."
        ),
    )
    parser.add_argument(
        "--sec-ticker-map-cache-path",
        default=None,
        help="Optional explicit ticker map cache path (overrides cache profile/root default).",
    )
    parser.add_argument(
        "--sec-companyfacts-cache-dir",
        default=None,
        help="Optional explicit companyfacts cache dir (overrides cache profile/root default).",
    )
    parser.add_argument(
        "--sec-submissions-cache-dir",
        default=None,
        help="Optional explicit submissions cache dir (overrides cache profile/root default).",
    )
    args = parser.parse_args(argv)

    try:
        credentials = _resolve_alpaca_credentials(
            accounts_json_path=str(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url=str(args.data_base_url),
            request_timeout_seconds=float(args.request_timeout_seconds),
            max_retries=int(args.max_retries),
        )
        alpaca_client = AlpacaHttpClient(credentials)

        symbols = _resolve_symbols(args=args, alpaca_client=alpaca_client)
        if not symbols:
            raise ValueError("Symbol universe is empty.")

        ticker_map_cache_path, companyfacts_cache_dir, submissions_cache_dir, sec_cache_source = _resolve_sec_cache_paths(
            sec_cache_profile=str(args.sec_cache_profile),
            sec_cache_root=str(args.sec_cache_root) if args.sec_cache_root else None,
            ticker_map_cache_path=str(args.sec_ticker_map_cache_path) if args.sec_ticker_map_cache_path else None,
            companyfacts_cache_dir=str(args.sec_companyfacts_cache_dir) if args.sec_companyfacts_cache_dir else None,
            submissions_cache_dir=str(args.sec_submissions_cache_dir) if args.sec_submissions_cache_dir else None,
        )
        print(
            "[AlphaCore] SEC cache: "
            f"profile={str(args.sec_cache_profile)} source={sec_cache_source} "
            f"ticker_map={ticker_map_cache_path.as_posix()} "
            f"companyfacts={companyfacts_cache_dir.as_posix()} "
            f"submissions={submissions_cache_dir.as_posix()}",
            flush=True,
        )
        sec_cache_mode_arg = str(args.sec_cache_mode or "auto").strip().lower()
        if sec_cache_mode_arg == "auto":
            sec_cache_mode = "prefer" if str(args.sec_cache_profile) == "backtest" else "network"
        else:
            sec_cache_mode = sec_cache_mode_arg
        print(
            f"[AlphaCore] SEC fetch mode: cache_mode={sec_cache_mode}, "
            f"memory_cache={bool(args.sec_memory_cache_enabled)}",
            flush=True,
        )

        sec_client = SecApiClient(
            user_agent=str(args.sec_user_agent),
            timeout_seconds=float(args.sec_timeout_seconds),
            max_retries=int(args.sec_max_retries),
            max_requests_per_second=float(args.sec_max_requests_per_second),
            ticker_map_cache_path=ticker_map_cache_path,
            companyfacts_cache_dir=companyfacts_cache_dir,
            submissions_cache_dir=submissions_cache_dir,
            refresh_ticker_map=bool(args.sec_refresh_ticker_map),
            refresh_companyfacts=bool(args.sec_refresh_companyfacts),
            refresh_submissions=bool(args.sec_refresh_submissions),
            sleep_seconds=float(args.sec_sleep_seconds),
            cache_mode=sec_cache_mode,
            memory_cache_enabled=bool(args.sec_memory_cache_enabled),
        )
        industry_map = _resolve_industry_map_for_symbols(
            symbols=symbols,
            sec_client=sec_client,
            industry_cache_output_path=(
                Path(args.industry_cache_output_path) if args.industry_cache_output_path else None
            ),
            submissions_workers=int(args.sec_submissions_workers),
        )
        alpha_core = AlphaCore(
            alpaca_client=alpaca_client,
            sec_client=sec_client,
            industry_map=industry_map,
            sec_submissions_workers=int(args.sec_submissions_workers),
            sec_companyfacts_workers=int(args.sec_companyfacts_workers),
            feed=str(args.feed),
            price_adjustment=str(args.price_adjustment),
            bars_window_calendar_days=int(args.bars_window_calendar_days),
            bars_chunk_size=int(args.bars_chunk_size),
            bars_workers=int(args.bars_workers),
            benchmark_symbol=str(args.benchmark_symbol),
            beta_lookback_sessions=int(args.beta_lookback_sessions),
            beta_min_observations=int(args.beta_min_observations),
            beta_shrinkage_target=float(args.beta_shrinkage_target),
            beta_shrinkage_strength=float(args.beta_shrinkage_strength),
            beta_clip_low=float(args.beta_clip_low) if args.beta_clip_low is not None else None,
            beta_clip_high=float(args.beta_clip_high) if args.beta_clip_high is not None else None,
            max_price_staleness_days=int(args.max_price_staleness_days),
            factor_weights=DEFAULT_FACTOR_WEIGHTS,
        )

        panel = alpha_core.build_for_date(as_of_date=args.date, symbols=symbols)
        target_date = _normalize_date(args.date)
        default_output_path = DEFAULT_OUTPUT_ROOT / f"alpha_core_panel_{target_date.strftime('%Y%m%d')}.csv"
        output_path = Path(args.output_path) if args.output_path else default_output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        panel.to_csv(output_path, index=False)

        summary = _build_summary(panel=panel, output_path=output_path, symbols_count=len(symbols))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted by user (Ctrl+C)."}, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    except (ValueError, FileNotFoundError, AlpacaRequestError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
