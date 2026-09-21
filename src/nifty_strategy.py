"""Reproducible NIFTY weekly short-strangle research engine.

Raw option CSV files are streamed directly from the provided expiry ZIPs.  The
module intentionally uses only Python's standard library so the research logic
does not depend on a private runtime.
"""

from __future__ import annotations

import csv
import gzip
import io
import math
import re
import statistics
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable


OPTION_MEMBER_RE = re.compile(
    r"^(?P<strike>\d+)(?P<right>CE|PE)_(?P<expiry>\d{8})\.csv$"
)
TIMESTAMP_FORMAT = "%d-%m-%Y %H:%M:%S"
DATA_CUTOFF = date(2025, 11, 30)


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int
    ticker: str


@dataclass(frozen=True)
class StrategyConfig:
    signal_time: time = time(15, 19)
    planned_fill_time: time = time(15, 20)
    max_fill_delay_minutes: int = 5
    fallback_holding_sessions: int = 5
    realized_lookback: int = 20
    minimum_vrp_ratio: float = 1.10
    maximum_vrp_ratio: float = 1.00
    vrp_filter_direction: str = "above"
    use_trend_filter: bool = True
    maximum_trend_z: float = 1.00
    strike_width_multiplier: float = 1.00
    profit_target_fraction: float = 0.50
    stop_multiple: float = 2.00
    execution_friction: float = 0.005
    initial_capital: float = 2_000_000.0
    risk_fraction: float = 0.01
    margin_allocation_fraction: float = 0.25
    margin_proxy_fraction: float = 0.10
    use_regime_filter: bool = True

    def validate(self) -> None:
        if self.fallback_holding_sessions <= 0 or self.realized_lookback < 2:
            raise ValueError("Holding period and lookback must be positive")
        if not 0 < self.profit_target_fraction < 1:
            raise ValueError("Profit target must be between zero and one")
        if self.stop_multiple <= 1:
            raise ValueError("Stop multiple must exceed one")
        if not 0 <= self.execution_friction < 1:
            raise ValueError("Execution friction must be in [0, 1)")
        if self.strike_width_multiplier <= 0:
            raise ValueError("Strike width multiplier must be positive")
        if self.vrp_filter_direction not in {"above", "below"}:
            raise ValueError("VRP filter direction must be 'above' or 'below'")


@dataclass(frozen=True)
class RegimeFeatures:
    daily_sigma: float
    expected_abs_realized_move_pct: float
    implied_move_pct: float
    vrp_ratio: float
    five_day_trend_log_return: float
    trend_z: float


@dataclass
class Trade:
    mode: str
    expiry: str
    archive: str
    entry_date: str
    holding_sessions: int
    feature_end_date: str
    signal_timestamp: str
    entry_timestamp: str
    trigger_timestamp: str
    exit_timestamp: str
    exit_reason: str
    spot_at_signal: float
    atm_strike: int
    atm_call_close: float
    atm_put_close: float
    implied_move_points: float
    implied_move_pct: float
    expected_abs_realized_move_pct: float
    vrp_ratio: float
    trend_z: float
    put_strike: int
    call_strike: int
    put_entry_market: float
    call_entry_market: float
    gross_entry_mark: float
    put_entry_fill: float
    call_entry_fill: float
    net_entry_credit_points: float
    put_exit_market: float
    call_exit_market: float
    put_exit_fill: float
    call_exit_fill: float
    net_exit_debit_points: float
    pnl_points: float
    lot_size: int
    lots: int
    pnl_rupees: float
    capital_before: float
    capital_after: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Skip:
    mode: str
    expiry: str
    archive: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_bar(row: dict[str, str]) -> Bar:
    return Bar(
        timestamp=datetime.strptime(row["Timestamp"], TIMESTAMP_FORMAT),
        open=float(row["Open"]),
        high=float(row["High"]),
        low=float(row["Low"]),
        close=float(row["Close"]),
        volume=int(float(row["Volume"])),
        oi=int(float(row["OI"])),
        ticker=row["Ticker"],
    )


def read_option_bars(archive: zipfile.ZipFile, member: str) -> dict[datetime, Bar]:
    with archive.open(member) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        bars: dict[datetime, Bar] = {}
        for row in reader:
            bar = parse_bar(row)
            if bar.timestamp in bars:
                raise ValueError(f"Duplicate timestamp in {member}: {bar.timestamp}")
            bars[bar.timestamp] = bar
    return bars


