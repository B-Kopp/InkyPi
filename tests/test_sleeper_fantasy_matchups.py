from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.sleeper_fantasy_matchups.analytics import (
    aggregate_remaining,
    estimate_win_probability,
    remaining_projection,
    score_projected_stats,
)
from plugins.sleeper_fantasy_matchups.models import (
    GameProgress,
    MatchupSide,
    MatchupStatus,
    PlayerProjection,
)
from plugins.sleeper_fantasy_matchups.preview_fixtures import preview_states
from plugins.sleeper_fantasy_matchups.projections import (
    SleeperProjectionAdapter,
    parse_game_progress,
    parse_projection,
)
from plugins.sleeper_fantasy_matchups.renderer import (
    Density,
    MatchupDashboardRenderer,
    SPACE_REFERENCE,
    TYPE_REFERENCE,
    display_context,
    fit_text,
    layout_for_count,
)
from plugins.sleeper_fantasy_matchups.sleeper_client import (
    SleeperClient,
    classify_matchup_status,
    normalize_roster,
    score_from_matchup,
    selected_league_ids,
)
from plugins.sleeper_fantasy_matchups.sleeper_fantasy_matchups import (
    SleeperFantasyMatchups,
    setting_enabled,
)

NOW = datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


def league_payload(league_id, name=None, multiplier=.04, status="in_season", playoff=15):
    return {
        "league_id": str(league_id),
        "name": name or f"League {league_id}",
        "season": "2026",
        "status": status,
        "scoring_settings": {"pass_yd": multiplier},
        "settings": {"playoff_week_start": playoff},
    }


def league_resources(league_id, user_roster=2, opponent_roster=8, owner="100", **changes):
    players = (f"{league_id}1", f"{league_id}2")
    data = {
        "league": league_payload(league_id),
        "users": [
            {"user_id": owner, "username": "guru", "display_name": "Guru", "metadata": {"team_name": f"My Team {league_id}"}},
            {"user_id": f"opp{league_id}", "username": f"rival{league_id}", "display_name": f"Rival {league_id}", "metadata": {"team_name": f"Opp Team {league_id}"}},
        ],
        "rosters": [
            {"roster_id": user_roster, "owner_id": owner, "starters": [players[0]], "players": [players[0]], "settings": {"wins": 3, "losses": 1, "ties": 0}},
            {"roster_id": opponent_roster, "owner_id": f"opp{league_id}", "starters": [players[1]], "players": [players[1]], "settings": {"wins": 2, "losses": 2, "ties": 1}},
        ],
        "matchups": [
            {"roster_id": user_roster, "matchup_id": 7, "points": 50.5, "starters": [players[0]], "players_points": {players[0]: 50.5}},
            {"roster_id": opponent_roster, "matchup_id": 7, "points": 44.0, "starters": [players[1]], "players_points": {players[1]: 44.0}},
        ],
    }
    data.update(changes)
    return data


class FakeSession:
    def __init__(self, league_ids=("11",), resources=None, invalid_user=False, fail=()):
        self.league_ids = tuple(str(value) for value in league_ids)
        self.resources = resources or {value: league_resources(value) for value in self.league_ids}
        self.invalid_user = invalid_user
        self.fail = set(fail)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if any(value in url for value in self.fail):
            raise requests.Timeout("timeout")
        if url.endswith("/state/nfl"):
            return FakeResponse({"season": "2026", "league_season": "2026", "week": 1, "season_type": "regular"})
        if "/leagues/nfl/" in url:
            return FakeResponse([self.resources[value]["league"] for value in self.league_ids])
        if "/v1/user/" in url:
            return FakeResponse({} if self.invalid_user else {"user_id": "100", "username": "guru", "display_name": "Gridiron Guru"})
        for league_id, values in self.resources.items():
            prefix = f"/league/{league_id}"
            if prefix not in url:
                continue
            if url.endswith("/rosters"):
                return FakeResponse(values["rosters"])
            if url.endswith("/users"):
                return FakeResponse(values["users"])
            if "/matchups/" in url:
                return FakeResponse(values["matchups"])
            if url.endswith(prefix):
                return FakeResponse(values["league"])
        raise AssertionError(url)


