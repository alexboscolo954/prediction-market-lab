"""Quoting invariants, checked over many random market states with Hypothesis."""

import math

from hypothesis import given
from hypothesis import strategies as st

from pmlab.quoting import QuoteParams, floor_to_tick, plan

ticks = st.sampled_from([0.01, 0.001])


@st.composite
def params(draw: st.DrawFn) -> QuoteParams:
    tick = draw(ticks)
    mid = draw(st.floats(0.02, 0.98))
    asks = st.none() | st.floats(0.01, 0.99)
    return QuoteParams(
        mid=mid,
        max_spread=draw(st.sampled_from([0.01, 0.025, 0.03, 0.045, 0.065])),
        tick=tick,
        size=draw(st.floats(5, 5000)),
        best_ask_yes=draw(asks),
        best_ask_no=draw(asks),
        aggressiveness=draw(st.floats(0, 0.95)),
        inventory_yes=draw(st.floats(0, 2000)),
        inventory_no=draw(st.floats(0, 2000)),
        max_inventory=draw(st.sampled_from([500.0, 1000.0, math.inf])),
    )


@given(params())
def test_quote_invariants(p: QuoteParams) -> None:
    bids = plan(p)
    mids = {0: p.mid, 1: 1 - p.mid}
    asks = {0: p.best_ask_yes, 1: p.best_ask_no}
    held = {0: p.inventory_yes, 1: p.inventory_no}
    assert len({b.outcome for b in bids}) == len(bids)  # at most one bid per outcome
    for b in bids:
        assert 0 < b.price < mids[b.outcome], "a bid sits below its midpoint"
        assert mids[b.outcome] - b.price < p.max_spread, "inside the reward spread"
        assert asks[b.outcome] is None or b.price < asks[b.outcome], "never crosses: maker only"
        assert abs(b.price / p.tick - round(b.price / p.tick)) < 1e-6, "on the tick grid"
        assert held[b.outcome] < p.max_inventory, "no more of an outcome we're full on"
    if len(bids) == 2:
        assert bids[0].price + bids[1].price < 1, "a filled YES+NO pair can't lock in a loss"
    if not 0.10 <= p.mid <= 0.90:
        assert len(bids) in (0, 2), "extreme zone: both sides or nothing"


@given(st.floats(0.011, 0.989), ticks)
def test_rounding_goes_away_from_the_midpoint(price: float, tick: float) -> None:
    floored = floor_to_tick(price, tick)
    assert floored <= price + 1e-12 and price - floored < tick


def test_floor_survives_float_noise() -> None:
    assert floor_to_tick(0.47, 0.01) == 0.47  # 0.47 / 0.01 = 46.999999...


def test_typical_two_sided_quote() -> None:
    bids = plan(QuoteParams(mid=0.45, max_spread=0.025, tick=0.01, size=1000))
    assert [(b.outcome, b.price) for b in bids] == [(0, 0.43), (1, 0.53)]


def test_one_sided_when_full_on_yes() -> None:
    bids = plan(
        QuoteParams(
            mid=0.45, max_spread=0.025, tick=0.01, size=1000, inventory_yes=1500, max_inventory=1000
        )
    )
    assert [b.outcome for b in bids] == [1]


def test_extreme_zone_drops_a_lonely_side() -> None:
    p = QuoteParams(mid=0.95, max_spread=0.03, tick=0.01, size=100, best_ask_no=0.02)
    assert plan(p) == []