def load_spot_bars(path: Path) -> dict[datetime, Bar]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        bars: dict[datetime, Bar] = {}
        for row in reader:
            bar = parse_bar(row)
            if bar.timestamp in bars:
                raise ValueError(f"Duplicate consolidated spot timestamp: {bar.timestamp}")
            bars[bar.timestamp] = bar
    return bars


def load_complete_dates(path: Path) -> list[date]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            date.fromisoformat(row["date"])
            for row in rows
            if row["regular_session_complete"].lower() == "true"
        ]


def build_daily_closes(
    spot_bars: dict[datetime, Bar], complete_dates: Iterable[date]
) -> dict[date, float]:
    result: dict[date, float] = {}
    for trading_date in complete_dates:
        timestamp = datetime.combine(trading_date, time(15, 30))
        bar = spot_bars.get(timestamp)
        if bar is None:
            candidates = [
                item
                for item in spot_bars.values()
                if item.timestamp.date() == trading_date
                and time(15, 15) <= item.timestamp.time() <= time(15, 30)
            ]
            if not candidates:
                continue
            bar = max(candidates, key=lambda item: item.timestamp)
        result[trading_date] = bar.close
    return result


def calculate_regime_features(
    prior_closes: list[float],
    spot_at_signal: float,
    atm_straddle_price: float,
    holding_sessions: int = 5,
    lookback: int = 20,
) -> RegimeFeatures:
    if len(prior_closes) < max(lookback + 1, holding_sessions + 1):
        raise ValueError("Insufficient prior closes")
    if spot_at_signal <= 0 or atm_straddle_price <= 0:
        raise ValueError("Spot and straddle price must be positive")
    log_returns = [
        math.log(current / previous)
        for previous, current in zip(prior_closes[:-1], prior_closes[1:])
    ]
    recent_returns = log_returns[-lookback:]
    sigma = statistics.stdev(recent_returns)
    if sigma <= 0:
        raise ValueError("Realized volatility is zero")
    expected_abs = sigma * math.sqrt(holding_sessions) * math.sqrt(2 / math.pi)
    implied_move_pct = atm_straddle_price / spot_at_signal
    trend_return = math.log(prior_closes[-1] / prior_closes[-(holding_sessions + 1)])
    trend_z = abs(trend_return) / (sigma * math.sqrt(holding_sessions))
    return RegimeFeatures(
        daily_sigma=sigma,
        expected_abs_realized_move_pct=expected_abs,
        implied_move_pct=implied_move_pct,
        vrp_ratio=implied_move_pct / expected_abs,
        five_day_trend_log_return=trend_return,
        trend_z=trend_z,
    )


def lot_size_for_expiry(expiry: date) -> int:
    """Historical NIFTY market lots for expiries in the supplied 2024-25 data."""
    if expiry <= date(2024, 4, 25):
        return 50
    if expiry <= date(2024, 12, 26):
        return 25
    if expiry == date(2025, 1, 30):
        return 25  # final monthly expiry retaining the pre-revision lot
    return 75


def adverse_sell_fill(market_price: float, friction: float) -> float:
    if market_price <= 0:
        raise ValueError("Market price must be positive")
    return market_price * (1 - friction)


def adverse_buy_fill(market_price: float, friction: float) -> float:
    if market_price < 0:
        raise ValueError("Market price cannot be negative")
    return market_price * (1 + friction)


def position_size(
    capital: float,
    spot: float,
    gross_credit_points: float,
    lot_size: int,
    config: StrategyConfig,
) -> int:
    """Integer lots capped independently by stop-risk and margin proxies."""
    stop_loss_points = (config.stop_multiple - 1) * gross_credit_points
    # Ten percent buffer covers friction and gaps through the stop level.
    risk_per_lot = stop_loss_points * lot_size * 1.10
    margin_per_lot = config.margin_proxy_fraction * spot * lot_size
    risk_lots = math.floor(capital * config.risk_fraction / risk_per_lot)
    margin_lots = math.floor(
        capital * config.margin_allocation_fraction / margin_per_lot
    )
    return max(0, min(risk_lots, margin_lots))


def member_name(strike: int, right: str, expiry: date) -> str:
    return f"{strike}{right}_{expiry:%Y%m%d}.csv"