class FakeProjectionAdapter:
    def __init__(self, league_ids=("11",), state="pregame", missing=()):
        self.calls = []
        self.projections = {}
        self.games = {}
        for league_id in league_ids:
            for suffix in ("1", "2"):
                player_id = f"{league_id}{suffix}"
                if player_id in missing:
                    continue
                game_id = f"g{player_id}"
                self.projections[player_id] = PlayerProjection(
                    player_id, "QB" if suffix == "1" else "WR", game_id, {"pass_yd": 250}
                )
                fraction = {"pregame": 1.0, "live": .5, "final": 0.0}[state]
                self.games[game_id] = GameProgress(game_id, state, fraction)

    def get_week(self, season, week, season_type="regular"):
        self.calls.append((season, week, season_type))
        return self.projections, self.games


def client_for(league_ids=("11",), resources=None, adapter=None, **session_kwargs):
    session = FakeSession(league_ids, resources, **session_kwargs)
    adapter = adapter or FakeProjectionAdapter(league_ids)
    return SleeperClient(session, adapter, lambda: NOW), session


def test_username_resolves_to_user_id():
    client, _ = client_for()
    assert client.resolve_user("guru").user_id == "100"


def test_numerical_user_id_resolves():
    client, session = client_for()
    assert client.resolve_user("100").username == "guru"
    assert session.calls[0][0].endswith("/user/100")


def test_invalid_user_has_clear_error():
    client, _ = client_for(invalid_user=True)
    with pytest.raises(RuntimeError, match="not found"):
        client.resolve_user("nobody")


@pytest.mark.parametrize("league_ids", [("11",), ("11", "22", "33")])
def test_user_league_discovery(league_ids):
    client, _ = client_for(league_ids)
    assert [value.league_id for value in client.discover_leagues("100", "2026")] == list(league_ids)


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_one_to_four_selected_leagues(count):
    ids = tuple(str(11 * value) for value in range(1, count + 1))
    client, _ = client_for(ids)
    dashboard = client.get_dashboard("guru", list(ids))
    assert len(dashboard.matchups) == count
    # The fake matchup rows already contain fantasy scoring, so an active week
    # must never be held in PREGAME by stale scheduled game rows.
    assert all(value.status is MatchupStatus.LIVE for value in dashboard.matchups)


def test_nonzero_fantasy_score_does_not_make_scheduler_matchup_live():
    client, _ = client_for(adapter=FakeProjectionAdapter(("11",), "pregame"))
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.status is MatchupStatus.LIVE  # display remains backward-compatible
    assert matchup.scheduler_is_live is False
    assert matchup.user_live_starters_count == 0
    assert matchup.opponent_live_starters_count == 0


def test_user_and_opponent_live_starters_set_scheduler_live_state():
    client, _ = client_for(adapter=FakeProjectionAdapter(("11",), "live"))
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.scheduler_is_live is True
    assert matchup.user_live_starters_count == 1
    assert matchup.opponent_live_starters_count == 1


@pytest.mark.parametrize(
    "live_player,expected_counts",
    [("111", (1, 0)), ("112", (0, 1))],
)
def test_either_lineups_actual_starting_player_can_make_matchup_live(live_player, expected_counts):
    adapter = FakeProjectionAdapter(("11",), "pregame")
    game_id = adapter.projections[live_player].game_id
    adapter.games[game_id] = GameProgress(game_id, "live", 0.5)
    client, _ = client_for(adapter=adapter)
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.scheduler_is_live is True
    assert (matchup.user_live_starters_count, matchup.opponent_live_starters_count) == expected_counts


def test_live_bench_player_does_not_make_real_matchup_live():
    resources = {"11": league_resources("11")}
    resources["11"]["matchups"][0]["players"] = ["111", "bench"]
    adapter = FakeProjectionAdapter(("11",), "pregame")
    adapter.projections["bench"] = PlayerProjection("bench", "RB", "gbench", {})
    adapter.games["gbench"] = GameProgress("gbench", "live", 0.5)
    client, _ = client_for(("11",), resources, adapter)
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.scheduler_is_live is False
    assert matchup.user_live_starters_count == 0


