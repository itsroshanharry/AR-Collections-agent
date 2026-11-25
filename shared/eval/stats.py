"""Is the difference between two prompt variants real, or noise?

The eval set is small (tens of scenarios), and a 10-8 split on 18 scenarios happens by
chance about as often as not. Reporting a win-rate without saying so invites shipping a
prompt on a coin flip. These are the standard small-sample tools, implemented with the
standard library so the dashboard image stays slim:

  * Wilson score interval for a proportion. Unlike the textbook normal approximation it
    stays inside [0, 1] and behaves at n = 10, which is exactly the regime here.
  * Exact two-sided sign test for head-to-head wins. Ties carry no preference, so they
    are excluded from the test (and reported separately).
  * Paired bootstrap for the difference in mean composite score. Scenarios are paired -
    both variants run the same scripted customer - so resampling pairs, not variants,
    removes between-scenario variance from the comparison.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

# Two-sided 95%.
Z_95 = 1.959963984540054
ALPHA = 0.05


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Confidence interval for a proportion. (0, 0) when there is no data."""
    if n <= 0:
        return (0.0, 0.0)
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # At 0 or n successes the bound is exactly 0 or 1; floating point lands a hair off.
    lo = 0.0 if successes == 0 else max(0.0, centre - half)
    hi = 1.0 if successes == n else min(1.0, centre + half)
    return (lo, hi)


def sign_test_p(wins_a: int, wins_b: int) -> float:
    """Exact two-sided binomial p-value that A and B are equally likely to win.

    1.0 when there are no decisive comparisons: no evidence of any difference.
    """
    n = wins_a + wins_b
    if n == 0:
        return 1.0
    k = min(wins_a, wins_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations: int = 5000,
    seed: int = 0,
) -> tuple[float, tuple[float, float]]:
    """Mean of (a - b) over paired observations, with a 95% percentile interval.

    Seeded, so the same experiment always reports the same interval.
    """
    if len(a) != len(b):
        raise ValueError("paired samples must be the same length")
    if not a:
        return 0.0, (0.0, 0.0)
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    observed = statistics.fmean(diffs)
    if len(diffs) == 1:
        return observed, (observed, observed)
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(iterations))
    lo = means[int(0.025 * (iterations - 1))]
    hi = means[int(0.975 * (iterations - 1))]
    return observed, (lo, hi)


@dataclass(frozen=True)
class Significance:
    decisive: int
    ties: int
    p_value: float
    significant: bool
    win_rate_ci_a: tuple[float, float]
    win_rate_ci_b: tuple[float, float]
    composite_diff: float  # mean(A - B)
    composite_diff_ci: tuple[float, float]
    interpretation: str


def compare(
    wins_a: int,
    wins_b: int,
    ties: int,
    composites_a: Sequence[float],
    composites_b: Sequence[float],
    *,
    seed: int = 0,
) -> Significance:
    """Everything needed to say whether A and B actually differ."""
    total = wins_a + wins_b + ties
    p = sign_test_p(wins_a, wins_b)
    diff, diff_ci = paired_bootstrap_diff(composites_a, composites_b, seed=seed)
    significant = p < ALPHA

    if wins_a + wins_b == 0:
        interpretation = "No decisive comparisons: every scenario was a tie."
    elif significant:
        leader = "A" if wins_a > wins_b else "B"
        interpretation = (
            f"Variant {leader} is preferred and the difference is significant "
            f"(sign test p={p:.3f} < {ALPHA})."
        )
    else:
        interpretation = (
            f"Not significant (sign test p={p:.3f}). {wins_a + wins_b} decisive comparisons "
            "are not enough to tell these variants apart; treat the win-rate as noise."
        )
    if diff_ci[0] > 0 or diff_ci[1] < 0:
        interpretation += (
            f" Mean composite differs by {diff:+.2f} (95% CI {diff_ci[0]:+.2f} to "
            f"{diff_ci[1]:+.2f}), which excludes zero."
        )

    return Significance(
        decisive=wins_a + wins_b,
        ties=ties,
        p_value=round(p, 4),
        significant=significant,
        win_rate_ci_a=tuple(round(x, 3) for x in wilson_interval(wins_a, total)),
        win_rate_ci_b=tuple(round(x, 3) for x in wilson_interval(wins_b, total)),
        composite_diff=round(diff, 3),
        composite_diff_ci=(round(diff_ci[0], 3), round(diff_ci[1], 3)),
        interpretation=interpretation,
    )
