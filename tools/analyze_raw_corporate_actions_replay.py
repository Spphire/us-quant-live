from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOT = Path(r"W:/Quat/us-quant-live")
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(PRODUCTION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PRODUCTION_ROOT / "src"))

from alpha_core import (  # noqa: E402
    DEFAULT_FACTOR_WEIGHTS,
    _lagged_rolling_beta,
    _sector_zscore,
    _single_date_zscore,
)
from decision_engine import DecisionConfig, DecisionEngine  # noqa: E402
from vendors.alpaca import AlpacaCredentials, AlpacaHttpClient  # noqa: E402


SESSION_DATES = (
    "2026-07-24",
    "2026-07-27",
    "2026-07-28",
    "2026-07-29",
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-07",
    "2026-08-10",
)
FACTOR_COLUMNS = tuple(DEFAULT_FACTOR_WEIGHTS)
RETURN_COLUMNS = ("return_5d", "momentum_l120_s20", "beta")


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")


def _write_gzip_json(path: Path, payload: Any) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, default=_json_default)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[idx : idx + size]) for idx in range(0, len(values), size)]


def _clean_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rfc3339_boundary(value: str) -> str:
    token = str(value or "").strip()
    if len(token) == 10:
        date.fromisoformat(token)
        return f"{token}T00:00:00Z"
    return token


def _load_union_and_panels(production_root: Path) -> tuple[list[str], dict[str, pd.DataFrame]]:
    panels: dict[str, pd.DataFrame] = {}
    symbols: set[str] = set()
    for session_date in SESSION_DATES:
        stem = session_date.replace("-", "")
        path = production_root / "artifacts" / "daily_alpaca_scheduler" / f"{stem}_decision" / f"alpha_core_panel_{stem}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["symbol"] = frame["symbol"].map(_clean_symbol)
        frame["session_date"] = session_date
        panels[session_date] = frame
        symbols.update(value for value in frame["symbol"] if value)
    if not panels:
        raise RuntimeError("No production alpha panels were found.")
    symbols.add("SPY")
    return sorted(symbols), panels


def _load_account(production_root: Path, account_name: str) -> AlpacaHttpClient:
    config_path = production_root / "configs" / "alpaca_acounts" / "alpaca_accounts.local.json"
    payload = _read_json(config_path, {})
    config = payload.get(account_name) if isinstance(payload, Mapping) else None
    if not isinstance(config, Mapping):
        raise RuntimeError(f"Account profile {account_name!r} is missing from {config_path}.")
    credentials = AlpacaCredentials(
        api_key_id=str(config.get("api_key") or ""),
        api_secret_key=str(config.get("secret_key") or ""),
        trading_base_url=str(config.get("base_url") or "https://paper-api.alpaca.markets"),
        data_base_url="https://data.alpaca.markets",
        request_timeout_seconds=90.0,
        max_retries=4,
    )
    if not credentials.api_key_id or not credentials.api_secret_key:
        raise RuntimeError("The selected Alpaca profile has empty credentials.")
    return AlpacaHttpClient(credentials)


def _fetch_bars(
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
    adjustment: str,
    feed: str,
    chunk_size: int,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunks = _chunks(list(symbols), chunk_size)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def fetch(chunk: list[str]) -> list[dict[str, Any]]:
        return client.get_stock_bars(
            symbols=chunk,
            start=start,
            end=end,
            timeframe="1Day",
            adjustment=adjustment,
            feed=feed,
            limit=10000,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(chunks)))) as pool:
        futures = {pool.submit(fetch, chunk): chunk for chunk in chunks}
        for future in concurrent.futures.as_completed(futures):
            chunk = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - preserve per-chunk evidence
                errors.append({"symbols": chunk, "error": str(exc)})
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        symbol = _clean_symbol(row.get("symbol"))
        timestamp = str(row.get("t") or "")
        session = timestamp[:10]
        close = _safe_float(row.get("c"))
        if symbol and session and close is not None and close > 0:
            dedup[(symbol, session)] = {
                "symbol": symbol,
                "session_date": session,
                "open": _safe_float(row.get("o")),
                "high": _safe_float(row.get("h")),
                "low": _safe_float(row.get("l")),
                "close": close,
                "volume": _safe_float(row.get("v")),
                "trade_count": _safe_float(row.get("n")),
                "vwap": _safe_float(row.get("vw")),
                "timestamp": timestamp,
            }
    return list(dedup.values()), errors


def _load_asset_cusip_map(production_root: Path) -> dict[str, str]:
    candidates = sorted(
        (production_root / "artifacts" / "daily_alpaca_scheduler").glob("*_prepare/broker_assets_active_us_equity.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        payload = _read_json(path, [])
        if not isinstance(payload, list):
            continue
        result: dict[str, str] = {}
        for row in payload:
            if isinstance(row, Mapping):
                cusip = str(row.get("cusip") or "").strip().upper()
                symbol = _clean_symbol(row.get("symbol"))
                if cusip and symbol:
                    result[cusip] = symbol
        if result:
            return result
    return {}


def _fetch_actions(
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
    chunk_size: int,
    workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunks = _chunks(list(symbols), chunk_size)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def fetch(chunk: list[str]) -> list[dict[str, Any]]:
        return client.get_corporate_actions(symbols=chunk, start=start, end=end, limit=1000)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(chunks)))) as pool:
        futures = {pool.submit(fetch, chunk): chunk for chunk in chunks}
        for future in concurrent.futures.as_completed(futures):
            chunk = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - preserve per-chunk evidence
                errors.append({"symbols": chunk, "error": str(exc)})
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        action_id = str(item.get("id") or "").strip()
        identity = action_id or json.dumps(item, sort_keys=True, default=str)
        dedup[identity] = item
    return list(dedup.values()), errors


