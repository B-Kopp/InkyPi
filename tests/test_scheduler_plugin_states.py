import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.mlb_live_score.mlb_live_score import MlbLiveScore
from plugins.mlb_live_score.preview_fixtures import preview_states
from plugins.sleeper_fantasy_matchups.models import MatchupStatus
from plugins.sleeper_fantasy_matchups.sleeper_fantasy_matchups import SleeperFantasyMatchups


class Device:
    def get_resolution(self):
        return (800, 480)

    def get_config(self, key, default=None):
        return "horizontal" if key == "orientation" else default


def test_mlb_exposes_normalized_state_and_reuses_model_for_render():
    client = MagicMock()
    client.get_presentation.return_value = preview_states()["live_runner_first"]
    renderer = MagicMock()
    renderer.render.return_value = Image.new("RGB", (8, 8))
    plugin = MlbLiveScore(
        {"id": "mlb_live_score"}, data_client=client, renderer=renderer, monotonic=lambda: 10
    )
    settings = {"mlbTeamId": "144"}
    state = plugin.get_scheduler_state(settings, Device(), None)
    plugin.generate_image(settings, Device())
    assert state["game"]["status"] == "live"
    assert state["game"]["is_live"] is True
    assert state["game"]["selected_team"] == "ATL"
    client.get_presentation.assert_called_once_with(144)


def test_sleeper_exposes_counts_and_reuses_dashboard_for_render():
    live_close = SimpleNamespace(
        status=MatchupStatus.LIVE,
        scheduler_is_live=True,
        user_live_starters_count=1,
        opponent_live_starters_count=0,
        user_side=SimpleNamespace(current_score=50, win_probability=0.52),
        opponent_side=SimpleNamespace(current_score=48),
    )
    live_not_close = SimpleNamespace(
        status=MatchupStatus.LIVE,
        scheduler_is_live=True,
        user_live_starters_count=0,
        opponent_live_starters_count=1,
        user_side=SimpleNamespace(current_score=80, win_probability=0.8),
        opponent_side=SimpleNamespace(current_score=40),
    )
    final = SimpleNamespace(
        status=MatchupStatus.FINAL,
        scheduler_is_live=False,
        user_live_starters_count=0,
        opponent_live_starters_count=0,
        user_side=None,
        opponent_side=None,
    )
    dashboard = SimpleNamespace(
        sleeper_user=SimpleNamespace(user_id="100"),
        matchups=[live_close, live_not_close, final],
    )
    client = MagicMock()
    client.get_dashboard.return_value = dashboard
    renderer = MagicMock()
    renderer.render.return_value = Image.new("RGB", (8, 8))
    plugin = SleeperFantasyMatchups(
        {"id": "sleeper_fantasy_matchups"},
        client=client,
        renderer=renderer,
        monotonic=lambda: 10,
    )
    settings = {"sleeperUser": "guru", "leagueId1": "11"}
    state = plugin.get_scheduler_state(settings, Device(), None)
    plugin.generate_image(settings, Device())
    assert state["matchups"]["live_count"] == 2
    assert state["matchups"]["any_live"] is True
    assert state["matchups"]["close_game_count"] == 1
    assert state["matchups"]["live_starters_count"] == 2
    client.get_dashboard.assert_called_once_with("guru", ["11"])
