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
# # Phase 0, module 3: returns and statistics
#
# Compute the core return and risk statistics for SPY, QQQ and five stocks from **our own clean
# daily bars** (split- and dividend-adjusted), and look at the stylized facts every model has to
# respect: volatility clusters, tails are fat, returns are (nearly) uncorrelated but their size is
# not, and prices are not stationary while returns roughly are.
#
# Prerequisites: `uv sync --group research` and data in the lake:
# `uv run tp-data bars backfill --years 5` (or add `--source fake` to try it without API keys;
# the synthetic data is a plain random walk, so the "stylized facts" below won't show up in it).
#
# Open in Jupyter (`uv run --group research jupyter lab`, then open this file as a notebook) or
# run top to bottom: `uv run --group research python research/notebooks/phase0_03_return_stats.py`.

# %%
import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

from tp_core import metrics as m
from tp_core.bars import close_matrix, load_bars
from tp_research.notebook import REFERENCE, SERIES, lake, use_style

use_style()
pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "JPM", "XOM"]
bars = load_bars(lake(), SYMBOLS)
prices = close_matrix(bars, adjusted=True)[SYMBOLS]
print(f"{prices.index[0].date()} .. {prices.index[-1].date()}, {len(prices)} sessions")

# %% [markdown]
# ## Simple vs log returns
#
# Simple returns aggregate **across assets** (a portfolio's return is the weighted sum of its
# holdings' simple returns). Log returns aggregate **across time** (the sum of daily log returns
# is the log of the total growth). They are almost identical for small moves and diverge for big
# ones.

# %%
simple = m.simple_returns(prices)
logr = m.log_returns(prices)

spy_total = (1 + simple["SPY"]).prod() - 1
print(f"SPY total return from simple returns:     {spy_total:.4%}")
print(f"SPY total return from summed log returns: {math.expm1(logr['SPY'].sum()):.4%}")
print(f"Naive sum of simple returns (wrong):      {simple['SPY'].sum():.4%}")

biggest = simple["NVDA"].abs().idxmax()
print(
    f"NVDA's biggest day {biggest.date()}: "
    f"simple {simple.loc[biggest, 'NVDA']:+.4f} vs log {logr.loc[biggest, 'NVDA']:+.4f}"
)

# %% [markdown]
# ## Tear sheet
#
# Annualized with 252 trading days; Sharpe/Sortino with a 0% risk-free rate (pass `rf=` to
# change). `beta` is versus SPY.

# %%
sheet = m.tear_sheet(simple, benchmark=simple["SPY"])
sheet.loc[
    [
        "cagr",
        "ann_volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "max_dd_duration",
        "calmar",
        "hit_rate",
        "autocorr_1",
        "skew",
        "excess_kurtosis",
        "tail_ratio_3sd",
        "beta",
    ]
]

# %% [markdown]
# ## Volatility clusters
#
# Calm periods and turbulent periods each persist. This is why volatility is forecastable (GARCH,
# HAR) even though returns mostly are not, and why position sizing should use a current
# volatility estimate rather than a long-run average.

# %%
rolling_vol = simple[["SPY", "NVDA"]].rolling(21).std() * math.sqrt(252)
fig, ax = plt.subplots()
for (name, series), color in zip(rolling_vol.items(), SERIES, strict=False):
    ax.plot(series.index, series, label=name, color=color)
ax.set_title("21-day realized volatility, annualized")
ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
ax.legend(loc="upper left")
plt.show()

# %% [markdown]
# ## Fat tails
#
# Under a normal distribution a move beyond 3 standard deviations happens on ~0.27% of days
# (about once every 1.5 years). `tail_ratio_3sd` in the tear sheet is how many times more often
# it actually happened. Excess kurtosis > 0 says the same thing.

# %%
z = (simple["SPY"] - simple["SPY"].mean()) / simple["SPY"].std()
grid = np.linspace(-6, 6, 400)
normal_pdf = np.exp(-(grid**2) / 2) / math.sqrt(2 * math.pi)
fig, ax = plt.subplots()
ax.hist(z, bins=120, density=True, color=SERIES[0], alpha=0.85, label="SPY daily returns")
ax.plot(grid, normal_pdf, color=REFERENCE, ls="--", label="normal distribution")
ax.set_yscale("log")
ax.set_ylim(1e-4, 1)
ax.set_xlabel("standard deviations from the mean")
ax.set_title("SPY daily returns vs a normal distribution (log scale shows the tails)")
ax.legend()
plt.show()

# %% [markdown]
# ## Autocorrelation: returns vs the size of returns
#
# Daily returns of liquid assets are close to uncorrelated with their own past (otherwise they
# would be easy to trade). Absolute returns are clearly positively correlated: that is the
# volatility clustering above, in one number.

# %%
lags = range(1, 6)
pd.DataFrame(
    {
        "returns": [m.autocorrelation(simple["SPY"], lag) for lag in lags],
        "abs(returns)": [m.autocorrelation(simple["SPY"].abs(), lag) for lag in lags],
    },
    index=pd.Index(lags, name="lag (days)"),
)

# %% [markdown]
# ## Stationarity
#
# The Augmented Dickey-Fuller test's null hypothesis is "has a unit root" (non-stationary). A
# p-value above 0.05 means we can't reject it. Prices should fail the test, returns should pass:
# that is why models are fit on returns, not prices.

# %%
rows = {}
for symbol in ["SPY", "NVDA"]:
    log_price = np.log(prices[symbol].dropna())
    rows[f"{symbol} log price"] = adfuller(log_price, result_object=True).pvalue
    rows[f"{symbol} daily return"] = adfuller(simple[symbol].dropna(), result_object=True).pvalue
pd.Series(rows, name="ADF p-value").to_frame()

# %% [markdown]
# ## For the module note
#
# - Which of the seven had the best Sharpe, and does that survive a different start date?
# - How much bigger was NVDA's max drawdown than SPY's, and how long did it last?
# - How often did SPY move more than 3 sigma, vs the normal prediction?
# - Why does a strategy with a 2.0 backtest Sharpe often trade at 0.5 live? (Hint: estimation
#   error on the mean is enormous; see how much Sharpe moves when you change the window.)
