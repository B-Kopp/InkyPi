"""Deterministic timing and physical-display regressions for existing plugins."""

from datetime import datetime, timedelta, timezone
from itertools import product
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageColor, ImageDraw

from tests.test_mlb_live_score import feed, status
from plugins.mlb_live_score.mlb_data import normalize_game
from plugins.mlb_live_score.mlb_live_score import MlbLiveScore
from plugins.mlb_live_score.preview_fixtures import base_game, presentation
from plugins.mlb_live_score.renderer import ScoreboardRenderer, base_runner_description
from plugins.mlb_live_score.renderer import _font as mlb_font
from plugins.sleeper_fantasy_matchups.preview_fixtures import preview_states
from plugins.sleeper_fantasy_matchups.renderer import (
    BLACK, CARD, CREAM, LIME, MIN_SECONDARY_SIZE, MUTED,
    MatchupDashboardRenderer, TYPE_REFERENCE,
)


LOCAL = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 17, 20, tzinfo=LOCAL)


def scheduler_state(game, *, no_game_today=False, now=NOW):
    client = MagicMock()
    client.get_presentation.return_value = presentation(game, no_game_today=no_game_today)
    device = MagicMock()
    device.get_config.return_value = "America/New_York"
    plugin = MlbLiveScore({"id": "mlb_live_score"}, data_client=client)
    return plugin.get_scheduler_state({"mlbTeamId": "144"}, device, now)["game"]


@pytest.mark.parametrize("minutes,expected", [(121, False), (120, True), (90, True), (0, True), (-1, False)])
def test_pregame_first_pitch_window(minutes, expected):
    game = base_game(is_pregame=True, uses_live_layout=False,
                     official_date=NOW.date().isoformat(), scheduled_time=NOW + timedelta(minutes=minutes))
    assert scheduler_state(game)["pregame_within_2_hours"] is expected


@pytest.mark.parametrize("hours,expected", [(1, True), (3, True), (3.01, False), (-1, False)])
def test_final_freshness_window(hours, expected):
    game = base_game(is_final=True, uses_live_layout=False, official_date=NOW.date().isoformat(),
                     ended_at=(NOW - timedelta(hours=hours)).astimezone(timezone.utc))
    assert scheduler_state(game)["final_within_3_hours"] is expected


def test_live_game_has_neither_recent_flag():
    game = base_game(scheduled_time=NOW + timedelta(minutes=30), ended_at=NOW)
    state = scheduler_state(game)
    assert not state["pregame_within_2_hours"] and not state["final_within_3_hours"]


@pytest.mark.parametrize("final", [False, True])
def test_no_game_today_never_sets_recent_flags(final):
    game = base_game(is_pregame=not final, is_final=final, uses_live_layout=False,
                     scheduled_time=NOW + timedelta(hours=1), ended_at=NOW - timedelta(hours=1))
    state = scheduler_state(game, no_game_today=True)
    assert not state["pregame_within_2_hours"] and not state["final_within_3_hours"]


def test_next_days_near_midnight_pregame_is_not_todays_game():
    now = NOW.replace(hour=23)
    game = base_game(is_pregame=True, uses_live_layout=False, scheduled_time=now + timedelta(hours=1))
    assert not scheduler_state(game, now=now)["pregame_within_2_hours"]


def test_recent_final_must_be_on_current_local_day():
    now = NOW.replace(hour=0, minute=30)
    game = base_game(is_final=True, ended_at=now - timedelta(hours=1), uses_live_layout=False)
    assert not scheduler_state(game, now=now)["final_within_3_hours"]


def test_configured_timezone_not_host_or_injected_timezone_sets_today():
    now = datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc)  # Sep 17 locally
    game = base_game(is_final=True, uses_live_layout=False, official_date="2026-09-17",
                     ended_at=now - timedelta(hours=1))
    assert scheduler_state(game, now=now)["final_within_3_hours"]


@pytest.mark.parametrize("end", [None, NOW.replace(tzinfo=None)])
def test_unavailable_end_time_never_invents_recent_final(end):
    game = base_game(is_final=True, uses_live_layout=False, ended_at=end)
    assert not scheduler_state(game)["final_within_3_hours"]


@pytest.mark.parametrize("now", [None, NOW.replace(tzinfo=None)])
def test_missing_or_naive_evaluation_time_is_safe(now):
    state = scheduler_state(base_game(is_pregame=True), now=now)
    assert not state["pregame_within_2_hours"] and not state["final_within_3_hours"]