def status_rows(points=0, player_points=None):
    return (
        {"starters": ["p1"], "points": points, "players_points": player_points or {}},
        {"starters": ["p2"], "points": 0, "players_points": {}},
    )


def status_sources(left="pregame", right="pregame"):
    projections = {
        "p1": PlayerProjection("p1", "QB", "g1", {}),
        "p2": PlayerProjection("p2", "WR", "g2", {}),
    }
    games = {
        "g1": GameProgress("g1", left, 1 if left == "pregame" else 0),
        "g2": GameProgress("g2", right, 1 if right == "pregame" else 0),
    }
    return projections, games


def test_active_week_before_kickoff_is_pregame():
    user, opponent = status_rows()
    projections, games = status_sources()
    assert classify_matchup_status(user, opponent, games, projections, "in_season") is MatchupStatus.PREGAME


def test_active_week_with_game_in_progress_is_live():
    user, opponent = status_rows()
    projections, games = status_sources("live", "pregame")
    assert classify_matchup_status(user, opponent, games, projections, "in_season") is MatchupStatus.LIVE


def test_active_week_with_recorded_player_points_cannot_remain_pregame():
    user, opponent = status_rows(player_points={"p1": 7.4})
    projections, games = status_sources()
    assert classify_matchup_status(user, opponent, games, projections, "in_season") is MatchupStatus.LIVE


def test_completed_matchup_is_final():
    user, opponent = status_rows(points=100)
    projections, games = status_sources("final", "final")
    assert classify_matchup_status(user, opponent, games, projections, "in_season") is MatchupStatus.FINAL


def test_completed_game_plus_later_kickoff_is_live_between_windows():
    user, opponent = status_rows()
    projections, games = status_sources("final", "pregame")
    assert classify_matchup_status(user, opponent, games, projections, "in_season") is MatchupStatus.LIVE


def test_more_than_four_leagues_rejected():
    with pytest.raises(RuntimeError, match="1 and 4"):
        selected_league_ids({f"leagueId{i}": str(i) for i in range(1, 6)} | {"selectedLeagueIds[]": ["1", "2", "3", "4", "5"]})


def test_duplicate_leagues_rejected():
    with pytest.raises(RuntimeError, match="different"):
        selected_league_ids({"leagueId1": "11", "leagueId2": "11"})


def test_user_roster_is_identified_independently_per_league():
    resources = {"11": league_resources("11", user_roster=2), "22": league_resources("22", user_roster=19)}
    client, _ = client_for(("11", "22"), resources)
    dashboard = client.get_dashboard("guru", ["11", "22"])
    assert [value.user_side.roster_id for value in dashboard.matchups] == [2, 19]


def test_user_absent_from_one_league_does_not_hide_other():
    resources = {"11": league_resources("11"), "22": league_resources("22", owner="someone_else")}
    client, _ = client_for(("11", "22"), resources)
    dashboard = client.get_dashboard("guru", ["11", "22"])
    assert dashboard.matchups[0].error_state is None
    assert "does not own" in dashboard.matchups[1].error_state


def test_correct_matchup_and_opponent_are_found_by_roster_and_matchup_id():
    values = league_resources("11")
    values["matchups"].append({"roster_id": 99, "matchup_id": 9, "points": 999})
    values["rosters"].append({"roster_id": 99, "owner_id": "other", "settings": {}})
    client, _ = client_for(("11",), {"11": values})
    result = client.get_dashboard("guru", ["11"]).matchups[0]
    assert result.user_side.roster_id == 2
    assert result.opponent_side.roster_id == 8
    assert result.opponent_side.current_score == 44


def test_current_score_and_custom_points_precedence():
    assert score_from_matchup({"points": 99.5}) == 99.5
    assert score_from_matchup({"points": 99.5, "custom_points": 87.25}) == 87.25
    assert score_from_matchup({"points": 99.5, "custom_points": 0}) == 0


