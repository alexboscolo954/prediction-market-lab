import random

from pmlab.anomaly import price_jumps, robust_z, volume_spikes


def test_robust_z_ignores_one_outlier_in_the_baseline() -> None:
    base = [10.0] * 10 + [11.0] * 10 + [1000.0]  # one earlier spike
    assert robust_z(base, 11) < 2


def test_robust_z_falls_back_when_most_moves_are_zero() -> None:
    moves = [0.0] * 20 + [0.005, 0.01]
    z = robust_z(moves, 0.2)
    assert 10 < z < float("inf")


def test_volume_spike_found_at_the_right_place() -> None:
    rng = random.Random(1)
    cumulative, total = [], 0.0
    for i in range(80):
        total += 5000 + rng.uniform(-1000, 1000) + (90_000 if i == 60 else 0)
        cumulative.append(total)
    spikes = volume_spikes(cumulative, window=24, threshold=6)
    assert [s.index for s in spikes] == [60]


def test_old_rolling_24h_ratio_missed_a_real_spike() -> None:
    """The earlier version compared snapshots of the rolling `volume24hr` to their
    mean, alerting above 5x. An 18x hour on a steady market moves the rolling sum
    only ~1.75x, so it never fired. Differencing cumulative volume catches it."""
    rng = random.Random(3)
    per_hour = [5000 + rng.uniform(-500, 500) for _ in range(90)]
    per_hour[60] += 85_000
    rolling = [sum(per_hour[max(0, i - 23) : i + 1]) for i in range(len(per_hour))]
    cumulative = [sum(per_hour[: i + 1]) for i in range(len(per_hour))]

    old_ratio = rolling[60] / (sum(rolling[36:60]) / 24)
    assert old_ratio < 2  # far below the old 5x threshold: missed
    assert [s.index for s in volume_spikes(cumulative, window=24)] == [60]


def test_noise_alone_raises_no_price_jumps() -> None:
    rng = random.Random(7)
    p, prices = 0.5, []
    for _ in range(500):
        p = min(0.99, max(0.01, p + rng.gauss(0, 0.004)))
        prices.append(round(p, 3))
    assert price_jumps(prices, window=24, threshold=6, min_jump=0.03) == []


def test_price_jump_detected() -> None:
    prices = [0.40 + (0.002 if i % 2 else 0) for i in range(50)] + [0.25] * 5
    jumps = price_jumps(prices, window=24, threshold=6, min_jump=0.03)
    assert [j.index for j in jumps] == [50] and jumps[0].value < 0
