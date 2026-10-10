"""Anomaly detection on market time series, with robust statistics.

Volume: Gamma's `volume24hr` is a *rolling* 24h sum, so comparing snapshots of
it smears any spike over a day. The detector uses increments of the
*cumulative* volume (`volumeNum`) between snapshots instead.

Scores are robust z-scores, (x - median) / (1.4826 * MAD): one huge spike
doesn't inflate the baseline it is measured against, as it would with mean/stdev.

An earlier version also ran an IsolationForest over hand-made features. With no
labelled anomalies to evaluate against, it couldn't be shown to catch anything
these two detectors miss, so it was dropped rather than kept as decoration.
"""

import itertools
from dataclasses import dataclass
from statistics import median

MAD_TO_SIGMA = 1.4826  # MAD -> standard deviation for normally distributed data


MEANAD_TO_SIGMA = 1.2533  # mean absolute deviation -> standard deviation


def robust_z(history: list[float], value: float) -> float:
    """Modified z-score of `value` against `history`.

    Uses the median absolute deviation; when more than half the history is
    identical (MAD = 0, common for hourly prediction-market moves that are
    mostly zero) it falls back to the mean absolute deviation, as the usual
    modified z-score does. inf only when the history is completely flat.
    """
    if len(history) < 3:
        return 0.0
    med = median(history)
    deviations = [abs(x - med) for x in history]
    scale = MAD_TO_SIGMA * median(deviations)
    if scale == 0:
        scale = MEANAD_TO_SIGMA * sum(deviations) / len(deviations)
    if scale == 0:
        return 0.0 if value == med else float("inf")
    return (value - med) / scale


@dataclass(frozen=True)
class Anomaly:
    index: int  # position in the series
    kind: str  # "volume_spike" | "price_jump"
    value: float
    score: float


def volume_spikes(
    cumulative: list[float], window: int = 24, threshold: float = 6.0, min_increment: float = 0.0
) -> list[Anomaly]:
    """Spikes in traded volume per interval, from a cumulative volume series."""
    increments = [max(0.0, b - a) for a, b in itertools.pairwise(cumulative)]
    out = []
    for i in range(window, len(increments)):
        z = robust_z(increments[i - window : i], increments[i])
        if z >= threshold and increments[i] >= min_increment:
            out.append(Anomaly(i + 1, "volume_spike", increments[i], z))
    return out


def price_jumps(
    prices: list[float], window: int = 24, threshold: float = 6.0, min_jump: float = 0.03
) -> list[Anomaly]:
    """Moves much larger than the market's recent typical move. min_jump (absolute
    probability points) keeps a 0.1 -> 0.2 cent wiggle on a dead market quiet."""
    moves = [b - a for a, b in itertools.pairwise(prices)]
    out = []
    for i in range(window, len(moves)):
        z = robust_z([abs(m) for m in moves[i - window : i]], abs(moves[i]))
        if z >= threshold and abs(moves[i]) >= min_jump:
            out.append(Anomaly(i + 1, "price_jump", moves[i], z))
    return out
