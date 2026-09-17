import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.mlb_live_score.mlb_live_score import MlbLiveScore
from plugins.mlb_live_score.models import GameState, GameSummary, ScoreboardPresentation
from plugins.sleeper_fantasy_matchups.models import (
    DashboardModel,
    FantasyMatchup,
    GameProgress,
    MatchupSide,
    MatchupStatus,
    PlayerProjection,
    SleeperUser,
)
from plugins.sleeper_fantasy_matchups.projections import parse_game_progress
from plugins.sleeper_fantasy_matchups.sleeper_client import count_live_starters
from plugins.sleeper_fantasy_matchups.sleeper_fantasy_matchups import SleeperFantasyMatchups


def mlb_state(game=None, *, selected_team_id=1, no_game_today=False):
    presentation = ScoreboardPresentation(
        selected_team_id, "Atlanta", "ATL", game,
        no_game_today=no_game_today,
    )
    client = MagicMock()
    client.get_presentation.return_value = presentation
    plugin = MlbLiveScore({"id": "mlb_live_score"}, data_client=client, monotonic=lambda: 1)
    return plugin.get_scheduler_state({"mlbTeamId": str(selected_team_id)}, None, None)["game"]


def game(state, away=4, home=3, selected_away=True, **changes):
    values = dict(
        state=state,
        status=state.value,
        uses_live_layout=state in {GameState.LIVE, GameState.DELAYED_LIVE, GameState.SUSPENDED_LIVE},
        is_pregame=state is GameState.PREGAME,
        is_final=state is GameState.FINAL,
        away_team_id=1 if selected_away else 2,
        home_team_id=2 if selected_away else 1,
        away_runs=away,
        home_runs=home,
        inning=8,
        inning_half="Bottom",
        scheduled_time=datetime(2026, 9, 14, 23, 5, tzinfo=timezone.utc),
    )
    values.update(changes)
    return GameSummary(**values)


@pytest.mark.parametrize(
    "state,is_live,is_pregame,is_final",
    [
        (GameState.PREGAME, False, True, False),
        (GameState.LIVE, True, False, False),
        (GameState.FINAL, False, False, True),
    ],
)
def test_mlb_scheduler_lifecycle_states(state, is_live, is_pregame, is_final):
    value = mlb_state(game(state))
    assert (value["is_live"], value["is_pregame"], value["is_final"]) == (is_live, is_pregame, is_final)


@pytest.mark.parametrize(
    "away,home,selected_away,leading,trailing,tied,difference,close",
    [
        (4, 3, True, True, False, False, 1, True),
        (3, 4, True, False, True, False, 1, True),
        (4, 4, True, False, False, True, 0, True),
        (7, 3, True, True, False, False, 4, False),
        (7, 5, False, False, True, False, 2, True),
    ],
)
def test_mlb_scheduler_score_context(away, home, selected_away, leading, trailing, tied, difference, close):
    value = mlb_state(game(GameState.LIVE, away, home, selected_away))
    assert value["selected_team_leading"] is leading
    assert value["selected_team_trailing"] is trailing
    assert value["tied"] is tied
    assert value["score_diff"] == difference
    assert value["is_close"] is close


def test_mlb_no_game_today_and_missing_scores_are_safe():
    value = mlb_state(game(GameState.PREGAME, away=None, home=None), no_game_today=True)
    assert value["has_game_today"] is False
    assert value["selected_team_score"] is None
    assert value["opponent_score"] is None
    assert value["score_diff"] is None
    assert value["is_close"] is False


def test_mlb_pregame_placeholder_scores_and_inning_are_not_exposed_as_facts():
    value = mlb_state(game(GameState.PREGAME, away=0, home=0, inning=1, inning_half="Top"))
    assert value["selected_team_score"] is None
    assert value["opponent_score"] is None
    assert value["score_diff"] is None
    assert value["tied"] is False
    assert value["inning"] is None and value["inning_half"] is None


def projection(player, game_id):
    return PlayerProjection(player, "RB", game_id, {})


@pytest.mark.parametrize("payload", [
    {"status": "in_progress"}, {"status": "halftime"},
    {"status": "overtime"}, {"status": "live"},
    {"status": "delayed", "quarter": 2},
])
def test_nfl_in_progress_statuses_normalize_live(payload):
    assert parse_game_progress({"game_id": "g", **payload}).state == "live"


@pytest.mark.parametrize("status", ["scheduled", "pregame", "final", "complete", None])
def test_nfl_non_live_statuses_do_not_normalize_live(status):
    assert parse_game_progress({"game_id": "g", "status": status}).state != "live"