def test_new_schema_fields_match_state_and_are_boolean():
    schema = MlbLiveScore({"id": "mlb_live_score"}, data_client=MagicMock()).get_scheduler_state_schema()
    state = scheduler_state(base_game())
    for field in ("pregame_within_2_hours", "final_within_3_hours"):
        assert schema["game." + field]["type"] == "boolean"
        assert isinstance(state[field], bool)


@pytest.mark.parametrize("complete,end,expected", [
    (True, "2026-09-18T00:00:00Z", datetime(2026, 9, 18, tzinfo=timezone.utc)),
    (False, "2026-09-18T00:00:00Z", None), (True, "2026-09-18T00:00:00", None),
    (True, None, None), (True, "invalid", None),
])
def test_final_normalization_uses_completed_terminal_play_not_feed_timestamp(complete, end, expected):
    data = feed(game_status=status("Final", "Final", "F"))
    data["metaData"] = {"timeStamp": "20260918_010000"}
    data["liveData"]["plays"]["allPlays"] = [
        {"about": {"isComplete": True, "endTime": "2026-09-17T23:00:00Z"}},
        {"about": {"isComplete": complete, "endTime": end}},
    ]
    assert normalize_game(data).ended_at == expected


def ended_half(data, inning=7, top=True):
    line = data["liveData"]["linescore"]
    line["outs"] = 3
    play = data["liveData"]["plays"]["currentPlay"]
    play["about"] = {"inning": inning, "isTopInning": top, "isComplete": True}
    play["count"] = {"balls": 2, "strikes": 2, "outs": 3}
    play["result"] = {"awayScore": 4, "homeScore": 3}
    play["matchup"].update(pitcher={"fullName": "Old Pitcher"}, batter={"fullName": "Old Batter"})
    return line, play


@pytest.mark.parametrize("inning,half,next_inning,next_half", [(7, "Top", 7, "BOTTOM"), (7, "Bottom", 8, "TOP")])
def test_next_offense_team_and_pair_establish_new_half(inning, half, next_inning, next_half):
    data = feed(inning=inning, half=half)
    line, play = ended_half(data, inning, half == "Top")
    line["offense"] = {"team": {"id": 144 if next_half == "BOTTOM" else 147}, "batter": {"fullName": "Next Batter"}}
    line["defense"] = {"pitcher": {"fullName": "Next Pitcher"}}
    game = normalize_game(data)
    assert (game.inning, game.inning_half, game.outs) == (next_inning, next_half, 0)
    assert (game.pitcher_name, game.batter_name, game.balls, game.strikes) == ("Next Pitcher", "Next Batter", 0, 0)
    assert not (game.runner_on_first or game.runner_on_second or game.runner_on_third)


def test_unverified_advanced_names_keep_ended_play_atomically():
    data = feed(bases=("first", "second"))
    line, play = ended_half(data)
    line["offense"]["batter"] = {"fullName": "Next Batter"}
    line["defense"]["pitcher"] = {"fullName": "Next Pitcher"}
    play["matchup"]["postOnThird"] = {"fullName": "Stranded Runner"}
    game = normalize_game(data)
    assert (game.inning, game.inning_half, game.outs) == (7, "TOP", 3)
    assert (game.pitcher_name, game.batter_name, game.balls, game.strikes) == ("Old Pitcher", "Old Batter", 2, 2)
    assert (game.runner_on_first, game.runner_on_second, game.runner_on_third) == (False, False, True)
    assert (game.away_runs, game.home_runs) == (4, 3)


def test_new_current_play_overrides_stale_third_out():
    data = feed(bases=("first", "third"))
    line, play = ended_half(data)
    play["about"] = {"inning": 7, "isTopInning": False, "isComplete": False}
    play["count"] = {"balls": 1, "strikes": 0, "outs": 0}
    play["matchup"] = {"pitcher": {"fullName": "Next Pitcher"}, "batter": {"fullName": "Next Batter"}}
    game = normalize_game(data)
    assert (game.inning, game.inning_half, game.outs) == (7, "BOTTOM", 0)
    assert (game.pitcher_name, game.batter_name, game.balls, game.strikes) == ("Next Pitcher", "Next Batter", 1, 0)
    assert not (game.runner_on_first or game.runner_on_second or game.runner_on_third)


