"""Opt-in live market making. Signs real orders with a wallet key.

Fences, all enforced in code and tested with a fake client:
- off unless `--live` is passed; without it, the loop only prints what it would do;
- the key is read only from the environment and never logged (SecretStr);
- signature_type must be set explicitly (0 EOA, 1 Polymarket proxy, 2 Gnosis Safe);
- explicit caps are required: no defaults for money limits;
- post-only orders: a mispriced quote is rejected instead of filling as a taker;
- cancel-all at startup and on exit/signal; fills reconciled every loop from the
  exchange's trade history, not from what we think we placed;
- exposure limits are checked on the *replacement* quote set before placing it.

Not exercised against the real exchange in CI. Check Polymarket's terms for your
jurisdiction before using it. Not financial advice.
"""

import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import SecretStr

from pmlab.api import Client, RewardConfig
from pmlab.quoting import Bid, QuoteParams, plan
from pmlab.rewards import adjusted_midpoint

log = logging.getLogger("pmlab.live")


class Exchange(Protocol):
    """The slice of py_clob_client.ClobClient this module uses."""

    def cancel_all(self) -> Any: ...
    def cancel_market_orders(self, market: str = "", asset_id: str = "") -> Any: ...
    def create_order(self, order_args: Any, options: Any = None) -> Any: ...
    def post_order(self, order: Any, orderType: Any = ..., post_only: bool = False) -> Any: ...
    def get_trades(self, params: Any = None, next_cursor: str = "MA==") -> Any: ...


@dataclass(frozen=True)
class Caps:
    per_market_usdc: float  # USDC committed in resting bids, per market
    total_usdc: float  # across all markets
    max_inventory: float  # shares of one outcome before we stop bidding for more

    def __post_init__(self) -> None:
        if min(self.per_market_usdc, self.total_usdc, self.max_inventory) <= 0:
            raise ValueError("caps must be positive")


@dataclass
class MarketState:
    reward: RewardConfig
    token_ids: list[str]
    inventory: list[float] = field(default_factory=lambda: [0.0, 0.0])
    committed: float = 0.0


def committed_usdc(bids: list[Bid]) -> float:
    return sum(b.price * b.size for b in bids)


