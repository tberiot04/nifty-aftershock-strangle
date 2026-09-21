from __future__ import annotations

import math
import statistics
import sys
import unittest
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nifty_strategy import (  # noqa: E402
    DATA_CUTOFF,
    Bar,
    StrategyConfig,
    adverse_buy_fill,
    adverse_sell_fill,
    calculate_regime_features,
    find_common_fill_timestamp,
    lot_size_for_expiry,
    paired_strikes,
    position_size,
)


def bar_at(timestamp: datetime) -> Bar:
    return Bar(timestamp, 1.0, 1.0, 1.0, 1.0, 1, 1, "TEST")


class StrategyMathTests(unittest.TestCase):
    def test_regime_features_match_independent_formula(self) -> None:
        returns = [0.01, -0.005, 0.007, -0.003, 0.002] * 5
        closes = [100.0]
        for value in returns:
            closes.append(closes[-1] * math.exp(value))
        spot = 105.0
        straddle = 3.5
        result = calculate_regime_features(closes, spot, straddle, 5, 20)
        expected_sigma = statistics.stdev(returns[-20:])
        expected_abs = expected_sigma * math.sqrt(5) * math.sqrt(2 / math.pi)
        expected_trend = math.log(closes[-1] / closes[-6])
        self.assertAlmostEqual(result.daily_sigma, expected_sigma, places=14)
        self.assertAlmostEqual(
            result.expected_abs_realized_move_pct, expected_abs, places=14
        )
        self.assertAlmostEqual(result.implied_move_pct, straddle / spot, places=14)
        self.assertAlmostEqual(result.vrp_ratio, (straddle / spot) / expected_abs, places=12)
        self.assertAlmostEqual(
            result.trend_z,
            abs(expected_trend) / (expected_sigma * math.sqrt(5)),
            places=14,
        )

    def test_adverse_fills_are_conservative(self) -> None:
        self.assertAlmostEqual(adverse_sell_fill(100.0, 0.005), 99.5)
        self.assertAlmostEqual(adverse_buy_fill(100.0, 0.005), 100.5)

    def test_position_size_uses_tighter_cap(self) -> None:
        config = StrategyConfig(
            initial_capital=2_000_000,
            risk_fraction=0.01,
            margin_allocation_fraction=0.25,
            margin_proxy_fraction=0.10,
        )
        # Risk cap: floor(20,000 / (100 * 25 * 1.1)) = 7.
        # Margin cap: floor(500,000 / (0.10 * 20,000 * 25)) = 10.
        self.assertEqual(position_size(2_000_000, 20_000, 100, 25, config), 7)

    def test_historical_lot_boundaries(self) -> None:
        self.assertEqual(lot_size_for_expiry(date(2024, 4, 25)), 50)
        self.assertEqual(lot_size_for_expiry(date(2024, 5, 2)), 25)
        self.assertEqual(lot_size_for_expiry(date(2024, 12, 26)), 25)
        self.assertEqual(lot_size_for_expiry(date(2025, 1, 2)), 75)
        self.assertEqual(lot_size_for_expiry(date(2025, 1, 30)), 25)
        self.assertEqual(lot_size_for_expiry(date(2025, 2, 6)), 75)

    def test_common_fill_is_not_before_planned_time(self) -> None:
        planned = datetime(2024, 7, 1, 15, 20)
        call = {
            datetime(2024, 7, 1, 15, 19): bar_at(datetime(2024, 7, 1, 15, 19)),
            datetime(2024, 7, 1, 15, 21): bar_at(datetime(2024, 7, 1, 15, 21)),
        }
        put = {
            datetime(2024, 7, 1, 15, 20): bar_at(datetime(2024, 7, 1, 15, 20)),
            datetime(2024, 7, 1, 15, 21): bar_at(datetime(2024, 7, 1, 15, 21)),
        }
        self.assertEqual(find_common_fill_timestamp(call, put, planned, 5), planned.replace(minute=21))

    def test_paired_strikes_exclude_one_sided_contracts(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "20240704.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in (
                    "22000CE_20240704.csv",
                    "22000PE_20240704.csv",
                    "22100CE_20240704.csv",
                    "21900PE_20240704.csv",
                ):
                    archive.writestr(name, "header\n")
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(paired_strikes(archive, date(2024, 7, 4)), [22000])

    def test_invalid_config_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StrategyConfig(stop_multiple=1.0).validate()

    def test_data_cutoff_is_hard_coded(self) -> None:
        self.assertEqual(DATA_CUTOFF, date(2025, 11, 30))


if __name__ == "__main__":
    unittest.main()