def test_same_half_incomplete_play_cannot_blindly_reset_outs():
    data = feed()
    data["liveData"]["linescore"]["outs"] = 3
    play = data["liveData"]["plays"]["currentPlay"]
    play["about"] = {"inning": 7, "isTopInning": True, "isComplete": False}
    play["count"]["outs"] = 0
    game = normalize_game(data)
    assert game.outs == 3
    assert game.pitcher_name is None and game.batter_name is None
    assert game.balls is None and game.strikes is None


def test_next_batter_without_next_pitcher_is_not_a_reliable_transition():
    data = feed()
    line, play = ended_half(data)
    line["offense"] = {"team": {"id": 144}, "batter": {"fullName": "Next Batter"}}
    line["defense"] = {"pitcher": {"fullName": "Old Pitcher"}}
    game = normalize_game(data)
    assert (game.inning_half, game.outs, game.batter_name) == ("TOP", 3, "Old Batter")


def test_missing_play_half_metadata_cannot_pair_old_players_with_new_half():
    data = feed()
    line, play = ended_half(data)
    del play["about"]
    line["inningHalf"] = "Bottom"
    game = normalize_game(data)
    assert game.outs == 3
    assert game.pitcher_name is None and game.batter_name is None
    assert game.balls is None and game.strikes is None


def test_completed_play_history_proves_new_half_when_linescore_half_already_advanced():
    from copy import deepcopy
    data = feed()
    line, play = ended_half(data)
    data["liveData"]["plays"]["allPlays"] = [deepcopy(play)]
    line["inningHalf"] = "Bottom"
    play["about"] = {"inning": 7, "isTopInning": False, "isComplete": False}
    play["count"] = {"balls": 0, "strikes": 1, "outs": 0}
    play["matchup"] = {"pitcher": {"fullName": "Next Pitcher"}, "batter": {"fullName": "Next Batter"}}
    game = normalize_game(data)
    assert (game.inning_half, game.outs, game.pitcher_name, game.batter_name) == ("BOTTOM", 0, "Next Pitcher", "Next Batter")


def test_extra_inning_runner_retained_when_new_offense_is_verified():
    data = feed(inning=10, half="Top")
    line, play = ended_half(data, inning=10)
    line["offense"] = {"team": {"id": 144}, "batter": {"fullName": "Next Batter"}, "second": {"fullName": "Automatic Runner"}}
    line["defense"] = {"pitcher": {"fullName": "Next Pitcher"}}
    game = normalize_game(data)
    assert (game.inning_half, game.outs, game.runner_on_second) == ("BOTTOM", 0, True)


@pytest.mark.parametrize("occupied", list(product((False, True), repeat=3)))
def test_readable_runner_labels_wrap_at_phrase_boundaries_and_stay_centered(occupied):
    game = base_game(runner_on_first=occupied[0], runner_on_second=occupied[1], runner_on_third=occupied[2])
    renderer = ScoreboardRenderer()
    calls = []
    original = renderer._text_in_box
    def capture(draw, text, box, font, **kwargs):
        calls.append((text, box, font, kwargs))
        return original(draw, text, box, font, **kwargs)
    renderer._text_in_box = capture
    renderer.render(presentation(game), (800, 480))
    expected = {
        (False, False, False): "NO ONE ON BASE", (True, False, False): "RUNNER ON 1ST",
        (False, True, False): "RUNNER ON 2ND", (False, False, True): "RUNNER ON 3RD",
        (True, True, False): "RUNNERS ON 1ST AND 2ND", (True, False, True): "RUNNERS ON 1ST AND 3RD",
        (False, True, True): "RUNNERS ON 2ND AND 3RD", (True, True, True): "BASES LOADED",
    }
    assert base_runner_description(game) == expected[occupied]
    lines = renderer.last_runner_label_lines
    assert " ".join(line for line, _box, _size in lines) == expected[occupied]
    assert len(lines) == (2 if sum(occupied) == 2 else 1)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for line, box, size in lines:
        assert size == 30
        font = mlb_font(size, bold=True)
        assert draw.textlength(line, font=font) <= box[2] - box[0]
        assert box[0] + box[2] == renderer.last_diamond_bounds[0] + renderer.last_diamond_bounds[2]
        captured = next(item for item in calls if item[0] == line)
        assert captured[3].get("align", "center") == "center"
    assert lines[-1][1][3] + 12 <= renderer.last_diamond_bounds[1]
    if len(lines) == 2:
        assert lines[0][0] == "RUNNERS ON"
        assert draw.textlength(expected[occupied], font=mlb_font(30, bold=True)) > lines[0][1][2] - lines[0][1][0]
        assert lines[1][1][1] - lines[0][1][3] == 6


