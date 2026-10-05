# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Strategy 1: Faber's 10-month moving-average timing
#
# Spec: `docs/strategies/01-ma-timing.md`. Rule: hold an asset while its month-end close is above
# its 10-month average, otherwise hold cash. We check Faber's headline claim (**similar return to
# buy-and-hold, lower volatility, much smaller drawdowns**) on:
#
# 1. SPY from our own lake (Alpaca, 2016 onward),
# 2. a long S&P 500 total-return history from Yahoo (1988 onward) with T-bill cash from FRED,
# 3. Faber's 5-asset GTAA portfolio (SPY, EFA, IEF, VNQ, DBC) from the lake,
#
# then test how fragile the result is to the lookback and to costs.
#
# **Locked test set.** Per the backtest protocol, data from 2024 onward is held out. Leave
# `TOUCH_TEST_SET = False` while iterating; flip it once, at the end, and record what happened.
#
# Prerequisites: `uv sync --group research`, and bars in the lake (`uv run tp-data bars backfill`).
# Section 2 needs internet access to Yahoo and FRED and is skipped if they are unreachable.

# %%
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import display

from tp_core import metrics as m
from tp_core.bars import close_matrix, load_bars
from tp_research.notebook import REFERENCE, SERIES, lake, use_style
from tp_strategies.ma_timing import BacktestResult, equal_weight_monthly, ma_timing

use_style()
pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

TOUCH_TEST_SET = False
TEST_START = pd.Timestamp("2024-01-01")
MONTHS = 10
COST_BPS = 5.0


def held_out(prices: pd.DataFrame) -> pd.DataFrame:
    """Drop the locked test window unless we've decided to look at it."""
    return prices if TOUCH_TEST_SET else prices[prices.index < TEST_START]


def compare(
    prices: pd.DataFrame, cash: pd.Series | None = None
) -> tuple[dict[str, BacktestResult], pd.DataFrame]:
    """Timing with next-day and same-day execution vs an aligned buy-and-hold benchmark."""
    runs = {
        "timing (next close)": ma_timing(
            prices, months=MONTHS, lag_days=1, cost_bps=COST_BPS, cash_returns=cash
        ),
        "timing (paper: same close)": ma_timing(
            prices, months=MONTHS, lag_days=0, cost_bps=COST_BPS, cash_returns=cash
        ),
    }
    first_signal = runs["timing (next close)"].signals.index[MONTHS - 1]
    runs["buy and hold"] = equal_weight_monthly(
        prices, start_after=first_signal, lag_days=1, cost_bps=COST_BPS
    )
    start = max(r.returns.index[0] for r in runs.values())
    returns = pd.DataFrame({name: r.returns.loc[start:] for name, r in runs.items()})
    sheet = m.tear_sheet(returns)
    sheet.loc["avg_exposure"] = [runs[c].exposure.loc[start:].mean() for c in returns]
    sheet.loc["trades_per_year"] = [runs[c].trades_per_year() for c in returns]
    return runs, sheet


def faber_claims(sheet: pd.DataFrame, strategy: str = "timing (next close)") -> pd.Series:
    t, b = sheet[strategy], sheet["buy and hold"]
    return pd.Series(
        {
            "lower volatility": t["ann_volatility"] < b["ann_volatility"],
            "shallower max drawdown": t["max_drawdown"] > b["max_drawdown"],
            "CAGR within 2 points of buy-and-hold": abs(t["cagr"] - b["cagr"]) < 0.02,
            "higher Sharpe": t["sharpe"] > b["sharpe"],
        },
        name=strategy,
    )


def plot_growth_and_drawdown(runs: dict[str, BacktestResult], title: str) -> None:
    strategy, bench = runs["timing (next close)"], runs["buy and hold"]
    start = max(strategy.returns.index[0], bench.returns.index[0])
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6), sharex=True, height_ratios=[3, 2])
    for result, label, color in [
        (bench, "buy and hold", REFERENCE),
        (strategy, "10-month MA timing", SERIES[0]),
    ]:
        r = result.returns.loc[start:]
        top.plot(m.wealth_index(r), label=label, color=color)
        bottom.plot(m.drawdowns(r), label=label, color=color)
    top.set_yscale("log")
    for axis in (top.yaxis.set_major_formatter, top.yaxis.set_minor_formatter):
        axis(lambda v, _: f"${v:,.2f}")
    top.set_title(f"{title}: growth of $1 (log scale)")
    top.legend(loc="upper left")
    bottom.set_title("Drawdown from peak")
    bottom.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    fig.tight_layout()
    plt.show()


# %% [markdown]
# ## 1. SPY from our lake

# %%
spy = held_out(close_matrix(load_bars(lake(), ["SPY"])))
print(f"SPY {spy.index[0].date()} .. {spy.index[-1].date()} ({len(spy)} sessions)")
spy_runs, spy_sheet = compare(spy)
spy_sheet

