#!/usr/bin/env python3
"""Independently reconcile every arithmetic identity in prototype trade logs."""

from __future__ import annotations

import argparse
import csv
import io
import math
import zipfile
from datetime import date, datetime
from pathlib import Path


def close(left: float, right: float, *, tolerance: float = 1e-9) -> None:
    if not math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{left!r} != {right!r}")


TIMESTAMP_FORMAT = "%d-%m-%Y %H:%M:%S"


def raw_bars(archive: zipfile.ZipFile, member: str) -> dict[datetime, dict[str, float]]:
    with archive.open(member) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        return {
            datetime.strptime(row["Timestamp"], TIMESTAMP_FORMAT): {
                "Open": float(row["Open"]),
                "Close": float(row["Close"]),
                "OI": float(row["OI"]),
            }
            for row in reader
        }


def expected_lot_size(expiry: date) -> int:
    if expiry <= date(2024, 4, 25):
        return 50
    if expiry <= date(2024, 12, 26) or expiry == date(2025, 1, 30):
        return 25
    return 75


def validate_raw_prices_and_triggers(
    row: dict[str, str], data_root: Path, prefix: str
) -> None:
    expiry = date.fromisoformat(row["expiry"])
    signal = datetime.fromisoformat(row["signal_timestamp"])
    entry = datetime.fromisoformat(row["entry_timestamp"])
    exit_time = datetime.fromisoformat(row["exit_timestamp"])
    trigger = datetime.fromisoformat(row["trigger_timestamp"]) if row["trigger_timestamp"] else None
    put_strike = int(float(row["put_strike"]))
    call_strike = int(float(row["call_strike"]))
    atm_strike = int(float(row["atm_strike"]))
    archive_path = data_root / row["archive"]
    if archive_path.stem != expiry.strftime("%Y%m%d"):
        raise AssertionError(f"{prefix}: archive does not match expiry")
    with zipfile.ZipFile(archive_path) as archive:
        put = raw_bars(archive, f"{put_strike}PE_{expiry:%Y%m%d}.csv")
        call = raw_bars(archive, f"{call_strike}CE_{expiry:%Y%m%d}.csv")
        atm_put = raw_bars(archive, f"{atm_strike}PE_{expiry:%Y%m%d}.csv")
        atm_call = raw_bars(archive, f"{atm_strike}CE_{expiry:%Y%m%d}.csv")

    close(float(row["atm_put_close"]), atm_put[signal]["Close"])
    close(float(row["atm_call_close"]), atm_call[signal]["Close"])
    if atm_put[signal]["OI"] <= 0 or atm_call[signal]["OI"] <= 0:
        raise AssertionError(f"{prefix}: ATM signal was not open-interest supported")
    close(float(row["put_entry_market"]), put[entry]["Open"])
    close(float(row["call_entry_market"]), call[entry]["Open"])
    close(float(row["put_exit_market"]), put[exit_time]["Open"])
    close(float(row["call_exit_market"]), call[exit_time]["Open"])

    gross = float(row["gross_entry_mark"])
    profit_threshold = 0.5 * gross
    stop_threshold = 2.0 * gross
    common = sorted(put.keys() & call.keys())
    first_trigger: tuple[datetime, str] | None = None
    planned_expiry_exit = datetime.combine(expiry, datetime.strptime("15:20", "%H:%M").time())
    for timestamp in common:
        if not entry <= timestamp < planned_expiry_exit:
            continue
        combined_close = put[timestamp]["Close"] + call[timestamp]["Close"]
        if combined_close <= profit_threshold:
            first_trigger = timestamp, "profit_target"
            break
        if combined_close >= stop_threshold:
            first_trigger = timestamp, "stop_loss"
            break

    if trigger is not None:
        if first_trigger is None or first_trigger[0] != trigger:
            raise AssertionError(f"{prefix}: exported trigger is not the first raw trigger")
        if first_trigger[1] != row["exit_reason"]:
            raise AssertionError(f"{prefix}: raw trigger reason differs")
        next_common = min(timestamp for timestamp in common if timestamp > trigger)
        if next_common != exit_time or (exit_time - trigger).total_seconds() > 300:
            raise AssertionError(f"{prefix}: exit is not the timely next common bar")
    else:
        if first_trigger is not None:
            raise AssertionError(f"{prefix}: a raw trigger was missed")
        if row["exit_reason"] != "time_exit":
            raise AssertionError(f"{prefix}: missing trigger for non-time exit")