@pytest.mark.parametrize(
    "metadata,display_name,username,expected",
    [
        ({"team_name": "Metadata Team"}, "Display", "username", "Metadata Team"),
        ({}, "Display", "username", "Display"),
        ({}, "", "username", "username"),
        ({}, "", "", "TEAM 2"),
    ],
)
def test_team_name_fallback(metadata, display_name, username, expected):
    roster = normalize_roster(
        {"roster_id": 2, "owner_id": "100", "settings": {}},
        {"100": {"metadata": metadata, "display_name": display_name, "username": username}},
    )
    assert roster.team_name == expected


def test_records_and_ties_are_independent():
    client, _ = client_for()
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.user_side.record == "3-1"
    assert matchup.opponent_side.record == "2-2-1"


def test_bye_or_no_opponent():
    values = league_resources("11")
    values["matchups"] = values["matchups"][:1]
    client, _ = client_for(("11",), {"11": values})
    assert client.get_dashboard("guru", ["11"]).matchups[0].status is MatchupStatus.BYE


def test_playoff_matchup_is_labeled_independently():
    values = league_resources("11")
    values["league"]["settings"]["playoff_week_start"] = 1
    client, _ = client_for(("11",), {"11": values})
    result = client.get_dashboard("guru", ["11"]).matchups[0]
    assert result.is_playoff and result.status_label == "PLAYOFF · LIVE"


def test_final_matchup_status_and_probability():
    adapter = FakeProjectionAdapter(("11",), "final")
    client, _ = client_for(("11",), adapter=adapter)
    result = client.get_dashboard("guru", ["11"]).matchups[0]
    assert result.status is MatchupStatus.FINAL
    assert result.user_side.win_probability == 1.0


def test_different_matchup_states_across_selected_leagues():
    adapter = FakeProjectionAdapter(("11", "22"))
    for player_id in ("221", "222"):
        game_id = adapter.projections[player_id].game_id
        adapter.games[game_id] = GameProgress(game_id, "final", 0)
    client, _ = client_for(("11", "22"), adapter=adapter)
    values = client.get_dashboard("guru", ["11", "22"]).matchups
    assert [value.status for value in values] == [MatchupStatus.LIVE, MatchupStatus.FINAL]
    assert [value.scheduler_is_live for value in values] == [False, False]


def test_projection_parsing():
    parsed = parse_projection({"player_id": "p1", "game_id": "g1", "player": {"position": "QB"}, "stats": {"pass_yd": "250", "bad": None}})
    assert parsed == PlayerProjection("p1", "QB", "g1", {"pass_yd": 250.0})


def test_projection_endpoint_unavailable_is_optional():
    session = MagicMock()
    session.get.side_effect = requests.Timeout("offline")
    adapter = SleeperProjectionAdapter(session, lambda: NOW)
    assert adapter.get_week("2026", 1) == ({}, {})


def test_null_schedule_status_is_a_valid_pregame_row():
    assert parse_game_progress({"game_id": "g", "week": 1, "status": None}).state == "pregame"


def test_projection_adapter_uses_schedule_and_filters_current_week():
    session = MagicMock()
    session.get.side_effect = [
        FakeResponse([{"player_id": "p1", "game_id": "g1", "player": {"position": "QB"}, "stats": {"pass_yd": 250}}]),
        FakeResponse([
            {"game_id": "old", "week": 2, "status": "complete"},
            {"game_id": "g1", "week": 1, "status": "scheduled"},
        ]),
    ]
    projections, games = SleeperProjectionAdapter(session, lambda: NOW).get_week("2026", 1)
    assert set(projections) == {"p1"} and set(games) == {"g1"}
    assert "/schedule/nfl/regular/2026" in session.get.call_args_list[1].args[0]


def test_successful_projection_and_schedule_payloads_are_cached_by_week():
    session = MagicMock()
    session.get.side_effect = [
        FakeResponse([{"player_id": "p1", "game_id": "g1", "stats": {"pass_yd": 250}}]),
        FakeResponse([{"game_id": "g1", "week": 1, "status": "scheduled"}]),
    ]
    adapter = SleeperProjectionAdapter(session, lambda: NOW)
    assert adapter.get_week("2026", 1) == adapter.get_week("2026", 1)
    assert session.get.call_count == 2


