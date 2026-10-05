# research

- `src/tp_research/`: importable research code: Phase 0 exercises (`phase0/`) and notebook
  helpers (`notebook.py`). Linted, type-checked and tested like everything else.
- `notebooks/`: [jupytext](https://jupytext.readthedocs.io) notebooks stored as percent-format
  `.py` files: clean diffs, no committed outputs, linted by ruff.

## Running notebooks

From the repo root (data must be in the lake first: `uv run tp-data bars backfill`, or add
`--source fake` to try things without API keys):

```bash
uv sync --group research                              # jupyterlab, jupytext, matplotlib, ...
uv run --group research jupyter lab                   # open a .py notebook: right-click > Open With > Notebook
uv run --group research python research/notebooks/phase0_03_return_stats.py   # or run top to bottom
```

| Notebook | What it covers |
|---|---|
| `phase0_03_return_stats.py` | Phase 0 module 3: return/risk metrics and stylized facts for SPY, QQQ and 5 stocks |
