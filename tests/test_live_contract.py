"""Contract test against the real public APIs. Both earlier versions of this
project died silently when Polymarket's response shapes didn't match the code;
CI runs this weekly so a schema change shows up as a red build instead."""

import pytest

from pmlab.api import Client
from pmlab.quoting import QuoteParams, plan
from pmlab.rewards import adjusted_midpoint

pytestmark = pytest.mark.live_api


def test_live_markets_books_and_rewards_parse() -> None:
    c = Client()
    markets = c.markets(limit=50, order="volume24hr", ascending="false")
    assert len(markets) >= 10
    binary = [m for m in markets if m.is_binary and m.order_book]
    assert binary, "no binary markets with an order book"
    book = c.book(binary[0].token_ids[0])
    assert book.tick_size > 0 and (book.bids or book.asks)

    rewards = c.rewards(max_pages=1)
    assert rewards and all(r.max_spread > 0 and r.min_size >= 0 for r in rewards)


def test_live_quote_is_valid_on_a_rewarded_market() -> None:
    c = Client()
    for r in sorted(c.rewards(max_pages=2), key=lambda x: -x.daily_rate)[:20]:
        m = c.market(r.condition_id)
        if not (m and m.is_binary and m.order_book):
            continue
        yes, no = c.book(m.token_ids[0]), c.book(m.token_ids[1])
        mid = adjusted_midpoint(yes, r.min_size)
        if mid is None or not 0.15 < mid < 0.85:
            continue
        for bid in plan(
            QuoteParams(
                mid=mid,
                max_spread=r.max_spread,
                tick=yes.tick_size,
                size=r.min_size,
                best_ask_yes=yes.best_ask,
                best_ask_no=no.best_ask,
            )
        ):
            ask = (yes if bid.outcome == 0 else no).best_ask
            assert ask is None or bid.price < ask
        return
    pytest.skip("no rewarded market with a two-sided book right now")
