"""A small grammar of timing signals, and a genetic search over it (the plan's Phase 4 strategy
search: "genetic programming over a constrained grammar of signals").

A rule says, at each close, which assets of the universe to hold:

    ma(N)       the close above its N-day average           N: 20, 50, 100, 150, 200
    mom(N)      the N-day return above zero                 N: 21, 63, 126, 252
    cross(F,S)  the F-day average above the S-day average   (20,50) (20,100) (50,150) (50,200)
    calm(N)     N-day volatility below its median over the past year   N: 21, 63
    rsi(N,K)    RSI(N) above K                              N: 14; K: 40, 50, 60
    and(a,b)  or(a,b)  not(a)

The assets it says yes to are held at equal weight (one share of the universe each, the rest in
cash), checked daily, weekly or at month end. Every value uses closes up to that day only.

``evolve`` breeds rules on the training period alone: a population of random rules, scored by
their training Sharpe less a penalty per node (simpler rules win ties), improved by crossover and
mutation for some generations. Every rule it ever scores is returned, so the search can count
them all as trials. The final population then goes through the same guards as the fixed rule
sets (``tp_strategies.search``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tp_core import metrics
from tp_core.indicators import wilder_rsi

Node = tuple[Any, ...]  # ("ma", 200) / ("and", a, b) / ("not", a)

PRIMITIVES: dict[str, list[tuple[int, ...]]] = {
    "ma": [(n,) for n in (20, 50, 100, 150, 200)],
    "mom": [(n,) for n in (21, 63, 126, 252)],
    "cross": [(20, 50), (20, 100), (50, 150), (50, 200)],
    "calm": [(21,), (63,)],
    "rsi": [(14, k) for k in (40, 50, 60)],
}
REBALANCE = ("daily", "weekly", "monthly")
MAX_DEPTH = 3
LOOKBACK = (
    320  # sessions of closes a rule needs: 252 for mom/calm's year, plus the volatility window
)


# -- text ------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*([a-z]+|\d+|[(),])")


def parse(text: str) -> Node:
    """``"and(ma(200),not(calm(21)))"`` -> its tree. Raises ValueError on anything else."""
    tokens = list(_TOKEN.findall(text.replace(" ", "")))
    if "".join(tokens) != text.replace(" ", ""):
        raise ValueError(f"not a rule: {text!r}")
    node, rest = _parse(tokens)
    if rest:
        raise ValueError(f"not a rule: {text!r} (left over: {''.join(rest)})")
    return node


def _parse(tokens: list[str]) -> tuple[Node, list[str]]:
    if not tokens or not tokens[0].isalpha():
        raise ValueError("expected a name")
    name, rest = tokens[0], tokens[1:]
    if not rest or rest[0] != "(":
        raise ValueError(f"{name}: expected (")
    rest = rest[1:]
    args: list[Any] = []
    while rest and rest[0] != ")":
        if rest[0].isdigit():
            args.append(int(rest[0]))
            rest = rest[1:]
        else:
            child, rest = _parse(rest)
            args.append(child)
        if rest and rest[0] == ",":
            rest = rest[1:]
    if not rest:
        raise ValueError(f"{name}: expected )")
    node = (name, *args)
    _check(node)
    return node, rest[1:]


def _check(node: Node) -> None:
    name, args = node[0], node[1:]
    if name in PRIMITIVES:
        if tuple(args) not in PRIMITIVES[name]:
            raise ValueError(f"{name}{args}: allowed {PRIMITIVES[name]}")
    elif name in ("and", "or"):
        if len(args) != 2 or not all(isinstance(a, tuple) for a in args):
            raise ValueError(f"{name} takes two rules")
    elif name == "not":
        if len(args) != 1 or not isinstance(args[0], tuple):
            raise ValueError("not takes one rule")
    else:
        raise ValueError(f"unknown signal {name!r}")


def to_text(node: Node) -> str:
    name, args = node[0], node[1:]
    return f"{name}({','.join(to_text(a) if isinstance(a, tuple) else str(a) for a in args)})"


def size(node: Node) -> int:
    return 1 + sum(size(a) for a in node[1:] if isinstance(a, tuple))


def depth(node: Node) -> int:
    kids = [depth(a) for a in node[1:] if isinstance(a, tuple)]
    return 1 + (max(kids) if kids else 0)


def simplify(node: Node) -> Node:
    """not(not(x)) -> x; and(x,x) / or(x,x) -> x; operands of and/or in a fixed order."""
    name = node[0]
    if name == "not":
        inner = simplify(node[1])
        return inner[1] if inner[0] == "not" else ("not", inner)
    if name in ("and", "or"):
        a, b = sorted((simplify(node[1]), simplify(node[2])), key=to_text)
        return a if a == b else (name, a, b)
    return node


# -- signals ---------------------------------------------------------------------------------------


def signal(
    node: Node, closes: pd.DataFrame, cache: dict[str, pd.DataFrame] | None = None
) -> pd.DataFrame:
    """True where the rule holds the asset (decided at that close)."""
    cache = {} if cache is None else cache
    key = to_text(node)
    if key in cache:
        return cache[key]
    name, args = node[0], node[1:]
    if name == "and":
        out = signal(args[0], closes, cache) & signal(args[1], closes, cache)
    elif name == "or":
        out = signal(args[0], closes, cache) | signal(args[1], closes, cache)
    elif name == "not":
        out = ~signal(args[0], closes, cache)
    else:
        out = _primitive(name, args, closes).fillna(False).astype(bool)
    cache[key] = out
    return out


def _primitive(name: str, args: tuple[Any, ...], closes: pd.DataFrame) -> pd.DataFrame:
    if name == "ma":
        n = args[0]
        return closes > closes.rolling(n, min_periods=n).mean()
    if name == "mom":
        past = closes.shift(args[0])
        return (closes / past - 1 > 0).where(past.notna())
    if name == "cross":
        f, s = args
        fast, slow = (
            closes.rolling(f, min_periods=f).mean(),
            closes.rolling(s, min_periods=s).mean(),
        )
        return (fast > slow).where(slow.notna())
    if name == "calm":
        n = args[0]
        vol = closes.pct_change(fill_method=None).rolling(n, min_periods=n).std()
        typical = vol.rolling(252, min_periods=126).median()
        calm: pd.DataFrame = (vol < typical).where(typical.notna())
        return calm
    n, k = args
    rsi = wilder_rsi(closes, n)
    above: pd.DataFrame = (rsi > k).where(rsi.notna())
    return above


def _rebalance_days(index: pd.DatetimeIndex, every: str) -> pd.Series:
    """True on the sessions a rule is re-checked."""
    if every == "daily":
        return pd.Series(True, index=index)
    keys = (
        (index.year, index.month)
        if every == "monthly"
        else (index.isocalendar().year, index.isocalendar().week)
    )
    last = pd.Series(index, index=index).groupby(list(keys)).transform("max")
    return pd.Series(index == last.to_numpy(), index=index)


def rule_weights(node: Node, closes: pd.DataFrame, every: str = "monthly",
                 cache: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:  # fmt: skip
    """Each asset's weight at each close: 1/assets while the rule holds it, re-checked
    ``every`` daily, weekly or monthly (held between checks)."""
    on = signal(node, closes, cache).astype(float) / max(len(closes.columns), 1)
    if every == "daily":
        return on
    checks = _rebalance_days(pd.DatetimeIndex(closes.index), every).to_numpy()
    held: pd.DataFrame = on.loc[checks].reindex(on.index).ffill().fillna(0.0)
    return held


def latest(node: Node, closes: pd.DataFrame) -> pd.Series:
    """The rule's verdict per asset at the last close."""
    return signal(node, closes).iloc[-1]