def _normalize_action(raw: Mapping[str, Any], cusip_to_symbol: Mapping[str, str]) -> dict[str, Any]:
    item = dict(raw)
    action_type = str(item.get("action_type") or item.get("type") or "").strip().lower()
    cusip = str(item.get("cusip") or "").strip().upper()
    source_symbol = _clean_symbol(
        item.get("source_symbol") or item.get("acquiree_symbol") or item.get("old_symbol")
    )
    new_symbol = _clean_symbol(
        item.get("new_symbol") or item.get("acquirer_symbol") or item.get("target_symbol")
    )
    symbol = _clean_symbol(item.get("symbol")) or source_symbol or _clean_symbol(cusip_to_symbol.get(cusip))
    ex_date = str(item.get("ex_date") or item.get("effective_date") or "")[:10]
    old_rate = _safe_float(item.get("old_rate"))
    new_rate = _safe_float(item.get("new_rate"))
    rate = _safe_float(item.get("rate"))
    source_rate = _safe_float(item.get("source_rate") or item.get("acquiree_rate"))
    consideration_rate = _safe_float(item.get("acquirer_rate") or item.get("new_rate"))
    cash_rate = _safe_float(item.get("cash_rate"))
    split_factor = new_rate / old_rate if old_rate and new_rate and old_rate > 0 else None
    item.update(
        {
            "normalized_action_type": action_type,
            "normalized_symbol": symbol,
            "normalized_source_symbol": source_symbol or symbol,
            "normalized_new_symbol": new_symbol,
            "normalized_ex_date": ex_date,
            "normalized_split_factor": split_factor,
            "normalized_rate": rate,
            "normalized_source_rate": source_rate,
            "normalized_consideration_rate": consideration_rate,
            "normalized_cash_rate": cash_rate,
            "cusip_symbol_mapping_source": "symbol" if _clean_symbol(raw.get("symbol")) else ("cusip_asset_map" if symbol else "unmapped"),
        }
    )
    return item


