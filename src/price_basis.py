from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


VALID_ALPACA_ADJUSTMENTS = frozenset({"raw", "split", "dividend", "all"})
VALID_ALPHA_RETURN_ADJUSTMENTS = frozenset({"split", "all"})
RAW_PRICE_ADJUSTMENT = "raw"
DEFAULT_ALPHA_PRICE_ADJUSTMENT = "all"


def normalize_price_adjustment(value: str, *, field_name: str = "price_adjustment") -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_ALPACA_ADJUSTMENTS:
        choices = ", ".join(sorted(VALID_ALPACA_ADJUSTMENTS))
        raise ValueError(f"Unsupported {field_name}={value!r}; expected one of: {choices}.")
    return normalized


def normalize_alpha_price_adjustment(value: str) -> str:
    normalized = normalize_price_adjustment(value, field_name="alpha_price_adjustment")
    if normalized not in VALID_ALPHA_RETURN_ADJUSTMENTS:
        choices = ", ".join(sorted(VALID_ALPHA_RETURN_ADJUSTMENTS))
        raise ValueError(
            f"Unsupported alpha_price_adjustment={value!r}; return-based alpha requires: {choices}."
        )
    return normalized


def build_dual_price_panel(
    *,
    raw_bars: Sequence[Mapping[str, Any]],
    adjusted_bars: Sequence[Mapping[str, Any]],
    adjusted_price_adjustment: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    adjusted_basis = normalize_price_adjustment(
        adjusted_price_adjustment,
        field_name="adjusted_price_adjustment",
    )
    raw_frame = _bars_to_basis_frame(raw_bars, basis="raw")
    adjusted_frame = _bars_to_basis_frame(adjusted_bars, basis="adjusted")
    panel = raw_frame.merge(
        adjusted_frame,
        on=["symbol", "session_date"],
        how="outer",
        validate="one_to_one",
    )
    if panel.empty:
        panel = _empty_dual_price_panel()
    else:
        panel = panel.sort_values(["symbol", "session_date"]).reset_index(drop=True)
        panel["raw_price_available"] = panel["raw_close"].notna()
        panel["adjusted_price_available"] = panel["adjusted_close"].notna()
        panel["price_basis_status"] = np.select(
            [
                panel["raw_price_available"] & panel["adjusted_price_available"],
                panel["raw_price_available"],
                panel["adjusted_price_available"],
            ],
            ["matched", "raw_only", "adjusted_only"],
            default="missing",
        )
        panel["adjustment_factor"] = np.where(
            panel["raw_close"].gt(0.0) & panel["adjusted_close"].gt(0.0),
            panel["adjusted_close"] / panel["raw_close"],
            np.nan,
        )
        panel["has_price_adjustment"] = panel["adjustment_factor"].map(
            lambda value: bool(
                _is_finite_number(value)
                and not math.isclose(float(value), 1.0, rel_tol=1e-9, abs_tol=1e-9)
            )
        )
        panel["raw_price_adjustment"] = RAW_PRICE_ADJUSTMENT
        panel["alpha_price_adjustment"] = adjusted_basis
        panel["alpha_return_price_source"] = "adjusted_close"
        panel["absolute_price_source"] = "raw_close"

    diagnostics = summarize_dual_price_panel(
        panel,
        adjusted_price_adjustment=adjusted_basis,
        raw_input_row_count=len(raw_bars),
        adjusted_input_row_count=len(adjusted_bars),
    )
    return panel, diagnostics


def summarize_dual_price_panel(
    panel: pd.DataFrame,
    *,
    adjusted_price_adjustment: str,
    raw_input_row_count: int | None = None,
    adjusted_input_row_count: int | None = None,
) -> dict[str, Any]:
    adjusted_basis = normalize_price_adjustment(
        adjusted_price_adjustment,
        field_name="adjusted_price_adjustment",
    )
    frame = panel.copy() if isinstance(panel, pd.DataFrame) else pd.DataFrame()
    row_count = int(len(frame))
    raw_available = _bool_series(frame, "raw_price_available", fallback_column="raw_close")
    adjusted_available = _bool_series(
        frame,
        "adjusted_price_available",
        fallback_column="adjusted_close",
    )
    matched = raw_available & adjusted_available
    raw_only = raw_available & ~adjusted_available
    adjusted_only = adjusted_available & ~raw_available
    factors = pd.to_numeric(
        frame.get("adjustment_factor", pd.Series(dtype=float)),
        errors="coerce",
    )
    finite_factors = factors[np.isfinite(factors) & factors.gt(0.0)]
    affected = finite_factors[~np.isclose(finite_factors, 1.0, rtol=1e-9, atol=1e-9)]
    affected_rows = frame.loc[affected.index].copy() if len(affected) else pd.DataFrame()
    affected_symbols = sorted(
        {
            str(value).strip().upper()
            for value in affected_rows.get("symbol", pd.Series(dtype=str)).tolist()
            if str(value).strip()
        }
    )
    outlier_rows: list[dict[str, Any]] = []
    if len(affected_rows):
        affected_rows["_distance"] = np.abs(np.log(pd.to_numeric(affected_rows["adjustment_factor"])))
        for row in affected_rows.sort_values("_distance", ascending=False).head(50).itertuples(index=False):
            outlier_rows.append(
                {
                    "symbol": str(getattr(row, "symbol", "")),
                    "session_date": str(getattr(row, "session_date", "")),
                    "raw_close": _json_number(getattr(row, "raw_close", None)),
                    "adjusted_close": _json_number(getattr(row, "adjusted_close", None)),
                    "adjustment_factor": _json_number(getattr(row, "adjustment_factor", None)),
                }
            )

    status = "pass"
    if row_count == 0:
        status = "empty"
    elif int(matched.sum()) != row_count:
        status = "partial"
    return {
        "schema_version": "1.0",
        "artifact_type": "price_basis_diagnostics",
        "status": status,
        "semantics": {
            "alpha_return_prices": f"Alpaca daily bars adjustment={adjusted_basis}",
            "absolute_prices": "Alpaca daily bars adjustment=raw",
            "adjustment_factor": "adjusted_close / raw_close",
            "execution_prices": "live unadjusted broker/quote-provider prices; adjusted bars are forbidden",
        },
        "raw_price_adjustment": RAW_PRICE_ADJUSTMENT,
        "alpha_price_adjustment": adjusted_basis,
        "raw_input_row_count": int(raw_input_row_count if raw_input_row_count is not None else row_count),
        "adjusted_input_row_count": int(
            adjusted_input_row_count if adjusted_input_row_count is not None else row_count
        ),
        "panel_row_count": row_count,
        "symbol_count": int(frame["symbol"].nunique()) if row_count and "symbol" in frame else 0,
        "matched_row_count": int(matched.sum()),
        "raw_only_row_count": int(raw_only.sum()),
        "adjusted_only_row_count": int(adjusted_only.sum()),
        "adjusted_row_count": int(len(affected)),
        "adjusted_symbol_count": len(affected_symbols),
        "adjusted_symbols": affected_symbols,
        "adjustment_factor_min": _json_number(finite_factors.min()) if len(finite_factors) else None,
        "adjustment_factor_max": _json_number(finite_factors.max()) if len(finite_factors) else None,
        "largest_adjustment_rows": outlier_rows,
    }


def summarize_alpha_price_basis_panel(
    panel: pd.DataFrame,
    *,
    configured_adjustment: str | None = None,
) -> dict[str, Any]:
    frame = panel.copy() if isinstance(panel, pd.DataFrame) else pd.DataFrame()
    configured = str(configured_adjustment or "").strip().lower()
    if configured:
        configured = normalize_price_adjustment(
            configured,
            field_name="configured_alpha_price_adjustment",
        )
    observed_adjustments = sorted(
        {
            str(value).strip().lower()
            for value in frame.get("alpha_price_adjustment", pd.Series(dtype=str)).dropna().tolist()
            if str(value).strip()
        }
    )
    basis = configured or (observed_adjustments[0] if len(observed_adjustments) == 1 else "all")
    if basis not in VALID_ALPACA_ADJUSTMENTS:
        basis = DEFAULT_ALPHA_PRICE_ADJUSTMENT
    required_columns = {
        "raw_close",
        "adjusted_close",
        "lagged_raw_close",
        "lagged_adjusted_close",
        "adjustment_factor",
        "alpha_price_adjustment",
        "alpha_return_price_source",
        "absolute_price_source",
        "market_cap_price_asof_session_date",
        "shares_outstanding_reported",
        "shares_split_adjustment_factor",
        "shares_outstanding_price_basis",
        "shares_price_basis_status",
        "shares_split_adjustment_start",
        "shares_split_adjustment_end",
        "shares_split_adjustment_dates",
        "shares_split_action_count",
    }
    missing_columns = sorted(required_columns - set(frame.columns))
    diagnostics = summarize_dual_price_panel(
        frame,
        adjusted_price_adjustment=basis,
    )
    observed_return_sources = sorted(
        {
            str(value).strip()
            for value in frame.get("alpha_return_price_source", pd.Series(dtype=str)).dropna().tolist()
            if str(value).strip()
        }
    )
    observed_absolute_sources = sorted(
        {
            str(value).strip()
            for value in frame.get("absolute_price_source", pd.Series(dtype=str)).dropna().tolist()
            if str(value).strip()
        }
    )
    adjustment_matches = observed_adjustments == [basis]
    alpha_return_adjustment_supported = basis in VALID_ALPHA_RETURN_ADJUSTMENTS
    return_source_matches = observed_return_sources == ["adjusted_close"]
    absolute_source_matches = observed_absolute_sources == ["raw_close"]
    factors = pd.to_numeric(
        frame.get("adjustment_factor", pd.Series(dtype=float)),
        errors="coerce",
    )
    factor_available_count = int((np.isfinite(factors) & factors.gt(0.0)).sum())
    reported_shares = pd.to_numeric(
        frame.get(
            "shares_outstanding_reported",
            pd.Series(np.nan, index=frame.index, dtype=float),
        ),
        errors="coerce",
    )
    share_basis_status = (
        frame.get(
            "shares_price_basis_status",
            pd.Series("", index=frame.index, dtype=str),
        )
        .fillna("")
        .astype(str)
    )
    reported_share_mask = np.isfinite(reported_shares) & reported_shares.gt(0.0)
    valid_share_basis_statuses = {"adjusted_for_splits", "no_split_adjustment"}
    incomplete_share_basis_count = int(
        (reported_share_mask & ~share_basis_status.isin(valid_share_basis_statuses)).sum()
    )
    split_adjustment_factors = pd.to_numeric(
        frame.get(
            "shares_split_adjustment_factor",
            pd.Series(np.nan, index=frame.index, dtype=float),
        ),
        errors="coerce",
    )
    split_adjusted_mask = np.isfinite(split_adjustment_factors) & ~np.isclose(
        split_adjustment_factors,
        1.0,
        rtol=1e-9,
        atol=1e-9,
    )
    evidence_complete = bool(
        not missing_columns
        and diagnostics["status"] == "pass"
        and adjustment_matches
        and alpha_return_adjustment_supported
        and return_source_matches
        and absolute_source_matches
        and factor_available_count == len(frame)
        and incomplete_share_basis_count == 0
    )
    diagnostics.update(
        {
            "artifact_type": "alpha_price_basis_evidence",
            "alpha_row_count": int(len(frame)),
            "configured_alpha_price_adjustment": basis,
            "observed_alpha_price_adjustments": observed_adjustments,
            "alpha_price_adjustment_matches_config": adjustment_matches,
            "alpha_return_adjustment_supported": alpha_return_adjustment_supported,
            "observed_alpha_return_price_sources": observed_return_sources,
            "alpha_return_price_source_matches": return_source_matches,
            "observed_absolute_price_sources": observed_absolute_sources,
            "absolute_price_source_matches": absolute_source_matches,
            "adjustment_factor_available_count": factor_available_count,
            "reported_share_row_count": int(reported_share_mask.sum()),
            "incomplete_share_price_basis_row_count": incomplete_share_basis_count,
            "split_adjusted_share_row_count": int(split_adjusted_mask.sum()),
            "share_price_basis_status_counts": {
                str(key): int(value)
                for key, value in share_basis_status.value_counts(dropna=False).to_dict().items()
            },
            "share_split_adjustments": [
                {
                    "symbol": str(row.get("symbol") or ""),
                    "reported_shares": _json_number(row.get("shares_outstanding_reported")),
                    "split_adjustment_factor": _json_number(row.get("shares_split_adjustment_factor")),
                    "price_basis_shares": _json_number(row.get("shares_outstanding_price_basis")),
                    "adjustment_start": str(row.get("shares_split_adjustment_start") or ""),
                    "adjustment_end": str(row.get("shares_split_adjustment_end") or ""),
                    "split_dates": str(row.get("shares_split_adjustment_dates") or ""),
                    "split_action_count": int(_safe_float(row.get("shares_split_action_count")) or 0),
                }
                for row in frame.loc[split_adjusted_mask].to_dict("records")[:100]
            ],
            "required_columns": sorted(required_columns),
            "missing_required_columns": missing_columns,
            "status": (
                "missing" if frame.empty else "pass" if evidence_complete else "partial"
            ),
        }
    )
    return diagnostics


def _bars_to_basis_frame(
    bars: Sequence[Mapping[str, Any]],
    *,
    basis: str,
) -> pd.DataFrame:
    prefix = str(basis).strip().lower()
    if prefix not in {"raw", "adjusted"}:
        raise ValueError(f"Unsupported price basis prefix: {basis!r}")
    rows: list[dict[str, Any]] = []
    for bar in bars:
        symbol = str(bar.get("symbol") or "").strip().upper()
        timestamp = str(bar.get("t") or bar.get("timestamp") or "")
        session_date = timestamp[:10] if len(timestamp) >= 10 else ""
        close = _safe_float(bar.get("c"))
        if close is None:
            close = _safe_float(bar.get("close"))
        if not symbol or not session_date or close is None or close <= 0.0:
            continue
        row: dict[str, Any] = {
            "symbol": symbol,
            "session_date": session_date,
            f"{prefix}_close": float(close),
        }
        for short_name, long_name in (
            ("o", "open"),
            ("h", "high"),
            ("l", "low"),
            ("v", "volume"),
            ("vw", "vwap"),
            ("n", "trade_count"),
        ):
            value = _safe_float(bar.get(short_name))
            if value is None:
                value = _safe_float(bar.get(long_name))
            row[f"{prefix}_{long_name}"] = float(value) if value is not None else np.nan
        rows.append(row)
    columns = [
        "symbol",
        "session_date",
        *[f"{prefix}_{name}" for name in ("open", "high", "low", "close", "volume", "vwap", "trade_count")],
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return (
        pd.DataFrame(rows)
        .sort_values(["symbol", "session_date"])
        .drop_duplicates(["symbol", "session_date"], keep="last")
        .reset_index(drop=True)
    )


def _empty_dual_price_panel() -> pd.DataFrame:
    columns = ["symbol", "session_date"]
    for basis in ("raw", "adjusted"):
        columns.extend(
            f"{basis}_{name}"
            for name in ("open", "high", "low", "close", "volume", "vwap", "trade_count")
        )
    columns.extend(
        [
            "raw_price_available",
            "adjusted_price_available",
            "price_basis_status",
            "adjustment_factor",
            "has_price_adjustment",
            "raw_price_adjustment",
            "alpha_price_adjustment",
            "alpha_return_price_source",
            "absolute_price_source",
        ]
    )
    return pd.DataFrame(columns=columns)


def _bool_series(frame: pd.DataFrame, column: str, *, fallback_column: str) -> pd.Series:
    if column in frame:
        return frame[column].fillna(False).astype(bool)
    if fallback_column in frame:
        return pd.to_numeric(frame[fallback_column], errors="coerce").gt(0.0)
    return pd.Series(False, index=frame.index, dtype=bool)


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_finite_number(value: Any) -> bool:
    parsed = _safe_float(value)
    return parsed is not None


def _json_number(value: Any) -> float | None:
    parsed = _safe_float(value)
    return float(parsed) if parsed is not None else None