def paired_strikes(archive: zipfile.ZipFile, expiry: date) -> list[int]:
    calls: set[int] = set()
    puts: set[int] = set()
    for name in archive.namelist():
        match = OPTION_MEMBER_RE.match(name)
        if not match or match.group("expiry") != f"{expiry:%Y%m%d}":
            continue
        strike = int(match.group("strike"))
        (calls if match.group("right") == "CE" else puts).add(strike)
    return sorted(calls & puts)


def find_common_fill_timestamp(
    call_bars: dict[datetime, Bar],
    put_bars: dict[datetime, Bar],
    planned: datetime,
    max_delay_minutes: int,
) -> datetime | None:
    deadline = planned + timedelta(minutes=max_delay_minutes)
    common = call_bars.keys() & put_bars.keys()
    candidates = [timestamp for timestamp in common if planned <= timestamp <= deadline]
    return min(candidates) if candidates else None


def _valid_signal_bar(bar: Bar | None) -> bool:
    return bool(bar and bar.close > 0 and bar.oi > 0)


def _candidate_contract(
    archive: zipfile.ZipFile,
    expiry: date,
    strikes: Iterable[int],
    right: str,
    signal_timestamp: datetime,
    cache: dict[str, dict[datetime, Bar]],
) -> tuple[int, dict[datetime, Bar]] | None:
    for strike in strikes:
        name = member_name(strike, right, expiry)
        bars = cache.setdefault(name, read_option_bars(archive, name))
        if _valid_signal_bar(bars.get(signal_timestamp)):
            return strike, bars
    return None


def _next_common_timestamp(
    call_bars: dict[datetime, Bar],
    put_bars: dict[datetime, Bar],
    after: datetime,
    no_later_than: datetime,
) -> datetime | None:
    candidates = [
        timestamp
        for timestamp in call_bars.keys() & put_bars.keys()
        if after < timestamp <= no_later_than
    ]
    return min(candidates) if candidates else None


