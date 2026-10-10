"""Parsing of real recorded API responses (tests/fixtures, recorded 2026-10-10)."""

from pmlab.api import Client
from tests.conftest import fixture


def test_markets_decode_json_string_fields(client: Client) -> None:
    markets = client.markets()
    assert markets and all(m.outcomes and m.token_ids for m in markets)
    raw = fixture("gamma_markets.json")[0]
    assert isinstance(raw["clobTokenIds"], str)  # the API really sends a JSON string
    first = markets[0]
    assert first.token_ids[0].isdigit() and len(first.outcome_prices) == 2
    assert abs(sum(first.outcome_prices) - 1) < 0.05


def test_outcomes_are_not_assumed_yes_no(client: Client) -> None:
    names = {o for m in client.markets() for o in m.outcomes}
    assert names - {"Yes", "No"}, "fixture should include team-name outcomes"


def test_book_best_prices_do_not_depend_on_order(client: Client) -> None:
    raw = fixture("book_yes.json")
    b = client.book(raw["asset_id"])
    assert b.best_bid == max(float(lv["price"]) for lv in raw["bids"])
    assert b.best_ask == min(float(lv["price"]) for lv in raw["asks"])
    assert b.best_bid < b.best_ask
    # the API lists bids ascending: the best bid is last, not first
    assert float(raw["bids"][-1]["price"]) == b.best_bid


def test_rewards_units_and_pagination(client: Client) -> None:
    rewards = client.rewards()
    r = rewards[0]
    assert r.daily_rate == 6300 and r.max_spread_cents == 2.5 and r.max_spread == 0.025
    assert r.min_size == 1000


def test_price_history(client: Client) -> None:
    hist = client.price_history("x")
    assert len(hist) > 100 and all(0 <= p.p <= 1 for p in hist)


def test_placeholder_reward_programs_are_dropped() -> None:
    import httpx

    page = {
        "data": [
            {
                "condition_id": "a",
                "rewards_max_spread": 0,
                "rewards_min_size": 0,
                "total_daily_rate": 0.001,
            },
            {
                "condition_id": "b",
                "rewards_max_spread": 3.5,
                "rewards_min_size": 100,
                "total_daily_rate": 50,
            },
        ],
        "next_cursor": "LTE=",
    }
    c = Client(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page)))
    )
    assert [r.condition_id for r in c.rewards()] == ["b"]
