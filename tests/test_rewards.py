"""Reward math against the worked example in Polymarket's docs
(docs.polymarket.com/programs/liquidity-rewards): midpoint 0.50, max spread 3 cents."""

import pytest

from pmlab.rewards import Order, adjusted_midpoint, estimated_share, order_score, q_min, side_scores
from tests.conftest import book

V = 0.03


def test_docs_example_q_one() -> None:
    # 100 bid on m @ 0.49, 200 bid on m @ 0.48, 100 ask on m' @ 0.51
    orders = [
        Order("m", "bid", 0.49, 100),
        Order("m", "bid", 0.48, 200),
        Order("m'", "ask", 0.51, 100),
    ]
    q_one, q_two = side_scores(orders, mid=0.50, v=V)
    assert q_one == pytest.approx((2 / 3) ** 2 * 100 + (1 / 3) ** 2 * 200 + (2 / 3) ** 2 * 100)
    assert q_one == pytest.approx(111.11, abs=0.01)
    assert q_two == 0  # nothing on the opposite side


def test_docs_example_q_no_arithmetic() -> None:
    # The page's second sample: spreads 1.5, 2 and 0.5 cents with sizes 100, 100, 200
    total = order_score(3, 1.5) * 100 + order_score(3, 2) * 100 + order_score(3, 0.5) * 200
    assert total == pytest.approx(175.0)


def test_score_is_zero_at_and_beyond_max_spread() -> None:
    assert order_score(V, V) == 0 and order_score(V, 0.05) == 0
    assert order_score(V, 0) == 1


def test_orders_below_min_size_do_not_count() -> None:
    q = side_scores([Order("m", "bid", 0.49, 99)], 0.5, V, min_size=100)
    assert q == (0, 0)


def test_single_sided_scores_a_third_in_the_safe_zone() -> None:
    assert q_min(90, 0, mid=0.5) == pytest.approx(30)  # c = 3


def test_single_sided_scores_nothing_in_the_extreme_zone() -> None:
    assert q_min(90, 0, mid=0.95) == 0
    assert q_min(90, 60, mid=0.95) == 60


def test_two_sided_beats_one_sided() -> None:
    one = q_min(*side_scores([Order("m", "bid", 0.49, 100)], 0.5, V), 0.5)
    two = q_min(
        *side_scores([Order("m", "bid", 0.49, 100), Order("m'", "bid", 0.49, 100)], 0.5, V), 0.5
    )
    assert two == pytest.approx(3 * one)


def test_adjusted_midpoint_ignores_small_levels() -> None:
    b = book("y", bids=[(0.40, 5000), (0.47, 10)], asks=[(0.60, 5000), (0.49, 10)])
    assert adjusted_midpoint(b, min_size=1000) == pytest.approx(0.50)
    assert adjusted_midpoint(b, min_size=0) == pytest.approx(0.48)


def test_estimated_share_bounds() -> None:
    ours = [Order("m", "bid", 0.49, 100), Order("m'", "bid", 0.49, 100)]
    assert estimated_share(ours, [], 0.5, V, 0) == 1.0
    assert estimated_share([], ours, 0.5, V, 0) == 0.0
    assert estimated_share(ours, ours, 0.5, V, 0) == pytest.approx(0.5)