def test_nfl_schedule_state_cache_refreshes_without_redownloading_projections():
    current = [NOW]
    session = MagicMock()
    session.get.side_effect = [
        FakeResponse([{"player_id": "p1", "game_id": "g1", "stats": {"pass_yd": 250}}]),
        FakeResponse([{"game_id": "g1", "week": 1, "status": "scheduled"}]),
        FakeResponse([{"game_id": "g1", "week": 1, "status": "in_progress"}]),
    ]
    adapter = SleeperProjectionAdapter(session, lambda: current[0])
    assert adapter.get_week("2026", 1)[1]["g1"].state == "pregame"
    current[0] += timedelta(seconds=30)
    assert adapter.get_week("2026", 1)[1]["g1"].state == "pregame"
    current[0] += timedelta(seconds=31)
    assert adapter.get_week("2026", 1)[1]["g1"].state == "live"
    assert session.get.call_count == 3


def test_missing_projection_hides_both_estimates_without_hiding_score():
    adapter = FakeProjectionAdapter(("11",), missing=("111",))
    client, _ = client_for(("11",), adapter=adapter)
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.user_side.current_score == 50.5
    assert matchup.user_side.projected_final is None
    assert not matchup.projections_available


def test_partial_projection_data_disables_probability_without_failing_matchup():
    adapter = FakeProjectionAdapter(("11",), missing=("112",))
    matchup = client_for(("11",), adapter=adapter)[0].get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.user_side.projected_final is not None
    assert matchup.opponent_side.projected_final is None
    assert matchup.user_side.win_probability is None
    assert not matchup.projections_available


@pytest.mark.parametrize(
    "state,fraction,expected",
    [("pregame", 1, 12), ("live", .25, 3), ("final", 0, 0), ("live", None, None)],
)
def test_remaining_points_calculation_and_no_double_counting(state, fraction, expected):
    assert remaining_projection(12, state, fraction) == expected


def test_projected_final_adds_only_remaining_to_current_score():
    adapter = FakeProjectionAdapter(("11",), "live")
    client, _ = client_for(("11",), adapter=adapter)
    side = client.get_dashboard("guru", ["11"]).matchups[0].user_side
    assert side.estimated_remaining_points == pytest.approx(5.0)
    assert side.projected_final == pytest.approx(55.5)


def test_projections_and_win_probability_remain_available_during_live_scoring():
    client, _ = client_for(("11",), adapter=FakeProjectionAdapter(("11",), "live"))
    matchup = client.get_dashboard("guru", ["11"]).matchups[0]
    assert matchup.status is MatchupStatus.LIVE
    assert matchup.projections_available
    assert matchup.user_side.projected_final is not None
    assert matchup.user_side.win_probability is not None


def test_different_league_scoring_settings_change_projection():
    resources = {"11": league_resources("11"), "22": league_resources("22")}
    resources["22"]["league"]["scoring_settings"]["pass_yd"] = .08
    client, _ = client_for(("11", "22"), resources)
    values = client.get_dashboard("guru", ["11", "22"]).matchups
    assert values[0].user_side.estimated_remaining_points == 10
    assert values[1].user_side.estimated_remaining_points == 20


def test_score_projected_stats_uses_only_one_passed_scoring_map():
    assert score_projected_stats({"pass_yd": 250, "pass_td": 2}, {"pass_yd": .04}) == 10
    assert score_projected_stats({"pass_yd": 250, "pass_td": 2}, {"pass_yd": .04, "pass_td": 6}) == 22


def test_balanced_matchup_probability():
    probability = estimate_win_probability(50, 50, 20, 20, 25, 25)
    assert probability == pytest.approx(.5)


def test_favorite_and_underdog_probabilities():
    favorite = estimate_win_probability(80, 50, 20, 20, 100, 100)
    underdog = estimate_win_probability(50, 80, 20, 20, 100, 100)
    assert favorite > .5 and underdog < .5