class LiveTrader:
    def __init__(
        self,
        exchange: Exchange,
        data: Client,
        markets: list[MarketState],
        caps: Caps,
        size: float,
        aggressiveness: float,
        dry_run: bool = True,
        order_factory: Callable[[str, float, float], Any] | None = None,
        order_type: Any = None,
        maker_address: str = "",
    ) -> None:
        self.ex, self.data, self.markets, self.caps = exchange, data, markets, caps
        self.size, self.aggressiveness, self.dry_run = size, aggressiveness, dry_run
        self.order_factory = order_factory or _clob_buy_order
        self.order_type = order_type  # None: py_clob_client's GTC, resolved on first use
        self.maker_address = maker_address
        self.seen_trades: set[str] = set()
        self.running = False

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self.running = True
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)
        if not self.dry_run:
            self.ex.cancel_all()  # never inherit orders from a previous run

    def _stop(self, *_: Any) -> None:
        self.running = False

    def shutdown(self) -> None:
        if not self.dry_run:
            self.ex.cancel_all()
        log.info(
            "shutdown: all orders cancelled" if not self.dry_run else "dry run: nothing to cancel"
        )

    # -- one iteration ---------------------------------------------------------
    def step(self) -> list[tuple[str, Bid]]:
        """Reconcile fills, plan, enforce caps, replace orders. Returns what was placed."""
        self.reconcile()
        placed: list[tuple[str, Bid]] = []
        for m in self.markets:
            yes, no = self.data.book(m.token_ids[0]), self.data.book(m.token_ids[1])
            mid = adjusted_midpoint(yes, m.reward.min_size)
            bids = (
                []
                if mid is None
                else plan(
                    QuoteParams(
                        mid=mid,
                        max_spread=m.reward.max_spread,
                        tick=yes.tick_size,
                        size=self.size,
                        best_ask_yes=yes.best_ask,
                        best_ask_no=no.best_ask,
                        aggressiveness=self.aggressiveness,
                        inventory_yes=m.inventory[0],
                        inventory_no=m.inventory[1],
                        max_inventory=self.caps.max_inventory,
                    )
                )
            )
            # Caps on the replacement set: this market's new bids replace its old
            # ones; every other market keeps what it has resting right now.
            new = committed_usdc(bids)
            elsewhere = sum(x.committed for x in self.markets if x is not m)
            if new > self.caps.per_market_usdc or elsewhere + new > self.caps.total_usdc:
                log.warning(
                    "caps: %s would commit %.2f USDC (elsewhere %.2f), not quoting",
                    m.reward.condition_id[:10],
                    new,
                    elsewhere,
                )
                bids = []
            self.replace(m, bids)
            m.committed = committed_usdc(bids)
            placed += [(m.token_ids[b.outcome], b) for b in bids]
        return placed

    def replace(self, m: MarketState, bids: list[Bid]) -> None:
        if self.dry_run:
            for b in bids:
                log.info(
                    "DRY RUN would bid %s %s @ %.3f x %.0f",
                    m.reward.condition_id[:10],
                    "YES" if b.outcome == 0 else "NO",
                    b.price,
                    b.size,
                )
            return
        self.ex.cancel_market_orders(market=m.reward.condition_id)
        for b in bids:
            order = self.ex.create_order(
                self.order_factory(m.token_ids[b.outcome], b.price, b.size)
            )
            if self.order_type is None:
                self.order_type = _gtc()
            self.ex.post_order(order, self.order_type, post_only=True)

    def reconcile(self) -> None:
        """Inventory from the exchange's own trade history, not from what we placed.
        Post-only orders always fill as maker: our quantity is `matched_amount` on our
        entries in `maker_orders` (docs show the fields but no maker example, so taker
        trades are handled too)."""
        if self.dry_run:
            return
        by_token = {t: (m, i) for m in self.markets for i, t in enumerate(m.token_ids)}
        me = self.maker_address.lower()

        def apply(key: str, asset: str, side: str, size: float) -> None:
            if key in self.seen_trades or asset not in by_token:
                return
            self.seen_trades.add(key)
            market, outcome = by_token[asset]
            market.inventory[outcome] += size if side == "BUY" else -size

        for tr in self.ex.get_trades() or []:
            if tr.get("trader_side") == "MAKER":
                for mo in tr.get("maker_orders", []):
                    if mo.get("maker_address", "").lower() == me:
                        apply(
                            f"{tr['id']}:{mo['order_id']}",
                            mo["asset_id"],
                            mo["side"],
                            float(mo["matched_amount"]),
                        )
            else:
                apply(tr["id"], tr["asset_id"], tr["side"], float(tr["size"]))


def _gtc() -> Any:
    from py_clob_client.clob_types import OrderType

    return OrderType.GTC


def _clob_buy_order(token_id: str, price: float, size: float) -> Any:
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    return OrderArgs(token_id=token_id, price=price, size=size, side=BUY)


@dataclass(frozen=True)
class Credentials:
    private_key: SecretStr
    funder: str
    signature_type: int


def credentials_from_env(env: dict[str, str] | None = None) -> Credentials:
    env = dict(os.environ) if env is None else env
    missing = [
        k for k in ("PMLAB_PRIVATE_KEY", "PMLAB_FUNDER", "PMLAB_SIGNATURE_TYPE") if not env.get(k)
    ]
    if missing:
        raise SystemExit(f"live mode needs {', '.join(missing)} in the environment")
    sig = int(env["PMLAB_SIGNATURE_TYPE"])
    if sig not in (0, 1, 2):
        raise SystemExit(
            "PMLAB_SIGNATURE_TYPE must be 0 (EOA), 1 (Polymarket proxy) or 2 (Gnosis Safe)"
        )
    return Credentials(SecretStr(env["PMLAB_PRIVATE_KEY"]), env["PMLAB_FUNDER"], sig)


def connect(creds: Credentials) -> Exchange:
    try:
        from py_clob_client.client import ClobClient
    except ImportError as e:
        raise SystemExit(
            "live mode needs the extra: pip install 'prediction-market-lab[live]'"
        ) from e
    client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=creds.private_key.get_secret_value(),
        signature_type=creds.signature_type,
        funder=creds.funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client  # type: ignore[no-any-return]
