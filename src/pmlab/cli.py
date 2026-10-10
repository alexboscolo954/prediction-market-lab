"""pmlab: scan | rewards | jumps | watch | record | paper | live"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

from pmlab import anomaly, paper
from pmlab.api import Client, Market
from pmlab.quoting import QuoteParams, plan
from pmlab.rewards import Order, adjusted_midpoint, book_orders, estimated_share


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M")


def cmd_scan(c: Client, a: argparse.Namespace) -> None:
    print(f"{'24h volume':>14}  {'price':>6}  question")
    for m in c.markets(limit=a.limit, order="volume24hr", ascending="false"):
        price = m.outcome_prices[0] if m.outcome_prices else float("nan")
        print(f"{m.volume_24h:>14,.0f}  {price:>6.3f}  {m.question[:70]} [{m.outcomes[0]}]")


def cmd_rewards(c: Client, a: argparse.Namespace) -> None:
    """Rank reward programs by what `size` shares per side would earn today, estimated."""
    configs = sorted(c.rewards(), key=lambda r: -r.daily_rate)[: a.top]
    rows = []
    for r in configs:
        m = c.market(r.condition_id)
        if m is None or not m.is_binary or not m.order_book:
            continue
        yes, no = c.book(m.token_ids[0]), c.book(m.token_ids[1])
        mid = adjusted_midpoint(yes, r.min_size)
        size = max(a.size, r.min_size)
        bids = (
            []
            if mid is None
            else plan(
                QuoteParams(
                    mid=mid,
                    max_spread=r.max_spread,
                    tick=yes.tick_size,
                    size=size,
                    best_ask_yes=yes.best_ask,
                    best_ask_no=no.best_ask,
                    aggressiveness=a.aggressiveness,
                )
            )
        )
        ours = [Order("m" if b.outcome == 0 else "m'", "bid", b.price, b.size) for b in bids]
        share = estimated_share(ours, book_orders(yes, no), mid or 0.5, r.max_spread, r.min_size)
        capital = sum(b.price * b.size for b in bids)
        rows.append((share * r.daily_rate, share, capital, r, m, mid))
    print(
        f"{'est $/day':>9} {'share':>6} {'capital':>8} {'pool/day':>8} {'spread':>6} "
        f"{'min sz':>6} {'mid':>5}  question"
    )
    for est, share, capital, r, m, mid in sorted(rows, key=lambda x: -x[0]):
        flag = " *" if share >= 0.99 else ""
        mid_s = f"{mid:>5.2f}" if mid is not None else "    -"
        print(
            f"{est:>9.2f} {share:>6.1%} {capital:>8.0f} {r.daily_rate:>8.0f} "
            f"{r.max_spread_cents:>5.1f}c {r.min_size:>6.0f} {mid_s}  {m.question[:55]}{flag}"
        )
    print(
        "\nOne-sample estimate: the documented scoring formula against the book resting right"
        " now.\n* no competing liquidity inside the reward spread at this moment: the share"
        " collapses as soon\n  another maker quotes. Not a forecast of earnings."
    )


def resolve(c: Client, key: str) -> Market:
    found = c.markets(limit=1, slug=key) or ([m] if (m := c.market(key)) else [])
    if not found:
        raise SystemExit(f"no market for {key!r} (use a slug or condition id)")
    return found[0]


def cmd_jumps(c: Client, a: argparse.Namespace) -> None:
    m = resolve(c, a.market)
    hist = c.price_history(m.token_ids[0], interval=a.interval, fidelity=a.fidelity)
    prices = [p.p for p in hist]
    found = anomaly.price_jumps(prices, window=a.window, threshold=a.threshold, min_jump=a.min_jump)
    print(f"{m.question} [{m.outcomes[0]}]: {len(hist)} points, {len(found)} jump(s)")
    for j in found:
        print(
            f"  {ts(hist[j.index].t)}  {prices[j.index - 1]:.3f} -> {prices[j.index]:.3f} "
            f"({j.value:+.3f}, z={j.score:.1f})"
        )


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text},
                timeout=10,
            )
        except httpx.HTTPError as e:
            print(f"telegram failed: {type(e).__name__}", file=sys.stderr)  # never echo the URL


def cmd_watch(c: Client, a: argparse.Namespace) -> None:
    """Poll the top markets; alert on per-interval volume far above their own baseline."""
    series: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    for _ in range(a.rounds):
        for m in c.markets(limit=a.limit, order="volume24hr", ascending="false"):
            s = series.setdefault(m.id, [])
            s.append(m.volume)
            names[m.id] = m.question
            spikes = anomaly.volume_spikes(
                s, window=a.window, threshold=a.threshold, min_increment=a.min_volume
            )
            if spikes and spikes[-1].index == len(s) - 1:
                msg = (
                    f"volume spike: {names[m.id][:80]}\n"
                    f"${spikes[-1].value:,.0f} in the last interval (z={spikes[-1].score:.1f})"
                )
                print(msg, flush=True)
                telegram(msg)
        time.sleep(a.every)


def cmd_record(c: Client, a: argparse.Namespace) -> None:
    m = c.market(a.condition)
    if m is None or not m.is_binary:
        raise SystemExit("need a binary market condition id")
    with Path(a.out).open("a") as f:
        for i in range(a.count):
            yes, no = c.book(m.token_ids[0]), c.book(m.token_ids[1])
            f.write(
                json.dumps(
                    {
                        "t": time.time(),
                        "yes": yes.model_dump(by_alias=True),
                        "no": no.model_dump(by_alias=True),
                    }
                )
                + "\n"
            )
            f.flush()
            if i < a.count - 1:
                time.sleep(a.every)
    print(f"recorded {a.count} snapshots of {m.question[:60]} -> {a.out}")


def cmd_paper(c: Client, a: argparse.Namespace) -> None:
    snaps = paper.load(Path(a.snapshots))
    reward = next((r for r in c.rewards() if r.condition_id == a.condition), None)
    if reward is None:
        raise SystemExit("no active reward program for that condition id")
    r = paper.run(
        snaps,
        reward,
        size=max(a.size, reward.min_size),
        aggressiveness=a.aggressiveness,
        max_inventory=a.max_inventory,
    )
    hours = (snaps[-1].t - snaps[0].t) / 3600
    print(
        f"{len(snaps)} snapshots over {hours:.1f} h | fills {len(r.fills)} | "
        f"inventory YES {r.inventory_yes:.0f} NO {r.inventory_no:.0f}"
    )
    print(
        f"est. rewards ${r.rewards:.2f} | mark-to-market ${r.mark_to_market:.2f} | "
        f"total ${r.total:.2f}  (crude fill model, see paper.py)"
    )


def cmd_live(c: Client, a: argparse.Namespace) -> None:
    from pmlab import live

    caps = live.Caps(a.per_market_cap, a.total_cap, a.max_inventory)
    rewards = {r.condition_id: r for r in c.rewards()}
    states = []
    for cid in a.condition:
        m, r = c.market(cid), rewards.get(cid)
        if m is None or r is None or not m.is_binary:
            raise SystemExit(f"{cid}: not a binary market with an active reward program")
        states.append(live.MarketState(r, m.token_ids))
    if a.live:
        creds = live.credentials_from_env()
        exchange = live.connect(creds)
        maker = creds.funder
    else:
        exchange, maker = cast("live.Exchange", _NoExchange()), ""
        print("DRY RUN: printing intended orders only. Add --live to trade.")
    trader = live.LiveTrader(
        exchange,
        c,
        states,
        caps,
        size=a.size,
        aggressiveness=a.aggressiveness,
        dry_run=not a.live,
        maker_address=maker,
    )
    trader.start()
    try:
        while trader.running:
            trader.step()
            if not a.live and a.once:
                break
            time.sleep(a.every)
    finally:
        trader.shutdown()


class _NoExchange:
    """Stand-in exchange for dry runs: touching it is a bug."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"dry run must not call the exchange ({name})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pmlab", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="top markets by 24h volume")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("rewards", help="rank liquidity-reward programs by estimated $/day")
    s.add_argument("--top", type=int, default=20, help="programs to evaluate (by pool size)")
    s.add_argument("--size", type=float, default=0, help="shares per side (min: program min)")
    s.add_argument("--aggressiveness", type=float, default=0.5)

    s = sub.add_parser("jumps", help="price jumps in a market's history")
    s.add_argument("market", help="slug or condition id")
    s.add_argument("--interval", default="1w")
    s.add_argument("--fidelity", type=int, default=60, help="minutes per point")
    s.add_argument("--window", type=int, default=24)
    s.add_argument("--threshold", type=float, default=6.0)
    s.add_argument("--min-jump", type=float, default=0.03)

    s = sub.add_parser("watch", help="volume-spike alerts (Telegram if TELEGRAM_* set)")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--every", type=int, default=60)
    s.add_argument("--rounds", type=int, default=10**9)
    s.add_argument("--window", type=int, default=30)
    s.add_argument("--threshold", type=float, default=8.0)
    s.add_argument("--min-volume", type=float, default=5000)

    s = sub.add_parser("record", help="record order-book snapshots for paper trading")
    s.add_argument("condition")
    s.add_argument("--every", type=int, default=60)
    s.add_argument("--count", type=int, default=60)
    s.add_argument("--out", default="snapshots.jsonl")

    s = sub.add_parser("paper", help="paper market making on recorded snapshots")
    s.add_argument("snapshots")
    s.add_argument("--condition", required=True)
    s.add_argument("--size", type=float, default=0)
    s.add_argument("--aggressiveness", type=float, default=0.5)
    s.add_argument("--max-inventory", type=float, default=float("inf"))

    s = sub.add_parser("live", help="market making; dry run unless --live")
    s.add_argument("--condition", action="append", required=True)
    s.add_argument("--size", type=float, required=True)
    s.add_argument("--per-market-cap", type=float, required=True, help="USDC in bids per market")
    s.add_argument("--total-cap", type=float, required=True, help="USDC in bids overall")
    s.add_argument("--max-inventory", type=float, required=True, help="shares per outcome")
    s.add_argument("--aggressiveness", type=float, default=0.5)
    s.add_argument("--every", type=int, default=30)
    s.add_argument("--once", action="store_true", help="dry run: one iteration then exit")
    s.add_argument("--live", action="store_true", help="sign and send real orders")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    handlers = {
        "scan": cmd_scan,
        "rewards": cmd_rewards,
        "jumps": cmd_jumps,
        "watch": cmd_watch,
        "record": cmd_record,
        "paper": cmd_paper,
        "live": cmd_live,
    }
    handlers[a.cmd](Client(), a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
