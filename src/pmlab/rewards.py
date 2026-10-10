"""Polymarket liquidity-reward scoring, as documented in
https://docs.polymarket.com/programs/liquidity-rewards (equations 1-4).

    S(v, s) = ((v - s) / v)^2 * b          order score, 0 at or beyond max spread v
    Q_one   = sum S * BidSize on m  +  sum S * AskSize on m'   (m' = complement token)
    Q_two   = sum S * AskSize on m  +  sum S * BidSize on m'
    Q_min   = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))   midpoint in [0.10, 0.90]
            = min(Q_one, Q_two)                                otherwise (two-sided only)

Two inputs are not fully specified by the docs and are approximated here:
- the "size-cutoff-adjusted midpoint": taken as the midpoint between the best
  bid and best ask among levels at least `min_size` large;
- the in-game multiplier b: 1.
"""

from dataclasses import dataclass
from typing import Literal

from pmlab.api import Book

C = 3.0  # scaling factor, "currently 3.0 on all markets"
SAFE_LOW, SAFE_HIGH = 0.10, 0.90


@dataclass(frozen=True)
class Order:
    token: Literal["m", "m'"]  # market token (e.g. YES) or its complement (NO)
    side: Literal["bid", "ask"]
    price: float
    size: float


def order_score(v: float, s: float, b: float = 1.0) -> float:
    """S(v, s). v and s in the same unit; spreads at or beyond v score 0."""
    if s >= v or v <= 0:
        return 0.0
    return ((v - s) / v) ** 2 * b


def side_scores(
    orders: list[Order], mid: float, v: float, min_size: float = 0.0
) -> tuple[float, float]:
    """(Q_one, Q_two) for a set of orders. `mid` is the m token's adjusted midpoint;
    the complement's midpoint is 1 - mid. Orders below min_size don't qualify."""
    q_one = q_two = 0.0
    for o in orders:
        if o.size < min_size:
            continue
        token_mid = mid if o.token == "m" else 1 - mid  # noqa: S105 - outcome token, not a secret
        score = order_score(v, abs(o.price - token_mid)) * o.size
        bid_on_m_or_ask_on_complement = (o.token == "m") == (o.side == "bid")  # noqa: S105
        if bid_on_m_or_ask_on_complement:
            q_one += score
        else:
            q_two += score
    return q_one, q_two


def q_min(q_one: float, q_two: float, mid: float, c: float = C) -> float:
    if SAFE_LOW <= mid <= SAFE_HIGH:
        return max(min(q_one, q_two), max(q_one / c, q_two / c))
    return min(q_one, q_two)


def adjusted_midpoint(book: Book, min_size: float) -> float | None:
    """Approximation (see module docstring): midpoint of the best levels that are
    at least min_size large, so a tiny order can't drag the midpoint."""
    bids = [lv.price for lv in book.bids if lv.size >= min_size]
    asks = [lv.price for lv in book.asks if lv.size >= min_size]
    if not bids or not asks:
        return None
    return (max(bids) + min(asks)) / 2


def book_orders(yes: Book, no: Book) -> list[Order]:
    """Everything resting in the two books, as Orders."""
    out = []
    for book, token in ((yes, "m"), (no, "m'")):
        out += [Order(token, "bid", lv.price, lv.size) for lv in book.bids]  # type: ignore[arg-type]
        out += [Order(token, "ask", lv.price, lv.size) for lv in book.asks]  # type: ignore[arg-type]
    return out


def estimated_share(
    ours: list[Order], competition: list[Order], mid: float, v: float, min_size: float
) -> float:
    """Our expected fraction of a sample's rewards: Q_min(ours) / (Q_min(ours) + Q_min(book)).
    ponytail: treats the whole resting book as one competitor; the real normalization
    is per maker, so this over-weights a book dominated by one large maker."""
    mine = q_min(*side_scores(ours, mid, v, min_size), mid)
    theirs = q_min(*side_scores(competition, mid, v, min_size), mid)
    return mine / (mine + theirs) if mine > 0 else 0.0
