#!/usr/bin/env python3
"""Compute performance and robustness statistics from candidate trade logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


INITIAL_CAPITAL = 2_000_000.0
RISK_FRACTION = 0.01
MARGIN_ALLOCATION = 0.25
MARGIN_PROXY = 0.10
STOP_MULTIPLE = 2.0


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def integer_lots(capital: float, row: dict[str, str]) -> int:
    gross_credit = float(row["gross_entry_mark"])
    spot = float(row["spot_at_signal"])
    lot_size = int(float(row["lot_size"]))
    risk_per_lot = (STOP_MULTIPLE - 1) * gross_credit * lot_size * 1.10
    margin_per_lot = MARGIN_PROXY * spot * lot_size
    risk_lots = math.floor(capital * RISK_FRACTION / risk_per_lot)
    margin_lots = math.floor(capital * MARGIN_ALLOCATION / margin_per_lot)
    return max(0, min(risk_lots, margin_lots))


def selected_run(
    candidates: list[dict[str, str]], threshold: float, friction: float
) -> tuple[
    dict[str, object], list[dict[str, object]], list[dict[str, object]]
]:
    capital = INITIAL_CAPITAL
    peak = capital
    maximum_drawdown = 0.0
    weekly_returns: list[float] = []
    selected: list[dict[str, object]] = []
    weekly_equity: list[dict[str, object]] = []
    for row in candidates:
        capital_before = capital
        pnl_rupees = 0.0
        lots = 0
        is_selected = float(row["vrp_ratio"]) <= threshold
        if is_selected:
            lots = integer_lots(capital, row)
            if lots > 0:
                entry_market = float(row["gross_entry_mark"])
                exit_market = float(row["put_exit_market"]) + float(
                    row["call_exit_market"]
                )
                pnl_points = entry_market * (1 - friction) - exit_market * (1 + friction)
                pnl_rupees = pnl_points * int(float(row["lot_size"])) * lots
                capital += pnl_rupees
                selected.append(
                    {
                        "expiry": row["expiry"],
                        "entry_timestamp": row["entry_timestamp"],
                        "exit_timestamp": row["exit_timestamp"],
                        "vrp_ratio": float(row["vrp_ratio"]),
                        "lots": lots,
                        "pnl_points": pnl_points,
                        "pnl_rupees": pnl_rupees,
                        "capital_before": capital_before,
                        "capital_after": capital,
                    }
                )
        weekly_return = pnl_rupees / capital_before
        weekly_returns.append(weekly_return)
        peak = max(peak, capital)
        drawdown = capital / peak - 1
        maximum_drawdown = min(maximum_drawdown, drawdown)
        weekly_equity.append(
            {
                "expiry": row["expiry"],
                "selected": is_selected and lots > 0,
                "weekly_return": weekly_return,
                "equity": capital,
                "drawdown": drawdown,
            }
        )

    pnl_values = [float(row["pnl_rupees"]) for row in selected]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    first_entry = datetime.fromisoformat(candidates[0]["entry_timestamp"])
    last_exit = datetime.fromisoformat(candidates[-1]["exit_timestamp"])
    elapsed_days = max(1, (last_exit - first_entry).days)
    total_return = capital / INITIAL_CAPITAL - 1
    annualized_return = (capital / INITIAL_CAPITAL) ** (365 / elapsed_days) - 1
    weekly_volatility = statistics.stdev(weekly_returns) if len(weekly_returns) > 1 else 0.0
    annualized_volatility = weekly_volatility * math.sqrt(52)
    sharpe = (
        statistics.mean(weekly_returns) / weekly_volatility * math.sqrt(52)
        if weekly_volatility > 0
        else None
    )
    summary = {
        "threshold": threshold,
        "friction_per_transaction": friction,
        "candidate_expiries": len(candidates),
        "trades": len(selected),
        "total_pnl_rupees": capital - INITIAL_CAPITAL,
        "ending_capital": capital,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_ratio_zero_rf": sharpe,
        "maximum_realized_equity_drawdown": maximum_drawdown,
        "win_rate": len(wins) / len(selected) if selected else None,
        "average_payoff_rupees": statistics.mean(pnl_values) if pnl_values else None,
        "average_win_rupees": statistics.mean(wins) if wins else None,
        "average_loss_rupees": statistics.mean(losses) if losses else None,
        "payoff_ratio": statistics.mean(wins) / abs(statistics.mean(losses))
        if wins and losses
        else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if wins and losses else None,
    }
    return summary, weekly_equity, selected


def period_name(timestamp: str) -> str:
    value = datetime.fromisoformat(timestamp)
    if value.year == 2024:
        return "2024"
    if value < datetime(2025, 7, 1):
        return "2025 H1"
    return "2025 H2 through Nov"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    candidates = read_rows(args.candidates)

    base, equity, base_trades = selected_run(candidates, 1.00, 0.005)
    threshold_rows = [
        selected_run(candidates, threshold, 0.005)[0]
        for threshold in (0.80, 0.90, 1.00, 1.10, 1.20)
    ]
    cost_rows = [
        selected_run(candidates, 1.00, friction)[0]
        for friction in (0.0, 0.0025, 0.005, 0.01)
    ]

    subperiod_rows: list[dict[str, object]] = []
    for period in ("2024", "2025 H1", "2025 H2 through Nov"):
        rows = [row for row in base_trades if period_name(row["entry_timestamp"]) == period]
        pnl = [float(row["pnl_rupees"]) for row in rows]
        subperiod_rows.append(
            {
                "period": period,
                "trades": len(rows),
                "wins": sum(value > 0 for value in pnl),
                "win_rate": sum(value > 0 for value in pnl) / len(pnl) if pnl else None,
                "pnl_rupees": sum(pnl),
                "average_pnl_rupees": statistics.mean(pnl) if pnl else None,
            }
        )

    write_rows(args.output_root / "threshold_sensitivity.csv", threshold_rows)
    write_rows(args.output_root / "cost_sensitivity.csv", cost_rows)
    write_rows(args.output_root / "weekly_equity_curve.csv", equity)
    write_rows(args.output_root / "subperiod_summary.csv", subperiod_rows)
    output = {
        "base": base,
        "threshold_sensitivity": threshold_rows,
        "cost_sensitivity": cost_rows,
        "subperiod_summary": subperiod_rows,
        "notes": {
            "return_frequency": "one return per candidate expiry; skipped expiries receive zero",
            "annualization": "52 expiry observations per year; zero risk-free rate",
            "drawdown": "realized weekly equity, not intratrade mark-to-market",
            "subperiod_sizing": "continuous strategy capital and integer-lot sizing",
        },
    }
    (args.output_root / "performance_summary.json").write_text(
        json.dumps(output, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
