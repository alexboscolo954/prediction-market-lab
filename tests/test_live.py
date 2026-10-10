"""Live-mode fences, against a fake exchange that records every call."""

from typing import Any

import pytest

from pmlab import cli, live
from pmlab.api import Client
from tests.conftest import fixture, fixture_client

MARKET = fixture("gamma_markets.json")[0]
TOKENS = __import__("json").loads(MARKET["clobTokenIds"])
ME = "0xMaker"


class FakeExchange:
    def __init__(self, trades: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.trades = trades or []

    def cancel_all(self) -> None:
        self.calls.append(("cancel_all", None))

    def cancel_market_orders(self, market: str = "", asset_id: str = "") -> None:
        self.calls.append(("cancel_market", market))

    def create_order(self, order_args: Any, options: Any = None) -> Any:
        return order_args

    def post_order(self, order: Any, orderType: Any = None, post_only: bool = False) -> None:
        self.calls.append(("post", (order, post_only)))

    def get_trades(self, params: Any = None, next_cursor: str = "MA==") -> list[dict[str, Any]]:
        return self.trades

    def posted(self) -> list[tuple[Any, bool]]:
        return [c[1] for c in self.calls if c[0] == "post"]


def trader(
    ex: FakeExchange,
    data: Client,
    caps: live.Caps | None = None,
    dry_run: bool = False,
    size: float = 1000,
) -> live.LiveTrader:
    reward = data.rewards()[0]
    state = live.MarketState(reward, TOKENS)
    return live.LiveTrader(
        ex,
        data,
        [state],
        caps or live.Caps(2000, 2000, 5000),
        size=size,
        aggressiveness=0.5,
        dry_run=dry_run,
        order_factory=lambda tok, px, sz: (tok, px, sz),
        order_type="GTC",
        maker_address=ME,
    )


def test_dry_run_never_touches_the_exchange() -> None:
    t = trader(cli._NoExchange(), fixture_client(), dry_run=True)  # type: ignore[arg-type]
    t.start()
    placed = t.step()
    t.shutdown()
    assert placed  # it planned real quotes from the recorded book...
    # ...and _NoExchange raises on any attribute access, so reaching here proves no calls


def test_live_cancels_everything_on_start_and_shutdown() -> None:
    ex = FakeExchange()
    t = trader(ex, fixture_client())
    t.start()
    t.shutdown()
    assert [c[0] for c in ex.calls] == ["cancel_all", "cancel_all"]


def test_orders_are_post_only_bids_that_replace_old_ones() -> None:
    ex = FakeExchange()
    trader(ex, fixture_client()).step()
    assert ex.calls[0][0] == "cancel_market"  # old quotes go first
    posted = ex.posted()
    assert posted and all(post_only for _, post_only in posted)
    prices = [order[1] for order, _ in posted]
    assert sum(prices) < 1  # YES bid + NO bid


def test_per_market_cap_blocks_the_whole_quote_set() -> None:
    ex = FakeExchange()
    trader(
        ex,
        fixture_client(),
        caps=live.Caps(per_market_usdc=100, total_usdc=10_000, max_inventory=5000),
    ).step()
    assert ex.posted() == []


def test_total_cap_counts_what_other_markets_have_resting() -> None:
    ex = FakeExchange()
    t = trader(ex, fixture_client(), caps=live.Caps(2000, total_usdc=1500, max_inventory=5000))
    other_reward = t.markets[0].reward.model_copy(update={"condition_id": "other"})
    t.markets.append(live.MarketState(other_reward, TOKENS, committed=1000))
    t.step()
    cancelled = [c[1] for c in ex.calls if c[0] == "cancel_market"]
    # market 1: ~970 new + 1000 resting on "other" > 1500 -> pulled, nothing posted.
    # "other" is then requoted against market 1's now-zero exposure -> allowed.
    assert cancelled == [t.markets[0].reward.condition_id, "other"]
    assert len(ex.posted()) == 2
    assert t.markets[0].committed == 0 and 0 < t.markets[1].committed <= 1500


def test_maker_fills_update_inventory_and_stop_further_bids() -> None:
    fill = {
        "id": "t1",
        "trader_side": "MAKER",
        "asset_id": TOKENS[1],
        "side": "SELL",
        "size": "9999",
        "maker_orders": [
            {
                "order_id": "o1",
                "maker_address": ME.lower(),
                "asset_id": TOKENS[0],
                "side": "BUY",
                "matched_amount": "6000",
            },
            {
                "order_id": "o2",
                "maker_address": "0xsomeoneelse",
                "asset_id": TOKENS[0],
                "side": "BUY",
                "matched_amount": "777",
            },
        ],
    }
    ex = FakeExchange([fill])
    t = trader(ex, fixture_client())
    t.step()
    t.step()  # the same trade seen twice must not double count
    assert t.markets[0].inventory == [6000, 0]
    outcomes = {order[0] for order, _ in ex.posted()}
    assert outcomes == {TOKENS[1]}  # over max_inventory on YES: only the NO bid remains


def test_credentials_are_required_and_never_printed() -> None:
    with pytest.raises(SystemExit, match="PMLAB_PRIVATE_KEY"):
        live.credentials_from_env({})
    with pytest.raises(SystemExit, match="SIGNATURE_TYPE"):
        live.credentials_from_env(
            {"PMLAB_PRIVATE_KEY": "k", "PMLAB_FUNDER": "f", "PMLAB_SIGNATURE_TYPE": "7"}
        )
    creds = live.credentials_from_env(
        {"PMLAB_PRIVATE_KEY": "0xsupersecret", "PMLAB_FUNDER": "f", "PMLAB_SIGNATURE_TYPE": "1"}
    )
    assert "supersecret" not in repr(creds) and "supersecret" not in str(creds)


def test_caps_must_be_positive() -> None:
    with pytest.raises(ValueError):
        live.Caps(0, 100, 100)


def test_cli_without_live_flag_never_reads_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "Client", fixture_client)
    monkeypatch.setattr(
        live, "credentials_from_env", lambda *a: pytest.fail("dry run must not read credentials")
    )
    cli.main(
        [
            "live",
            "--condition",
            MARKET["conditionId"],
            "--size",
            "1000",
            "--per-market-cap",
            "2000",
            "--total-cap",
            "2000",
            "--max-inventory",
            "5000",
            "--once",
        ]
    )
    assert "DRY RUN" in capsys.readouterr().out
