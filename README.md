# prediction-market-lab

[![ci](https://github.com/alexboscolo954/prediction-market-lab/actions/workflows/ci.yaml/badge.svg)](https://github.com/alexboscolo954/prediction-market-lab/actions/workflows/ci.yaml)

A personal side project on [Polymarket](https://polymarket.com) prediction markets. It covers three things:
- spotting unusual moves;
- understanding the math of its liquidity-reward program;
- paper-trading a reward-seeking market maker, with an opt-in, heavily fenced live mode.

It's read-only by default and needs no account or key.

```
pmlab scan       top markets by 24h volume
pmlab jumps      unusual price moves in a market's history
pmlab watch      volume-spike alerts (stdout, or Telegram if TELEGRAM_* is set)
pmlab rewards    rank liquidity-reward programs by estimated $/day for a given size
pmlab record     save order-book snapshots
pmlab paper      replay snapshots through the market maker: rewards, fills, inventory
pmlab live       the same market maker against the exchange; dry run unless --live
```

## Price jumps on real data

```
$ pmlab jumps will-luiz-incio-lula-da-silva-win-the-2026-brazilian-presidential-election --interval 1m
Will Luiz Inácio Lula da Silva win the 2026 Brazilian presidential election? [Yes]: 717 points, 5 jump(s)
  2026-10-02 07:00  0.395 -> 0.455 (+0.060, z=38.3)
  2026-10-02 08:00  0.455 -> 0.425 (-0.030, z=6.4)
  2026-10-03 23:00  0.385 -> 0.355 (-0.030, z=9.6)
  2026-10-04 21:00  0.365 -> 0.140 (-0.225, z=30.8)
  2026-10-09 19:00  0.135 -> 0.105 (-0.030, z=28.7)
```

The biggest move lands on the evening of 4 October, the day of Brazil's first-round vote.
Each move is scored against the market's own recent typical move with a **modified z-score**: the median absolute deviation, falling back to the mean absolute deviation when most hourly moves are exactly zero. A minimum absolute move (3 points by default) keeps penny wiggles quiet.

## Liquidity rewards, from the documented formula

Polymarket pays makers for resting orders near the midpoint. [`rewards.py`](src/pmlab/rewards.py) implements the [published scoring](https://docs.polymarket.com/programs/liquidity-rewards):

```
S(v, s) = ((v - s) / v)^2              # quadratic in the distance s from the midpoint, 0 at the max spread v
Q_one   = Σ S·bids on YES + Σ S·asks on NO
Q_two   = Σ S·asks on YES + Σ S·bids on NO
Q_min   = max(min(Q_one, Q_two), max(Q_one, Q_two) / 3)   midpoint in [0.10, 0.90]
        = min(Q_one, Q_two)                               outside: two-sided only
```

The tests reproduce the docs' worked example (Q ≈ 111.1). Two inputs aren't fully specified by the docs, so they're approximated and labelled in the code: the "size-cutoff-adjusted midpoint" and the multiplier `b`.

```
$ pmlab rewards --top 8
est $/day  share  capital pool/day spread min sz   mid  question
  1200.00 100.0%      540     1200   2.5c   1000  0.56  Wake Forest vs. NC State: O/U 55.5 *
   111.58   1.8%      970     6300   2.5c   1000  0.45  Will Manchester United FC win on 2026-10-10?
    11.41   0.7%      960     1714   2.5c   1000  0.20  South Carolina vs. Florida
     0.05   0.0%      190     1331   4.5c    200  0.61  2026 Balance of Power: D Senate, D House
...
* no competing liquidity inside the reward spread at this moment: the share collapses as soon
  another maker quotes. Not a forecast of earnings.
```

## The market maker

[`quoting.py`](src/pmlab/quoting.py) plans quotes; `paper` and `live` share it. On Polymarket, "two-sided" means **a bid on YES and a bid on NO**, because buying NO is economically selling YES and needs no inventory. Hypothesis checks these invariants over random market states:

- **YES bid + NO bid < $1.** If both fill you hold a full set worth exactly $1, so this can never lock in a loss.
- **Prices are floored to the tick, away from the midpoint.** Rounding to the nearest tick can push an order past the max spread, where it scores 0, or across the best ask.
- **Every bid is strictly inside the reward spread and below the best ask**, so it's maker-only.
- **The tick size comes from each market's book.** It's 0.001 on some markets.
- **Outside [0.10, 0.90] it's both sides or nothing**, because one side alone scores 0 there.
- **It stops bidding on an outcome once inventory hits the limit.**

### Paper trading

[`examples/balance-of-power.jsonl`](examples/balance-of-power.jsonl) holds 14 real order-book snapshots, 30 s apart, of "2026 Balance of Power: D Senate, D House" (pool $1,331/day, 4.5¢ max spread, 200-share minimum). `pmlab paper` replays them through the same planner as live mode:

```
$ pmlab paper examples/balance-of-power.jsonl --condition 0x16c63b7c... --size 200
14 snapshots over 7 min | 200 shares per side | pool $1,331/day
avg reward share 0.0039% | est. rewards $0.0002 (~$0.05/day at this share)

$ pmlab paper examples/balance-of-power.jsonl --condition 0x16c63b7c... --size 20000
14 snapshots over 7 min | 20000 shares per side | pool $1,331/day
avg reward share 0.3910% | est. rewards $0.0238 (~$5.20/day at this share)
```

The book on this market holds **millions of shares within a cent of the midpoint** (2.1M at 0.60, 666k at 0.61). About $19,000 of bids earns about $5 a day *before* adverse selection, and a minimum-size quote earns pennies. Reward farming on deep markets is a scale business, and the simulator makes that visible in seconds. No fills happened in these 7 minutes.

The fill model is crude: a bid fills when a later snapshot's best ask trades down to it. Queue position and trades between snapshots are ignored. Read the numbers as mechanics, not returns.

### Live mode (opt-in)

`pip install 'prediction-market-lab[live]'` adds `py-clob-client`, the order-signing dependency, which the default install leaves out. Live mode is fenced in code and tested against a fake exchange:

| Fence | |
|---|---|
| Dry run by default | Without `--live` it prints intended orders. The test uses an exchange object that raises on any access. |
| Keys | Read only from `PMLAB_PRIVATE_KEY`, wrapped in `SecretStr`, never logged. `PMLAB_SIGNATURE_TYPE` must be explicit (0 EOA, 1 proxy, 2 Safe). |
| Money limits | `--per-market-cap`, `--total-cap` and `--max-inventory` are required, with no defaults. Caps are checked on the **replacement** quote set, counting what other markets have resting. |
| Orders | Post-only GTC: a mispriced quote is rejected instead of filling as a taker. Old quotes are cancelled before new ones go out. |
| Lifecycle | Cancel-all on start, on exit and on SIGINT/SIGTERM |
| Inventory | Rebuilt every loop from the exchange's trade history (your `maker_orders` entries), not from what the bot thinks it placed |

Live mode isn't exercised against the real exchange in CI. Check Polymarket's terms for your jurisdiction. Not financial advice.

## Testing

- **Unit (41 tests):** API parsing against **recorded real responses**, reward math against the docs, quoting invariants as property tests, anomaly detectors on synthetic series, the paper trader, every live-mode fence, and recorder/watcher resilience: a DNS blip killed the first real recording halfway, and now a failed poll is skipped.
- **A weekly contract test against the live API.** Both of this project's earlier versions broke silently when Polymarket's responses didn't match the code: `clobTokenIds`, `outcomes` and `outcomePrices` arrive as JSON *strings*, and outcomes aren't always Yes/No. Now a schema change turns the build red instead. Its first run found reward programs that are placeholders (0 spread, $0.001/day), which the client now filters out.

## What changed from the first versions

This replaces two earlier private repos, an anomaly bot and a liquidity bot:

- **API drift.** Both had stopped working because of the response-shape issues above. The scanner found zero markets, and the anomaly bot dropped every market.
- **The anomaly bot** compared snapshots of a *rolling* 24h volume to its mean. A test shows an 18× hourly spike moves that ratio only about 1.7×, under its 5× threshold, so it was never flagged. It also called a method that didn't exist on every alert. An IsolationForest model was removed: with no labelled anomalies, there was no way to show it caught anything the robust detectors miss.
- **The liquidity bot** read the reward pool from a size field, hardcoded a 0.01 tick, quoted asks it had no inventory for, and checked exposure limits before clearing the old exposure. It also never tracked fills.

## License

MIT
