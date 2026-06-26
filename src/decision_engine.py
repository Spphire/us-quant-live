from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from lot_manager import (
    DEFAULT_FACTOR_MIN_HOLDS,
    FACTOR_COLUMNS,
    EPS,
    LotManager,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALPHA_ROOT = PROJECT_ROOT / "artifacts" / "alpha_core"
DEFAULT_DECISION_ROOT = PROJECT_ROOT / "artifacts" / "decision"

DEFAULT_FACTOR_WEIGHTS = {
    "reversal_score": 0.25,
    "momentum_score": 0.10,
    "small_size_score": 0.30,
    "low_beta_score": 0.20,
    "cash_quality_score": 0.15,
}
DEFAULT_BETA_BAND_GRID = (0.05, 0.10, 0.15, 0.20)


@dataclass(frozen=True)
class DecisionConfig:
    factor_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_FACTOR_WEIGHTS))
    factor_min_holds: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_FACTOR_MIN_HOLDS))
    candidate_pool_per_side: int = 120
    max_single_name_side_weight: float = 1.0 / 30.0
    min_nonzero_names: int = 20
    score_weight: float = 0.01
    sector_penalty: float = 25.0
    turnover_penalty: float = 0.005
    turnover_budget: float = 0.15
    beta_band_grid: Sequence[float] = DEFAULT_BETA_BAND_GRID


@dataclass
class DecisionResult:
    status: str
    session_idx: int
    session_date: str
    targets: pd.DataFrame
    diagnostics: dict[str, Any]
    skip_reason: str | None = None