# -- breeding --------------------------------------------------------------------------------------


def random_rule(rng: np.random.Generator, max_depth: int = MAX_DEPTH) -> Node:
    if max_depth <= 1 or rng.random() < 0.45:
        name = str(rng.choice(sorted(PRIMITIVES)))
        options = PRIMITIVES[name]
        return (name, *options[int(rng.integers(len(options)))])
    op = rng.choice(["and", "or", "not"], p=[0.45, 0.4, 0.15])
    if op == "not":
        return ("not", random_rule(rng, max_depth - 1))
    return (str(op), random_rule(rng, max_depth - 1), random_rule(rng, max_depth - 1))


def _paths(node: Node, here: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
    out = [here]
    for i, a in enumerate(node[1:], start=1):
        if isinstance(a, tuple):
            out += _paths(a, (*here, i))
    return out


def _get(node: Node, path: tuple[int, ...]) -> Node:
    for i in path:
        node = node[i]
    return node


def _put(node: Node, path: tuple[int, ...], new: Node) -> Node:
    if not path:
        return new
    i = path[0]
    return (*node[:i], _put(node[i], path[1:], new), *node[i + 1 :])


def mutate(node: Node, rng: np.random.Generator) -> Node:
    paths = _paths(node)
    path = paths[int(rng.integers(len(paths)))]
    target = _get(node, path)
    if target[0] in PRIMITIVES and rng.random() < 0.5:  # a new parameter for the same signal
        options = PRIMITIVES[target[0]]
        new = (target[0], *options[int(rng.integers(len(options)))])
    else:
        new = random_rule(rng, max(1, MAX_DEPTH - len(path)))
    out = _put(node, path, new)
    return out if depth(out) <= MAX_DEPTH else node


def crossover(a: Node, b: Node, rng: np.random.Generator) -> Node:
    pa, pb = _paths(a), _paths(b)
    child = _put(a, pa[int(rng.integers(len(pa)))], _get(b, pb[int(rng.integers(len(pb)))]))
    return child if depth(child) <= MAX_DEPTH else a


@dataclass(frozen=True)
class Candidate:
    rule: Node
    rebalance: str

    @property
    def text(self) -> str:
        return to_text(self.rule)

    @property
    def key(self) -> str:
        return f"{self.rebalance}:{self.text}"


@dataclass
class Evolution:
    population: list[tuple[Candidate, float]]  # the last generation, best first, with fitness
    evaluated: dict[str, pd.Series] = field(default_factory=dict)  # every rule scored: returns
    candidates: dict[str, Candidate] = field(default_factory=dict)
    generations: int = 0


def evolve(
    closes: pd.DataFrame,
    train: pd.Series,
    returns_of: Callable[[pd.DataFrame], pd.Series],
    *,
    population: int = 60,
    generations: int = 15,
    penalty: float = 0.03,
    seed: int = 0,
) -> Evolution:
    """Breed rules on the ``train`` days (a boolean Series on ``closes``' index). ``returns_of``
    turns a weights frame into daily returns (with costs). Fitness: training Sharpe less
    ``penalty`` per node beyond the first."""
    rng = np.random.default_rng(seed)
    cache: dict[str, pd.DataFrame] = {}
    evo = Evolution(population=[])
    mask = train.reindex(closes.index, fill_value=False).to_numpy(dtype=bool)

    def fitness(c: Candidate) -> float:
        if c.key not in evo.evaluated:
            weights = rule_weights(c.rule, closes, c.rebalance, cache)
            evo.evaluated[c.key] = returns_of(weights)
            evo.candidates[c.key] = c
        r = evo.evaluated[c.key][mask]
        if r.abs().sum() == 0:
            return -math.inf  # never invested
        sharpe = metrics.sharpe_ratio(r)
        if sharpe != sharpe:
            return -math.inf
        return float(sharpe) - penalty * (size(c.rule) - 1)

    def fresh() -> Candidate:
        return Candidate(simplify(random_rule(rng)), str(rng.choice(REBALANCE)))

    pop: dict[str, Candidate] = {}
    while len(pop) < population:
        c = fresh()
        pop.setdefault(c.key, c)
    scored = sorted(((c, fitness(c)) for c in pop.values()), key=lambda x: -x[1])

    def pick(pool: list[tuple[Candidate, float]]) -> Candidate:
        """Tournament selection: the fittest of three drawn at random."""
        entrants = [pool[int(rng.integers(len(pool)))] for _ in range(3)]
        return max(entrants, key=lambda x: x[1])[0]

    for _ in range(generations):
        nxt: dict[str, Candidate] = {c.key: c for c, _ in scored[:2]}  # elitism

        attempts = 0
        while len(nxt) < population and attempts < population * 20:
            attempts += 1
            parent = pick(scored)
            rule, rebalance = parent.rule, parent.rebalance
            if rng.random() < 0.7:
                rule = crossover(rule, pick(scored).rule, rng)
            if rng.random() < 0.3:
                rule = mutate(rule, rng)
            if rng.random() < 0.1:
                rebalance = str(rng.choice(REBALANCE))
            child = Candidate(simplify(rule), rebalance)
            nxt.setdefault(child.key, child)
        scored = sorted(((c, fitness(c)) for c in nxt.values()), key=lambda x: -x[1])
        evo.generations += 1
    evo.population = [(c, f) for c, f in scored if f > -math.inf]
    return evo
