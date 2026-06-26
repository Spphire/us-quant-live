from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from vendors import (  # noqa: E402
    AlpacaCredentials,
    AlpacaHttpClient,
    AlpacaRequestError,
)


DEFAULT_CANDIDATE_SYMBOLS_PATH = (
    PROJECT_ROOT / "configs" / "universe" / "top1000_fixed_symbols_20260417.txt"
)

ETF_PATTERN = re.compile(
    r"\b(ETF|ETN|FUND|FUNDS|INDEX FUND|EXCHANGE[- ]TRADED|SPDR|ISHARES|VANGUARD|"
    r"PROSHARES|DIREXION|GLOBAL X|WISDOMTREE|FIRST TRUST|VANECK)\b",
    re.IGNORECASE,
)
NON_COMMON_PATTERN = re.compile(
    r"\b(WARRANTS?|RIGHTS?|UNITS?|PREFERRED|PREF|DEBENTURES?|NOTES?|BONDS?|"
    r"ACQUISITION|BLANK CHECK|SPAC)\b",
    re.IGNORECASE,
)
ADR_PATTERN = re.compile(
    r"\b(ADR|ADS|AMERICAN DEPOSITARY|DEPOSITARY SHARES?|PLC|P\.L\.C\.|N\.V\.|"
    r"S\.A\.|SE|LTD|LIMITED|COMPANHIA|BANCO|HOLDING[S]? PLC)\b",
    re.IGNORECASE,
)
TRUST_PATTERN = re.compile(r"\b(REIT|REAL ESTATE INVESTMENT TRUST|ROYALTY TRUST)\b", re.IGNORECASE)


@dataclass(slots=True)
class RefreshDiagnostics:
    """记录一次 fresh 的关键中间统计，便于排查口径差异。"""

    as_of_date: str
    candidate_symbols: int
    clean_core_candidates: int
    tradable_candidates: int
    bars_rows: int
    symbols_with_prior_bars: int
    eligible_symbols: int
    selected_symbols: int
    lookback_sessions: int
    min_observations: int
    price_floor: float
    beta_full_observations: int
    pool_size: int
    start_date_for_bars: str
    end_date_for_bars: str


