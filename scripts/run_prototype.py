#!/usr/bin/env python3
"""Run and report the July 2024 one-month strategy prototype."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nifty_strategy import (  # noqa: E402
    DATA_CUTOFF,
    Skip,
    StrategyConfig,
    Trade,
    archive_expiry,
    build_daily_closes,
    evaluate_expiry,
    load_complete_dates,
    load_spot_bars,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--spot", default=PROJECT_ROOT / "data/processed/nifty_spot_1min.csv.gz", type=Path
    )
    parser.add_argument(
        "--coverage", default=PROJECT_ROOT / "data/audit/spot_daily_coverage.csv", type=Path
    )
    parser.add_argument(
        "--output-root", default=PROJECT_ROOT / "data/prototype", type=Path
    )
    parser.add_argument("--start", default="2024-07-01")
    parser.add_argument("--end", default="2024-07-31")
    parser.add_argument("--filter-direction", choices=("above", "below"), default="above")
    parser.add_argument("--vrp-threshold", type=float)
    parser.add_argument("--disable-trend-filter", action="store_true")
    return parser.parse_args()


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(trades: list[Trade], skips: list[Skip], initial_capital: float) -> dict[str, object]:
    pnl = sum(trade.pnl_rupees for trade in trades)
    wins = sum(trade.pnl_rupees > 0 for trade in trades)
    equity = initial_capital
    peak = equity
    max_drawdown = 0.0
    for trade in trades:
        equity += trade.pnl_rupees
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "trades": len(trades),
        "skips": len(skips),
        "total_pnl_rupees": pnl,
        "ending_capital": initial_capital + pnl,
        "return_fraction": pnl / initial_capital,
        "win_rate": wins / len(trades) if trades else None,
        "average_pnl_rupees": pnl / len(trades) if trades else None,
        "max_trade_level_drawdown": max_drawdown,
        "exit_reasons": {
            reason: sum(trade.exit_reason == reason for trade in trades)
            for reason in ("profit_target", "stop_loss", "time_exit")
        },
        "skip_reasons": {
            reason: sum(skip.reason == reason for skip in skips)
            for reason in sorted({skip.reason for skip in skips})
        },
    }


def main() -> None:
    args = parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end > DATA_CUTOFF:
        raise SystemExit(f"Requested end date exceeds hard data cutoff {DATA_CUTOFF}")
    config = StrategyConfig(
        vrp_filter_direction=args.filter_direction,
        minimum_vrp_ratio=args.vrp_threshold
        if args.filter_direction == "above" and args.vrp_threshold is not None
        else 1.10,
        maximum_vrp_ratio=args.vrp_threshold
        if args.filter_direction == "below" and args.vrp_threshold is not None
        else 1.00,
        use_trend_filter=not args.disable_trend_filter,
    )
    spot_bars = load_spot_bars(args.spot)
    complete_dates = load_complete_dates(args.coverage)
    daily_closes = build_daily_closes(spot_bars, complete_dates)
    all_archives = [
        path
        for path in sorted(args.data_root.glob("*.zip"))
        if archive_expiry(path) <= end
    ]
    archives = [
        path
        for path in all_archives
        if start <= archive_expiry(path) <= end
    ]
    if not archives:
        raise SystemExit("No expiry archives found in requested prototype period")

    all_results: dict[str, dict[str, object]] = {}
    for mode, mode_config in (
        ("filtered", config),
        ("benchmark", replace(config, use_regime_filter=False)),
    ):
        capital = mode_config.initial_capital
        last_exit = None
        trades: list[Trade] = []
        skips: list[Skip] = []
        for archive_path in archives:
            expiry = archive_expiry(archive_path)
            prior_expiries = [
                archive_expiry(path)
                for path in all_archives
                if archive_expiry(path) < expiry
            ]
            previous_expiry = max(prior_expiries) if prior_expiries else None
            result = evaluate_expiry(
                archive_path,
                expiry,
                spot_bars,
                complete_dates,
                daily_closes,
                capital,
                mode_config,
                previous_expiry,
                last_exit,
            )
            if isinstance(result, Trade):
                trades.append(result)
                capital = result.capital_after
                last_exit = datetime.fromisoformat(result.exit_timestamp)
            else:
                skips.append(result)
        write_rows(args.output_root / f"{mode}_trades.csv", [item.to_dict() for item in trades])
        write_rows(args.output_root / f"{mode}_skips.csv", [item.to_dict() for item in skips])
        all_results[mode] = {
            "config": asdict(mode_config),
            "summary": summarize(trades, skips, mode_config.initial_capital),
            "trades": [item.to_dict() for item in trades],
            "skips": [item.to_dict() for item in skips],
        }

    output = {
        "period": {"start": str(start), "end": str(end)},
        "archive_count": len(archives),
        "results": all_results,
    }
    args.output_root.mkdir(parents=True, exist_ok=True)
    (args.output_root / "prototype_summary.json").write_text(
        json.dumps(output, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
