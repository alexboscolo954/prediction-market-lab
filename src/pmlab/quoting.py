"""Quote planning for reward-seeking market making on a binary market.

"Two-sided" on Polymarket means a bid on the market token (YES) *and* a bid on
its complement (NO): buying NO is economically selling YES, and it needs no
inventory. This planner never emits a sell, so it can't sell what it doesn't hold.

Invariants (tests/test_quoting.py checks them with Hypothesis):
- bid_yes + bid_no < 1: if both fill you hold a full set worth exactly $1.
- bids are rounded *away* from the midpoint (down), never to the nearest tick:
  nearest-tick rounding can push an order past the max spread (scores 0) or
  across the best ask (fills as a taker).
- every bid sits strictly inside the reward max spread and below the best ask.
- outside [0.10, 0.90] rewards need both sides, so it's both or nothing.
- inventory over the limit on one outcome stops bidding for more of it.
"""

import math
from dataclasses import dataclass

from pmlab.rewards import SAFE_HIGH, SAFE_LOW


@dataclass(frozen=True)
class QuoteParams:
    mid: float  # YES adjusted midpoint
    max_spread: float  # reward max spread, price units (0.025 = 2.5 cents)
    tick: float
    size: float  # shares per bid (>= the market's reward min size to score)
    best_ask_yes: float | None = None
    best_ask_no: float | None = None
    aggressiveness: float = 0.5  # 0 = at the max spread edge, 1 = at the midpoint
    inventory_yes: float = 0.0
    inventory_no: float = 0.0
    max_inventory: float = float("inf")  # per outcome, in shares


@dataclass(frozen=True)
class Bid:
    outcome: int  # 0 = YES / market token, 1 = NO / complement
    price: float
    size: float


def floor_to_tick(price: float, tick: float) -> float:
    # small epsilon so 0.47 / 0.01 = 46.99999 doesn't drop a whole tick
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def plan(p: QuoteParams) -> list[Bid]:
    if not 0 < p.mid < 1 or p.max_spread <= 0 or p.tick <= 0:
        return []
    distance = max(p.max_spread * (1 - p.aggressiveness), p.tick)
    candidates = []
    for outcome, mid, best_ask, holding in (
        (0, p.mid, p.best_ask_yes, p.inventory_yes),
        (1, 1 - p.mid, p.best_ask_no, p.inventory_no),
    ):
        price = floor_to_tick(mid - distance, p.tick)
        valid = (
            price >= p.tick
            and mid - price < p.max_spread  # strictly inside: S(v, v) = 0
            and (best_ask is None or price < best_ask)  # never cross: maker only
            and holding < p.max_inventory
        )
        candidates.append(Bid(outcome, price, p.size) if valid else None)

    bids = [b for b in candidates if b is not None]
    if not SAFE_LOW <= p.mid <= SAFE_HIGH and len(bids) < 2:
        return []  # extreme zone: single-sided liquidity scores nothing
    if len(bids) == 2 and bids[0].price + bids[1].price >= 1:
        return []  # would lock in a loss if both filled; can't happen with distance > 0
    return bids