@pytest.mark.parametrize("user,opp,expected", [(100, 90, 1.0), (90, 100, 0.0), (90, 90, None)])
def test_completed_winner_loser_and_tie(user, opp, expected):
    assert estimate_win_probability(user, opp, 0, 0, 0, 0, completed=True) == expected


def test_probability_unavailable_and_no_divide_by_zero_or_nan():
    assert estimate_win_probability(0, 0, None, 0, 0, 0) is None
    assert estimate_win_probability(70, 60, 0, 0, 0, 0) is None
    expected, variance = aggregate_remaining([(0, "QB"), (10, "WR")])
    assert expected == 10 and variance > 0


def test_projection_game_progress_parser():
    assert parse_game_progress({"game_id": "g", "status": "final"}).state == "final"
    live = parse_game_progress({"game_id": "g", "status": "in_progress", "quarter": 3, "time": "7:30"})
    assert live.state == "live" and live.remaining_fraction == pytest.approx(.375)
    assert parse_game_progress({"game_id": "g", "status": "scheduled"}).remaining_fraction == 1


def test_league_a_data_never_appears_in_league_b():
    client, _ = client_for(("11", "22"))
    values = client.get_dashboard("guru", ["11", "22"]).matchups
    assert values[0].user_side.team_name == "My Team 11"
    assert values[1].user_side.team_name == "My Team 22"
    assert values[0].opponent_side.roster_id == 8


def test_league_specific_cache_keys_include_league_and_week():
    client, _ = client_for(("11", "22"))
    client.get_dashboard("guru", ["11", "22"])
    keys = set(client._cache)
    assert ("matchups", "11", 1) in keys and ("matchups", "22", 1) in keys
    assert ("rosters", "11") in keys and ("rosters", "22") in keys


def test_switching_selection_does_not_reuse_other_league_matchup():
    client, session = client_for(("11", "22"))
    assert client.get_dashboard("guru", ["11"]).matchups[0].league_id == "11"
    assert client.get_dashboard("guru", ["22"]).matchups[0].league_id == "22"
    matchup_urls = [url for url, _, _ in session.calls if "/matchups/" in url]
    assert any("/11/" in value for value in matchup_urls) and any("/22/" in value for value in matchup_urls)


def test_one_league_failure_does_not_prevent_other_card():
    client, _ = client_for(("11", "22"), fail=("/league/22/rosters",))
    dashboard = client.get_dashboard("guru", ["11", "22"])
    assert dashboard.matchups[0].user_side is not None
    assert dashboard.matchups[1].status is MatchupStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "fixture,count,densities",
    [
        ("one_close_live", 1, [Density.HERO]),
        ("two_live", 2, [Density.LARGE, Density.LARGE]),
        ("three_mixed", 3, [Density.MEDIUM, Density.MEDIUM, Density.MEDIUM]),
        ("four_live", 4, [Density.COMPACT] * 4),
    ],
)
def test_adaptive_800x480_layouts(fixture, count, densities):
    renderer = MatchupDashboardRenderer()
    image = renderer.render(preview_states()[fixture], (800, 480))
    assert image.size == (800, 480)
    assert [item.density for item in layout_for_count((800, 480), count)] == densities


def test_four_league_layout_is_two_by_two():
    boxes = [item.bounds for item in layout_for_count((800, 480), 4)]
    assert boxes[0][1] == boxes[1][1] and boxes[2][1] == boxes[3][1]
    assert boxes[0][0] == boxes[2][0] and boxes[1][0] == boxes[3][0]


@pytest.mark.parametrize("dimensions", [(640, 400), (480, 800), (1024, 600)])
def test_alternate_display_resolution(dimensions):
    image = MatchupDashboardRenderer().render(preview_states()["four_mixed"], dimensions)
    assert image.size == dimensions and image.mode == "RGB"


@pytest.mark.parametrize(
    "fixture",
    [
        "long_names_high_scores", "two_long_names", "three_long_names",
        "four_long_names", "projections_unavailable",
        "four_projections_unavailable", "four_mixed", "four_one_failed",
        "one_no_matchup",
    ],
)
def test_rendering_edge_fixtures(fixture):
    renderer = MatchupDashboardRenderer()
    image = renderer.render(preview_states()[fixture], (800, 480))
    assert isinstance(image, Image.Image)
    assert image.getbbox() == (0, 0, 800, 480)


