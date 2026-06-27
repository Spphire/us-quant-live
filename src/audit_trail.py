"""
Audit trail enhancements for decision engine.

Provides wrappers to capture optimizer diagnostics without modifying core logic.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class OptimizerDiagnostics:
    """Records optimizer solve details for post-mortem analysis."""
    timestamp_utc: str
    status: str  # "success" | "infeasible" | "unbounded" | "failed"
    solver: str = "highs"
    solver_exit_code: int | None = None
    solver_message: str | None = None
    solve_time_seconds: float | None = None

    # Constraint summary
    long_leverage_target: tuple[float, float] = (0.95, 1.0)
    short_leverage_target: tuple[float, float] = (0.95, 1.0)
    turnover_budget: float | None = None
    turnover_actual: float | None = None
    max_single_name_weight: float | None = None
    beta_neutrality_band: float | None = None
    beta_actual: float | None = None

    # Optimization params
    score_weight: float | None = None
    sector_penalty: float | None = None
    turnover_penalty: float | None = None

    # Candidate pool
    candidate_longs: int = 0
    candidate_shorts: int = 0

    # Fallback info
    fallback_triggered: bool = False
    fallback_method: str | None = None
    fallback_beta_band_used: float | None = None

    # Solution quality
    objective_value: float | None = None
    iterations: int | None = None

    # Extended diagnostics
    infeasibility_hints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)


@dataclass
class UniverseFilteringStats:
    """Records how many symbols were filtered out and why."""
    timestamp_utc: str
    total_symbols_from_data_source: int

    filtered_out: dict[str, int] = field(default_factory=dict)
    # Example keys:
    #   "missing_price", "missing_fundamental", "market_cap_too_small",
    #   "shares_outstanding_missing", "beta_obs_insufficient"

    passed_all_filters: int = 0

    # Filter rules applied
    filter_rules: dict[str, Any] = field(default_factory=dict)
    # Example: {"min_market_cap": None, "min_price": 1.0, ...}

    # Additional metadata
    data_date: str | None = None
    universe_source: str = "alpaca"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)


@dataclass
class ExecutionRecordEnhanced:
    """Enhanced execution record with failure reasons."""
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    limit_price: float | None = None

    submit_status: str = "unknown"  # "filled" | "rejected" | "timeout" | "error"

    # Success path
    alpaca_order_id: str | None = None
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    fill_time_utc: str | None = None

    # Failure path
    alpaca_error_code: str | None = None
    alpaca_error_message: str | None = None
    retry_count: int = 0

    # Metadata
    submitted_at_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_linprog_failure(
    result: Any,  # scipy.optimize.OptimizeResult
    *,
    n_longs: int,
    n_shorts: int,
    turnover_budget: float,
    max_weight: float,
    beta_band: float | None = None,
) -> list[str]:
    """
    Heuristic analysis of why linprog failed.

    Returns a list of possible infeasibility hints.
    """
    hints = []

    # Status code analysis (HiGHS-specific)
    status_code = getattr(result, "status", None)
    if status_code == 3:
        hints.append("HiGHS Status 3: Problem solved to optimal solution (but scipy marked as failure?)")
    elif status_code == 8:
        hints.append("HiGHS Status 8: The problem is infeasible (constraints conflict)")
    elif status_code == 10:
        hints.append("HiGHS Status 10: The problem is unbounded")

    # Message parsing
    msg = str(getattr(result, "message", ""))
    if "infeasible" in msg.lower():
        hints.append("Solver reported: infeasible problem")
        # Common reasons
        if turnover_budget < 0.05:
            hints.append(f"turnover_budget={turnover_budget:.3f} is very tight — may conflict with leverage targets")
        if n_longs < 20 or n_shorts < 20:
            hints.append(f"candidate_pool is small (longs={n_longs}, shorts={n_shorts}) — fewer degrees of freedom")
        if beta_band is not None and beta_band < 0.05:
            hints.append(f"beta_neutrality_band={beta_band:.3f} is very tight — hard to satisfy with limited pool")

    if "unbounded" in msg.lower():
        hints.append("Solver reported: unbounded problem (likely a constraint is missing or badly scaled)")

    # Numeric issues
    if "singular" in msg.lower() or "ill-conditioned" in msg.lower():
        hints.append("Numerical instability detected — constraint matrix may be rank-deficient")

    return hints


def create_optimizer_diagnostics_from_result(
    result: Any,
    *,
    start_time: float,
    candidate_longs: int,
    candidate_shorts: int,
    turnover_budget: float,
    max_weight: float,
    score_weight: float,
    sector_penalty: float,
    turnover_penalty: float,
    beta_band: float | None = None,
    fallback_triggered: bool = False,
    fallback_method: str | None = None,
    fallback_beta_band_used: float | None = None,
) -> OptimizerDiagnostics:
    """Create diagnostics object from scipy linprog result."""
    from datetime import datetime, timezone

    solve_time = time.time() - start_time
    status_map = {0: "success", 1: "iteration_limit", 2: "infeasible", 3: "unbounded", 4: "numerical_error"}
    status_code = getattr(result, "status", -1)
    status_str = status_map.get(status_code, "failed")
    if result.success:
        status_str = "success"

    hints = []
    if not result.success:
        hints = analyze_linprog_failure(
            result,
            n_longs=candidate_longs,
            n_shorts=candidate_shorts,
            turnover_budget=turnover_budget,
            max_weight=max_weight,
            beta_band=beta_band,
        )

    # Extract actual solution metrics if available
    turnover_actual = None
    beta_actual = None
    objective_value = None
    if result.success and hasattr(result, "x") and result.x is not None:
        objective_value = float(getattr(result, "fun", np.nan))
        # turnover/beta would need access to prev weights and beta vector — skip for now

    return OptimizerDiagnostics(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        status=status_str,
        solver="highs",
        solver_exit_code=status_code,
        solver_message=str(getattr(result, "message", "")),
        solve_time_seconds=solve_time,
        turnover_budget=turnover_budget,
        turnover_actual=turnover_actual,
        max_single_name_weight=max_weight,
        beta_neutrality_band=beta_band,
        beta_actual=beta_actual,
        score_weight=score_weight,
        sector_penalty=sector_penalty,
        turnover_penalty=turnover_penalty,
        candidate_longs=candidate_longs,
        candidate_shorts=candidate_shorts,
        fallback_triggered=fallback_triggered,
        fallback_method=fallback_method,
        fallback_beta_band_used=fallback_beta_band_used,
        objective_value=objective_value,
        infeasibility_hints=hints,
    )