class DecisionEngine:
    """Standalone single-session decision engine matching locked-lot mechanics."""

    def __init__(self, config: DecisionConfig, *, eps: float = EPS) -> None:
        self.config = config
        self.eps = float(eps)
        self._validate_config()

    def decide(
        self,
        *,
        alpha_frame: pd.DataFrame,
        lot_manager: LotManager,
        session_idx: int,
        session_date: str,
    ) -> DecisionResult:
        scored = self._prepare_alpha_frame(alpha_frame)
        needed = ["symbol", "composite_score", "beta", "sic2_sector", *FACTOR_COLUMNS]
        base = scored.dropna(subset=needed).drop_duplicates("symbol").copy()

        available_symbols = set(base["symbol"].astype(str))
        dropped_summary = lot_manager.prune(available_symbols=available_symbols, session_idx=int(session_idx))
        previous_weights = lot_manager.previous_weights()
        locked_weights = lot_manager.locked_weights(int(session_idx))
        locked_symbols = {side: set(locked_weights[side]) for side in ("long", "short")}

        longs = self._candidate_union_locked(
            base,
            side="long",
            previous_weights=previous_weights["long"],
            locked_symbols=locked_symbols["long"],
            blocked_symbols=locked_symbols["short"],
            candidate_pool_per_side=int(self.config.candidate_pool_per_side),
        )
        shorts = self._candidate_union_locked(
            base,
            side="short",
            previous_weights=previous_weights["short"],
            locked_symbols=locked_symbols["short"],
            blocked_symbols=locked_symbols["long"],
            candidate_pool_per_side=int(self.config.candidate_pool_per_side),
        )

        effective_budget = float(self.config.turnover_budget)
        deploy_gap = self._deploy_gap_from_previous(previous_weights)
        turnover_cap_total = float(effective_budget + deploy_gap)
        carry_reason = ""
        used_beta_band: float | None = None

        if len(base) < 2 * int(self.config.min_nonzero_names) or longs.empty or shorts.empty:
            repaired = self._repair_after_optimizer_failure(
                lot_manager=lot_manager,
                base=base,
                longs=longs,
                shorts=shorts,
                previous_weights=previous_weights,
                locked_weights=locked_weights,
                session_date=str(session_date),
                session_idx=int(session_idx),
                reason="insufficient_base_or_candidates",
                dropped_summary=dropped_summary,
            )
            if repaired is not None:
                return repaired

        try:
            long_weights, short_weights = self._optimize_joint_weights_locked(
                longs=longs,
                shorts=shorts,
                max_weight=float(self.config.max_single_name_side_weight),
                score_weight=float(self.config.score_weight),
                sector_penalty=float(self.config.sector_penalty),
                turnover_penalty=float(self.config.turnover_penalty),
                previous_long_weights=previous_weights["long"],
                previous_short_weights=previous_weights["short"],
                locked_long_weights=locked_weights["long"],
                locked_short_weights=locked_weights["short"],
                turnover_budget=float(effective_budget),
                deploy_gap=float(deploy_gap),
            )
        except ValueError as exc:
            relaxed = self._try_relaxed_beta_fallback(
                longs=longs,
                shorts=shorts,
                max_weight=float(self.config.max_single_name_side_weight),
                score_weight=float(self.config.score_weight),
                sector_penalty=float(self.config.sector_penalty),
                turnover_penalty=float(self.config.turnover_penalty),
                previous_long_weights=previous_weights["long"],
                previous_short_weights=previous_weights["short"],
                locked_long_weights=locked_weights["long"],
                locked_short_weights=locked_weights["short"],
                turnover_budget=float(effective_budget),
                deploy_gap=float(deploy_gap),
                beta_band_grid=self.config.beta_band_grid,
            )
            if relaxed is not None:
                long_weights, short_weights, used_beta_band = relaxed
                carry_reason = (
                    f"fallback_relaxed_beta_band_{used_beta_band:.3f}"
                    f"_after_optimizer_failed:{str(exc).replace(' ', '_')}"
                )
            else:
                repaired = self._repair_after_optimizer_failure(
                    lot_manager=lot_manager,
                    base=base,
                    longs=longs,
                    shorts=shorts,
                    previous_weights=previous_weights,
                    locked_weights=locked_weights,
                    session_date=str(session_date),
                    session_idx=int(session_idx),
                    reason=f"optimizer_failed:{exc}",
                    dropped_summary=dropped_summary,
                )
                if repaired is not None:
                    return repaired
                diagnostics = {
                    "status": "skip",
                    "skip_reason": f"optimizer_failed_unrepairable:{exc}",
                    "session_date": str(session_date),
                    "session_idx": int(session_idx),
                    "base_names": int(len(base)),
                    "long_candidates": int(len(longs)),
                    "short_candidates": int(len(shorts)),
                    "dropped_lot_summary": dropped_summary,
                }
                return DecisionResult(
                    status="skip",
                    session_idx=int(session_idx),
                    session_date=str(session_date),
                    targets=pd.DataFrame(),
                    diagnostics=diagnostics,
                    skip_reason="optimizer_failed_unrepairable",
                )

        if int((long_weights > self.eps).sum()) < int(self.config.min_nonzero_names):
            repaired = self._repair_after_optimizer_failure(
                lot_manager=lot_manager,
                base=base,
                longs=longs,
                shorts=shorts,
                previous_weights=previous_weights,
                locked_weights=locked_weights,
                session_date=str(session_date),
                session_idx=int(session_idx),
                reason="insufficient_long_nonzero",
                dropped_summary=dropped_summary,
            )
            if repaired is not None:
                return repaired
        if int((short_weights > self.eps).sum()) < int(self.config.min_nonzero_names):
            repaired = self._repair_after_optimizer_failure(
                lot_manager=lot_manager,
                base=base,
                longs=longs,
                shorts=shorts,
                previous_weights=previous_weights,
                locked_weights=locked_weights,
                session_date=str(session_date),
                session_idx=int(session_idx),
                reason="insufficient_short_nonzero",
                dropped_summary=dropped_summary,
            )
            if repaired is not None:
                return repaired

        raw_turnover = self._side_turnover(previous_weights["long"], longs["symbol"], long_weights)
        raw_turnover += self._side_turnover(previous_weights["short"], shorts["symbol"], short_weights)
        rebalance_turnover = max(0.0, float(raw_turnover) - float(deploy_gap))

        long_target = self._target_dict(longs["symbol"], long_weights)
        short_target = self._target_dict(shorts["symbol"], short_weights)
        lot_manager.update_for_targets(
            target_weights={"long": long_target, "short": short_target},
            base=base,
            session_idx=int(session_idx),
            session_date=str(session_date),
            factor_weights=self.config.factor_weights,
            factor_min_holds=self.config.factor_min_holds,
        )

        rows = [
            *self._position_rows(longs, long_weights, session_date=str(session_date), session_idx=int(session_idx), side="long"),
            *self._position_rows(shorts, short_weights, session_date=str(session_date), session_idx=int(session_idx), side="short"),
        ]
        targets = pd.DataFrame(rows)
        diagnostics = {
            "status": "ok",
            "session_date": str(session_date),
            "session_idx": int(session_idx),
            "base_names": int(len(base)),
            "long_candidates": int(len(longs)),
            "short_candidates": int(len(shorts)),
            "turnover_budget": float(effective_budget),
            "deploy_gap": float(deploy_gap),
            "turnover_cap_total": float(turnover_cap_total),
            "effective_turnover_budget": float(effective_budget),
            "target_turnover_raw": float(raw_turnover),
            "target_turnover": float(rebalance_turnover),
            "budget_used_fraction": float(rebalance_turnover / effective_budget) if effective_budget > 0 else np.nan,
            "carry_reason": carry_reason,
            "used_beta_band": used_beta_band,
            "long_names": int((long_weights > self.eps).sum()),
            "short_names": int((short_weights > self.eps).sum()),
            "long_beta": float(np.dot(long_weights, longs["beta"])),
            "short_beta": float(np.dot(short_weights, shorts["beta"])),
            "net_beta": float(np.dot(long_weights, longs["beta"]) - np.dot(short_weights, shorts["beta"])),
            "locked_long_weight": float(sum(locked_weights["long"].values())),
            "locked_short_weight": float(sum(locked_weights["short"].values())),
            "locked_total_weight": float(sum(locked_weights["long"].values()) + sum(locked_weights["short"].values())),
            "dropped_lot_summary": dropped_summary,
        }
        return DecisionResult(
            status="ok",
            session_idx=int(session_idx),
            session_date=str(session_date),
            targets=targets,
            diagnostics=diagnostics,
            skip_reason=None,
        )

    def _prepare_alpha_frame(self, alpha_frame: pd.DataFrame) -> pd.DataFrame:
        frame = alpha_frame.copy()
        for required in ("symbol", "beta", "sic2_sector"):
            if required not in frame.columns:
                raise ValueError(f"alpha frame missing required column: {required}")

        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        frame["beta"] = pd.to_numeric(frame["beta"], errors="coerce")
        frame["sic2_sector"] = frame["sic2_sector"].astype(str)
        if "sic4_industry" not in frame.columns:
            frame["sic4_industry"] = frame["sic2_sector"].astype(str)

        for factor in FACTOR_COLUMNS:
            if factor not in frame.columns:
                raise ValueError(f"alpha frame missing factor column: {factor}")
            frame[factor] = pd.to_numeric(frame[factor], errors="coerce")

        if "composite_score" in frame.columns:
            frame["composite_score"] = pd.to_numeric(frame["composite_score"], errors="coerce")
        else:
            score = np.zeros(len(frame), dtype=float)
            total_weight = 0.0
            for column, weight in self.config.factor_weights.items():
                if column not in frame.columns:
                    continue
                values = pd.to_numeric(frame[column], errors="coerce").fillna(0.0).to_numpy(dtype=float)
                score += float(weight) * values
                total_weight += abs(float(weight))
            if total_weight <= 0:
                raise ValueError("factor_weights must include at least one non-zero value")
            frame["composite_score_raw"] = score / total_weight
            frame["composite_score"] = self._zscore_array(frame["composite_score_raw"].to_numpy(dtype=float))
        frame["composite_score"] = frame["composite_score"].replace([np.inf, -np.inf], np.nan)
        return frame

    def _carry_previous(
        self,
        *,
        base: pd.DataFrame,
        previous_weights: Mapping[str, Mapping[str, float]],
        session_date: str,
        session_idx: int,
        reason: str,
        dropped_summary: Mapping[str, float | int],
    ) -> DecisionResult | None:
        if self._is_empty_book(previous_weights):
            return None
        longs = self._weights_frame(base, previous_weights["long"])
        shorts = self._weights_frame(base, previous_weights["short"])
        if longs.empty or shorts.empty:
            return None
        long_weights = longs["target_weight"].to_numpy(dtype=float)
        short_weights = shorts["target_weight"].to_numpy(dtype=float)
        if abs(float(long_weights.sum()) - 1.0) > 1e-6 or abs(float(short_weights.sum()) - 1.0) > 1e-6:
            return None
        rows = [
            *self._position_rows(
                longs.drop(columns=["target_weight"]),
                long_weights,
                session_date=session_date,
                session_idx=session_idx,
                side="long",
            ),
            *self._position_rows(
                shorts.drop(columns=["target_weight"]),
                short_weights,
                session_date=session_date,
                session_idx=session_idx,
                side="short",
            ),
        ]
        diagnostics = {
            "status": "carry",
            "session_date": str(session_date),
            "session_idx": int(session_idx),
            "carry_reason": str(reason),
            "base_names": int(len(base)),
            "long_candidates": int(len(longs)),
            "short_candidates": int(len(shorts)),
            "target_turnover": 0.0,
            "dropped_lot_summary": dict(dropped_summary),
        }
        return DecisionResult(
            status="carry",
            session_idx=int(session_idx),
            session_date=str(session_date),
            targets=pd.DataFrame(rows),
            diagnostics=diagnostics,
            skip_reason=None,
        )

    def _repair_after_optimizer_failure(
        self,
        *,
        lot_manager: LotManager,
        base: pd.DataFrame,
        longs: pd.DataFrame,
        shorts: pd.DataFrame,
        previous_weights: Mapping[str, Mapping[str, float]],
        locked_weights: Mapping[str, Mapping[str, float]],
        session_date: str,
        session_idx: int,
        reason: str,
        dropped_summary: Mapping[str, float | int],
    ) -> DecisionResult | None:
        strict_carry = self._carry_previous(
            base=base,
            previous_weights=previous_weights,
            session_date=session_date,
            session_idx=session_idx,
            reason=reason,
            dropped_summary=dropped_summary,
        )
        if strict_carry is not None:
            strict_carry.status = "repair"
            strict_carry.diagnostics.update(
                {
                    "status": "repair",
                    "fallback_method": "carry_previous",
                    "fallback_reason": str(reason),
                    "skip_reason": None,
                }
            )
            self._update_lot_manager_from_targets(
                lot_manager=lot_manager,
                base=base,
                long_target=dict(previous_weights["long"]),
                short_target=dict(previous_weights["short"]),
                session_idx=session_idx,
                session_date=session_date,
            )
            return strict_carry

        for method, enforce_cap, allow_cap_relax, fill_empty in (
            ("carry_repair_low_turnover", False, False, False),
            ("carry_repair_fill_ranked", True, False, True),
            ("ranked_emergency_fill", True, True, True),
        ):
            long_target = self._fill_side_target(
                candidates=longs,
                previous=previous_weights["long"],
                locked=locked_weights["long"],
                side="long",
                enforce_cap=enforce_cap,
                allow_cap_relax=allow_cap_relax,
                fill_empty=fill_empty,
            )
            short_target = self._fill_side_target(
                candidates=shorts,
                previous=previous_weights["short"],
                locked=locked_weights["short"],
                side="short",
                enforce_cap=enforce_cap,
                allow_cap_relax=allow_cap_relax,
                fill_empty=fill_empty,
            )
            if not long_target or not short_target:
                continue

            long_frame = self._weights_frame(base, long_target)
            short_frame = self._weights_frame(base, short_target)
            if long_frame.empty or short_frame.empty:
                continue
            long_weights = long_frame["target_weight"].to_numpy(dtype=float)
            short_weights = short_frame["target_weight"].to_numpy(dtype=float)
            if abs(float(long_weights.sum()) - 1.0) > 1e-6 or abs(float(short_weights.sum()) - 1.0) > 1e-6:
                continue

            raw_turnover = self._side_turnover(previous_weights["long"], long_frame["symbol"], long_weights)
            raw_turnover += self._side_turnover(previous_weights["short"], short_frame["symbol"], short_weights)
            deploy_gap = self._deploy_gap_from_previous(previous_weights)
            rebalance_turnover = max(0.0, float(raw_turnover) - float(deploy_gap))
            net_beta = float(np.dot(long_weights, long_frame["beta"]) - np.dot(short_weights, short_frame["beta"]))

            rows = [
                *self._position_rows(
                    long_frame.drop(columns=["target_weight"]),
                    long_weights,
                    session_date=session_date,
                    session_idx=session_idx,
                    side="long",
                ),
                *self._position_rows(
                    short_frame.drop(columns=["target_weight"]),
                    short_weights,
                    session_date=session_date,
                    session_idx=session_idx,
                    side="short",
                ),
            ]
            targets = pd.DataFrame(rows)
            self._update_lot_manager_from_targets(
                lot_manager=lot_manager,
                base=base,
                long_target=long_target,
                short_target=short_target,
                session_idx=session_idx,
                session_date=session_date,
            )

            diagnostics = {
                "status": "repair",
                "fallback_method": method,
                "fallback_reason": str(reason),
                "session_date": str(session_date),
                "session_idx": int(session_idx),
                "base_names": int(len(base)),
                "long_candidates": int(len(longs)),
                "short_candidates": int(len(shorts)),
                "long_names": int(len(long_target)),
                "short_names": int(len(short_target)),
                "target_turnover_raw": float(raw_turnover),
                "target_turnover": float(rebalance_turnover),
                "net_beta": float(net_beta),
                "max_single_name_side_weight": float(self.config.max_single_name_side_weight),
                "max_long_side_weight": float(long_weights.max()) if len(long_weights) else 0.0,
                "max_short_side_weight": float(short_weights.max()) if len(short_weights) else 0.0,
                "locked_long_weight": float(sum(locked_weights["long"].values())),
                "locked_short_weight": float(sum(locked_weights["short"].values())),
                "locked_total_weight": float(sum(locked_weights["long"].values()) + sum(locked_weights["short"].values())),
                "dropped_lot_summary": dict(dropped_summary),
                "cap_enforced": bool(enforce_cap),
                "cap_relaxed": bool(allow_cap_relax),
            }
            diagnostics["cap_relaxed"] = bool(
                diagnostics["cap_relaxed"]
                or not diagnostics["cap_enforced"]
                or diagnostics["max_long_side_weight"] > diagnostics["max_single_name_side_weight"] + 1e-8
                or diagnostics["max_short_side_weight"] > diagnostics["max_single_name_side_weight"] + 1e-8
            )
            return DecisionResult(
                status="repair",
                session_idx=int(session_idx),
                session_date=str(session_date),
                targets=targets,
                diagnostics=diagnostics,
                skip_reason=None,
            )
        return None

    def _update_lot_manager_from_targets(
        self,
        *,
        lot_manager: LotManager,
        base: pd.DataFrame,
        long_target: Mapping[str, float],
        short_target: Mapping[str, float],
        session_idx: int,
        session_date: str,
    ) -> None:
        lot_manager.update_for_targets(
            target_weights={"long": dict(long_target), "short": dict(short_target)},
            base=base,
            session_idx=int(session_idx),
            session_date=str(session_date),
            factor_weights=self.config.factor_weights,
            factor_min_holds=self.config.factor_min_holds,
        )

    def _fill_side_target(
        self,
        *,
        candidates: pd.DataFrame,
        previous: Mapping[str, float],
        locked: Mapping[str, float],
        side: str,
        enforce_cap: bool,
        allow_cap_relax: bool,
        fill_empty: bool,
    ) -> dict[str, float]:
        if candidates.empty:
            return {}
        symbols_in_candidates = set(candidates["symbol"].astype(str))
        max_weight = float(self.config.max_single_name_side_weight)
        min_names = int(self.config.min_nonzero_names)
        if allow_cap_relax:
            max_weight = max(max_weight, 1.0 / max(1, min_names))
        cap = max(max_weight, self.eps)
        required_names = int(min_names)
        if enforce_cap:
            required_names = max(required_names, int(np.ceil(1.0 / cap - 1e-12)))

        target: dict[str, float] = {}
        for symbol, value in previous.items():
            symbol_text = str(symbol)
            if symbol_text not in symbols_in_candidates:
                continue
            weight = max(0.0, float(value))
            if weight <= self.eps:
                continue
            target[symbol_text] = weight
        for symbol, value in locked.items():
            symbol_text = str(symbol)
            if symbol_text not in symbols_in_candidates:
                continue
            weight = max(0.0, float(value))
            if weight <= self.eps:
                continue
            target[symbol_text] = max(float(target.get(symbol_text, 0.0)), weight)

        ranked = self._ranked_symbols_for_side(candidates, side=side)
        if not target:
            if not fill_empty:
                return {}
            if not ranked:
                return {}
            use_count = min(len(ranked), max(1, required_names))
            if not allow_cap_relax and float(use_count) * cap < 1.0 - 1e-8:
                return {}
            chosen = ranked[:use_count]
            if not chosen:
                return {}
            equal_weight = 1.0 / float(len(chosen))
            return {symbol: equal_weight for symbol in chosen}

        for symbol in ranked:
            locked_target = {
                str(item): max(0.0, float(value))
                for item, value in locked.items()
                if str(item) in target and float(value) > self.eps
            }
            locked_sum = float(sum(locked_target.values()))
            residual_capacity = sum(max(0.0, float(cap) - float(locked_target.get(str(item), 0.0))) for item in target)
            capacity_ok = locked_sum >= 1.0 - 1e-8 or float(residual_capacity) >= max(0.0, 1.0 - locked_sum) - 1e-8
            if len(target) >= required_names and (capacity_ok or not enforce_cap):
                break
            if symbol not in target:
                target[symbol] = 0.0

        target = self._scale_down_to_budget_preserving_locked(
            target=target,
            locked=locked,
            cap=cap,
            enforce_cap=enforce_cap,
        )
        return {symbol: float(value) for symbol, value in target.items() if float(value) > self.eps}

    def _ranked_symbols_for_side(self, candidates: pd.DataFrame, *, side: str) -> list[str]:
        ascending = [False, True] if side == "long" else [True, True]
        ranked = candidates.sort_values(["composite_score", "symbol"], ascending=ascending)
        return [str(symbol) for symbol in ranked["symbol"].astype(str).tolist()]

    def _scale_down_to_budget_preserving_locked(
        self,
        *,
        target: Mapping[str, float],
        locked: Mapping[str, float],
        cap: float,
        enforce_cap: bool,
    ) -> dict[str, float]:
        locked_target = {
            str(symbol): max(0.0, float(value))
            for symbol, value in locked.items()
            if str(symbol) in target and float(value) > self.eps
        }
        locked_sum = float(sum(locked_target.values()))
        if locked_sum >= 1.0:
            return self._normalize_weights(locked_target)
        remaining_budget = max(0.0, 1.0 - locked_sum)
        if not enforce_cap:
            return self._normalize_preserving_locked(target=target, locked=locked_target)

        symbols = [str(symbol) for symbol in target]
        capacities = {
            symbol: max(0.0, float(cap) - float(locked_target.get(symbol, 0.0)))
            for symbol in symbols
        }
        if float(sum(capacities.values())) < remaining_budget - 1e-8:
            return {}
        residual = {
            symbol: max(0.0, float(target.get(symbol, 0.0)) - float(locked_target.get(symbol, 0.0)))
            for symbol in symbols
        }
        active = {
            symbol: float(value)
            for symbol, value in residual.items()
            if float(value) > self.eps and capacities.get(symbol, 0.0) > self.eps
        }
        unused = [symbol for symbol in symbols if symbol not in active and capacities.get(symbol, 0.0) > self.eps]
        fixed: dict[str, float] = {}
        remaining = float(remaining_budget)
        while remaining > self.eps:
            if not active:
                if not unused:
                    return {}
                active = {symbol: 1.0 for symbol in unused}
                unused = []
            if remaining <= self.eps:
                break
            total = float(sum(active.values()))
            if total <= self.eps:
                scaled = {symbol: remaining / float(len(active)) for symbol in active}
            else:
                scaled = {symbol: float(value) / total * remaining for symbol, value in active.items()}
            over_cap = {
                symbol: value
                for symbol, value in scaled.items()
                if float(value) > float(capacities.get(symbol, 0.0)) + 1e-10
            }
            if not enforce_cap or not over_cap:
                fixed.update({symbol: float(value) for symbol, value in scaled.items() if float(value) > self.eps})
                remaining = 0.0
                break
            for symbol in over_cap:
                fixed[symbol] = float(capacities.get(symbol, 0.0))
                active.pop(symbol, None)
            remaining -= float(sum(fixed[symbol] for symbol in over_cap))
            if remaining < -1e-8:
                return {}
        if remaining > 1e-8:
            return {}
        out = dict(locked_target)
        for symbol, value in fixed.items():
            total_value = float(out.get(symbol, 0.0)) + float(value)
            if total_value > self.eps:
                out[symbol] = total_value
        return out

    def _normalize_preserving_locked(
        self,
        *,
        target: Mapping[str, float],
        locked: Mapping[str, float],
    ) -> dict[str, float]:
        locked_target = {
            str(symbol): max(0.0, float(value))
            for symbol, value in locked.items()
            if float(value) > self.eps
        }
        locked_sum = float(sum(locked_target.values()))
        if locked_sum >= 1.0:
            return self._normalize_weights(locked_target)
        flexible = {
            str(symbol): max(0.0, float(value) - float(locked_target.get(str(symbol), 0.0)))
            for symbol, value in target.items()
            if max(0.0, float(value) - float(locked_target.get(str(symbol), 0.0))) > self.eps
        }
        flexible_sum = float(sum(flexible.values()))
        if flexible_sum <= self.eps:
            return self._normalize_weights(locked_target)
        out = dict(locked_target)
        scale = max(0.0, 1.0 - locked_sum) / flexible_sum
        for symbol, value in flexible.items():
            scaled = float(value) * scale
            if scaled > self.eps:
                out[symbol] = float(out.get(symbol, 0.0)) + scaled
        return out

    def _normalize_weights(self, weights: Mapping[str, float]) -> dict[str, float]:
        cleaned = {
            str(symbol): max(0.0, float(value))
            for symbol, value in weights.items()
            if float(value) > self.eps
        }
        total = float(sum(cleaned.values()))
        if total <= self.eps:
            return {}
        return {symbol: float(value) / total for symbol, value in cleaned.items()}

    def _candidate_union_locked(
        self,
        group: pd.DataFrame,
        *,
        side: str,
        previous_weights: Mapping[str, float],
        locked_symbols: set[str],
        blocked_symbols: set[str],
        candidate_pool_per_side: int,
    ) -> pd.DataFrame:
        available = group[~group["symbol"].astype(str).isin(blocked_symbols)].copy()
        if side == "long":
            ranked = available.sort_values(["composite_score", "symbol"], ascending=[False, True])
        elif side == "short":
            ranked = available.sort_values(["composite_score", "symbol"], ascending=[True, True])
        else:
            raise ValueError(f"unknown_side:{side}")
        previous_symbols = set(previous_weights)
        locked = available[available["symbol"].astype(str).isin(locked_symbols)]
        incumbents = available[available["symbol"].astype(str).isin(previous_symbols)]
        top = ranked.head(int(candidate_pool_per_side))
        union = pd.concat([locked, incumbents, top], ignore_index=True).drop_duplicates("symbol", keep="first")
        return union.reset_index(drop=True)

    def _optimize_joint_weights_locked(
        self,
        *,
        longs: pd.DataFrame,
        shorts: pd.DataFrame,
        max_weight: float,
        score_weight: float,
        sector_penalty: float,
        turnover_penalty: float,
        previous_long_weights: Mapping[str, float],
        previous_short_weights: Mapping[str, float],
        locked_long_weights: Mapping[str, float],
        locked_short_weights: Mapping[str, float],
        turnover_budget: float,
        deploy_gap: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        n_long = len(longs)
        n_short = len(shorts)
        n = n_long + n_short

        long_lower = self._sanitize_locked_lower(
            np.array([float(locked_long_weights.get(symbol, 0.0)) for symbol in longs["symbol"]], dtype=float),
            max_weight=max_weight,
        )
        short_lower = self._sanitize_locked_lower(
            np.array([float(locked_short_weights.get(symbol, 0.0)) for symbol in shorts["symbol"]], dtype=float),
            max_weight=max_weight,
        )
        lower = np.concatenate([long_lower, short_lower])

        if float(long_lower.sum()) > 1.0 + 1e-8 or float(short_lower.sum()) > 1.0 + 1e-8:
            raise ValueError("locked_weight_exceeds_side_budget")
        if np.any(lower > max_weight + 1e-8):
            raise ValueError("locked_weight_exceeds_name_cap")
        if n_long * max_weight < 1.0 - 1e-12 or n_short * max_weight < 1.0 - 1e-12:
            raise ValueError("insufficient_weight_capacity")

        long_score = self._zscore_array(longs["composite_score"].to_numpy(dtype=float))
        short_score = self._zscore_array((-shorts["composite_score"]).to_numpy(dtype=float))
        score = np.concatenate([long_score, short_score])
        prev = np.concatenate(
            [
                np.array([float(previous_long_weights.get(symbol, 0.0)) for symbol in longs["symbol"]], dtype=float),
                np.array([float(previous_short_weights.get(symbol, 0.0)) for symbol in shorts["symbol"]], dtype=float),
            ]
        )

        aeq = []
        beq = []
        row = np.zeros(n)
        row[:n_long] = 1.0
        aeq.append(row)
        beq.append(1.0)
        row = np.zeros(n)
        row[n_long:] = 1.0
        aeq.append(row)
        beq.append(1.0)
        row = np.zeros(n)
        row[:n_long] = longs["beta"].to_numpy(dtype=float)
        row[n_long:] = -shorts["beta"].to_numpy(dtype=float)
        aeq.append(row)
        beq.append(0.0)
        a_eq = np.vstack(aeq)
        b_eq = np.array(beq, dtype=float)

        sector_matrix = self._group_exposure_matrix(longs, shorts, "sic2_sector")
        m_sector = sector_matrix.shape[0]

        c = np.concatenate(
            [
                -score_weight * score,
                np.full(n, turnover_penalty, dtype=float),
                np.full(m_sector, sector_penalty, dtype=float),
            ]
        )
        bounds: list[tuple[float, float | None]] = (
            [(float(lo), max_weight) for lo in lower] + [(0.0, None)] * n + [(0.0, None)] * m_sector
        )

        a_eq = np.column_stack([a_eq, np.zeros((a_eq.shape[0], n + m_sector))])
        turnover_top = np.column_stack([np.eye(n), -np.eye(n), np.zeros((n, m_sector))])
        turnover_bottom = np.column_stack([-np.eye(n), -np.eye(n), np.zeros((n, m_sector))])
        turnover_cap = np.concatenate([np.zeros(n), np.ones(n), np.zeros(m_sector)])[None, :]
        sector_top = np.column_stack([sector_matrix, np.zeros((m_sector, n)), -np.eye(m_sector)])
        sector_bottom = np.column_stack([-sector_matrix, np.zeros((m_sector, n)), -np.eye(m_sector)])

        turnover_limit = max(0.0, float(turnover_budget) + float(deploy_gap))

        result = linprog(
            c,
            A_ub=np.vstack([turnover_top, turnover_bottom, turnover_cap, sector_top, sector_bottom]),
            b_ub=np.concatenate([prev, -prev, [turnover_limit], np.zeros(m_sector), np.zeros(m_sector)]),
            A_eq=a_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
        if not result.success:
            raise ValueError(str(result.message).replace(" ", "_"))
        x = np.clip(result.x[:n], lower, max_weight)
        long_weights = x[:n_long]
        short_weights = x[n_long:]
        if abs(float(long_weights.sum()) - 1.0) > 1e-6 or abs(float(short_weights.sum()) - 1.0) > 1e-6:
            raise ValueError("side_sum_constraint_breach")
        net_beta = float(np.dot(long_weights, longs["beta"]) - np.dot(short_weights, shorts["beta"]))
        if abs(net_beta) > 1e-5:
            raise ValueError("beta_constraint_breach")
        return long_weights, short_weights

    def _try_relaxed_beta_fallback(
        self,
        *,
        longs: pd.DataFrame,
        shorts: pd.DataFrame,
        max_weight: float,
        score_weight: float,
        sector_penalty: float,
        turnover_penalty: float,
        previous_long_weights: Mapping[str, float],
        previous_short_weights: Mapping[str, float],
        locked_long_weights: Mapping[str, float],
        locked_short_weights: Mapping[str, float],
        turnover_budget: float,
        deploy_gap: float,
        beta_band_grid: Sequence[float],
    ) -> tuple[np.ndarray, np.ndarray, float] | None:
        for beta_band in beta_band_grid:
            beta_band_value = float(beta_band)
            if not np.isfinite(beta_band_value) or beta_band_value <= 0.0:
                continue
            try:
                long_weights, short_weights = self._optimize_joint_weights_locked_relaxed_beta(
                    longs=longs,
                    shorts=shorts,
                    max_weight=max_weight,
                    score_weight=score_weight,
                    sector_penalty=sector_penalty,
                    turnover_penalty=turnover_penalty,
                    previous_long_weights=previous_long_weights,
                    previous_short_weights=previous_short_weights,
                    locked_long_weights=locked_long_weights,
                    locked_short_weights=locked_short_weights,
                    turnover_budget=turnover_budget,
                    deploy_gap=deploy_gap,
                    beta_band=beta_band_value,
                )
                return long_weights, short_weights, beta_band_value
            except ValueError:
                continue
        return None

    def _optimize_joint_weights_locked_relaxed_beta(
        self,
        *,
        longs: pd.DataFrame,
        shorts: pd.DataFrame,
        max_weight: float,
        score_weight: float,
        sector_penalty: float,
        turnover_penalty: float,
        previous_long_weights: Mapping[str, float],
        previous_short_weights: Mapping[str, float],
        locked_long_weights: Mapping[str, float],
        locked_short_weights: Mapping[str, float],
        turnover_budget: float,
        deploy_gap: float,
        beta_band: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        n_long = len(longs)
        n_short = len(shorts)
        n = n_long + n_short
        long_lower = self._sanitize_locked_lower(
            np.array([float(locked_long_weights.get(symbol, 0.0)) for symbol in longs["symbol"]], dtype=float),
            max_weight=max_weight,
        )
        short_lower = self._sanitize_locked_lower(
            np.array([float(locked_short_weights.get(symbol, 0.0)) for symbol in shorts["symbol"]], dtype=float),
            max_weight=max_weight,
        )
        lower = np.concatenate([long_lower, short_lower])
        if float(long_lower.sum()) > 1.0 + 1e-8 or float(short_lower.sum()) > 1.0 + 1e-8:
            raise ValueError("locked_weight_exceeds_side_budget")
        if np.any(lower > max_weight + 1e-8):
            raise ValueError("locked_weight_exceeds_name_cap")
        if n_long * max_weight < 1.0 - 1e-12 or n_short * max_weight < 1.0 - 1e-12:
            raise ValueError("insufficient_weight_capacity")

        long_score = self._zscore_array(longs["composite_score"].to_numpy(dtype=float))
        short_score = self._zscore_array((-shorts["composite_score"]).to_numpy(dtype=float))
        score = np.concatenate([long_score, short_score])
        prev = np.concatenate(
            [
                np.array([float(previous_long_weights.get(symbol, 0.0)) for symbol in longs["symbol"]], dtype=float),
                np.array([float(previous_short_weights.get(symbol, 0.0)) for symbol in shorts["symbol"]], dtype=float),
            ]
        )

        aeq = []
        beq = []
        row = np.zeros(n)
        row[:n_long] = 1.0
        aeq.append(row)
        beq.append(1.0)
        row = np.zeros(n)
        row[n_long:] = 1.0
        aeq.append(row)
        beq.append(1.0)
        a_eq = np.vstack(aeq)
        b_eq = np.array(beq, dtype=float)

        beta_row = np.zeros(n)
        beta_row[:n_long] = longs["beta"].to_numpy(dtype=float)
        beta_row[n_long:] = -shorts["beta"].to_numpy(dtype=float)

        sector_matrix = self._group_exposure_matrix(longs, shorts, "sic2_sector")
        m_sector = sector_matrix.shape[0]
        c = np.concatenate(
            [
                -score_weight * score,
                np.full(n, turnover_penalty, dtype=float),
                np.full(m_sector, sector_penalty, dtype=float),
            ]
        )
        bounds: list[tuple[float, float | None]] = (
            [(float(lo), max_weight) for lo in lower] + [(0.0, None)] * n + [(0.0, None)] * m_sector
        )
        a_eq = np.column_stack([a_eq, np.zeros((a_eq.shape[0], n + m_sector))])
        turnover_top = np.column_stack([np.eye(n), -np.eye(n), np.zeros((n, m_sector))])
        turnover_bottom = np.column_stack([-np.eye(n), -np.eye(n), np.zeros((n, m_sector))])
        turnover_cap = np.concatenate([np.zeros(n), np.ones(n), np.zeros(m_sector)])[None, :]
        sector_top = np.column_stack([sector_matrix, np.zeros((m_sector, n)), -np.eye(m_sector)])
        sector_bottom = np.column_stack([-sector_matrix, np.zeros((m_sector, n)), -np.eye(m_sector)])
        beta_upper = np.concatenate([beta_row, np.zeros(n + m_sector)])[None, :]
        beta_lower = np.concatenate([-beta_row, np.zeros(n + m_sector)])[None, :]

        turnover_limit = max(0.0, float(turnover_budget) + float(deploy_gap))

        result = linprog(
            c,
            A_ub=np.vstack(
                [
                    turnover_top,
                    turnover_bottom,
                    turnover_cap,
                    sector_top,
                    sector_bottom,
                    beta_upper,
                    beta_lower,
                ]
            ),
            b_ub=np.concatenate(
                [
                    prev,
                    -prev,
                    [turnover_limit],
                    np.zeros(m_sector),
                    np.zeros(m_sector),
                    [float(beta_band)],
                    [float(beta_band)],
                ]
            ),
            A_eq=a_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
        if not result.success:
            raise ValueError(str(result.message).replace(" ", "_"))
        x = np.clip(result.x[:n], lower, max_weight)
        long_weights = x[:n_long]
        short_weights = x[n_long:]
        if abs(float(long_weights.sum()) - 1.0) > 1e-6 or abs(float(short_weights.sum()) - 1.0) > 1e-6:
            raise ValueError("side_sum_constraint_breach")
        net_beta = float(np.dot(long_weights, longs["beta"]) - np.dot(short_weights, shorts["beta"]))
        if abs(net_beta) > float(beta_band) + 1e-5:
            raise ValueError("beta_band_constraint_breach")
        return long_weights, short_weights

    def _sanitize_locked_lower(self, values: np.ndarray, *, max_weight: float) -> np.ndarray:
        lower = np.asarray(values, dtype=float).copy()
        lower[np.abs(lower) <= self.eps] = 0.0
        if np.any(lower > max_weight) and float(lower.max()) <= max_weight + 1e-7:
            lower = np.minimum(lower, max_weight)
        total = float(lower.sum())
        if total > 1.0 and total <= 1.0 + 1e-7:
            lower *= (1.0 - 1e-9) / total
        return lower

    def _group_exposure_matrix(self, longs: pd.DataFrame, shorts: pd.DataFrame, group_column: str) -> np.ndarray:
        groups = sorted(set(longs[group_column].astype(str)).union(set(shorts[group_column].astype(str))))
        n_long = len(longs)
        n_short = len(shorts)
        matrix = np.zeros((len(groups), n_long + n_short), dtype=float)
        long_values = longs[group_column].astype(str).to_numpy()
        short_values = shorts[group_column].astype(str).to_numpy()
        for i, group in enumerate(groups):
            matrix[i, :n_long] = (long_values == group).astype(float)
            matrix[i, n_long:] = -((short_values == group).astype(float))
        return matrix

    def _position_rows(
        self,
        candidates: pd.DataFrame,
        weights: np.ndarray,
        *,
        session_date: str,
        session_idx: int,
        side: str,
    ) -> list[dict[str, Any]]:
        sign = 1.0 if side == "long" else -1.0
        rows: list[dict[str, Any]] = []
        for row, weight in zip(candidates.itertuples(index=False), weights):
            weight = float(weight)
            if weight <= self.eps:
                continue
            item = {
                "session_date": str(session_date),
                "session_idx": int(session_idx),
                "side": side,
                "symbol": str(row.symbol),
                "side_weight": weight,
                "signed_weight": float(sign * weight),
                "beta": float(row.beta),
                "sic2_sector": str(row.sic2_sector),
                "sic4_industry": str(row.sic4_industry),
                "composite_score": float(row.composite_score),
            }
            for column in FACTOR_COLUMNS:
                item[column] = float(getattr(row, column))
            rows.append(item)
        return rows

    def _weights_frame(self, base: pd.DataFrame, weights: Mapping[str, float]) -> pd.DataFrame:
        frame = base[base["symbol"].astype(str).isin(set(weights))].copy()
        frame["target_weight"] = frame["symbol"].astype(str).map(lambda symbol: float(weights.get(symbol, 0.0)))
        return frame[frame["target_weight"].gt(self.eps)].sort_values("symbol").reset_index(drop=True)

    def _target_dict(self, symbols: pd.Series, weights: np.ndarray) -> dict[str, float]:
        return {
            str(symbol): float(weight)
            for symbol, weight in zip(symbols.astype(str), weights)
            if float(weight) > self.eps
        }

    def _side_turnover(self, previous: Mapping[str, float], symbols: pd.Series, weights: np.ndarray) -> float:
        new = self._target_dict(symbols, weights)
        universe = set(previous) | set(new)
        return float(sum(abs(float(new.get(symbol, 0.0)) - float(previous.get(symbol, 0.0))) for symbol in universe))

    def _deploy_gap_from_previous(self, previous_weights: Mapping[str, Mapping[str, float]]) -> float:
        long_sum = float(sum(float(value) for value in previous_weights.get("long", {}).values()))
        short_sum = float(sum(float(value) for value in previous_weights.get("short", {}).values()))
        long_gap = max(0.0, 1.0 - long_sum)
        short_gap = max(0.0, 1.0 - short_sum)
        return float(long_gap + short_gap)

    @staticmethod
    def _zscore_array(values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=float)
        mean = float(np.nanmean(values))
        std = float(np.nanstd(values))
        if not np.isfinite(std) or std <= 1e-12:
            return np.zeros_like(values)
        return np.clip((values - mean) / std, -3.0, 3.0)

    @staticmethod
    def _is_empty_book(previous_weights: Mapping[str, Mapping[str, float]]) -> bool:
        return not previous_weights["long"] and not previous_weights["short"]

    def _validate_config(self) -> None:
        cfg = self.config
        if int(cfg.candidate_pool_per_side) <= 0:
            raise ValueError("candidate_pool_per_side must be positive")
        if int(cfg.min_nonzero_names) <= 0:
            raise ValueError("min_nonzero_names must be positive")
        if not np.isfinite(float(cfg.max_single_name_side_weight)) or float(cfg.max_single_name_side_weight) <= 0:
            raise ValueError("max_single_name_side_weight must be positive")
        if not np.isfinite(float(cfg.turnover_budget)) or float(cfg.turnover_budget) <= 0:
            raise ValueError("turnover_budget must be positive")
        if not np.isfinite(float(cfg.turnover_penalty)) or float(cfg.turnover_penalty) < 0:
            raise ValueError("turnover_penalty must be non-negative")
        if not np.isfinite(float(cfg.sector_penalty)) or float(cfg.sector_penalty) < 0:
            raise ValueError("sector_penalty must be non-negative")
        if not np.isfinite(float(cfg.score_weight)) or float(cfg.score_weight) <= 0:
            raise ValueError("score_weight must be positive")
        if not cfg.factor_weights:
            raise ValueError("factor_weights cannot be empty")


def run_live_decision(
    *,
    alpha_csv_path: str | Path,
    ledger_path: str | Path,
    session_date: str = "latest",
    session_idx: int | None = None,
    output_root: str | Path = DEFAULT_DECISION_ROOT,
    account_equity: float = 1_000_000.0,
    config: DecisionConfig | None = None,
) -> dict[str, Any]:
    if not np.isfinite(account_equity) or float(account_equity) <= 0:
        raise ValueError("account_equity must be positive")

    alpha_path = Path(alpha_csv_path)
    ledger_path_obj = Path(ledger_path)
    output_root_obj = Path(output_root)
    output_root_obj.mkdir(parents=True, exist_ok=True)
    ledger_path_obj.parent.mkdir(parents=True, exist_ok=True)

    alpha_frame, selected_session_date = _load_alpha_frame(alpha_path, decision_date=session_date)
    lot_manager = LotManager.from_json(ledger_path_obj)
    resolved_session_idx = _resolve_session_idx(lot_manager, session_idx)

    engine = DecisionEngine(config or DecisionConfig())
    before_snapshot = lot_manager.snapshot(session_idx=resolved_session_idx)
    before_positions = lot_manager.position_frame(session_idx=resolved_session_idx)

    result = engine.decide(
        alpha_frame=alpha_frame,
        lot_manager=lot_manager,
        session_idx=resolved_session_idx,
        session_date=selected_session_date,
    )

    targets = result.targets.copy()
    if not targets.empty:
        targets["target_notional"] = pd.to_numeric(targets["signed_weight"], errors="coerce") * float(account_equity)
        targets["abs_signed_weight"] = pd.to_numeric(targets["signed_weight"], errors="coerce").abs()
        targets = targets.sort_values(["abs_signed_weight", "symbol"], ascending=[False, True]).reset_index(drop=True)

    after_snapshot = lot_manager.snapshot(session_idx=resolved_session_idx)
    after_positions = lot_manager.position_frame(session_idx=resolved_session_idx)

    lot_manager.meta.update(
        {
            "last_session_idx": int(resolved_session_idx),
            "last_session_date": str(selected_session_date),
        }
    )
    lot_manager.to_json(ledger_path_obj)

    token = f"{selected_session_date.replace('-', '')}_idx{int(resolved_session_idx):05d}"
    target_path = output_root_obj / f"target_{token}.csv"
    before_path = output_root_obj / f"positions_before_{token}.csv"
    after_path = output_root_obj / f"positions_after_{token}.csv"
    summary_path = output_root_obj / f"decision_summary_{token}.json"

    targets.to_csv(target_path, index=False)
    before_positions.to_csv(before_path, index=False)
    after_positions.to_csv(after_path, index=False)

    summary = {
        "created_at_utc": _utc_now(),
        "status": result.status,
        "session_date": str(selected_session_date),
        "session_idx": int(resolved_session_idx),
        "skip_reason": result.skip_reason,
        "diagnostics": result.diagnostics,
        "account_equity": float(account_equity),
        "before_snapshot": before_snapshot,
        "after_snapshot": after_snapshot,
        "target_names": int(len(targets)),
        "outputs": {
            "target_csv": target_path.as_posix(),
            "positions_before_csv": before_path.as_posix(),
            "positions_after_csv": after_path.as_posix(),
            "summary_json": summary_path.as_posix(),
            "ledger_json": ledger_path_obj.as_posix(),
        },
        "inputs": {
            "alpha_csv_path": alpha_path.as_posix(),
            "requested_session_date": str(session_date),
            "selected_session_date": str(selected_session_date),
            "turnover_budget": float((config or DecisionConfig()).turnover_budget),
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _load_alpha_frame(path: Path, *, decision_date: str) -> tuple[pd.DataFrame, str]:
    if not path.exists():
        raise FileNotFoundError(f"alpha csv not found: {path.as_posix()}")
    frame = pd.read_csv(path)

    if "session_date" not in frame.columns:
        inferred = _infer_date_from_filename(path.name)
        if inferred is None:
            inferred = datetime.now().date().isoformat()
        selected_date = inferred if decision_date.strip().lower() in {"latest", "", "last", "max"} else decision_date
        frame = frame.copy()
        frame["session_date"] = selected_date
        return frame, selected_date

    frame = frame.copy()
    frame["session_date"] = pd.to_datetime(frame["session_date"], errors="coerce")
    frame = frame.dropna(subset=["session_date"])
    if frame.empty:
        raise ValueError("alpha csv has no valid session_date rows")
    available = frame["session_date"].drop_duplicates().sort_values().reset_index(drop=True)

    token = decision_date.strip().lower()
    if token in {"latest", "", "last", "max"}:
        selected = pd.Timestamp(available.iloc[-1])
    else:
        requested = pd.Timestamp(decision_date)
        prior = available[available.le(requested)]
        if prior.empty:
            raise ValueError(f"decision_date={decision_date} earlier than first alpha session")
        selected = pd.Timestamp(prior.iloc[-1])

    out = frame[frame["session_date"].eq(selected)].copy()
    if out.empty:
        raise ValueError(f"no alpha rows on selected session: {selected.date().isoformat()}")
    out["session_date"] = out["session_date"].dt.date.astype(str)
    return out, selected.date().isoformat()


def _resolve_session_idx(lot_manager: LotManager, provided: int | None) -> int:
    if provided is not None:
        return int(provided)
    if "last_session_idx" in lot_manager.meta:
        return int(lot_manager.meta["last_session_idx"]) + 1
    return int(lot_manager.max_birth_idx()) + 1


def _infer_date_from_filename(name: str) -> str | None:
    match = re.search(r"(20\d{6})", name)
    if not match:
        return None
    token = match.group(1)
    return f"{token[:4]}-{token[4:6]}-{token[6:]}"


def _parse_float_mapping(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    token = str(text).strip()
    if not token:
        return out
    for piece in token.split(","):
        item = piece.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"invalid mapping token: {item}")
        key, value = item.split(":", 1)
        out[key.strip()] = float(value.strip())
    return out


def _parse_float_list(text: str) -> list[float]:
    token = str(text).strip()
    if not token:
        return []
    return [float(piece.strip()) for piece in token.split(",") if piece.strip()]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one standalone decision from alpha_core output CSV.")
    parser.add_argument("--alpha-csv-path", default=None)
    parser.add_argument("--ledger-path", default=str(DEFAULT_DECISION_ROOT / "live_ledger.json"))
    parser.add_argument("--session-date", default="latest")
    parser.add_argument("--session-idx", type=int, default=None)
    parser.add_argument("--output-root", default=str(DEFAULT_DECISION_ROOT))
    parser.add_argument("--account-equity", type=float, default=1_000_000.0)

    parser.add_argument("--candidate-pool-per-side", type=int, default=120)
    parser.add_argument("--max-single-name-side-weight", type=float, default=1.0 / 30.0)
    parser.add_argument("--min-nonzero-names", type=int, default=20)
    parser.add_argument("--score-weight", type=float, default=0.01)
    parser.add_argument("--sector-penalty", type=float, default=25.0)
    parser.add_argument("--turnover-penalty", type=float, default=0.005)
    parser.add_argument("--turnover-budget", type=float, default=0.15)
    parser.add_argument("--beta-band-grid", default="0.05,0.10,0.15,0.20")
    parser.add_argument(
        "--factor-weights",
        default="",
        help="e.g. reversal_score:0.25,momentum_score:0.10,small_size_score:0.30,low_beta_score:0.20,cash_quality_score:0.15",
    )
    parser.add_argument(
        "--factor-min-holds",
        default="",
        help="e.g. reversal_score:5,momentum_score:10,small_size_score:20,low_beta_score:20,cash_quality_score:20",
    )
    args = parser.parse_args(argv)

    if args.alpha_csv_path:
        alpha_csv_path = Path(args.alpha_csv_path)
    else:
        token = datetime.now().strftime("%Y%m%d")
        alpha_csv_path = DEFAULT_ALPHA_ROOT / f"alpha_core_panel_{token}.csv"

    factor_weights = dict(DEFAULT_FACTOR_WEIGHTS)
    factor_weights.update(_parse_float_mapping(args.factor_weights))
    factor_min_holds = dict(DEFAULT_FACTOR_MIN_HOLDS)
    factor_min_holds.update({k: int(v) for k, v in _parse_float_mapping(args.factor_min_holds).items()})

    config = DecisionConfig(
        factor_weights=factor_weights,
        factor_min_holds=factor_min_holds,
        candidate_pool_per_side=int(args.candidate_pool_per_side),
        max_single_name_side_weight=float(args.max_single_name_side_weight),
        min_nonzero_names=int(args.min_nonzero_names),
        score_weight=float(args.score_weight),
        sector_penalty=float(args.sector_penalty),
        turnover_penalty=float(args.turnover_penalty),
        turnover_budget=float(args.turnover_budget),
        beta_band_grid=tuple(_parse_float_list(args.beta_band_grid)),
    )

    summary = run_live_decision(
        alpha_csv_path=alpha_csv_path,
        ledger_path=Path(args.ledger_path),
        session_date=str(args.session_date),
        session_idx=args.session_idx,
        output_root=Path(args.output_root),
        account_equity=float(args.account_equity),
        config=config,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

