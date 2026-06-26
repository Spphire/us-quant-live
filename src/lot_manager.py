from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

EPS = 1e-10

FACTOR_COLUMNS = (
    "reversal_score",
    "momentum_score",
    "small_size_score",
    "low_beta_score",
    "cash_quality_score",
)

DEFAULT_FACTOR_MIN_HOLDS = {
    "reversal_score": 5,
    "momentum_score": 10,
    "small_size_score": 20,
    "low_beta_score": 20,
    "cash_quality_score": 20,
}


class LotManager:
    """Locked-lot ledger manager compatible with Phase7K mechanics."""

    def __init__(
        self,
        ledger: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
        eps: float = EPS,
    ) -> None:
        self.eps = float(eps)
        self.meta: dict[str, Any] = dict(meta or {})
        self.ledger: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
        source = ledger or {"long": [], "short": []}
        for side in ("long", "short"):
            self.ledger[side] = [
                lot
                for lot in (self._normalize_lot(raw) for raw in source.get(side, []))
                if lot is not None
            ]

    @classmethod
    def from_json(cls, path: str | Path, *, eps: float = EPS) -> "LotManager":
        p = Path(path)
        if not p.exists():
            return cls(eps=eps)
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "ledger" in payload:
            ledger = payload.get("ledger")
            meta = payload.get("meta")
        else:
            ledger = payload
            meta = {}
        if not isinstance(ledger, Mapping):
            raise ValueError("ledger payload must be a mapping with long/short keys")
        return cls(ledger=ledger, meta=meta if isinstance(meta, Mapping) else {}, eps=eps)

    def to_json(self, path: str | Path, *, extra_meta: Mapping[str, Any] | None = None) -> None:
        meta = dict(self.meta)
        meta.update(dict(extra_meta or {}))
        payload = {
            "schema_version": "1.0",
            "updated_at_utc": _utc_now(),
            "meta": meta,
            "ledger": self.ledger,
        }
        Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def clone(self) -> "LotManager":
        return LotManager(ledger=self.ledger, meta=self.meta, eps=self.eps)

    def max_birth_idx(self) -> int:
        values = [
            int(lot["birth_idx"])
            for side in ("long", "short")
            for lot in self.ledger[side]
            if "birth_idx" in lot
        ]
        return max(values) if values else -1

    def previous_weights(self) -> dict[str, dict[str, float]]:
        return {side: self._ledger_weights(self.ledger[side]) for side in ("long", "short")}

    def locked_weights(self, session_idx: int) -> dict[str, dict[str, float]]:
        return {
            side: self._locked_weights(self.ledger[side], session_idx)
            for side in ("long", "short")
        }

    def locked_symbols(self, session_idx: int) -> dict[str, set[str]]:
        return {side: set(weights.keys()) for side, weights in self.locked_weights(session_idx).items()}

    def prune(self, available_symbols: set[str], session_idx: int) -> dict[str, float | int]:
        available = {str(symbol).upper() for symbol in available_symbols}
        pruned: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
        dropped_lots = 0
        dropped_weight = 0.0
        dropped_locked_weight = 0.0
        for side in ("long", "short"):
            for lot in self.ledger[side]:
                weight = float(lot["weight"])
                if str(lot["symbol"]).upper() in available:
                    pruned[side].append(dict(lot))
                else:
                    dropped_lots += 1
                    dropped_weight += weight
                    if self._is_lot_locked(lot, session_idx):
                        dropped_locked_weight += weight
        self.ledger = pruned
        return {
            "dropped_lots": int(dropped_lots),
            "dropped_weight": float(dropped_weight),
            "dropped_locked_weight": float(dropped_locked_weight),
        }

    def position_frame(self, session_idx: int | None = None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for side in ("long", "short"):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for lot in self.ledger[side]:
                grouped.setdefault(str(lot["symbol"]), []).append(lot)
            for symbol, lots in sorted(grouped.items()):
                side_weight = float(sum(float(lot["weight"]) for lot in lots))
                if side_weight <= self.eps:
                    continue
                locked_weight = 0.0
                locked_lot_count = 0
                if session_idx is not None:
                    for lot in lots:
                        if self._is_lot_locked(lot, int(session_idx)):
                            locked_lot_count += 1
                            locked_weight += float(lot["weight"])
                sign = 1.0 if side == "long" else -1.0
                rows.append(
                    {
                        "side": side,
                        "symbol": symbol,
                        "side_weight": float(side_weight),
                        "signed_weight": float(sign * side_weight),
                        "lot_count": int(len(lots)),
                        "locked_lot_count": int(locked_lot_count),
                        "locked_weight": float(locked_weight),
                        "expired_weight": float(max(0.0, side_weight - locked_weight)),
                    }
                )
        if not rows:
            return pd.DataFrame(
                columns=[
                    "side",
                    "symbol",
                    "side_weight",
                    "signed_weight",
                    "lot_count",
                    "locked_lot_count",
                    "locked_weight",
                    "expired_weight",
                ]
            )
        out = pd.DataFrame(rows)
        out["abs_signed_weight"] = out["signed_weight"].abs()
        return out.sort_values(["abs_signed_weight", "symbol"], ascending=[False, True]).reset_index(drop=True)

    def snapshot(self, session_idx: int | None = None) -> dict[str, Any]:
        previous = self.previous_weights()
        long_sum = float(sum(previous["long"].values()))
        short_sum = float(sum(previous["short"].values()))
        locked_total = 0.0
        locked_lots = 0
        if session_idx is not None:
            locked = self.locked_weights(session_idx)
            locked_total = float(sum(locked["long"].values()) + sum(locked["short"].values()))
            locked_lots = int(
                sum(
                    1
                    for side in ("long", "short")
                    for lot in self.ledger[side]
                    if self._is_lot_locked(lot, int(session_idx))
                )
            )
        return {
            "lots": int(len(self.ledger["long"]) + len(self.ledger["short"])),
            "long_names": int(len(previous["long"])),
            "short_names": int(len(previous["short"])),
            "long_weight_sum": long_sum,
            "short_weight_sum": short_sum,
            "gross_exposure": float(long_sum + short_sum),
            "net_exposure": float(long_sum - short_sum),
            "locked_total_weight": float(locked_total),
            "locked_lot_count": int(locked_lots),
            "meta": dict(self.meta),
        }

    def update_for_targets(
        self,
        *,
        target_weights: Mapping[str, Mapping[str, float]],
        base: pd.DataFrame,
        session_idx: int,
        session_date: str | None = None,
        factor_weights: Mapping[str, float],
        factor_min_holds: Mapping[str, int] | None = None,
        created_at_utc: str | None = None,
    ) -> None:
        holds = dict(DEFAULT_FACTOR_MIN_HOLDS)
        holds.update({k: int(v) for k, v in dict(factor_min_holds or {}).items()})
        entry_time_utc = str(created_at_utc).strip() if created_at_utc else _utc_now()
        entry_session_date = str(session_date).strip() if session_date is not None else None
        self.ledger["long"] = self._update_side_ledger(
            previous_lots=self.ledger["long"],
            target_weights=target_weights.get("long", {}),
            session_idx=session_idx,
            side="long",
            base=base,
            factor_weights=factor_weights,
            factor_min_holds=holds,
            entry_session_date=entry_session_date,
            entry_time_utc=entry_time_utc,
        )
        self.ledger["short"] = self._update_side_ledger(
            previous_lots=self.ledger["short"],
            target_weights=target_weights.get("short", {}),
            session_idx=session_idx,
            side="short",
            base=base,
            factor_weights=factor_weights,
            factor_min_holds=holds,
            entry_session_date=entry_session_date,
            entry_time_utc=entry_time_utc,
        )

    def _update_side_ledger(
        self,
        *,
        previous_lots: Sequence[Mapping[str, Any]],
        target_weights: Mapping[str, float],
        session_idx: int,
        side: str,
        base: pd.DataFrame,
        factor_weights: Mapping[str, float],
        factor_min_holds: Mapping[str, int],
        entry_session_date: str | None,
        entry_time_utc: str,
    ) -> list[dict[str, Any]]:
        rows = {str(row.symbol): row for row in base.itertuples(index=False)}
        lots_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for lot in previous_lots:
            lots_by_symbol.setdefault(str(lot["symbol"]), []).append(dict(lot))

        updated: list[dict[str, Any]] = []
        for symbol, target in sorted(target_weights.items()):
            remaining = float(target)
            if remaining <= self.eps:
                continue
            existing = lots_by_symbol.get(str(symbol), [])
            locked = [lot for lot in existing if self._is_lot_locked(lot, session_idx)]
            expired = [lot for lot in existing if not self._is_lot_locked(lot, session_idx)]

            for lot in locked:
                weight = float(lot["weight"])
                if weight <= self.eps:
                    continue
                updated.append(self._copy_lot(lot, weight))
                remaining -= weight

            for lot in expired:
                if remaining <= self.eps:
                    break
                keep = min(float(lot["weight"]), remaining)
                if keep > self.eps:
                    updated.append(self._copy_lot(lot, keep))
                    remaining -= keep

            if remaining > self.eps and symbol in rows:
                shares = self._factor_support_shares(rows[symbol], side=side, factor_weights=factor_weights)
                for factor, share in shares.items():
                    weight = float(remaining) * float(share)
                    if weight <= self.eps:
                        continue
                    updated.append(
                        {
                            "symbol": str(symbol),
                            "factor": str(factor),
                            "weight": float(weight),
                            "birth_idx": int(session_idx),
                            "min_hold": int(factor_min_holds.get(str(factor), 10)),
                            "entry_session_date": entry_session_date,
                            "entry_time_utc": str(entry_time_utc),
                        }
                    )
        return [lot for lot in updated if float(lot["weight"]) > self.eps]

    def _factor_support_shares(
        self,
        row: Any,
        *,
        side: str,
        factor_weights: Mapping[str, float],
    ) -> dict[str, float]:
        supports: dict[str, float] = {}
        for factor in FACTOR_COLUMNS:
            factor_weight = float(factor_weights.get(factor, 0.0))
            if factor_weight <= 0.0:
                continue
            score = float(getattr(row, factor))
            directional_score = score if side == "long" else -score
            support = max(0.0, factor_weight * directional_score)
            if support > 0.0:
                supports[factor] = support
        total = float(sum(supports.values()))
        if total <= self.eps:
            supports = {
                str(factor): float(weight)
                for factor, weight in factor_weights.items()
                if float(weight) > 0.0
            }
            total = float(sum(supports.values()))
        if total <= self.eps:
            return {}
        return {factor: float(value) / total for factor, value in supports.items()}

    def _normalize_lot(self, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        symbol = str(raw.get("symbol", "")).strip().upper()
        factor = str(raw.get("factor", "")).strip()
        if not symbol:
            return None
        weight = float(raw.get("weight", 0.0))
        if not np.isfinite(weight) or weight <= self.eps:
            return None
        birth_idx = int(raw.get("birth_idx", 0))
        min_hold = int(raw.get("min_hold", 0))
        entry_session_date_raw = raw.get("entry_session_date")
        if entry_session_date_raw in (None, "") and raw.get("birth_session_date") not in (None, ""):
            entry_session_date_raw = raw.get("birth_session_date")
        entry_time_utc_raw = raw.get("entry_time_utc")
        if entry_time_utc_raw in (None, "") and raw.get("opened_at_utc") not in (None, ""):
            entry_time_utc_raw = raw.get("opened_at_utc")
        entry_session_date = None if entry_session_date_raw in (None, "") else str(entry_session_date_raw)
        entry_time_utc = None if entry_time_utc_raw in (None, "") else str(entry_time_utc_raw)
        return {
            "symbol": symbol,
            "factor": factor,
            "weight": float(weight),
            "birth_idx": int(birth_idx),
            "min_hold": int(min_hold),
            "entry_session_date": entry_session_date,
            "entry_time_utc": entry_time_utc,
        }

    def _ledger_weights(self, lots: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        weights: dict[str, float] = {}
        for lot in lots:
            symbol = str(lot["symbol"]).upper()
            weights[symbol] = weights.get(symbol, 0.0) + float(lot["weight"])
        return {symbol: float(weight) for symbol, weight in weights.items() if weight > self.eps}

    def _locked_weights(self, lots: Sequence[Mapping[str, Any]], session_idx: int) -> dict[str, float]:
        weights: dict[str, float] = {}
        for lot in lots:
            if not self._is_lot_locked(lot, session_idx):
                continue
            symbol = str(lot["symbol"]).upper()
            weights[symbol] = weights.get(symbol, 0.0) + float(lot["weight"])
        return {symbol: float(weight) for symbol, weight in weights.items() if weight > self.eps}

    @staticmethod
    def _is_lot_locked(lot: Mapping[str, Any], session_idx: int) -> bool:
        return int(session_idx) - int(lot["birth_idx"]) < int(lot["min_hold"])

    @staticmethod
    def _copy_lot(lot: Mapping[str, Any], weight: float) -> dict[str, Any]:
        copied = {
            "symbol": str(lot["symbol"]).upper(),
            "factor": str(lot["factor"]),
            "weight": float(weight),
            "birth_idx": int(lot["birth_idx"]),
            "min_hold": int(lot["min_hold"]),
        }
        copied["entry_session_date"] = (
            None if lot.get("entry_session_date") in (None, "") else str(lot.get("entry_session_date"))
        )
        copied["entry_time_utc"] = None if lot.get("entry_time_utc") in (None, "") else str(lot.get("entry_time_utc"))
        return copied

    def sync_to_broker_weights(
        self,
        *,
        broker_weights: Mapping[str, float],
        session_idx: int,
        session_date: str | None = None,
        sync_factor: str = "broker_sync",
        sync_time_utc: str | None = None,
    ) -> dict[str, Any]:
        entry_session_date = str(session_date).strip() if session_date is not None else None
        entry_time_utc = str(sync_time_utc).strip() if sync_time_utc else _utc_now()
        target_long: dict[str, float] = {}
        target_short: dict[str, float] = {}
        for symbol, signed_weight in broker_weights.items():
            symbol_u = str(symbol).strip().upper()
            value = float(signed_weight)
            if not symbol_u or abs(value) <= self.eps:
                continue
            if value > 0:
                target_long[symbol_u] = float(value)
            else:
                target_short[symbol_u] = float(abs(value))

        self.ledger["long"], long_diag = self._sync_side_to_target(
            existing_lots=self.ledger["long"],
            target_weights=target_long,
            session_idx=int(session_idx),
            sync_birth_idx=max(0, int(session_idx) - 1),
            sync_factor=str(sync_factor),
            entry_session_date=entry_session_date,
            entry_time_utc=entry_time_utc,
        )
        self.ledger["short"], short_diag = self._sync_side_to_target(
            existing_lots=self.ledger["short"],
            target_weights=target_short,
            session_idx=int(session_idx),
            sync_birth_idx=max(0, int(session_idx) - 1),
            sync_factor=str(sync_factor),
            entry_session_date=entry_session_date,
            entry_time_utc=entry_time_utc,
        )
        return {"long": long_diag, "short": short_diag}

    def _sync_side_to_target(
        self,
        *,
        existing_lots: Sequence[Mapping[str, Any]],
        target_weights: Mapping[str, float],
        session_idx: int,
        sync_birth_idx: int,
        sync_factor: str,
        entry_session_date: str | None,
        entry_time_utc: str,
    ) -> tuple[list[dict[str, Any]], dict[str, float | int]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for lot in existing_lots:
            normalized = self._normalize_lot(lot)
            if normalized is None:
                continue
            grouped.setdefault(str(normalized["symbol"]).upper(), []).append(dict(normalized))

        out: list[dict[str, Any]] = []
        reduced_locked_weight = 0.0
        added_sync_weight = 0.0
        dropped_symbol_count = 0
        target_symbols = {str(symbol).upper() for symbol in target_weights}
        existing_symbols = set(grouped.keys())
        dropped_symbol_count = int(len(existing_symbols - target_symbols))

        for symbol, target in sorted(target_weights.items()):
            target_w = float(target)
            if target_w <= self.eps:
                continue
            lots = [self._copy_lot(lot, float(lot["weight"])) for lot in grouped.get(symbol, []) if float(lot["weight"]) > self.eps]
            if not lots:
                out.append(
                    self._new_sync_lot(
                        symbol=symbol,
                        weight=target_w,
                        birth_idx=int(sync_birth_idx),
                        sync_factor=sync_factor,
                        entry_session_date=entry_session_date,
                        entry_time_utc=entry_time_utc,
                    )
                )
                added_sync_weight += target_w
                continue

            current_total = float(sum(float(lot["weight"]) for lot in lots))
            if current_total + self.eps < target_w:
                delta = target_w - current_total
                lots.append(
                    self._new_sync_lot(
                        symbol=symbol,
                        weight=delta,
                        birth_idx=int(sync_birth_idx),
                        sync_factor=sync_factor,
                        entry_session_date=entry_session_date,
                        entry_time_utc=entry_time_utc,
                    )
                )
                added_sync_weight += delta
            elif current_total > target_w + self.eps:
                trim = current_total - target_w
                trim, reduced_locked = self._trim_lots_to_target(lots=lots, trim=trim, session_idx=int(session_idx))
                reduced_locked_weight += reduced_locked
                if trim > self.eps:
                    scale = max(target_w, 0.0) / max(current_total, self.eps)
                    for lot in lots:
                        lot["weight"] = float(max(0.0, float(lot["weight"]) * scale))

            out.extend([lot for lot in lots if float(lot["weight"]) > self.eps])

        out = [lot for lot in out if float(lot["weight"]) > self.eps]
        return out, {
            "symbol_count": int(len(target_weights)),
            "lot_count": int(len(out)),
            "added_sync_weight": float(added_sync_weight),
            "reduced_locked_weight": float(reduced_locked_weight),
            "dropped_symbol_count": int(dropped_symbol_count),
        }

    def _trim_lots_to_target(
        self,
        *,
        lots: list[dict[str, Any]],
        trim: float,
        session_idx: int,
    ) -> tuple[float, float]:
        remaining = float(max(trim, 0.0))
        reduced_locked_weight = 0.0
        remaining = self._reduce_from_lots(lots=lots, remaining=remaining, session_idx=session_idx, reduce_locked=False)
        if remaining > self.eps:
            before = remaining
            remaining = self._reduce_from_lots(lots=lots, remaining=remaining, session_idx=session_idx, reduce_locked=True)
            reduced_locked_weight += float(max(0.0, before - remaining))
        return remaining, reduced_locked_weight

    def _reduce_from_lots(
        self,
        *,
        lots: list[dict[str, Any]],
        remaining: float,
        session_idx: int,
        reduce_locked: bool,
    ) -> float:
        if remaining <= self.eps:
            return 0.0
        idxs: list[int] = []
        for idx, lot in enumerate(lots):
            is_locked = self._is_lot_locked(lot, int(session_idx))
            if bool(is_locked) != bool(reduce_locked):
                continue
            if float(lot["weight"]) <= self.eps:
                continue
            idxs.append(idx)
        idxs.sort(
            key=lambda i: (
                int(lots[i].get("birth_idx", 0)),
                float(lots[i].get("weight", 0.0)),
            ),
            reverse=True,
        )
        left = float(remaining)
        for idx in idxs:
            if left <= self.eps:
                break
            w = float(lots[idx]["weight"])
            cut = min(w, left)
            lots[idx]["weight"] = float(max(0.0, w - cut))
            left -= cut
        return float(max(left, 0.0))

    @staticmethod
    def _new_sync_lot(
        *,
        symbol: str,
        weight: float,
        birth_idx: int,
        sync_factor: str,
        entry_session_date: str | None,
        entry_time_utc: str,
    ) -> dict[str, Any]:
        return {
            "symbol": str(symbol).upper(),
            "factor": str(sync_factor),
            "weight": float(weight),
            "birth_idx": int(birth_idx),
            "min_hold": 0,
            "entry_session_date": entry_session_date,
            "entry_time_utc": str(entry_time_utc),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect Phase7K lot ledger snapshot.")
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--session-idx", type=int, required=True)
    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv)

    manager = LotManager.from_json(args.ledger_path)
    frame = manager.position_frame(session_idx=int(args.session_idx))
    summary = manager.snapshot(session_idx=int(args.session_idx))
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.output_root:
        out_root = Path(args.output_root)
        out_root.mkdir(parents=True, exist_ok=True)
        frame_path = out_root / "phase7k_current_positions.csv"
        summary_path = out_root / "phase7k_current_positions_summary.json"
        frame.to_csv(frame_path, index=False)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_snapshot_cli())
