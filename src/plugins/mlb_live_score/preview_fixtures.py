"""Offline presentation fixtures shared by the preview script and tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .models import (
    DivisionStandingRow,
    DivisionStandings,
    GameState,
    GameSummary,
    ScoreboardPresentation,
)


def standings() -> DivisionStandings:
    return DivisionStandings("NL EAST", [
        DivisionStandingRow("ATL", 88, 57, "-", True),
        DivisionStandingRow("PHI", 84, 61, "4.0"),
        DivisionStandingRow("NYM", 77, 68, "11.0"),
        DivisionStandingRow("MIA", 65, 80, "23.0"),
        DivisionStandingRow("WSH", 61, 84, "27.0"),
    ])


def base_game(**changes) -> GameSummary:
    values = dict(
        game_pk=12345, state=GameState.LIVE, status="IN PROGRESS",
        uses_live_layout=True, away_team_id=147, away_team_name="New York Yankees",
        away_abbreviation="NYY", home_team_id=144, home_team_name="Atlanta Braves",
        home_abbreviation="ATL", away_runs=4, away_hits=8, away_errors=1,
        home_runs=3, home_hits=7, home_errors=0, inning=7, inning_half="TOP",
        outs=2, balls=2, strikes=1, pitcher_name="Spencer Strider",
        batter_name="Aaron Judge", scheduled_time=datetime.now(timezone.utc),
        venue="Truist Park", away_starting_pitcher="Gerrit Cole",
        away_starting_pitcher_wins=12, away_starting_pitcher_losses=6,
        home_starting_pitcher="Spencer Strider", home_starting_pitcher_wins=14,
        home_starting_pitcher_losses=5, season=2026,
    )
    values.update(changes)
    return GameSummary(**values)


def presentation(game=None, **changes) -> ScoreboardPresentation:
    values = dict(
        selected_team_id=144, selected_team_name="Atlanta Braves",
        selected_team_abbreviation="ATL", game=game or base_game(),
        standings=standings(), updated_at=datetime.now(timezone.utc),
    )
    values.update(changes)
    return ScoreboardPresentation(**values)


def preview_states() -> dict[str, ScoreboardPresentation]:
    live_empty = presentation()
    states = {
        "live_bases_empty": live_empty,
        "live_runner_first": presentation(base_game(runner_on_first=True)),
        "live_first_third": presentation(base_game(runner_on_first=True, runner_on_third=True)),
        "live_bases_loaded": presentation(base_game(runner_on_first=True, runner_on_second=True, runner_on_third=True)),
        "pregame_starters": presentation(base_game(state=GameState.PREGAME, status="SCHEDULED", uses_live_layout=False, is_pregame=True, away_runs=None, away_hits=None, away_errors=None, home_runs=None, home_hits=None, home_errors=None)),
        "pregame_tbd": presentation(base_game(state=GameState.PREGAME, status="SCHEDULED", uses_live_layout=False, is_pregame=True, away_runs=None, away_hits=None, away_errors=None, home_runs=None, home_hits=None, home_errors=None, away_starting_pitcher=None, away_starting_pitcher_wins=None, away_starting_pitcher_losses=None, home_starting_pitcher=None, home_starting_pitcher_wins=None, home_starting_pitcher_losses=None)),
        "final_wp_lp": presentation(base_game(state=GameState.FINAL, status="FINAL", uses_live_layout=False, is_final=True, inning=9, winning_pitcher="Spencer Strider", winning_pitcher_wins=14, winning_pitcher_losses=5, losing_pitcher="Gerrit Cole", losing_pitcher_wins=11, losing_pitcher_losses=8, pitcher_name=None, batter_name=None)),
        "final_wp_lp_sv": presentation(base_game(state=GameState.FINAL, status="FINAL", uses_live_layout=False, is_final=True, inning=9, winning_pitcher="Spencer Strider", winning_pitcher_wins=14, winning_pitcher_losses=5, losing_pitcher="Gerrit Cole", losing_pitcher_wins=11, losing_pitcher_losses=8, save_pitcher="Raisel Iglesias", save_pitcher_saves=31, pitcher_name=None, batter_name=None)),
        "extra_inning_final": presentation(base_game(state=GameState.FINAL, status="FINAL", uses_live_layout=False, is_final=True, inning=11, winning_pitcher="Spencer Strider", losing_pitcher="Gerrit Cole", save_pitcher=None)),
        "delayed": presentation(base_game(state=GameState.DELAYED_PREGAME, status="DELAYED", uses_live_layout=False, is_pregame=True)),
        "postponed": presentation(base_game(state=GameState.POSTPONED, status="POSTPONED", uses_live_layout=False)),
        "suspended": presentation(base_game(state=GameState.SUSPENDED_LIVE, status="SUSPENDED", uses_live_layout=True)),
        "standings_failure": presentation(base_game(state=GameState.PREGAME, status="SCHEDULED", uses_live_layout=False, is_pregame=True), standings=None, standings_unavailable=True),
    }
    next_game = base_game(
        state=GameState.PREGAME, status="SCHEDULED", uses_live_layout=False,
        is_pregame=True, scheduled_time=datetime.now(timezone.utc) + timedelta(days=2),
    )
    states["no_game_today"] = presentation(next_game, no_game_today=True)
    return {name: deepcopy(value) for name, value in states.items()}
