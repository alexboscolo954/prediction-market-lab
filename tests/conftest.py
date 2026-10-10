import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pmlab.api import Book, Client

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def fixture_client() -> Client:
    """Client whose HTTP layer answers from recorded API responses."""

    def handler(req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, req.url.params
        if path == "/markets":
            markets = fixture("gamma_markets.json")
            if "condition_ids" in q:
                markets = [m for m in markets if m["conditionId"] == q["condition_ids"]]
            return httpx.Response(200, json=markets)
        if path == "/book":
            yes, no = fixture("book_yes.json"), fixture("book_no.json")
            return httpx.Response(200, json=yes if q["token_id"] == yes["asset_id"] else no)
        if path == "/rewards/markets/current":
            return httpx.Response(200, json=fixture("rewards_current.json"))
        if path == "/prices-history":
            return httpx.Response(200, json=fixture("prices_history.json"))
        return httpx.Response(404)

    return Client(httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def client() -> Client:
    return fixture_client()


def book(
    token: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]], tick: float = 0.01
) -> Book:
    return Book.model_validate(
        {
            "asset_id": token,
            "tick_size": tick,
            "min_order_size": 5,
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
        }
    )
