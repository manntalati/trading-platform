from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_ingest.jobs.bars import run_backfill
from tp_ingest.sources.fake import FakeSource
from tp_paper.broker import FakePaperBroker
from tp_paper.config import PaperBook, Sleeve
from tp_paper.jobs import Paper
from tp_paper.runtime import lake_prices
from tp_paper.store import PaperStore
from tp_risk.limits import Limits
from tp_risk.manager import RiskManager
from tp_risk.state import FileRiskState
from tp_strategies.library import build

REPO = Path(__file__).resolve().parents[3]
# Wednesday 31 July 2024 is a month end: MA timing trades. Bars run through Friday 2 August.
MONTH_END_EVENING = datetime(2024, 7, 31, 23, 0, tzinfo=UTC)  # 7pm ET
NEXT_MORNING = datetime(2024, 8, 1, 12, 0, tzinfo=UTC)  # 8am ET, before the 9:28 cutoff
AFTER_OPEN = datetime(2024, 8, 1, 14, 0, tzinfo=UTC)  # 10am ET
NEXT_EVENING = datetime(2024, 8, 1, 23, 0, tzinfo=UTC)
RSI_UNIVERSE = "SPY,QQQ,IWM,DIA,AAPL,MSFT,JPM,XOM"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    paper: Paper
    clock: Clock
    root: Path
    broker: FakePaperBroker

    month_end_evening = MONTH_END_EVENING
    next_morning = NEXT_MORNING
    after_open = AFTER_OPEN
    next_evening = NEXT_EVENING

    def at(self, now: datetime) -> datetime:
        self.clock.now = now
        return now


def make_book() -> PaperBook:
    return PaperBook(
        (
            Sleeve(build("ma-timing"), 20_000, "manual"),
            Sleeve(build("rsi2", {"universe": RSI_UNIVERSE}), 20_000, "auto"),
        )
    )


@pytest.fixture(scope="session")
def lake_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("paper-lake")
    symbols = sorted({s for sl in make_book().sleeves for s in sl.strategy.symbols()})
    run_backfill(
        Lake(root), FakeSource(), symbols, now=datetime(2024, 8, 2, 22, tzinfo=UTC), years=3
    )
    return root


@pytest.fixture
def env(lake_template: Path, tmp_path: Path) -> Env:
    import shutil

    root = tmp_path / "data"
    shutil.copytree(lake_template, root)
    lake = Lake(root)
    clock = Clock(MONTH_END_EVENING)
    broker = FakePaperBroker(root / "state" / "fake_broker.json", lake_prices(lake), now=clock)
    classifier = Classifier.load(REPO / "config" / "classifications.toml")
    risk = RiskManager(Limits(), classifier, FileRiskState.under(root))
    paper = Paper(PaperStore.under(root), broker, make_book(), lake, risk, classifier)
    return Env(paper, clock, root, broker)
