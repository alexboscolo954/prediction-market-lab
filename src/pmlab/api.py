"""Read-only client for Polymarket's public APIs, with typed models.

Gamma (market metadata) encodes several list fields as JSON *strings*
(`clobTokenIds`, `outcomes`, `outcomePrices`). Both earlier versions of this
project broke on exactly that, so parsing lives in one place and a scheduled
CI job checks it against the live API (tests/test_live_contract.py).
"""

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _json_list(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


class Market(BaseModel):
    id: str
    question: str
    condition_id: str = Field(alias="conditionId")
    slug: str = ""
    outcomes: list[str]
    outcome_prices: list[float] = Field(alias="outcomePrices", default_factory=list)
    token_ids: list[str] = Field(alias="clobTokenIds", default_factory=list)
    volume: float = Field(alias="volumeNum", default=0.0)  # cumulative, all time
    volume_24h: float = Field(alias="volume24hr", default=0.0)  # rolling window
    liquidity: float = Field(alias="liquidityNum", default=0.0)
    best_bid: float | None = Field(alias="bestBid", default=None)
    best_ask: float | None = Field(alias="bestAsk", default=None)
    order_book: bool = Field(alias="enableOrderBook", default=False)

    _decode = field_validator("outcomes", "outcome_prices", "token_ids", mode="before")(_json_list)

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2 and len(self.token_ids) == 2


class Level(BaseModel):
    price: float
    size: float


class Book(BaseModel):
    """One outcome token's order book. The API lists bids ascending and asks
    descending (best price last), so never rely on position: use the properties."""

    token_id: str = Field(alias="asset_id")
    bids: list[Level]
    asks: list[Level]
    tick_size: float
    min_order_size: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return max((lv.price for lv in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((lv.price for lv in self.asks), default=None)


class RewardConfig(BaseModel):
    condition_id: str
    max_spread_cents: float = Field(alias="rewards_max_spread")
    min_size: float = Field(alias="rewards_min_size")
    daily_rate: float = Field(alias="total_daily_rate")

    @property
    def max_spread(self) -> float:
        """In price units: 2.5 cents -> 0.025."""
        return self.max_spread_cents / 100

    @property
    def active(self) -> bool:
        """The API also lists placeholder programs (0 spread, $0.001/day)."""
        return self.max_spread_cents > 0


class PricePoint(BaseModel):
    t: int
    p: float


class Client:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=20, headers={"User-Agent": "pmlab"})

    def _get(self, url: str, **params: Any) -> Any:
        resp = self.http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def markets(self, limit: int = 100, **filters: Any) -> list[Market]:
        params = {"limit": limit, "active": "true", "closed": "false", **filters}
        return [Market.model_validate(m) for m in self._get(f"{GAMMA}/markets", **params)]

    def market(self, condition_id: str) -> Market | None:
        found = self._get(f"{GAMMA}/markets", condition_ids=condition_id)
        return Market.model_validate(found[0]) if found else None

    def book(self, token_id: str) -> Book:
        return Book.model_validate(self._get(f"{CLOB}/book", token_id=token_id))

    def rewards(self, max_pages: int = 10) -> list[RewardConfig]:
        """Markets with an active liquidity-reward program (paginated; placeholders dropped)."""
        out, cursor = [], ""
        for _ in range(max_pages):
            page = self._get(f"{CLOB}/rewards/markets/current", next_cursor=cursor)
            configs = (RewardConfig.model_validate(r) for r in page["data"])
            out += [r for r in configs if r.active]
            cursor = page.get("next_cursor", "")
            if not cursor or cursor == "LTE=":  # LTE= is base64 "-1": last page
                break
        return out

    def price_history(
        self, token_id: str, interval: str = "1w", fidelity: int = 60
    ) -> list[PricePoint]:
        data = self._get(
            f"{CLOB}/prices-history", market=token_id, interval=interval, fidelity=fidelity
        )
        return [PricePoint.model_validate(p) for p in data["history"]]
