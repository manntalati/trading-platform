from __future__ import annotations

import pandas as pd
import pytest

from tp_ml.testing import random_bars


@pytest.fixture
def bars() -> dict[str, pd.DataFrame]:
    return random_bars(["SPY", "AAA", "BBB", "CCC", "DDD", "EEE"])