@pytest.mark.parametrize("fixture", ["one_close_live", "two_live", "three_mixed", "four_live", "four_one_failed"])
def test_all_measured_text_stays_inside_its_card(fixture):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[fixture], (800, 480))
    for role, text, card in renderer.last_text_bounds:
        assert card[0] <= text[0] <= text[2] <= card[2], role
        assert card[1] <= text[1] <= text[3] <= card[3], role


@pytest.mark.parametrize("fixture", ["one_close_live", "two_live", "three_mixed", "four_live"])
def test_important_text_retains_ten_pixel_card_clearance(fixture):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[fixture], (800, 480))
    for role, text, card in renderer.last_text_bounds:
        assert min(text[0] - card[0], text[1] - card[1], card[2] - text[2], card[3] - text[3]) >= 10, role


@pytest.mark.parametrize("fixture", ["one_close_live", "two_live", "three_mixed", "four_live"])
def test_header_title_never_overlaps_status(fixture):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[fixture], (800, 480))
    for card in renderer.last_card_bounds:
        league = next(box for role, box, owner in renderer.last_text_bounds if owner == card and role == "league")
        status = next(box for role, box, owner in renderer.last_text_bounds if owner == card and role == "status")
        assert league[2] < status[0]


@pytest.mark.parametrize("fixture", ["one_close_live", "two_live", "three_mixed", "four_live", "four_long_names"])
def test_team_names_and_scores_respect_protected_center(fixture):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[fixture], (800, 480))
    for card, content in zip(renderer.last_card_bounds, renderer.last_content_bounds):
        mid = (content[0] + content[2]) // 2
        names = [box for role, box, owner in renderer.last_text_bounds if owner == card and role == "team_name"]
        scores = [box for role, box, owner in renderer.last_text_bounds if owner == card and role == "score"]
        if len(names) == 2:
            assert names[0][2] < mid and names[1][0] > mid
        if len(scores) == 2:
            assert scores[0][2] < mid - 10 and scores[1][0] > mid + 10
            assert scores[0][1] == scores[1][1]
            assert scores[0][3] == scores[1][3]


def test_projection_and_probability_render_when_available():
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()["two_live"], (800, 480))
    roles = [role for role, _, _ in renderer.last_text_bounds]
    assert roles.count("projection") == 4
    assert roles.count("win_percent") == 4
    assert roles.count("win_label") == 2


def test_unavailable_projection_is_one_line_without_probability_placeholders():
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()["projections_unavailable"], (800, 480))
    roles = [role for role, _, _ in renderer.last_text_bounds]
    assert roles.count("projection_unavailable") == 1
    assert "win_percent" not in roles and "win_label" not in roles


def test_projection_without_calculable_probability_has_no_dangling_win_ui():
    fixture = preview_states()["one_close_live"]
    fixture.matchups[0].user_side.win_probability = None
    fixture.matchups[0].opponent_side.win_probability = None
    renderer = MatchupDashboardRenderer()
    renderer.render(fixture, (800, 480))
    roles = [role for role, _, _ in renderer.last_text_bounds]
    assert roles.count("projection") == 2
    assert "win_percent" not in roles and "win_label" not in roles


@pytest.mark.parametrize("fixture", ["one_close_live", "two_live", "three_mixed", "four_live"])
def test_cards_never_clip_or_overlap(fixture):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[fixture], (800, 480))
    boxes = renderer.last_card_bounds
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 < x1 <= 800 and 0 <= y0 < y1 <= 480
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            horizontal = first[2] <= second[0] or second[2] <= first[0]
            vertical = first[3] <= second[1] or second[3] <= first[1]
            assert horizontal or vertical


def test_essential_compact_typography_remains_readable():
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()["four_live"], (800, 480))
    roles = renderer.last_typography[Density.COMPACT]
    assert roles.card_league >= 17
    assert roles.team_name >= 17
    assert roles.score >= 39
    assert roles.win_percent >= 18