class DynamicSymbolPool:
    """基于 Alpaca 资产与历史日线，重建某个交易日的动态股票池。"""

    def __init__(
        self,
        *,
        client: AlpacaHttpClient,
        candidate_symbols: Sequence[str],
        pool_size: int = 1000,
        lookback_sessions: int = 20,
        min_observations: int = 15,
        price_floor: float = 10.0,
        bars_window_calendar_days: int = 60,
        bars_chunk_size: int = 120,
        bars_workers: int = 4,
        feed: str = "iex",
        beta_full_observations: int = 252,
    ) -> None:
        if pool_size <= 0:
            raise ValueError("pool_size must be > 0")
        if lookback_sessions <= 0:
            raise ValueError("lookback_sessions must be > 0")
        if min_observations <= 0:
            raise ValueError("min_observations must be > 0")
        if bars_window_calendar_days <= 0:
            raise ValueError("bars_window_calendar_days must be > 0")
        if bars_chunk_size <= 0:
            raise ValueError("bars_chunk_size must be > 0")
        if bars_workers <= 0:
            raise ValueError("bars_workers must be > 0")
        if beta_full_observations <= 0:
            raise ValueError("beta_full_observations must be > 0")

        cleaned = sorted({str(symbol).strip().upper() for symbol in candidate_symbols if str(symbol).strip()})
        if not cleaned:
            raise ValueError("candidate_symbols is empty")

        self._client = client
        self._candidate_symbols = cleaned
        self._pool_size = int(pool_size)
        self._lookback_sessions = int(lookback_sessions)
        self._min_observations = int(min_observations)
        self._price_floor = float(price_floor)
        self._bars_window_calendar_days = int(bars_window_calendar_days)
        self._bars_chunk_size = int(bars_chunk_size)
        self._bars_workers = int(bars_workers)
        self._feed = str(feed)
        self._beta_full_observations = int(beta_full_observations)

        self.symbols: set[str] = set()
        self.last_refresh_date: str | None = None
        self.last_diagnostics: RefreshDiagnostics | None = None

    @property
    def candidate_symbols(self) -> tuple[str, ...]:
        return tuple(self._candidate_symbols)

    def fresh(self, as_of_date: str | date | datetime) -> set[str]:
        # 1) 规范化日期输入，避免字符串/日期对象混用导致口径偏差。
        target_date = _normalize_date(as_of_date)
        target_date_str = target_date.isoformat()

        # 2) 先拿到 Alpaca 当前资产，动态计算 clean_core，再与可交易集合做交集。
        print("[Alpaca] Step 1/3: fetching active us_equity assets ...", flush=True)
        assets = self._client.list_assets(status="active", asset_class="us_equity")
        print(f"[Alpaca] Step 1/3 done: assets={len(assets)}", flush=True)
        clean_core_symbols = _build_runtime_clean_core_symbol_set(assets)
        clean_core_candidates = sorted(set(self._candidate_symbols).intersection(clean_core_symbols))
        tradable_symbols = _build_tradable_symbol_set(assets)
        tradable_candidates = sorted(set(clean_core_candidates).intersection(tradable_symbols))
        if not tradable_candidates:
            self.symbols = set()
            self.last_refresh_date = target_date_str
            self.last_diagnostics = RefreshDiagnostics(
                as_of_date=target_date_str,
                candidate_symbols=len(self._candidate_symbols),
                clean_core_candidates=len(clean_core_candidates),
                tradable_candidates=0,
                bars_rows=0,
                symbols_with_prior_bars=0,
                eligible_symbols=0,
                selected_symbols=0,
                lookback_sessions=self._lookback_sessions,
                min_observations=self._min_observations,
                price_floor=self._price_floor,
                beta_full_observations=self._beta_full_observations,
                pool_size=self._pool_size,
                start_date_for_bars=target_date_str,
                end_date_for_bars=target_date_str,
            )
            return set()

        # 3) 拉取 target_date 之前的一段历史 bars，用于计算“滞后流动性”。
        bars_start = target_date - timedelta(days=self._bars_window_calendar_days)
        bars_end = target_date
        print(
            f"[Alpaca] Step 2/3: fetching bars for {len(tradable_candidates)} symbols ...",
            flush=True,
        )
        bars = self._collect_bars_for_candidates(
            symbols=tradable_candidates,
            start=bars_start.isoformat(),
            end=bars_end.isoformat(),
        )
        print(f"[Alpaca] Step 2/3 done: bars={len(bars)}", flush=True)
        # 4) 从 bars 构造横截面评分并筛出当日池子。
        panel = _bars_to_panel(bars)
        print("[Alpaca] Step 3/3: ranking lagged liquidity ...", flush=True)
        selected_symbols, eligible_count, prior_count = self._build_pool_for_date(panel, target_date)
        print(f"[Alpaca] Step 3/3 done: selected={len(selected_symbols)}", flush=True)

        self.symbols = set(selected_symbols)
        self.last_refresh_date = target_date_str
        self.last_diagnostics = RefreshDiagnostics(
            as_of_date=target_date_str,
            candidate_symbols=len(self._candidate_symbols),
            clean_core_candidates=len(clean_core_candidates),
            tradable_candidates=len(tradable_candidates),
            bars_rows=len(panel),
            symbols_with_prior_bars=prior_count,
            eligible_symbols=eligible_count,
            selected_symbols=len(selected_symbols),
            lookback_sessions=self._lookback_sessions,
            min_observations=self._min_observations,
            price_floor=self._price_floor,
            beta_full_observations=self._beta_full_observations,
            pool_size=self._pool_size,
            start_date_for_bars=bars_start.isoformat(),
            end_date_for_bars=bars_end.isoformat(),
        )
        return set(self.symbols)

    def _collect_bars_for_candidates(self, *, symbols: Sequence[str], start: str, end: str) -> list[dict[str, Any]]:
        # Alpaca bars 接口对 symbols 长度有限制，按 chunk 分批请求。
        if not symbols:
            return []
        rows: list[dict[str, Any]] = []
        chunks = [list(chunk) for chunk in _chunks(symbols, self._bars_chunk_size)]
        total_chunks = max(1, len(chunks))
        worker_count = max(1, min(self._bars_workers, total_chunks))
        _print_progress(label="Alpaca bars", current=0, total=total_chunks)
        if worker_count == 1:
            for idx, chunk in enumerate(chunks, start=1):
                bars = self._client.get_stock_bars(
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Day",
                    adjustment="raw",
                    feed=self._feed,
                    limit=10000,
                )
                rows.extend(bar for bar in bars if isinstance(bar, dict))
                _print_progress(label="Alpaca bars", current=idx, total=total_chunks)
            return rows

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        interrupted = False
        future_to_chunk: dict[concurrent.futures.Future[list[dict[str, Any]]], list[str]] = {}
        try:
            future_to_chunk = {
                executor.submit(
                    self._client.get_stock_bars,
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Day",
                    adjustment="raw",
                    feed=self._feed,
                    limit=10000,
                ): chunk
                for chunk in chunks
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_to_chunk):
                bars = future.result()
                rows.extend(bar for bar in bars if isinstance(bar, dict))
                completed += 1
                _print_progress(label="Alpaca bars", current=completed, total=total_chunks)
        except KeyboardInterrupt:
            interrupted = True
            for future in future_to_chunk:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            print("\n[DynamicSymbolPool] interrupted by Ctrl+C during Alpaca bars fetch.", flush=True)
            raise
        finally:
            if not interrupted:
                executor.shutdown(wait=True)
        return rows

    def _build_pool_for_date(self, panel: pd.DataFrame, target_date: date) -> tuple[list[str], int, int]:
        if panel.empty:
            return [], 0, 0

        # 对每个 symbol 只使用 target_date 之前数据，避免未来函数污染。
        panel = panel.sort_values(["symbol", "session_date"]).copy()
        rows: list[dict[str, Any]] = []
        symbols_with_prior_bars = 0
        for symbol, group in panel.groupby("symbol", sort=True):
            prior = group[group["session_date"].lt(pd.Timestamp(target_date))]
            if prior.empty:
                continue
            symbols_with_prior_bars += 1
            trailing = prior.tail(self._lookback_sessions)
            prior_bar_count = int(len(prior))
            obs = int(len(trailing))
            lagged_close = float(trailing["close"].iloc[-1]) if obs else float("nan")
            trailing_median_dv = float(trailing["dollar_volume"].median()) if obs else float("nan")
            # 口径对齐历史池：
            # - 至少 min_observations 个近20日观测
            # - 至少 beta_full_observations 个历史条数（对应 beta full lookback proxy）
            # - 价格门槛 + 正流动性门槛
            eligible = (
                obs >= self._min_observations
                and prior_bar_count >= self._beta_full_observations
                and pd.notna(lagged_close)
                and lagged_close >= self._price_floor
                and pd.notna(trailing_median_dv)
                and trailing_median_dv > 0.0
            )
            rows.append(
                {
                    "symbol": str(symbol),
                    "trailing_obs": obs,
                    "prior_bar_count": prior_bar_count,
                    "lagged_close": lagged_close,
                    "trailing_median_dollar_volume_20": trailing_median_dv,
                    "liquidity_eligible": bool(eligible),
                }
            )

        if not rows:
            return [], 0, symbols_with_prior_bars
        rank_frame = pd.DataFrame(rows)
        eligible = rank_frame[rank_frame["liquidity_eligible"]].copy()
        if eligible.empty:
            return [], 0, symbols_with_prior_bars
        # 按滞后20日中位成交额降序选前 pool_size；symbol 作为稳定次序键。
        eligible = eligible.sort_values(
            ["trailing_median_dollar_volume_20", "symbol"],
            ascending=[False, True],
        ).reset_index(drop=True)
        selected = eligible.head(self._pool_size)["symbol"].astype(str).tolist()
        return selected, int(len(eligible)), symbols_with_prior_bars


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a dynamic symbol pool from Alpaca data and compare it with "
            "the legacy backtest dynamic pool for the same date."
        )
    )
    parser.add_argument("--date", required=True, help="Pool date in YYYY-MM-DD.")
    parser.add_argument(
        "--accounts-json-path",
        default="configs/alpaca_acounts/alpaca_accounts.local.json",
        help=(
            "Optional Alpaca accounts json path (for example "
            "alpaca_accounts.local.json). If provided with --account-name, "
            "credentials are loaded from this file."
        ),
    )
    parser.add_argument(
        "--account-name",
        default="ALPACA_US_FULL",
        help="Account key inside accounts json, for example ALPACA_US_FULL.",
    )
    parser.add_argument(
        "--candidate-symbols-path",
        default=str(DEFAULT_CANDIDATE_SYMBOLS_PATH),
        help="Fixed candidate symbol list (.txt one symbol per line, or .csv with symbol column).",
    )

    parser.add_argument("--pool-size", type=int, default=1000)
    parser.add_argument("--lookback-sessions", type=int, default=20)
    parser.add_argument("--min-observations", type=int, default=15)
    parser.add_argument("--price-floor", type=float, default=10.0)
    parser.add_argument("--bars-window-calendar-days", type=int, default=420)
    parser.add_argument("--bars-chunk-size", type=int, default=120)
    parser.add_argument("--bars-workers", type=int, default=4)
    parser.add_argument("--feed", default="iex", help="Alpaca bars feed: iex or sip.")
    parser.add_argument("--beta-full-observations", type=int, default=252)
    parser.add_argument("--data-base-url", default="https://data.alpaca.markets")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args(argv)

    try:
        # 允许两种凭据来源：accounts json（优先）或环境变量。
        credentials = _resolve_alpaca_credentials(
            accounts_json_path=str(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url=str(args.data_base_url),
            request_timeout_seconds=float(args.request_timeout_seconds),
            max_retries=int(args.max_retries),
        )
        client = AlpacaHttpClient(credentials)
        candidate_symbols = _load_candidate_symbols(Path(args.candidate_symbols_path))
        pool = DynamicSymbolPool(
            client=client,
            candidate_symbols=candidate_symbols,
            pool_size=int(args.pool_size),
            lookback_sessions=int(args.lookback_sessions),
            min_observations=int(args.min_observations),
            price_floor=float(args.price_floor),
            bars_window_calendar_days=int(args.bars_window_calendar_days),
            bars_chunk_size=int(args.bars_chunk_size),
            bars_workers=int(args.bars_workers),
            feed=str(args.feed),
            beta_full_observations=int(args.beta_full_observations),
        )
        refreshed = pool.fresh(args.date)
        print(refreshed)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted by user (Ctrl+C)."}, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    except AlpacaRequestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    return 0


def _load_candidate_symbols(path: Path) -> list[str]:
    """读取固定候选股票列表，支持 txt/csv 两种格式。"""

    if not path.exists():
        raise FileNotFoundError(f"Candidate symbols file not found: {path.as_posix()}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        symbols = [line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines()]
        cleaned = sorted({symbol for symbol in symbols if symbol})
        if not cleaned:
            raise ValueError(f"No symbols found in {path.as_posix()}")
        return cleaned
    if suffix == ".csv":
        frame = pd.read_csv(path)
        if "symbol" not in frame.columns:
            raise ValueError(f"CSV {path.as_posix()} must include a symbol column.")
        symbols = [str(symbol).strip().upper() for symbol in frame["symbol"].tolist()]
        cleaned = sorted({symbol for symbol in symbols if symbol})
        if not cleaned:
            raise ValueError(f"No symbols found in {path.as_posix()}")
        return cleaned
    raise ValueError(f"Unsupported candidate symbols file type: {path.as_posix()}")


def _resolve_alpaca_credentials(
    *,
    accounts_json_path: str,
    account_name: str,
    data_base_url: str,
    request_timeout_seconds: float,
    max_retries: int,
) -> AlpacaCredentials:
    """根据传参决定凭据来源：accounts json 或环境变量。"""

    has_accounts_args = bool(accounts_json_path.strip()) or bool(account_name.strip())
    if has_accounts_args:
        if not accounts_json_path.strip() or not account_name.strip():
            raise ValueError(
                "--accounts-json-path and --account-name must be provided together."
            )
        return _load_credentials_from_accounts_json(
            path=Path(accounts_json_path),
            account_name=account_name.strip(),
            data_base_url=data_base_url,
            request_timeout_seconds=request_timeout_seconds,
            max_retries=max_retries,
        )
    return AlpacaCredentials.from_env()


def _load_credentials_from_accounts_json(
    *,
    path: Path,
    account_name: str,
    data_base_url: str,
    request_timeout_seconds: float,
    max_retries: int,
) -> AlpacaCredentials:
    """从本地账户配置读取 Alpaca key，转为统一 Credentials 对象。"""

    if not path.exists():
        raise FileNotFoundError(f"accounts json not found: {path.as_posix()}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"accounts json payload must be an object: {path.as_posix()}")
    raw_account = payload.get(account_name)
    if not isinstance(raw_account, dict):
        raise ValueError(
            f"account_name {account_name!r} not found in {path.as_posix()}"
        )
    api_key_id = str(raw_account.get("api_key") or "").strip()
    api_secret_key = str(raw_account.get("secret_key") or "").strip()
    trading_base_url = str(raw_account.get("base_url") or "").strip().rstrip("/")
    if not api_key_id or not api_secret_key:
        raise ValueError(
            f"account {account_name!r} missing api_key or secret_key in {path.as_posix()}"
        )
    if not trading_base_url:
        trading_base_url = "https://paper-api.alpaca.markets"
    return AlpacaCredentials(
        api_key_id=api_key_id,
        api_secret_key=api_secret_key,
        trading_base_url=trading_base_url,
        data_base_url=str(data_base_url).strip().rstrip("/") or "https://data.alpaca.markets",
        request_timeout_seconds=float(request_timeout_seconds),
        max_retries=int(max_retries),
    )


def _normalize_date(raw: str | date | datetime) -> date:
    """统一日期输入格式。"""

    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"true", "t", "yes", "y", "1"}:
        return True
    if raw in {"false", "f", "no", "n", "0"}:
        return False
    return None


def _build_tradable_symbol_set(assets: Sequence[dict[str, Any]]) -> set[str]:
    """从 Alpaca assets 响应中过滤出 active + tradable 的美股代码。"""

    tradable: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        status = str(asset.get("status") or "").strip().lower()
        asset_class = str(asset.get("class") or "").strip().lower()
        is_tradable = _coerce_optional_bool(asset.get("tradable")) is True
        if status == "active" and asset_class == "us_equity" and is_tradable:
            tradable.add(symbol)
    return tradable


def _classify_asset_from_metadata(*, symbol: str, name: str, asset: dict[str, Any]) -> tuple[str, list[str]]:
    flags: list[str] = []
    if ETF_PATTERN.search(name):
        flags.append("etf_or_fund_name_pattern")
    if NON_COMMON_PATTERN.search(name):
        flags.append("non_common_security_name_pattern")
    if ADR_PATTERN.search(name):
        flags.append("adr_or_foreign_issuer_name_pattern")
    if TRUST_PATTERN.search(name):
        flags.append("trust_or_reit_name_pattern")
    if "." in symbol:
        flags.append("class_share_symbol")
    if _coerce_optional_bool(asset.get("tradable")) is False:
        flags.append("not_tradable_current_metadata")
    if _coerce_optional_bool(asset.get("shortable")) is False:
        flags.append("not_shortable_current_metadata")
    if _coerce_optional_bool(asset.get("easy_to_borrow")) is False:
        flags.append("not_easy_to_borrow_current_metadata")

    if "etf_or_fund_name_pattern" in flags or "non_common_security_name_pattern" in flags:
        classification = "blocked_non_common_like"
    elif "adr_or_foreign_issuer_name_pattern" in flags:
        classification = "review_foreign_or_adr_like"
    elif "trust_or_reit_name_pattern" in flags:
        classification = "review_trust_or_reit_like"
    else:
        classification = "common_stock_candidate"
    return classification, flags


def _build_runtime_clean_core_symbol_set(assets: Sequence[dict[str, Any]]) -> set[str]:
    """按 Phase0 口径从 Alpaca assets 动态计算 clean_core symbol 集。"""

    clean_core: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        name = str(asset.get("name") or "").strip()
        classification, flags = _classify_asset_from_metadata(symbol=symbol, name=name, asset=asset)
        review_required = classification != "common_stock_candidate" or bool(flags)
        if not review_required:
            clean_core.add(symbol)
    return clean_core


def _bars_to_panel(bars: Sequence[dict[str, Any]]) -> pd.DataFrame:
    """把 Alpaca bars 列表转成标准 DataFrame，并去重到(symbol, session_date)。"""

    if not bars:
        return pd.DataFrame(columns=["symbol", "session_date", "close", "dollar_volume"])
    rows: list[dict[str, Any]] = []
    for bar in bars:
        symbol = str(bar.get("symbol") or "").strip().upper()
        timestamp = bar.get("t")
        if not symbol or not isinstance(timestamp, str) or len(timestamp) < 10:
            continue
        session_date = timestamp[:10]
        close = _to_float(bar.get("c"))
        volume = _to_float(bar.get("v"))
        vwap = _to_float(bar.get("vw"))
        if close is None or volume is None:
            continue
        # 与项目历史 normalizer 对齐：优先 close*volume 作为 dollar_volume。
        reference_price = close if close is not None else vwap
        if reference_price is None:
            continue
        dollar_volume = reference_price * volume
        rows.append(
            {
                "symbol": symbol,
                "session_date": pd.Timestamp(session_date),
                "close": close,
                "dollar_volume": dollar_volume,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["symbol", "session_date", "close", "dollar_volume"])
    frame = pd.DataFrame(rows)
    frame = frame.sort_values(["symbol", "session_date"]).drop_duplicates(
        ["symbol", "session_date"],
        keep="last",
    )
    return frame.reset_index(drop=True)


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _print_progress(*, label: str, current: int, total: int, width: int = 28) -> None:
    safe_total = max(1, int(total))
    safe_current = min(max(0, int(current)), safe_total)
    ratio = safe_current / safe_total
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    print(
        f"\r[{label}] {safe_current}/{safe_total} [{bar}] {ratio * 100:6.2f}%",
        end="",
        flush=True,
    )
    if safe_current >= safe_total:
        print(flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