def evaluate_expiry(
    archive_path: Path,
    expiry: date,
    spot_bars: dict[datetime, Bar],
    complete_dates: list[date],
    daily_closes: dict[date, float],
    capital: float,
    config: StrategyConfig,
    entry_date_override: date | None = None,
    entry_not_before: datetime | None = None,
) -> Trade | Skip:
    config.validate()
    mode = "filtered" if config.use_regime_filter else "benchmark"
    if expiry > DATA_CUTOFF:
        return Skip(mode, str(expiry), archive_path.name, "post_cutoff")
    complete_before_or_on = [item for item in complete_dates if item <= expiry]
    if expiry not in complete_before_or_on:
        return Skip(mode, str(expiry), archive_path.name, "incomplete_expiry_session")
    expiry_index = complete_before_or_on.index(expiry)
    if entry_date_override is None:
        if expiry_index < config.fallback_holding_sessions:
            return Skip(mode, str(expiry), archive_path.name, "insufficient_calendar_history")
        entry_date = complete_before_or_on[
            expiry_index - config.fallback_holding_sessions
        ]
    else:
        entry_date = entry_date_override
        if entry_date not in complete_before_or_on or entry_date >= expiry:
            return Skip(
                mode,
                str(expiry),
                archive_path.name,
                "invalid_previous_expiry_entry",
                str(entry_date),
            )
    entry_index = complete_before_or_on.index(entry_date)
    holding_sessions = expiry_index - entry_index
    if holding_sessions <= 0:
        return Skip(mode, str(expiry), archive_path.name, "nonpositive_holding_period")
    prior_dates = [item for item in complete_dates if item < entry_date and item in daily_closes]
    if len(prior_dates) < config.realized_lookback + 1:
        return Skip(mode, str(expiry), archive_path.name, "insufficient_feature_history")
    feature_dates = prior_dates[-(config.realized_lookback + 1) :]
    prior_closes = [daily_closes[item] for item in feature_dates]
    signal_timestamp = datetime.combine(entry_date, config.signal_time)
    signal_spot_bar = spot_bars.get(signal_timestamp)
    if signal_spot_bar is None or signal_spot_bar.close <= 0:
        return Skip(mode, str(expiry), archive_path.name, "missing_signal_spot")

    cache: dict[str, dict[datetime, Bar]] = {}
    with zipfile.ZipFile(archive_path) as archive:
        strikes = paired_strikes(archive, expiry)
        if not strikes:
            return Skip(mode, str(expiry), archive_path.name, "no_paired_strikes")

        nearest = sorted(strikes, key=lambda strike: (abs(strike - signal_spot_bar.close), strike))
        atm: tuple[int, dict[datetime, Bar], dict[datetime, Bar]] | None = None
        for strike in nearest:
            call_name = member_name(strike, "CE", expiry)
            put_name = member_name(strike, "PE", expiry)
            call_bars = cache.setdefault(call_name, read_option_bars(archive, call_name))
            put_bars = cache.setdefault(put_name, read_option_bars(archive, put_name))
            if _valid_signal_bar(call_bars.get(signal_timestamp)) and _valid_signal_bar(
                put_bars.get(signal_timestamp)
            ):
                atm = strike, call_bars, put_bars
                break
        if atm is None:
            return Skip(mode, str(expiry), archive_path.name, "no_contemporaneous_atm_pair")
        atm_strike, atm_call_bars, atm_put_bars = atm
        atm_call = atm_call_bars[signal_timestamp]
        atm_put = atm_put_bars[signal_timestamp]
        implied_move_points = atm_call.close + atm_put.close
        features = calculate_regime_features(
            prior_closes,
            signal_spot_bar.close,
            implied_move_points,
            holding_sessions,
            config.realized_lookback,
        )
        if (
            config.use_regime_filter
            and config.vrp_filter_direction == "above"
            and features.vrp_ratio < config.minimum_vrp_ratio
        ):
            return Skip(
                mode,
                str(expiry),
                archive_path.name,
                "vrp_filter",
                f"{features.vrp_ratio:.6f} < {config.minimum_vrp_ratio:.6f}",
            )
        if (
            config.use_regime_filter
            and config.vrp_filter_direction == "below"
            and features.vrp_ratio > config.maximum_vrp_ratio
        ):
            return Skip(
                mode,
                str(expiry),
                archive_path.name,
                "vrp_filter",
                f"{features.vrp_ratio:.6f} > {config.maximum_vrp_ratio:.6f}",
            )
        if (
            config.use_regime_filter
            and config.use_trend_filter
            and features.trend_z > config.maximum_trend_z
        ):
            return Skip(
                mode,
                str(expiry),
                archive_path.name,
                "trend_filter",
                f"{features.trend_z:.6f} > {config.maximum_trend_z:.6f}",
            )

        width = config.strike_width_multiplier * implied_move_points
        call_target = signal_spot_bar.close + width
        put_target = signal_spot_bar.close - width
        call_candidates = [strike for strike in strikes if strike >= call_target]
        put_candidates = [strike for strike in reversed(strikes) if strike <= put_target]
        call_contract = _candidate_contract(
            archive, expiry, call_candidates, "CE", signal_timestamp, cache
        )
        put_contract = _candidate_contract(
            archive, expiry, put_candidates, "PE", signal_timestamp, cache
        )
        if call_contract is None or put_contract is None:
            return Skip(mode, str(expiry), archive_path.name, "no_liquid_outward_strangle")
        call_strike, call_bars = call_contract
        put_strike, put_bars = put_contract

        base_planned_entry = datetime.combine(entry_date, config.planned_fill_time)
        planned_entry = max(
            base_planned_entry,
            entry_not_before or base_planned_entry,
        )
        entry_deadline = base_planned_entry + timedelta(
            minutes=config.max_fill_delay_minutes
        )
        remaining_delay = int((entry_deadline - planned_entry).total_seconds() // 60)
        if remaining_delay < 0:
            return Skip(mode, str(expiry), archive_path.name, "prior_position_not_closed")
        entry_timestamp = find_common_fill_timestamp(
            call_bars, put_bars, planned_entry, remaining_delay
        )
        if entry_timestamp is None:
            return Skip(mode, str(expiry), archive_path.name, "missing_entry_fill")
        call_entry_market = call_bars[entry_timestamp].open
        put_entry_market = put_bars[entry_timestamp].open
        if min(call_entry_market, put_entry_market) <= 0:
            return Skip(mode, str(expiry), archive_path.name, "nonpositive_entry_price")
        gross_entry_mark = call_entry_market + put_entry_market
        call_entry_fill = adverse_sell_fill(call_entry_market, config.execution_friction)
        put_entry_fill = adverse_sell_fill(put_entry_market, config.execution_friction)
        net_entry_credit = call_entry_fill + put_entry_fill
        lot_size = lot_size_for_expiry(expiry)
        lots = position_size(
            capital, signal_spot_bar.close, gross_entry_mark, lot_size, config
        )
        if lots < 1:
            return Skip(mode, str(expiry), archive_path.name, "position_size_zero")

        expiry_planned_exit = datetime.combine(expiry, config.planned_fill_time)
        expiry_fill_deadline = expiry_planned_exit + timedelta(
            minutes=config.max_fill_delay_minutes
        )
        common_timestamps = sorted(call_bars.keys() & put_bars.keys())
        trigger_timestamp: datetime | None = None
        exit_timestamp: datetime | None = None
        exit_reason = "time_exit"
        for timestamp in common_timestamps:
            if not entry_timestamp <= timestamp < expiry_planned_exit:
                continue
            combined_close = call_bars[timestamp].close + put_bars[timestamp].close
            reason: str | None = None
            if combined_close <= config.profit_target_fraction * gross_entry_mark:
                reason = "profit_target"
            elif combined_close >= config.stop_multiple * gross_entry_mark:
                reason = "stop_loss"
            if reason:
                next_timestamp = _next_common_timestamp(
                    call_bars,
                    put_bars,
                    timestamp,
                    min(
                        timestamp + timedelta(minutes=config.max_fill_delay_minutes),
                        expiry_fill_deadline,
                    ),
                )
                if next_timestamp is not None:
                    trigger_timestamp = timestamp
                    exit_timestamp = next_timestamp
                    exit_reason = reason
                    break

        if exit_timestamp is None:
            exit_timestamp = find_common_fill_timestamp(
                call_bars,
                put_bars,
                expiry_planned_exit,
                config.max_fill_delay_minutes,
            )
        if exit_timestamp is None:
            return Skip(mode, str(expiry), archive_path.name, "missing_exit_fill")

        call_exit_market = call_bars[exit_timestamp].open
        put_exit_market = put_bars[exit_timestamp].open
        if min(call_exit_market, put_exit_market) < 0:
            return Skip(mode, str(expiry), archive_path.name, "negative_exit_price")
        call_exit_fill = adverse_buy_fill(call_exit_market, config.execution_friction)
        put_exit_fill = adverse_buy_fill(put_exit_market, config.execution_friction)
        net_exit_debit = call_exit_fill + put_exit_fill
        pnl_points = net_entry_credit - net_exit_debit
        pnl_rupees = pnl_points * lot_size * lots
        capital_after = capital + pnl_rupees

        if not signal_timestamp < entry_timestamp <= exit_timestamp:
            raise AssertionError("Signal/fill/exit chronology is invalid")
        if not put_strike < signal_spot_bar.close < call_strike:
            raise AssertionError("Selected contracts do not form an OTM strangle")

        return Trade(
            mode=mode,
            expiry=str(expiry),
            archive=archive_path.name,
            entry_date=str(entry_date),
            holding_sessions=holding_sessions,
            feature_end_date=str(feature_dates[-1]),
            signal_timestamp=signal_timestamp.isoformat(sep=" "),
            entry_timestamp=entry_timestamp.isoformat(sep=" "),
            trigger_timestamp=trigger_timestamp.isoformat(sep=" ")
            if trigger_timestamp
            else "",
            exit_timestamp=exit_timestamp.isoformat(sep=" "),
            exit_reason=exit_reason,
            spot_at_signal=signal_spot_bar.close,
            atm_strike=atm_strike,
            atm_call_close=atm_call.close,
            atm_put_close=atm_put.close,
            implied_move_points=implied_move_points,
            implied_move_pct=features.implied_move_pct,
            expected_abs_realized_move_pct=features.expected_abs_realized_move_pct,
            vrp_ratio=features.vrp_ratio,
            trend_z=features.trend_z,
            put_strike=put_strike,
            call_strike=call_strike,
            put_entry_market=put_entry_market,
            call_entry_market=call_entry_market,
            gross_entry_mark=gross_entry_mark,
            put_entry_fill=put_entry_fill,
            call_entry_fill=call_entry_fill,
            net_entry_credit_points=net_entry_credit,
            put_exit_market=put_exit_market,
            call_exit_market=call_exit_market,
            put_exit_fill=put_exit_fill,
            call_exit_fill=call_exit_fill,
            net_exit_debit_points=net_exit_debit,
            pnl_points=pnl_points,
            lot_size=lot_size,
            lots=lots,
            pnl_rupees=pnl_rupees,
            capital_before=capital,
            capital_after=capital_after,
        )


def archive_expiry(path: Path) -> date:
    return datetime.strptime(path.stem, "%Y%m%d").date()
