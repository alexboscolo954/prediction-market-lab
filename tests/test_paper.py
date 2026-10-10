import pytest

from pmlab.api import RewardConfig
from pmlab.paper import Snapshot, run
from tests.conftest import book

REWARD = RewardConfig.model_validate(
    {
        "condition_id": "c",
        "rewards_max_spread": 3,
        "rewards_min_size": 100,
        "total_daily_rate": 1440,
    }
)


def snap(t: float, yes_bid: float, yes_ask: float) -> Snapshot:
    return Snapshot(
        t,
        book("y", [(yes_bid, 500)], [(yes_ask, 500)]),
        book("n", [(round(1 - yes_ask, 2), 500)], [(round(1 - yes_bid, 2), 500)]),
    )


def test_rewards_accrue_per_minute_against_the_book() -> None:
    snaps = [snap(60 * i, 0.48, 0.52) for i in range(11)]  # 10 minutes, steady book
    r = run(snaps, REWARD, size=100)
    assert 0 < r.rewards < 10  # pool is $1/min; we share it with the resting book
    assert r.fills == []


def test_fill_when_the_market_trades_through_our_bid() -> None:
    snaps = [snap(0, 0.48, 0.52), snap(60, 0.48, 0.52), snap(120, 0.40, 0.45)]
    r = run(snaps, REWARD, size=100)
    assert [(f[1], f[2]) for f in r.fills] == [(0, 0.48)]  # mid 0.50 - 1.5c, floored
    assert r.inventory_yes == 100 and r.cash == pytest.approx(-48)


def test_yes_and_no_fills_merge_into_a_dollar() -> None:
    # both of our bids get hit: one full set, worth exactly $1 per share
    snaps = [snap(0, 0.48, 0.52), snap(60, 0.48, 0.52), snap(120, 0.30, 0.31)]
    snaps[-1].no.asks[0].price = 0.40  # NO side also trades down through our NO bid
    r = run(snaps, REWARD, size=100)
    assert r.inventory_yes == 0 and r.inventory_no == 0
    assert r.cash == pytest.approx(100 - 100 * (0.48 + 0.48))  # locked-in spread, positive