@pytest.mark.parametrize("density", list(Density))
def test_semantic_typography_preserves_visual_hierarchy(density):
    roles = TYPE_REFERENCE[density]
    assert roles.score > roles.team_name > roles.win_percent
    assert roles.win_percent >= roles.card_status
    assert roles.card_league >= roles.team_record >= roles.context_secondary
    assert roles.team_name_min >= 15


def test_compact_density_drops_detail_before_essential_type():
    assert SPACE_REFERENCE[Density.COMPACT].detail_height == 0
    assert SPACE_REFERENCE[Density.MEDIUM].detail_height > 0
    assert TYPE_REFERENCE[Density.COMPACT].team_name >= 19
    assert TYPE_REFERENCE[Density.COMPACT].score >= 39


def test_text_fitting_respects_bounds_and_readable_minimum():
    draw = ImageDraw.Draw(Image.new("RGB", (300, 100)))
    text, font = fit_text(
        draw, "An Extremely Long Fantasy Team Name", 125, 24, 15, True,
    )
    assert draw.textlength(text, font=font) <= 125
    assert font.size >= 15
    assert text.endswith("…")


def test_content_bounds_have_density_padding():
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()["four_live"], (800, 480))
    for card, content in zip(renderer.last_card_bounds, renderer.last_content_bounds):
        assert content[0] - card[0] >= 18  # rail plus horizontal padding
        assert content[1] - card[1] >= 10
        assert card[2] - content[2] >= 10
        assert card[3] - content[3] >= 10


def test_context_never_combines_missing_points_with_zero_players():
    value = MatchupSide(1, "Test Team", "Owner", "1-0", 10, starters_remaining=0)
    assert display_context(value) == "CONTEXT UNAVAILABLE"


def test_plugin_generate_image_uses_settings_resolution_and_stable_id():
    fake_client = MagicMock()
    fake_client.get_dashboard.return_value = preview_states()["two_live"]
    plugin = SleeperFantasyMatchups(
        {"id": "sleeper_fantasy_matchups"}, client=fake_client,
        renderer=MatchupDashboardRenderer(),
    )
    device = MagicMock()
    device.get_resolution.return_value = (800, 480)
    device.get_config.return_value = "horizontal"
    settings = {"sleeperUser": "guru", "resolvedUserId": "100", "leagueId1": "11", "leagueId2": "22"}
    image = plugin.generate_image(settings, device)
    fake_client.get_dashboard.assert_called_once_with("100", ["11", "22"])
    assert image.size == (800, 480) and settings["resolvedUserId"] == "100"


def test_plugin_rejects_non_numeric_league_id():
    plugin = SleeperFantasyMatchups({"id": "sleeper_fantasy_matchups"}, client=MagicMock())
    with pytest.raises(RuntimeError, match="numbers only"):
        plugin.generate_image({"sleeperUser": "guru", "leagueId1": "abc"}, MagicMock())


def test_settings_form_supports_discovery_four_slots_defaults_and_prepopulation():
    html = (Path(__file__).resolve().parents[1] / "src/plugins/sleeper_fantasy_matchups/settings.html").read_text()
    assert 'name="sleeperUser"' in html and "api.sleeper.app/v1/state/nfl" in html
    assert 'name="leagueId{{ slot }}"' in html and "range(1, 5)" in html
    assert "pluginSettings[`leagueId${index + 1}`]" in html
    assert all(name in html for name in ("showProjectedFinal", "showWinProbability", "showRecords", "showContextRow"))
    assert setting_enabled(None) and setting_enabled("true") and not setting_enabled("false")


def test_manifest_identity_is_exact():
    import json
    path = Path(__file__).resolve().parents[1] / "src/plugins/sleeper_fantasy_matchups/plugin-info.json"
    manifest = json.loads(path.read_text())
    assert manifest["display_name"] == "Sleeper Fantasy Matchups"
    assert manifest["id"] == "sleeper_fantasy_matchups"
    assert manifest["class"] == "SleeperFantasyMatchups"