@pytest.mark.parametrize("name", list(preview_states()))
def test_all_sleeper_fixtures_keep_readable_text_and_no_collisions(name):
    renderer = MatchupDashboardRenderer()
    renderer.render(preview_states()[name], (800, 480))
    assert all(size >= MIN_SECONDARY_SIZE for _role, size, _card in renderer.last_text_sizes)
    for index, (role, a, owner) in enumerate(renderer.last_text_bounds):
        assert owner[0] <= a[0] <= a[2] <= owner[2], (name, role)
        assert owner[1] <= a[1] <= a[3] <= owner[3], (name, role)
        for other, b, card in renderer.last_text_bounds[index + 1:]:
            assert not (card == owner and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]), (name, role, other)


@pytest.mark.parametrize("name", ["one_close_live", "two_live", "three_mixed", "four_live"])
def test_vs_divider_never_passes_through_glyph_box(name):
    renderer = MatchupDashboardRenderer()
    image = renderer.render(preview_states()[name], (800, 480))
    for role, box, card in renderer.last_text_bounds:
        if role != "vs":
            continue
        for segment, owner in renderer.last_divider_segments:
            if owner == card:
                assert segment[3] < box[1] or segment[1] > box[3]
        center = (box[0] + box[2]) // 2
        assert all(image.getpixel((center, y)) != ImageColor.getrgb(MUTED) for y in range(box[1], box[3]))


@pytest.mark.parametrize("name", ["one_close_live", "two_live", "three_mixed", "four_live"])
def test_lowercase_vs_has_no_divider_stubs_anywhere_in_protected_center(name):
    renderer = MatchupDashboardRenderer()
    labels = []
    original = renderer._text
    def capture(draw, xy, value, **kwargs):
        if kwargs["role"] == "vs":
            labels.append((value, kwargs["font"].size))
        return original(draw, xy, value, **kwargs)
    renderer._text = capture
    image = renderer.render(preview_states()[name], (800, 480))
    assert labels and set(labels) == {("vs", 28 if name == "one_close_live" else 24)}
    assert renderer.last_divider_segments == []
    for clear, card in renderer.last_separator_clear_zones:
        glyph = next(box for role, box, owner in renderer.last_text_bounds if role == "vs" and owner == card)
        assert clear[0] < glyph[0] < glyph[2] < clear[2]
        assert min(glyph[0] - clear[0], clear[2] - glyph[2]) >= 8
        assert clear[1] < glyph[1] < glyph[3] < clear[3]
        for y in range(clear[1], clear[3] + 1):
            for x in range(clear[0], clear[2] + 1):
                if glyph[0] <= x <= glyph[2] and glyph[1] <= y <= glyph[3]:
                    continue
                assert image.getpixel((x, y)) == ImageColor.getrgb(CARD)


@pytest.mark.parametrize("name,height", [("one_close_live",18), ("two_live",15), ("three_mixed",14), ("four_live",14)])
def test_thicker_probability_bar_preserves_proportions_border_and_clearance(name, height):
    renderer = MatchupDashboardRenderer()
    image = renderer.render(preview_states()[name], (800, 480))
    assert renderer.last_probability_bars
    for box, probability, card in renderer.last_probability_bars:
        assert box[3] - box[1] + 1 == height
        for role, text, owner in renderer.last_text_bounds:
            assert not (owner == card and box[0] < text[2] and text[0] < box[2] and box[1] < text[3] and text[1] < box[3]), role
        for x in range(box[0], box[2] + 1):
            assert image.getpixel((x,box[1])) == ImageColor.getrgb(BLACK)
            assert image.getpixel((x,box[3])) == ImageColor.getrgb(BLACK)
        for y in range(box[1], box[3] + 1):
            assert image.getpixel((box[0],y)) == ImageColor.getrgb(BLACK)
            assert image.getpixel((box[2],y)) == ImageColor.getrgb(BLACK)
        split = box[0] + round((box[2] - box[0]) * probability)
        y = (box[1] + box[3]) // 2
        for x in range(box[0] + 1, box[2]):
            expected = LIME if x <= split else CREAM
            assert image.getpixel((x,y)) == ImageColor.getrgb(expected)


def test_type_scale_has_a_single_secondary_floor():
    for roles in TYPE_REFERENCE.values():
        assert min(roles.team_record, roles.card_status, roles.win_label, roles.projected_score,
                   roles.context_primary, roles.context_secondary, roles.micro) >= MIN_SECONDARY_SIZE