def validate_file(
    path: Path,
    data_root: Path | None = None,
    previous_expiries: list[date] | None = None,
    friction: float = 0.005,
    filter_direction: str = "above",
    vrp_threshold: float = 1.10,
    use_trend_filter: bool = True,
) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    previous_capital_after: float | None = None
    previous_exit: datetime | None = None
    for index, row in enumerate(rows, start=1):
        prefix = f"{path.name} row {index}"
        expiry = date.fromisoformat(row["expiry"])
        entry_date = date.fromisoformat(row["entry_date"])
        feature_end = date.fromisoformat(row["feature_end_date"])
        signal = datetime.fromisoformat(row["signal_timestamp"])
        entry = datetime.fromisoformat(row["entry_timestamp"])
        exit_time = datetime.fromisoformat(row["exit_timestamp"])
        if not feature_end < entry_date:
            raise AssertionError(f"{prefix}: feature date is not lagged")
        if not signal < entry <= exit_time:
            raise AssertionError(f"{prefix}: timestamp chronology fails")
        if expiry > date(2025, 11, 30):
            raise AssertionError(f"{prefix}: post-cutoff data")
        if int(float(row["lot_size"])) != expected_lot_size(expiry):
            raise AssertionError(f"{prefix}: incorrect historical lot size")
        if previous_expiries is not None:
            expected_entry = max(item for item in previous_expiries if item < expiry)
            if entry_date != expected_entry:
                raise AssertionError(f"{prefix}: entry is not the immediately prior expiry")

        numeric = {key: float(value) for key, value in row.items() if key not in {
            "mode", "expiry", "archive", "entry_date", "feature_end_date",
            "signal_timestamp", "entry_timestamp", "trigger_timestamp",
            "exit_timestamp", "exit_reason"
        }}
        if not numeric["put_strike"] < numeric["spot_at_signal"] < numeric["call_strike"]:
            raise AssertionError(f"{prefix}: not an OTM strangle")
        close(
            numeric["implied_move_points"],
            numeric["atm_call_close"] + numeric["atm_put_close"],
        )
        close(
            numeric["implied_move_pct"],
            numeric["implied_move_points"] / numeric["spot_at_signal"],
        )
        close(
            numeric["put_entry_fill"],
            numeric["put_entry_market"] * (1 - friction),
        )
        close(
            numeric["call_entry_fill"],
            numeric["call_entry_market"] * (1 - friction),
        )
        close(
            numeric["put_exit_fill"],
            numeric["put_exit_market"] * (1 + friction),
        )
        close(
            numeric["call_exit_fill"],
            numeric["call_exit_market"] * (1 + friction),
        )
        close(
            numeric["net_entry_credit_points"],
            numeric["put_entry_fill"] + numeric["call_entry_fill"],
        )
        close(
            numeric["net_exit_debit_points"],
            numeric["put_exit_fill"] + numeric["call_exit_fill"],
        )
        close(
            numeric["pnl_points"],
            numeric["net_entry_credit_points"] - numeric["net_exit_debit_points"],
        )
        close(
            numeric["pnl_rupees"],
            numeric["pnl_points"] * numeric["lot_size"] * numeric["lots"],
        )
        close(
            numeric["capital_after"],
            numeric["capital_before"] + numeric["pnl_rupees"],
        )
        if previous_capital_after is not None:
            close(numeric["capital_before"], previous_capital_after)
        if previous_exit is not None and entry < previous_exit:
            raise AssertionError(f"{prefix}: position overlaps the prior trade")
        previous_capital_after = numeric["capital_after"]
        previous_exit = exit_time
        if row["mode"] == "filtered":
            violates_vrp = (
                numeric["vrp_ratio"] < vrp_threshold
                if filter_direction == "above"
                else numeric["vrp_ratio"] > vrp_threshold
            )
            violates_trend = use_trend_filter and numeric["trend_z"] > 1.00
            if violates_vrp or violates_trend:
                raise AssertionError(f"{prefix}: filtered trade violates signal thresholds")
        if data_root is not None:
            validate_raw_prices_and_triggers(row, data_root, prefix)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--filter-direction", choices=("above", "below"), default="above")
    parser.add_argument("--vrp-threshold", type=float, default=1.10)
    parser.add_argument("--disable-trend-filter", action="store_true")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    previous_expiries = None
    if args.data_root is not None:
        previous_expiries = sorted(
            datetime.strptime(path.stem, "%Y%m%d").date()
            for path in args.data_root.glob("*.zip")
            if path.stem.isdigit() and len(path.stem) == 8 and path.stem <= "20251130"
        )
    total = sum(
        validate_file(
            path,
            args.data_root,
            previous_expiries,
            filter_direction=args.filter_direction,
            vrp_threshold=args.vrp_threshold,
            use_trend_filter=not args.disable_trend_filter,
        )
        for path in args.paths
    )
    print(f"Validated {total} prototype trades across {len(args.paths)} files.")


if __name__ == "__main__":
    main()
