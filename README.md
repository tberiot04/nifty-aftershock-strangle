# NIFTY Aftershock Strangle

I built this project to test whether recent realized volatility can identify
weeks in which NIFTY option prices create attractive short-strangle opportunities.

The first version sold options when the implied-to-realized move ratio was high
and lost money. Investigating that failure led me to test the opposite regime,
which produced the final strategy described here.

## Strategy

The strategy compares the at-the-money straddle's priced move with a lagged
20-session estimate of the expected absolute realized move. It enters a short
strangle only when:

```text
option-implied move / expected absolute realized move <= 1.0
```

Signals use the 15:19 close on the preceding NIFTY expiry and entries use the
15:20 open. Positions exit at 50% premium decay, a 2x premium stop, or 15:20 on
expiry. Every purchase and sale includes a 0.50% adverse price adjustment.

## Base-case result

| Metric | Result |
|---|---:|
| Selected trades | 26 of 95 expiries |
| Net P&L | INR 47,142 |
| Total return | 2.36% |
| Annualized return | 1.30% |
| Annualized volatility | 1.83% |
| Zero-risk-free-rate Sharpe | 0.71 |
| Maximum realized drawdown | -2.45% |
| Win rate | 76.92% |
| Profit factor | 1.51 |

These results are exploratory. The final hypothesis was developed after an
initial high-ratio formulation failed on the same dataset, so this is not a
pristine out-of-sample result.

## Repository structure

```text
NIFTY_Aftershock_Strangle_Backtest.ipynb
README.md
deliverables/
  notebook_outputs/
    strategy_trades.csv
    strategy_skips.csv
    benchmark_trades.csv
    equity_curve.svg
    drawdown.svg
src/
  nifty_strategy.py
scripts/
  audit_nifty_data.py
  analyze_backtest.py
  run_prototype.py
  validate_prototype.py
tests/
  test_nifty_strategy.py
```

The underlying market dataset is intentionally excluded because it is large and
may not be redistributable.

## Data setup

Either place the supplied ZIP archives in `data/raw/nifty`, or set the data
location before opening the notebook:

```bash
export NIFTY_DATA_ROOT="/path/to/nifty"
```

The raw folder must contain expiry archives named like `20240208.zip`. Data
after 30 November 2025 is rejected by the strategy code.

## Running the notebook

Open `NIFTY_Aftershock_Strangle_Backtest.ipynb` in VS Code with the Microsoft
Python and Jupyter extensions installed, select a Python 3 kernel, and choose
**Run All**. The notebook uses only Python's standard library.

Generated files are written to `deliverables/notebook_outputs/`.

## Running the checks

```bash
python3 -m unittest discover -s tests -v
```

The independent raw-bar reconciliation can be run after the notebook has
generated its trade logs:

```bash
python3 scripts/validate_prototype.py \
  --data-root "$NIFTY_DATA_ROOT" \
  --filter-direction below \
  --vrp-threshold 1.0 \
  --disable-trend-filter \
  deliverables/notebook_outputs/strategy_trades.csv \
  deliverables/notebook_outputs/benchmark_trades.csv
```

## Limitations

- Minute bars do not provide bid/ask spreads, market depth, or exact execution.
- The margin assumption is a transparent proxy rather than a broker margin model.
- A close-based stop cannot guarantee the stop execution price during gaps.
- Twenty-six selected trades are insufficient to characterize rare tail losses.
- Threshold checks reuse the same historical sample and are not a future holdout.