def _build_events(actions: Sequence[Mapping[str, Any]], cusip_to_symbol: Mapping[str, str]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    normalized: list[dict[str, Any]] = []
    seen_event_keys: set[str] = set()
    for raw in actions:
        item = _normalize_action(raw, cusip_to_symbol)
        normalized.append(item)
        symbol = item["normalized_symbol"]
        ex_date = item["normalized_ex_date"]
        if symbol and ex_date:
            action_type = str(item.get("normalized_action_type") or "")
            if action_type in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers", "spin_offs"}:
                event_key = "|".join(
                    str(item.get(key) or "")
                    for key in (
                        "normalized_action_type",
                        "normalized_source_symbol",
                        "normalized_new_symbol",
                        "normalized_ex_date",
                        "normalized_consideration_rate",
                        "normalized_cash_rate",
                        "rate",
                    )
                )
                if event_key in seen_event_keys:
                    continue
                seen_event_keys.add(event_key)
            events[(symbol, ex_date)].append(item)
    return dict(events), normalized


def _action_effects(
    rows: Sequence[Mapping[str, Any]],
    *,
    current_symbol: str = "",
    ex_date: str = "",
    close_lookup: Mapping[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    split_factor = 1.0
    cash = 0.0
    unsupported: list[dict[str, Any]] = []
    supported: list[dict[str, Any]] = []
    ignored_duplicates: list[dict[str, Any]] = []
    stock_dividend_rates = {
        round(float(_safe_float(row.get("normalized_rate")) or 0.0), 8)
        for row in rows
        if str(row.get("normalized_action_type") or "") in {"stock_dividends", "stock_dividend"}
        and (_safe_float(row.get("normalized_rate")) or 0.0) > 1.0
    }
    merger_cash_rates = {
        round(float(value), 8)
        for row in rows
        if str(row.get("normalized_action_type") or "") in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}
        for value in [
            _safe_float(row.get("normalized_cash_rate"))
            if _safe_float(row.get("normalized_cash_rate")) is not None
            else (_safe_float(row.get("normalized_rate")) if str(row.get("normalized_action_type") or "") == "cash_mergers" else None)
        ]
        if value is not None and value >= 0
    }
    child_value = 0.0
    replacement_value: float | None = None
    for row in rows:
        action_type = str(row.get("normalized_action_type") or "").lower()
        rate = _safe_float(row.get("normalized_rate"))
        factor = _safe_float(row.get("normalized_split_factor"))
        if action_type in {"cash_dividends", "cash_dividend"} and rate is not None and rate >= 0:
            # SCCO and similar records can expose the stock-share factor again
            # as a cash-dividend row.  Keep the actual cash row, discard the
            # duplicated factor when a same-day stock-dividend row exists.
            if (rate > 1.0 and round(rate, 8) in stock_dividend_rates) or round(rate, 8) in merger_cash_rates:
                reason = "same_day_stock_dividend_factor" if rate > 1.0 and round(rate, 8) in stock_dividend_rates else "same_day_merger_cash_rate"
                ignored_duplicates.append({"type": action_type, "rate": rate, "reason": reason})
                continue
            cash += rate
            supported.append({"type": action_type, "cash_rate": rate})
        elif action_type in {"forward_splits", "reverse_splits", "unit_splits", "forward_split", "reverse_split", "unit_split"} and factor and factor > 0:
            split_factor *= factor
            supported.append({"type": action_type, "split_factor": factor})
        elif action_type in {"stock_dividends", "stock_dividend"} and rate is not None and rate >= 0:
            # Alpaca has used both 1.01 (final shares) and .01 (increment)
            stock_factor = rate if rate > 1.0 else 1.0 + rate
            split_factor *= stock_factor
            supported.append({"type": action_type, "stock_factor": stock_factor})
        elif action_type == "spin_offs":
            child_symbol = _clean_symbol(row.get("normalized_new_symbol"))
            child_rate = _safe_float(row.get("normalized_consideration_rate"))
            child_close = close_lookup.get((child_symbol, ex_date)) if close_lookup else None
            if child_symbol and child_rate is not None and child_rate >= 0 and child_close is not None and child_close > 0:
                child_value += float(child_rate) * float(child_close)
                supported.append({"type": action_type, "child_symbol": child_symbol, "child_rate": child_rate, "child_close": child_close})
            else:
                unsupported.append({
                    "type": action_type,
                    "reason": "missing_child_price_or_rate",
                    "child_symbol": child_symbol,
                    "child_rate": child_rate,
                    "child_close": child_close,
                })
        elif action_type in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}:
            acquiree = _clean_symbol(row.get("normalized_source_symbol"))
            acquirer = _clean_symbol(row.get("normalized_new_symbol"))
            if acquiree and acquirer and current_symbol and acquiree != current_symbol:
                unsupported.append({"type": action_type, "reason": "event_not_for_current_symbol", "acquiree": acquiree})
                continue
            consideration_rate = _safe_float(row.get("normalized_consideration_rate")) or 0.0
            merger_cash = _safe_float(row.get("normalized_cash_rate"))
            merger_cash = merger_cash if merger_cash is not None else (rate if action_type == "cash_mergers" else 0.0)
            acquirer_close = close_lookup.get((acquirer, ex_date)) if close_lookup else None
            if action_type == "cash_mergers":
                if merger_cash is not None and merger_cash >= 0:
                    replacement_value = float(merger_cash)
                    supported.append({"type": action_type, "cash_rate": merger_cash})
                else:
                    unsupported.append({"type": action_type, "reason": "missing_cash_rate"})
            elif acquirer and acquirer_close is not None and acquirer_close > 0 and consideration_rate > 0:
                replacement_value = float(consideration_rate) * float(acquirer_close) + float(merger_cash or 0.0)
                supported.append({"type": action_type, "acquirer_symbol": acquirer, "acquirer_rate": consideration_rate, "acquirer_close": acquirer_close, "cash_rate": float(merger_cash or 0.0)})
            else:
                unsupported.append({"type": action_type, "reason": "missing_acquirer_price_or_rate", "acquirer_symbol": acquirer})
        else:
            unsupported.append({
                "type": action_type,
                "id": str(row.get("id") or ""),
                "symbol": str(row.get("normalized_symbol") or ""),
                "cusip": str(row.get("cusip") or ""),
            })
    return {
        "split_factor": split_factor,
        "cash_rate": cash,
        "child_value": child_value,
        "replacement_value": replacement_value,
        "supported": supported,
        "unsupported": unsupported,
        "ignored_duplicates": ignored_duplicates,
        "status": "unsupported" if unsupported else "supported",
    }


def _build_price_frame(
    bars: Sequence[Mapping[str, Any]],
    *,
    events: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    custom: bool,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    frame = pd.DataFrame(list(bars))
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "session_date", "close", "wealth", "valid"]), []
    frame["symbol"] = frame["symbol"].map(_clean_symbol)
    frame["session_date"] = frame["session_date"].astype(str).str[:10]
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "session_date", "close"])
    frame = frame.sort_values(["symbol", "session_date"]).drop_duplicates(["symbol", "session_date"])
    close_lookup = {
        (_clean_symbol(row.symbol), str(row.session_date)): float(row.close)
        for row in frame.itertuples(index=False)
        if _safe_float(row.close) is not None and float(row.close) > 0
    }
    diagnostics: list[dict[str, Any]] = []
    out: list[pd.DataFrame] = []
    for symbol, group in frame.groupby("symbol", sort=False):
        group = group.sort_values("session_date").copy()
        closes = group["close"].to_numpy(dtype=float)
        wealth = np.ones(len(group), dtype=float)
        valid = np.ones(len(group), dtype=bool)
        event_statuses: list[str] = []
        for idx in range(1, len(group)):
            ex_date = str(group.iloc[idx]["session_date"])
            effect = _action_effects(
                events.get((symbol, ex_date), []),
                current_symbol=symbol,
                ex_date=ex_date,
                close_lookup=close_lookup,
            ) if custom else {
                "split_factor": 1.0,
                "cash_rate": 0.0,
                "unsupported": [],
                "status": "provider_all",
            }
            previous = closes[idx - 1]
            current = closes[idx]
            if previous <= 0 or not np.isfinite(previous) or current <= 0 or not np.isfinite(current):
                ratio = np.nan
                valid[idx] = False
            elif custom:
                if effect["unsupported"]:
                    ratio = current / previous
                    valid[idx] = False
                    event_statuses.append(ex_date)
                else:
                    numerator = effect.get("replacement_value")
                    if numerator is None:
                        numerator = (
                            current * float(effect["split_factor"])
                            + float(effect["cash_rate"])
                            + float(effect.get("child_value", 0.0))
                        )
                    ratio = float(numerator) / previous
            else:
                ratio = current / previous
            wealth[idx] = wealth[idx - 1] * ratio if np.isfinite(ratio) else np.nan
        group["wealth"] = wealth
        group["valid"] = valid
        group["provider_or_custom"] = "custom_raw_actions" if custom else "alpaca_all"
        group["action_unsupported_session_dates"] = ";".join(sorted(set(event_statuses)))
        out.append(group)
    return pd.concat(out, ignore_index=True), diagnostics


