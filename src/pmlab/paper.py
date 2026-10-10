"""Paper market making: replay recorded order-book snapshots, quote with the
same planner live mode uses, accrue estimated rewards, and model fills.

This shows the mechanics, not returns. The fill model is deliberately crude:
a resting bid counts as filled when a later snapshot's best ask trades down to
its price. Real fills depend on queue position and on trades between snapshots,
so treat P&L numbers as illustrations.
"""

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

from pmlab.api import Book, RewardConfig
from pmlab.quoting import Bid, QuoteParams, plan
from pmlab.rewards import Order, adjusted_midpoint, book_orders, estimated_share


@dataclass
class Snapshot:
    t: float  # unix seconds
    yes: Book
    no: Book


@dataclass
class PaperResult:
    rewards: float = 0.0
    cash: float = 0.0  # spent on fills (negative) and merges (+1 per full set)
    inventory_yes: float = 0.0
    inventory_no: float = 0.0
    fills: list[tuple[float, int, float, float]] = field(
        default_factory=list
    )  # t, outcome, px, size
    last_mid: float = 0.5

    @property
    def mark_to_market(self) -> float:
        return (
            self.cash + self.inventory_yes * self.last_mid + self.inventory_no * (1 - self.last_mid)
        )

    @property
    def total(self) -> float:
        return self.rewards + self.mark_to_market


def run(
    snapshots: list[Snapshot],
    reward: RewardConfig,
    size: float,
    aggressiveness: float = 0.5,
    max_inventory: float = float("inf"),
) -> PaperResult:
    r = PaperResult()
    resting: list[Bid] = []
    for prev, cur in itertools.pairwise(snapshots):
        # 1. Fills: did the market trade down through our resting bids?
        asks = {0: cur.yes.best_ask, 1: cur.no.best_ask}
        for bid in resting:
            ask = asks[bid.outcome]
            if ask is not None and ask <= bid.price:
                r.cash -= bid.price * bid.size
                if bid.outcome == 0:
                    r.inventory_yes += bid.size
                else:
                    r.inventory_no += bid.size
                r.fills.append((cur.t, bid.outcome, bid.price, bid.size))
        # A YES + NO pair merges back into $1, so full sets are cash.
        sets = min(r.inventory_yes, r.inventory_no)
        r.inventory_yes -= sets
        r.inventory_no -= sets
        r.cash += sets

        # 2. Requote on the current book.
        mid = adjusted_midpoint(cur.yes, reward.min_size)
        if mid is None:
            resting = []
            continue
        r.last_mid = mid
        resting = plan(
            QuoteParams(
                mid=mid,
                max_spread=reward.max_spread,
                tick=cur.yes.tick_size,
                size=size,
                best_ask_yes=cur.yes.best_ask,
                best_ask_no=cur.no.best_ask,
                aggressiveness=aggressiveness,
                inventory_yes=r.inventory_yes,
                inventory_no=r.inventory_no,
                max_inventory=max_inventory,
            )
        )

        # 3. Rewards for the interval our quotes rest (sampled once a minute).
        ours = [Order("m" if b.outcome == 0 else "m'", "bid", b.price, b.size) for b in resting]
        share = estimated_share(
            ours, book_orders(cur.yes, cur.no), mid, reward.max_spread, reward.min_size
        )
        minutes = max(0.0, (cur.t - prev.t) / 60)
        r.rewards += share * reward.daily_rate / 1440 * minutes
    return r


def load(path: Path) -> list[Snapshot]:
    """JSONL written by `pmlab record`: {"t": ..., "yes": book, "no": book} per line."""
    out = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        out.append(
            Snapshot(row["t"], Book.model_validate(row["yes"]), Book.model_validate(row["no"]))
        )
    return out