# %%
faber_claims(spy_sheet).to_frame()

# %%
plot_growth_and_drawdown(spy_runs, "SPY")

# %%
spy_runs["timing (next close)"].trades

# %% [markdown]
# ## 2. Long history: S&P 500 total return since 1988, T-bills as cash
#
# Alpaca's history starts in 2016, too short to say much about a strategy that trades a few
# times a year. Yahoo's `^SP500TR` index (dividends reinvested) goes back to 1988, and FRED's
# 3-month T-bill rate (DTB3, annualized discount yield in percent) stands in for cash, as in the
# paper. Yahoo is fine for research; it never feeds anything that trades.

# %%
try:
    import yfinance as yf

    sp500tr = yf.download("^SP500TR", start="1988-01-01", auto_adjust=False, progress=False)
    sp500tr = sp500tr["Close"].rename(columns={"^SP500TR": "SP500TR"})
    sp500tr.index = pd.DatetimeIndex(sp500tr.index).tz_localize(None)
    dtb3 = pd.read_csv(
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3",
        index_col=0,
        parse_dates=True,
        na_values=".",
    ).iloc[:, 0]
    # Annual percent -> daily simple return, carried forward over FRED's missing days.
    tbill_daily = ((1 + dtb3.ffill() / 100) ** (1 / 252) - 1).reindex(sp500tr.index).ffill()
    long_history = held_out(sp500tr.dropna())
except Exception as exc:  # no network, Yahoo changed its API, ...
    long_history = None
    print(f"skipped long-history section: {type(exc).__name__}: {exc}")

# %%
if long_history is not None and len(long_history) > 300:
    long_runs, long_sheet = compare(long_history, cash=tbill_daily)
    display(long_sheet)
    display(faber_claims(long_sheet).to_frame())
    plot_growth_and_drawdown(long_runs, "S&P 500 total return")

# %% [markdown]
# ## 3. Faber's 5-asset GTAA: SPY, EFA, IEF, VNQ, DBC
#
# Each asset gets a 20% slice and is timed independently; the benchmark is the same five assets
# equal-weighted and rebalanced monthly.

# %%
GTAA = ["SPY", "EFA", "IEF", "VNQ", "DBC"]
gtaa = held_out(close_matrix(load_bars(lake(), GTAA))[GTAA].dropna())
gtaa_runs, gtaa_sheet = compare(gtaa)
gtaa_sheet

# %%
faber_claims(gtaa_sheet).to_frame()

# %%
plot_growth_and_drawdown(gtaa_runs, "GTAA 5-asset")

# %% [markdown]
# ## 4. Parameter and cost sensitivity
#
# A robust result degrades smoothly around the chosen lookback; a cliff means it's fit to noise.
# Costs are doubled and quadrupled to see how much edge survives.

# %%
runs = {
    (months, cost): ma_timing(gtaa, months=months, lag_days=1, cost_bps=cost).returns
    for months in range(6, 15)
    for cost in (COST_BPS, 2 * COST_BPS, 4 * COST_BPS)
}
# Longer lookbacks need longer warm-ups: score every variant on the same window.
common_start = max(r.index[0] for r in runs.values())
grid = pd.DataFrame(
    [
        {
            "months": months,
            "cost_bps": cost,
            "sharpe": m.sharpe_ratio(r.loc[common_start:]),
            "max_drawdown": m.max_drawdown(r.loc[common_start:]),
        }
        for (months, cost), r in runs.items()
    ]
)
print(f"all variants scored from {common_start.date()}")
grid.pivot(index="months", columns="cost_bps", values="sharpe").round(2)

# %%
fig, ax = plt.subplots()
for cost, color in zip((COST_BPS, 2 * COST_BPS, 4 * COST_BPS), SERIES, strict=True):
    subset = grid[grid["cost_bps"] == cost]
    ax.plot(
        subset["months"], subset["sharpe"], marker="o", ms=6, color=color, label=f"{cost:g} bps"
    )
ax.axvline(MONTHS, color=REFERENCE, lw=1, ls="--")
ax.set_xlabel("moving-average lookback (months)")
ax.set_title("GTAA timing: Sharpe ratio by lookback and cost")
ax.legend(title="cost per trade")
plt.show()

# %% [markdown]
# ## Conclusions (fill in)
#
# - Do the paper's claims hold on 2016+ SPY? On 1988+ S&P 500? On GTAA?
# - How much does next-close execution cost versus the paper's same-close fills?
# - Is 10 months special, or is the plateau broad?
# - Copy the paper's own table into `docs/strategies/01-ma-timing.md` and compare.
# - Only after all of the above: set `TOUCH_TEST_SET = True`, run once, and record the result.