def _feature_frame(price_frame: pd.DataFrame, benchmark: pd.DataFrame, *, source: str) -> pd.DataFrame:
    frame = price_frame.copy()
    value_column = "wealth"
    frame = frame.sort_values(["symbol", "session_date"])
    group = frame.groupby("symbol", group_keys=False)
    frame["symbol_return"] = group[value_column].pct_change()
    frame["return_5d"] = group[value_column].pct_change(5)
    frame["momentum_l120_s20"] = group[value_column].shift(20) / group[value_column].shift(140) - 1.0
    bench = benchmark.sort_values("session_date").copy()
    bench["benchmark_return"] = bench[value_column].pct_change()
    pairs = frame[["session_date", "symbol", "symbol_return"]].merge(
        bench[["session_date", "benchmark_return"]], on="session_date", how="left"
    )
    beta = _lagged_rolling_beta(
        pairs,
        lookback_sessions=252,
        min_observations=126,
        shrinkage_target=1.0,
        shrinkage_strength=0.10,
        beta_clip_low=0.0,
        beta_clip_high=3.0,
        asof_lag_sessions=1,
    )
    frame = frame.merge(beta, on=["session_date", "symbol"], how="left")
    frame["feature_source"] = source
    return frame


def _rebuild_alpha(panel: pd.DataFrame, features: pd.DataFrame, decision_date: str) -> pd.DataFrame:
    cutoff = (date.fromisoformat(decision_date) - timedelta(days=1)).isoformat()
    latest = (
        features[features["session_date"] <= cutoff]
        .sort_values(["symbol", "session_date"])
        .drop_duplicates("symbol", keep="last")
    )
    base_columns = [
        "symbol", "sic2_sector", "sic4_industry", "market_cap_log", "cash_to_assets",
    ]
    base = panel[base_columns].copy()
    merged = base.merge(
        latest[["symbol", "session_date", "return_5d", "momentum_l120_s20", "beta", "beta_raw", "beta_obs", "valid"]],
        on="symbol", how="left",
    )
    merged["session_date"] = decision_date
    merged["reversal_score_raw"] = -pd.to_numeric(merged["return_5d"], errors="coerce")
    merged["momentum_score_raw"] = pd.to_numeric(merged["momentum_l120_s20"], errors="coerce")
    merged["small_size_score_raw"] = -pd.to_numeric(merged["market_cap_log"], errors="coerce")
    merged["low_beta_score_raw"] = -pd.to_numeric(merged["beta"], errors="coerce")
    merged["cash_quality_score_raw"] = pd.to_numeric(merged["cash_to_assets"], errors="coerce")
    raw_columns = {
        "reversal_score": "reversal_score_raw",
        "momentum_score": "momentum_score_raw",
        "small_size_score": "small_size_score_raw",
        "low_beta_score": "low_beta_score_raw",
        "cash_quality_score": "cash_quality_score_raw",
    }
    for factor, raw_column in raw_columns.items():
        merged[factor] = _sector_zscore(merged, raw_column).fillna(0.0)
    composite_raw = np.zeros(len(merged), dtype=float)
    total_weight = sum(abs(float(value)) for value in DEFAULT_FACTOR_WEIGHTS.values())
    for factor, weight in DEFAULT_FACTOR_WEIGHTS.items():
        composite_raw += float(weight) * pd.to_numeric(merged[factor], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    merged["composite_score_raw"] = composite_raw / total_weight
    merged["composite_score"] = _single_date_zscore(merged["composite_score_raw"]).fillna(0.0)
    merged["composite_rank"] = merged["composite_score"].rank(method="average", ascending=False, pct=True)
    merged["valid_price_history"] = merged["valid"].fillna(False).astype(bool)
    return merged


def _decision_config(run_context: Mapping[str, Any]) -> DecisionConfig:
    parsed = run_context.get("parsed_args", {}) if isinstance(run_context, Mapping) else {}
    return DecisionConfig(
        factor_weights=dict(DEFAULT_FACTOR_WEIGHTS),
        candidate_pool_per_side=int(parsed.get("candidate_pool_per_side", 120)),
        max_single_name_side_weight=float(parsed.get("max_single_name_side_weight", 1 / 30)),
        min_nonzero_names=int(parsed.get("min_nonzero_names", 20)),
        score_weight=float(parsed.get("score_weight", 0.01)),
        sector_penalty=float(parsed.get("sector_penalty", 25.0)),
        turnover_penalty=float(parsed.get("turnover_penalty", 0.005)),
        turnover_budget=float(parsed.get("turnover_budget", 0.15)),
        beta_band_grid=tuple(float(value) for value in str(parsed.get("beta_band_grid", "0.05,0.10,0.15,0.20")).split(",") if value),
    )


def _split_weights(payload: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    raw = payload.get("broker_weights_before", {}) if isinstance(payload, Mapping) else {}
    if isinstance(raw, Mapping) and "long" in raw and "short" in raw:
        return {
            "long": {str(key): float(value) for key, value in (raw.get("long") or {}).items()},
            "short": {str(key): float(value) for key, value in (raw.get("short") or {}).items()},
        }
    result = {"long": {}, "short": {}}
    if isinstance(raw, Mapping):
        for symbol, value in raw.items():
            number = _safe_float(value)
            if number is None:
                continue
            result["long" if number > 0 else "short"][str(symbol)] = abs(number)
    return result


def _target_dict(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {}
    return {
        _clean_symbol(row.symbol): float(row.signed_weight)
        for row in frame.itertuples(index=False)
        if _safe_float(row.signed_weight) is not None and abs(float(row.signed_weight)) > 1e-12
    }


def _load_actual_targets(production_root: Path, decision_date: str) -> dict[str, float]:
    stem = decision_date.replace("-", "")
    path = production_root / "artifacts" / "daily_alpaca_scheduler" / f"{stem}_decision" / "decision_targets.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    if "signed_weight" not in frame or "symbol" not in frame:
        return {}
    return _target_dict(frame)


def _weights_rows(weights: Mapping[str, float]) -> list[dict[str, Any]]:
    return [{"symbol": symbol, "signed_weight": float(value)} for symbol, value in sorted(weights.items())]


def _run_decisions(
    panels: Mapping[str, pd.DataFrame],
    rebuilt: Mapping[str, pd.DataFrame],
    production_root: Path,
    source: str,
    config: DecisionConfig,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    engine = DecisionEngine(config)
    targets: dict[str, dict[str, float]] = {}
    diagnostics: list[dict[str, Any]] = []
    for session_date in SESSION_DATES:
        if session_date not in panels or session_date not in rebuilt:
            continue
        context = _read_json(
            production_root / "artifacts" / "daily_alpaca_scheduler" / f"{session_date.replace('-', '')}_decision" / "run_context.json",
            {},
        )
        previous = _split_weights(_read_json(
            production_root / "artifacts" / "daily_alpaca_scheduler" / f"{session_date.replace('-', '')}_decision" / "portfolio_weights_snapshot.json",
            {},
        ))
        result = engine.decide(
            alpha_frame=rebuilt[session_date],
            previous_weights=previous,
            session_idx=int((context.get("parsed_args", {}) if isinstance(context, Mapping) else {}).get("session_idx") or 0),
            session_date=session_date,
        )
        weights = {}
        if result.status == "ok" and not result.targets.empty:
            weights = _target_dict(result.targets)
        targets[session_date] = weights
        diagnostics.append({
            "source": source,
            "session_date": session_date,
            "status": result.status,
            "skip_reason": result.skip_reason,
            "target_count": len(weights),
            "long_count": sum(value > 0 for value in weights.values()),
            "short_count": sum(value < 0 for value in weights.values()),
            "gross_weight": sum(abs(value) for value in weights.values()),
            "diagnostics": result.diagnostics,
        })
    return targets, diagnostics


def _target_diff(left: Mapping[str, float], right: Mapping[str, float]) -> dict[str, Any]:
    symbols = set(left) | set(right)
    diffs = {symbol: float(left.get(symbol, 0.0) - right.get(symbol, 0.0)) for symbol in symbols}
    changed = [symbol for symbol, value in diffs.items() if abs(value) > 1e-9]
    return {
        "symbol_count_left": len(left),
        "symbol_count_right": len(right),
        "overlap_count": len(set(left) & set(right)),
        "signed_weight_l1": float(sum(abs(value) for value in diffs.values())),
        "changed_symbol_count": len(changed),
        "long_overlap": len({symbol for symbol, value in left.items() if value > 0} & {symbol for symbol, value in right.items() if value > 0}),
        "short_overlap": len({symbol for symbol, value in left.items() if value < 0} & {symbol for symbol, value in right.items() if value < 0}),
        "largest_changes": [
            {"symbol": symbol, "signed_weight_diff": diffs[symbol]}
            for symbol in sorted(changed, key=lambda item: abs(diffs[item]), reverse=True)[:20]
        ],
    }


def _return_lookup(features: pd.DataFrame, value_column: str = "wealth") -> dict[tuple[str, str], float]:
    return {
        (_clean_symbol(row.symbol), str(row.session_date)): float(getattr(row, value_column))
        for row in features.itertuples(index=False)
        if _safe_float(getattr(row, value_column, None)) is not None
    }


def _portfolio_replay(
    target_sets: Mapping[str, Mapping[str, Mapping[str, float]]],
    all_prices: pd.DataFrame,
    custom_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    price_maps = {
        "alpaca_all": _return_lookup(all_prices),
        "custom_raw_actions": _return_lookup(custom_prices),
    }
    rows: list[dict[str, Any]] = []
    dates = [value for value in SESSION_DATES if value in target_sets.get("actual", {})]
    for idx in range(len(dates) - 1):
        start_date, end_date = dates[idx], dates[idx + 1]
        for target_name, daily_targets in target_sets.items():
            weights = daily_targets.get(start_date, {})
            for return_source, price_map in price_maps.items():
                long_return = 0.0
                short_return = 0.0
                missing = 0
                for symbol, weight in weights.items():
                    start_value = price_map.get((_clean_symbol(symbol), start_date))
                    end_value = price_map.get((_clean_symbol(symbol), end_date))
                    if start_value is None or end_value is None or start_value <= 0:
                        missing += 1
                        continue
                    symbol_return = end_value / start_value - 1.0
                    contribution = float(weight) * symbol_return
                    if weight >= 0:
                        long_return += contribution
                    else:
                        short_return += contribution
                rows.append({
                    "start_decision_date": start_date,
                    "end_decision_date": end_date,
                    "target_source": target_name,
                    "return_source": return_source,
                    "long_contribution": long_return,
                    "short_contribution": short_return,
                    "portfolio_return": long_return + short_return,
                    "missing_symbol_count": missing,
                })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame, {}
    summary_rows: list[dict[str, Any]] = []
    for (target_name, return_source), group in frame.groupby(["target_source", "return_source"]):
        equity = 1.0
        for value in group.sort_values("start_decision_date")["portfolio_return"]:
            equity *= 1.0 + float(value)
        summary_rows.append({
            "target_source": target_name,
            "return_source": return_source,
            "interval_count": len(group),
            "compound_return": equity - 1.0,
            "sum_long_contribution": group["long_contribution"].sum(),
            "sum_short_contribution": group["short_contribution"].sum(),
            "sum_portfolio_return": group["portfolio_return"].sum(),
            "max_missing_symbol_count": int(group["missing_symbol_count"].max()),
        })
    return frame, {"series": summary_rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Alpaca raw + corporate-action replay for the paper-trading period.")
    parser.add_argument("--production-root", default=str(PRODUCTION_ROOT))
    parser.add_argument("--account-name", default="ALPACA_US_FULL")
    parser.add_argument("--start", default="2025-06-20")
    parser.add_argument("--end", default="2026-08-11")
    parser.add_argument("--feed", default="sip", choices=("iex", "sip"))
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    production_root = Path(args.production_root)
    feed_name = str(args.feed).strip().lower()
    bar_start = _rfc3339_boundary(str(args.start))
    bar_end = _rfc3339_boundary(str(args.end))
    output_dir = (
        Path(args.output_dir)
        if str(args.output_dir).strip()
        else PROJECT_ROOT
        / "artifacts"
        / "research"
        / f"corporate_action_replay_{feed_name}_20260724_20260810"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols, panels = _load_union_and_panels(production_root)
    client = _load_account(production_root, str(args.account_name))
    all_symbols = sorted(set(symbols) | {"SPY"})

    raw_path = output_dir / "alpaca_raw_bars.json.gz"
    all_path = output_dir / "alpaca_all_bars.json.gz"
    action_path = output_dir / "alpaca_corporate_actions.json"
    metadata = {
        "schema_version": "1.0",
        "artifact_type": "corporate_action_raw_replay",
        "production_root": production_root.as_posix(),
        "candidate_commit": "unknown",
        "session_dates": list(SESSION_DATES),
        "requested_symbol_count_including_benchmark": len(all_symbols),
        "bar_start_input": str(args.start),
        "bar_end_input": str(args.end),
        "bar_start": bar_start,
        "bar_end": bar_end,
        "bar_end_semantics": "exclusive_rfc3339_boundary",
        "feed": feed_name,
        "adjustment_comparison": ["raw", "all"],
        "submit_enabled": False,
        "orders_submitted": False,
        "warnings": [
            "The controlled comparison is valid only when raw and all use the same feed.",
            "Production panels are a separate reference unless their recorded feed matches this replay feed.",
        ],
    }
    _write_json(output_dir / "run_metadata.json", metadata)

    if raw_path.exists():
        raw_bars = json.loads(gzip.open(raw_path, "rt", encoding="utf-8").read())
        raw_errors: list[dict[str, Any]] = []
    else:
        raw_bars, raw_errors = _fetch_bars(
            client, all_symbols, start=bar_start, end=bar_end, adjustment="raw", feed=feed_name,
            chunk_size=int(args.chunk_size), workers=int(args.workers),
        )
        _write_gzip_json(raw_path, raw_bars)
    if all_path.exists():
        all_bars = json.loads(gzip.open(all_path, "rt", encoding="utf-8").read())
        all_errors: list[dict[str, Any]] = []
    else:
        all_bars, all_errors = _fetch_bars(
            client, all_symbols, start=bar_start, end=bar_end, adjustment="all", feed=feed_name,
            chunk_size=int(args.chunk_size), workers=int(args.workers),
        )
        _write_gzip_json(all_path, all_bars)

    initial_bar_fetch_gate = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if raw_bars and all_bars and not raw_errors and not all_errors
            else "fail"
        ),
        "feed": feed_name,
        "bar_start": bar_start,
        "bar_end": bar_end,
        "raw_row_count": len(raw_bars),
        "all_row_count": len(all_bars),
        "raw_fetch_error_count": len(raw_errors),
        "all_fetch_error_count": len(all_errors),
        "raw_fetch_errors": raw_errors[:20],
        "all_fetch_errors": all_errors[:20],
    }
    _write_json(output_dir / "initial_bar_fetch_gate.json", initial_bar_fetch_gate)
    if initial_bar_fetch_gate["status"] != "pass":
        print(json.dumps(initial_bar_fetch_gate, ensure_ascii=False, indent=2))
        return 2

    cusip_map = _load_asset_cusip_map(production_root)
    if action_path.exists():
        actions_payload = _read_json(action_path, {})
        actions = actions_payload.get("actions", []) if isinstance(actions_payload, Mapping) else []
        action_errors = actions_payload.get("errors", []) if isinstance(actions_payload, Mapping) else []
    else:
        actions, action_errors = _fetch_actions(
            client,
            all_symbols,
            start=str(args.start),
            end=str(args.end),
            chunk_size=int(args.chunk_size),
            workers=int(args.workers),
        )
        _write_json(action_path, {"requested_symbols": all_symbols, "actions": actions, "errors": action_errors})
    corporate_action_fetch_gate = {
        "schema_version": "1.0",
        "status": "pass" if actions and not action_errors else "fail",
        "start": str(args.start),
        "end": str(args.end),
        "action_count": len(actions),
        "fetch_error_count": len(action_errors),
        "fetch_errors": action_errors[:20],
    }
    _write_json(output_dir / "corporate_action_fetch_gate.json", corporate_action_fetch_gate)
    if corporate_action_fetch_gate["status"] != "pass":
        print(json.dumps(corporate_action_fetch_gate, ensure_ascii=False, indent=2))
        return 3
    events, normalized_actions = _build_events(actions, cusip_map)

    # Spin-offs and mergers carry the receiving security in a separate symbol
    # field.  Fetch those symbols as a supplemental read-only batch so their
    # value can be included in the old holder's total-return wealth index.
    supplemental_symbols = {
        _clean_symbol(row.get("normalized_new_symbol"))
        for row in normalized_actions
        if _clean_symbol(row.get("normalized_new_symbol"))
    }
    supplemental_symbols.update(
        _clean_symbol(row.get("normalized_source_symbol"))
        for row in normalized_actions
        if _clean_symbol(row.get("normalized_source_symbol"))
    )
    known_bar_symbols = {_clean_symbol(row.get("symbol")) for row in [*raw_bars, *all_bars]}
    supplemental_symbols = sorted(symbol for symbol in supplemental_symbols if symbol not in known_bar_symbols)
    supplemental_raw_errors: list[dict[str, Any]] = []
    supplemental_all_errors: list[dict[str, Any]] = []
    if supplemental_symbols:
        extra_raw, supplemental_raw_errors = _fetch_bars(
            client, supplemental_symbols, start=bar_start, end=bar_end, adjustment="raw", feed=feed_name,
            # A single malformed historical CUSIP-like symbol must not make
            # the whole supplemental batch fail with Alpaca HTTP 400.
            chunk_size=1, workers=int(args.workers),
        )
        extra_all, supplemental_all_errors = _fetch_bars(
            client, supplemental_symbols, start=bar_start, end=bar_end, adjustment="all", feed=feed_name,
            chunk_size=1, workers=int(args.workers),
        )
        raw_bars.extend(extra_raw)
        all_bars.extend(extra_all)
        _write_gzip_json(raw_path, raw_bars)
        _write_gzip_json(all_path, all_bars)

    raw_frame, _ = _build_price_frame(raw_bars, events=events, custom=False)
    all_frame, _ = _build_price_frame(all_bars, events=events, custom=False)
    custom_frame, _ = _build_price_frame(raw_bars, events=events, custom=True)
    raw_close_lookup = {
        (_clean_symbol(row.symbol), str(row.session_date)): float(row.close)
        for row in raw_frame.itertuples(index=False)
        if _safe_float(row.close) is not None and float(row.close) > 0
    }
    raw_frame.to_csv(output_dir / "raw_bar_coverage.csv", index=False)
    all_frame.to_csv(output_dir / "all_bar_coverage.csv", index=False)
    custom_frame.to_csv(output_dir / "custom_wealth_index.csv", index=False)

    bar_counts = {
        "raw": {str(k): int(v) for k, v in raw_frame.groupby("symbol").size().sort_values().to_dict().items()},
        "all": {str(k): int(v) for k, v in all_frame.groupby("symbol").size().sort_values().to_dict().items()},
    }
    action_symbol_set = set(symbols)
    target_symbols_by_date = {
        session_date: set(_load_actual_targets(production_root, session_date)) for session_date in SESSION_DATES
    }
    target_union = set().union(*target_symbols_by_date.values()) if target_symbols_by_date else set()
    actual_union: set[str] = set()
    for session_date in SESSION_DATES:
        path = production_root / "artifacts" / "daily_alpaca_scheduler" / f"{session_date.replace('-', '')}_execute" / "broker_positions_after.csv"
        if path.exists():
            try:
                positions = pd.read_csv(path)
                if "symbol" in positions:
                    actual_union.update(positions["symbol"].map(_clean_symbol))
            except Exception:
                pass
    action_rows: list[dict[str, Any]] = []
    for action in normalized_actions:
        ex_date = str(action.get("normalized_ex_date") or "")
        symbol = _clean_symbol(action.get("normalized_symbol"))
        effects = _action_effects(
            [action],
            current_symbol=_clean_symbol(action.get("normalized_symbol")),
            ex_date=str(action.get("normalized_ex_date") or ""),
            close_lookup=raw_close_lookup,
        )
        action_rows.append({
            "symbol": symbol,
            "cusip": str(action.get("cusip") or ""),
            "ex_date": ex_date,
            "action_type": str(action.get("normalized_action_type") or ""),
            "rate": _safe_float(action.get("normalized_rate")),
            "split_factor": _safe_float(action.get("normalized_split_factor")),
            "mapping_source": str(action.get("cusip_symbol_mapping_source") or ""),
            "supported_by_custom_algorithm": effects["status"] == "supported",
            "unsupported_reason": ";".join(str(row.get("type") or "") for row in effects["unsupported"]),
            "in_alpha_union": symbol in action_symbol_set,
            "in_any_actual_target": symbol in target_union,
            "in_any_actual_position": symbol in actual_union,
            "during_live_test_window": "2026-07-24" <= ex_date <= "2026-08-10",
        })
    action_frame = pd.DataFrame(action_rows)
    if not action_frame.empty:
        action_frame = action_frame.sort_values(["ex_date", "symbol", "action_type"])
    action_frame.to_csv(output_dir / "corporate_action_summary.csv", index=False)
    _write_json(output_dir / "corporate_action_summary.json", {
        "action_count": len(action_rows),
        "action_type_counts": dict(Counter(row["action_type"] for row in action_rows)),
        "live_window_count": int(action_frame["during_live_test_window"].sum()) if not action_frame.empty else 0,
        "live_window_alpha_count": int((action_frame["during_live_test_window"] & action_frame["in_alpha_union"]).sum()) if not action_frame.empty else 0,
        "live_window_target_count": int((action_frame["during_live_test_window"] & action_frame["in_any_actual_target"]).sum()) if not action_frame.empty else 0,
        "unsupported_count": int((~action_frame["supported_by_custom_algorithm"]).sum()) if not action_frame.empty else 0,
        "rows": action_rows,
    })

    rebuilt_all: dict[str, pd.DataFrame] = {}
    rebuilt_custom: dict[str, pd.DataFrame] = {}
    comparison_rows: list[dict[str, Any]] = []
    all_source_name = f"{feed_name}_alpaca_all"
    for session_date, panel in panels.items():
        symbols_for_date = set(panel["symbol"])
        all_features = _feature_frame(all_frame[all_frame["symbol"].isin(symbols_for_date | {"SPY"})], all_frame[all_frame["symbol"].eq("SPY")], source=all_source_name)
        custom_features = _feature_frame(custom_frame[custom_frame["symbol"].isin(symbols_for_date | {"SPY"})], custom_frame[custom_frame["symbol"].eq("SPY")], source="raw_plus_actions")
        rebuilt_all[session_date] = _rebuild_alpha(panel, all_features[all_features["symbol"].isin(symbols_for_date)], session_date)
        rebuilt_custom[session_date] = _rebuild_alpha(panel, custom_features[custom_features["symbol"].isin(symbols_for_date)], session_date)
        rebuilt_all[session_date].to_csv(output_dir / f"rebuilt_alpha_{feed_name}_all_{session_date.replace('-', '')}.csv", index=False)
        rebuilt_custom[session_date].to_csv(output_dir / f"rebuilt_alpha_raw_actions_{session_date.replace('-', '')}.csv", index=False)
        left = rebuilt_all[session_date].set_index("symbol")
        right = rebuilt_custom[session_date].set_index("symbol")
        for factor in [*RETURN_COLUMNS, *FACTOR_COLUMNS, "composite_score"]:
            if factor not in left or factor not in right:
                continue
            joined = left[[factor]].join(right[[factor]], lsuffix="_all", rsuffix="_custom", how="inner").dropna()
            if joined.empty:
                continue
            diff = joined[f"{factor}_custom"] - joined[f"{factor}_all"]
            comparison_rows.append({
                "session_date": session_date,
                "metric": factor,
                "row_count": len(joined),
                "mae": float(diff.abs().mean()),
                "p95_abs": float(diff.abs().quantile(0.95)),
                "max_abs": float(diff.abs().max()),
                "l1": float(diff.abs().sum()),
                "nonzero_count": int((diff.abs() > 1e-9).sum()),
            })
    factor_comparison = pd.DataFrame(comparison_rows)
    factor_comparison.to_csv(output_dir / f"factor_comparison_{feed_name}_all_vs_raw_actions.csv", index=False)

    context = _read_json(production_root / "artifacts" / "daily_alpaca_scheduler" / "20260724_decision" / "run_context.json", {})
    config = _decision_config(context)
    all_targets, all_decision_diagnostics = _run_decisions(panels, rebuilt_all, production_root, all_source_name, config)
    custom_targets, custom_decision_diagnostics = _run_decisions(panels, rebuilt_custom, production_root, "raw_plus_actions", config)
    actual_targets = {session_date: _load_actual_targets(production_root, session_date) for session_date in SESSION_DATES}
    target_rows: list[dict[str, Any]] = []
    for session_date in SESSION_DATES:
        target_rows.append({"session_date": session_date, "target_source": "production_recorded", **_target_diff(actual_targets.get(session_date, {}), actual_targets.get(session_date, {}))})
        target_rows.append({"session_date": session_date, "target_source": f"{all_source_name}_vs_production", **_target_diff(all_targets.get(session_date, {}), actual_targets.get(session_date, {}))})
        target_rows.append({"session_date": session_date, "target_source": f"raw_plus_actions_vs_{all_source_name}", **_target_diff(custom_targets.get(session_date, {}), all_targets.get(session_date, {}))})
    target_comparison = pd.DataFrame(target_rows)
    target_comparison.to_csv(output_dir / "target_comparison.csv", index=False)
    _write_json(output_dir / "decision_replay_diagnostics.json", {
        all_source_name: all_decision_diagnostics,
        "raw_plus_actions": custom_decision_diagnostics,
    })

    replay_targets = {
        "actual": actual_targets,
        all_source_name: all_targets,
        "raw_plus_actions": custom_targets,
    }
    replay_frame, replay_summary = _portfolio_replay(replay_targets, all_frame, custom_frame)
    replay_frame.to_csv(output_dir / "ideal_portfolio_replay.csv", index=False)
    _write_json(output_dir / "ideal_portfolio_replay_summary.json", replay_summary)

    coverage = {
        "raw_row_count": len(raw_frame),
        "all_row_count": len(all_frame),
        "custom_row_count": len(custom_frame),
        "raw_symbol_count": int(raw_frame["symbol"].nunique()) if not raw_frame.empty else 0,
        "all_symbol_count": int(all_frame["symbol"].nunique()) if not all_frame.empty else 0,
        "custom_valid_symbol_count": int(custom_frame.groupby("symbol")["valid"].all().sum()) if not custom_frame.empty else 0,
        "raw_fetch_errors": raw_errors,
        "all_fetch_errors": all_errors,
        "action_fetch_errors": action_errors,
        "supplemental_raw_fetch_errors": supplemental_raw_errors,
        "supplemental_all_fetch_errors": supplemental_all_errors,
        "supplemental_symbol_count": len(supplemental_symbols),
        "action_type_counts": dict(Counter(str(row.get("normalized_action_type") or "") for row in normalized_actions)),
    }
    _write_json(output_dir / "coverage_summary.json", coverage)

    lines = [
        "# Corporate Action Raw Replay",
        "",
        f"- Bar source: Alpaca `{feed_name}` daily bars; raw vs all.",
        f"- Decision dates: {', '.join(SESSION_DATES)}.",
        f"- Orders submitted: no. This is an offline counterfactual only.",
        f"- Raw rows: {len(raw_frame)}; all rows: {len(all_frame)}; raw+actions rows: {len(custom_frame)}.",
        f"- Corporate-action rows: {len(normalized_actions)}; unsupported action rows: {sum(1 for row in action_rows if not row['supported_by_custom_algorithm'])}.",
        "",
        "## Interpretation Guardrails",
        "",
        f"The same-source {feed_name.upper()} Alpaca-all series is the controlled baseline for measuring the corporate-action algorithm. The recorded production panel is directly comparable only when its run artifact reports the same feed.",
        "",
        "The replay is an ideal target replay using daily bars. It is not realized account PnL and does not claim to reproduce order-fill timing or execution gap.",
        "",
        "Raw prices are transformed into a normalized wealth index. Supported events are cash dividends, splits, and stock dividends. Merger and spin-off rows are retained and marked unsupported unless a reliable symbol-level transformation is available.",
    ]
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output_dir": output_dir.as_posix(),
        "feed": feed_name,
        "raw_rows": len(raw_frame),
        "all_rows": len(all_frame),
        "action_rows": len(normalized_actions),
        "action_type_counts": dict(Counter(str(row.get("normalized_action_type") or "") for row in normalized_actions)),
        "fetch_error_counts": {"raw": len(raw_errors), "all": len(all_errors), "actions": len(action_errors)},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
