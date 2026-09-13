"""Transparent projection and win-chance analytics.

Sleeper does not publish matchup win probability.  This module estimates it
from expected points remaining and positional uncertainty, independently for
each league.
"""

from __future__ import annotations

from math import erf, isfinite, sqrt
from typing import Iterable


# Approximate week-to-week coefficient of variation by fantasy position.
# These deliberately broad values avoid presenting projections as certainty.
POSITION_CV = {
    "QB": 0.38,
    "RB": 0.58,
    "FB": 0.62,
    "WR": 0.64,
    "TE": 0.66,
    "K": 0.55,
    "DEF": 0.52,
    "DL": 0.62,
    "LB": 0.56,
    "DB": 0.62,
}
DEFAULT_CV = 0.65


def score_projected_stats(
    stats: dict[str, float], scoring_settings: dict[str, float]
) -> float:
    """Convert raw projected stats with one league's Sleeper scoring rules."""
    total = 0.0
    for stat, projected_value in stats.items():
        try:
            total += float(projected_value) * float(scoring_settings.get(stat, 0.0))
        except (TypeError, ValueError):
            continue
    return max(0.0, total)


def remaining_projection(
    full_projection: float, game_state: str, remaining_fraction: float | None
) -> float | None:
    """Return only the not-yet-played share of a weekly projection."""
    state = str(game_state or "").lower()
    if state == "final":
        return 0.0
    if state == "pregame":
        return max(0.0, full_projection)
    if state == "live" and remaining_fraction is not None:
        return max(0.0, full_projection * min(1.0, max(0.0, remaining_fraction)))
    return None


def player_variance(expected_remaining: float, position: str | None) -> float:
    cv = POSITION_CV.get(str(position or "").upper(), DEFAULT_CV)
    return (max(0.0, expected_remaining) * cv) ** 2


def aggregate_remaining(
    values: Iterable[tuple[float, str | None]],
) -> tuple[float, float]:
    expected = 0.0
    variance = 0.0
    for points, position in values:
        expected += max(0.0, points)
        variance += player_variance(points, position)
    return expected, variance


def estimate_win_probability(
    user_current: float,
    opponent_current: float,
    user_remaining: float | None,
    opponent_remaining: float | None,
    user_variance: float | None,
    opponent_variance: float | None,
    *,
    completed: bool = False,
) -> float | None:
    """Estimate P(user wins) with a Normal margin approximation.

    Returned probabilities are fractions. Unresolved matchups are capped to
    1%-99%; completed ties return ``None`` so the renderer can show TIE.
    """
    if completed:
        if user_current == opponent_current:
            return None
        return 1.0 if user_current > opponent_current else 0.0
    inputs = (user_remaining, opponent_remaining, user_variance, opponent_variance)
    if any(value is None for value in inputs):
        return None
    variance = float(user_variance) + float(opponent_variance)
    if variance <= 0 or not isfinite(variance):
        return None
    margin = (
        float(user_current) + float(user_remaining)
        - float(opponent_current) - float(opponent_remaining)
    )
    z_score = margin / sqrt(variance)
    probability = 0.5 * (1.0 + erf(z_score / sqrt(2.0)))
    if not isfinite(probability):
        return None
    return min(0.99, max(0.01, probability))