def test_only_user_starters_in_live_games_count():
    row = {"starters": ["user-live", "user-final"], "players": ["user-live", "user-final", "bench-live"]}
    projections = {key: projection(key, key) for key in row["players"]}
    games = {
        "user-live": GameProgress("user-live", "live", 0.5),
        "user-final": GameProgress("user-final", "final", 0),
        "bench-live": GameProgress("bench-live", "live", 0.5),
    }
    assert count_live_starters(row, projections, games) == 1


def test_opponent_live_starter_counts_but_bench_only_does_not():
    opponent = {"starters": ["opp-live"], "players": ["opp-live"]}
    bench_only = {"starters": ["future"], "players": ["future", "bench-live"]}
    projections = {key: projection(key, key) for key in ["opp-live", "future", "bench-live"]}
    games = {
        "opp-live": GameProgress("opp-live", "live", 0.5),
        "future": GameProgress("future", "pregame", 1),
        "bench-live": GameProgress("bench-live", "live", 0.5),
    }
    assert count_live_starters(opponent, projections, games) == 1
    assert count_live_starters(bench_only, projections, games) == 0


def test_completed_future_scoring_and_missing_state_never_count_live():
    row = {"starters": ["final", "future", "missing"], "points": 138.5}
    projections = {key: projection(key, key) for key in row["starters"]}
    games = {
        "final": GameProgress("final", "final", 0),
        "future": GameProgress("future", "pregame", 1),
    }
    assert count_live_starters(row, projections, games) == 0


def side(score, probability=None):
    return MatchupSide(1, "Team", "Owner", "1-0", score, win_probability=probability)


def matchup(name, live=False, user_live=0, opponent_live=0, user_score=10, opponent_score=9, probability=None):
    return FantasyMatchup(
        name, name, 1, MatchupStatus.LIVE if live else MatchupStatus.PREGAME,
        side(user_score, probability), side(opponent_score),
        scheduler_is_live=live,
        user_live_starters_count=user_live,
        opponent_live_starters_count=opponent_live,
    )


def sleeper_state(*matchups):
    dashboard = DashboardModel(SleeperUser("1", "user", "User"), "2026", 1, list(matchups))
    client = MagicMock()
    client.get_dashboard.return_value = dashboard
    plugin = SleeperFantasyMatchups({"id": "sleeper_fantasy_matchups"}, client=client, monotonic=lambda: 1)
    return plugin.get_scheduler_state({"sleeperUser": "user", "leagueId1": "11"}, None, None)["matchups"]


def test_multiple_sleeper_matchups_aggregate_live_starters_and_leads():
    value = sleeper_state(
        matchup("one", True, user_live=1, opponent_live=1, user_score=12, opponent_score=10, probability=0.5),
        matchup("two", False, user_score=7, opponent_score=11, probability=0.71),
    )
    assert value["total_count"] == 2
    assert value["live_count"] == 1 and value["any_live"] is True
    assert value["live_starters_count"] == 2
    assert value["user_leading_count"] == 1 and value["user_trailing_count"] == 1
    assert value["min_user_win_probability"] == 0.5
    assert value["max_user_win_probability"] == 0.71


def test_two_live_matchups_count_two_and_bench_only_model_stays_false():
    value = sleeper_state(matchup("one", True, user_live=1), matchup("two", True, opponent_live=1))
    assert value["live_count"] == 2
    bench_only = sleeper_state(matchup("bench", False))
    assert bench_only["live_count"] == 0 and bench_only["any_live"] is False


@pytest.mark.parametrize("probability,expected", [(0.5, True), (0.35, True), (0.65, True), (0.34, False), (0.66, False), (None, False)])
def test_sleeper_close_definition(probability, expected):
    item = matchup("one", True, probability=probability)
    assert SleeperFantasyMatchups._is_close(item) is expected


def flatten(value, prefix=""):
    paths = set()
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            paths.update(flatten(item, path))
        else:
            paths.add(path)
    return paths


def test_plugin_schema_paths_match_actual_state_and_metadata_is_valid():
    mlb_plugin = MlbLiveScore({"id": "mlb_live_score"}, data_client=MagicMock())
    mlb_actual = mlb_state(game(GameState.LIVE))
    sleeper_plugin = SleeperFantasyMatchups({"id": "sleeper_fantasy_matchups"}, client=MagicMock())
    sleeper_actual = sleeper_state(matchup("one", True, probability=0.5))
    for schema, actual in [
        (mlb_plugin.get_scheduler_state_schema(), {"game": mlb_actual}),
        (sleeper_plugin.get_scheduler_state_schema(), {"matchups": sleeper_actual}),
    ]:
        assert set(schema) <= flatten(actual)
        assert all(metadata.get("label") and metadata.get("type") in {
            "boolean", "number", "enum", "string", "probability", "datetime"
        } for metadata in schema.values())
