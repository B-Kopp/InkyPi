from __future__ import annotations

import sys
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.mlb_live_score.mlb_data import MlbDataClient, normalize_game, select_relevant_game
from plugins.mlb_live_score.mlb_live_score import MlbLiveScore, setting_enabled
from plugins.mlb_live_score.models import (
    DivisionStandingRow,
    DivisionStandings,
    GameState,
    classify_game_state,
)
from plugins.mlb_live_score.preview_fixtures import base_game, presentation, preview_states, standings
from plugins.mlb_live_score.renderer import (
    CREAM,
    DARK_GREEN,
    GREEN,
    LEFT_PANEL_RATIO,
    RenderMetrics,
    ScoreboardRenderer,
    TypographyScale,
    fit_text,
)


NOW = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)


def status(abstract="Live", detailed="In Progress", coded="I", reason=None):
    value = {"abstractGameState": abstract, "detailedState": detailed, "codedGameState": coded}
    if reason:
        value["reason"] = reason
    return value


def schedule_game(pk=12345, official_date="2026-09-12", game_status=None, hour=23):
    return {
        "gamePk": pk,
        "gameDate": f"{official_date}T{hour:02d}:20:00Z",
        "officialDate": official_date,
        "season": "2026",
        "status": game_status or status(),
        "venue": {"name": "Truist Park"},
        "teams": {
            "away": {"team": {"id": 147, "name": "New York Yankees", "abbreviation": "NYY"}, "score": 4, "probablePitcher": {"fullName": "Gerrit Cole"}},
            "home": {"team": {"id": 144, "name": "Atlanta Braves", "abbreviation": "ATL"}, "score": 3, "probablePitcher": {"fullName": "Spencer Strider"}},
        },
    }


def feed(
    game_status=None,
    inning=7,
    half="Top",
    bases=(),
    pitcher="Spencer Strider",
    batter="Aaron Judge",
    include_stats=True,
    probable=("Gerrit Cole", "Spencer Strider"),
    decisions=None,
):
    offense = {}
    for base, name in (("first", "Runner One"), ("second", "Runner Two"), ("third", "Runner Three")):
        if base in bases:
            offense[base] = {"id": len(offense) + 1, "fullName": name}
    if batter is not None:
        offense["batter"] = {"id": 99, "fullName": batter}
    defense = {"pitcher": {"id": 77, "fullName": pitcher}} if pitcher is not None else {}
    away_batting = {"runs": 4, "hits": 8, "errors": 1} if include_stats else {}
    home_batting = {"runs": 3, "hits": 7, "errors": 0} if include_stats else {}
    data = {
        "gameData": {
            "game": {"pk": 12345, "season": "2026"},
            "status": game_status or status(),
            "datetime": {"dateTime": "2026-09-12T23:20:00Z", "officialDate": "2026-09-12"},
            "venue": {"name": "Truist Park"},
            "teams": {
                "away": {"id": 147, "name": "New York Yankees", "abbreviation": "NYY"},
                "home": {"id": 144, "name": "Atlanta Braves", "abbreviation": "ATL"},
            },
            "probablePitchers": {},
        },
        "liveData": {
            "linescore": {
                "currentInning": inning,
                "inningHalf": half,
                "isTopInning": half.lower() == "top",
                "outs": 2,
                "offense": offense,
                "defense": defense,
                "teams": {"away": away_batting, "home": home_batting},
            },
            "boxscore": {"teams": {
                "away": {"teamStats": {"batting": away_batting}},
                "home": {"teamStats": {"batting": home_batting}},
            }},
            "plays": {"currentPlay": {
                "count": {"balls": 2, "strikes": 1, "outs": 2},
                "matchup": {
                    **({"pitcher": {"fullName": pitcher}} if pitcher else {}),
                    **({"batter": {"fullName": batter}} if batter else {}),
                },
            }},
            "decisions": decisions or {},
        },
    }
    if probable[0]:
        data["gameData"]["probablePitchers"]["away"] = {"fullName": probable[0]}
    if probable[1]:
        data["gameData"]["probablePitchers"]["home"] = {"fullName": probable[1]}
    return data


def schedule_payload(*games):
    return {"dates": [{"date": game["officialDate"], "games": [game]} for game in games]}


def team_payload():
    return {"teams": [{
        "id": 144, "name": "Atlanta Braves", "abbreviation": "ATL",
        "league": {"id": 104}, "division": {"id": 204, "nameShort": "NL East"},
    }]}


def standings_payload():
    return {"records": [{"teamRecords": [
        {"team": {"id": 144, "abbreviation": "ATL"}, "wins": 88, "losses": 57, "gamesBack": "-"},
        {"team": {"id": 143, "abbreviation": "PHI"}, "wins": 84, "losses": 61, "gamesBack": "4.0"},
        {"team": {"id": 121, "abbreviation": "NYM"}, "wins": 77, "losses": 68, "gamesBack": "11.0"},
        {"team": {"id": 146, "abbreviation": "MIA"}, "wins": 65, "losses": 80, "gamesBack": "23.0"},
        {"team": {"id": 120, "abbreviation": "WSH"}, "wins": 61, "losses": 84, "gamesBack": "27.0"},
    ]}]}


def with_decision_stats(payload, decisions, winner=(14, 5), loser=(11, 8), saves=31):
    payload["liveData"]["decisions"] = decisions
    players = payload["liveData"]["boxscore"]["teams"]["away"].setdefault("players", {})
    stat_lines = {
        "winner": {"wins": winner[0], "losses": winner[1]},
        "loser": {"wins": loser[0], "losses": loser[1]},
        "save": {"saves": saves},
    }
    for role, decision in decisions.items():
        players[f"ID{decision['id']}"] = {
            "person": decision,
            "seasonStats": {"pitching": stat_lines[role]},
        }
    return payload


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


class FakeSession:
    def __init__(self, schedule=None, game_feed=None, fail_paths=()):
        self.schedule = schedule or schedule_payload(schedule_game())
        self.game_feed = game_feed or feed()
        self.fail_paths = set(fail_paths)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if any(path in url for path in self.fail_paths):
            raise requests.Timeout("timeout")
        if "/schedule" in url:
            return FakeResponse(self.schedule)
        if "/feed/live" in url:
            return FakeResponse(self.game_feed)
        if "/teams/" in url:
            return FakeResponse(team_payload())
        if "/standings" in url:
            return FakeResponse(standings_payload())
        raise AssertionError(url)


@pytest.mark.parametrize(
    "bases,expected",
    [
        ((), (False, False, False)),
        (("first",), (True, False, False)),
        (("first", "third"), (True, False, True)),
        (("first", "second", "third"), (True, True, True)),
    ],
)
def test_live_base_occupancy(bases, expected):
    game = normalize_game(feed(bases=bases), schedule_game())
    assert (game.runner_on_first, game.runner_on_second, game.runner_on_third) == expected


@pytest.mark.parametrize("half", ["Top", "Bottom"])
def test_live_inning_half_count_outs_and_away_home_order(half):
    game = normalize_game(feed(half=half), schedule_game())
    assert game.inning_half == half.upper()
    assert (game.balls, game.strikes, game.outs) == (2, 1, 2)
    assert (game.away_abbreviation, game.home_abbreviation) == ("NYY", "ATL")
    assert (game.away_runs, game.away_hits, game.away_errors) == (4, 8, 1)
    assert (game.home_runs, game.home_hits, game.home_errors) == (3, 7, 0)


@pytest.mark.parametrize(
    "payload,expected,live",
    [
        (status("Preview", "Scheduled", "S"), GameState.PREGAME, False),
        (status("Preview", "Warmup", "W"), GameState.PREGAME, False),
        (status("Preview", "Delayed Start", "S"), GameState.DELAYED_PREGAME, False),
        (status("Live", "Delayed", "I"), GameState.DELAYED_LIVE, True),
        (status("Final", "Final", "F"), GameState.FINAL, False),
        (status("Final", "Postponed", "D"), GameState.POSTPONED, False),
        (status("Live", "Suspended", "I"), GameState.SUSPENDED_LIVE, True),
        (status("Final", "Suspended", "U"), GameState.SUSPENDED, False),
        (status("Final", "Cancelled", "C"), GameState.CANCELLED, False),
    ],
)
def test_central_game_state_classifier(payload, expected, live):
    classified = classify_game_state(payload)
    assert classified.state is expected
    assert classified.uses_live_layout is live


@pytest.mark.parametrize("inning,expected", [(9, 9), (11, 11)])
def test_final_regulation_and_extra_innings(inning, expected):
    game = normalize_game(feed(game_status=status("Final", "Final", "F"), inning=inning), schedule_game())
    assert game.is_final
    assert game.inning == expected
    assert not game.uses_live_layout


@pytest.mark.parametrize(
    "probable,expected",
    [
        (("Gerrit Cole", "Spencer Strider"), ("Gerrit Cole", "Spencer Strider")),
        (("Gerrit Cole", None), ("Gerrit Cole", "Spencer Strider")),
        ((None, None), ("Gerrit Cole", "Spencer Strider")),
    ],
)
def test_probable_pitchers_with_schedule_fallback(probable, expected):
    game = normalize_game(feed(probable=probable), schedule_game())
    assert (game.away_starting_pitcher, game.home_starting_pitcher) == expected


def test_probable_pitchers_can_both_be_tbd():
    scheduled = schedule_game()
    scheduled["teams"]["away"].pop("probablePitcher")
    scheduled["teams"]["home"].pop("probablePitcher")
    game = normalize_game(feed(probable=(None, None)), scheduled)
    assert game.away_starting_pitcher is None
    assert game.home_starting_pitcher is None


@pytest.mark.parametrize(
    "decisions,expected",
    [
        ({"winner": {"fullName": "Winner"}, "loser": {"fullName": "Loser"}}, ("Winner", "Loser", None)),
        ({"winner": {"fullName": "Winner"}, "loser": {"fullName": "Loser"}, "save": {"fullName": "Saver"}}, ("Winner", "Loser", "Saver")),
    ],
)
def test_decision_pitchers(decisions, expected):
    game = normalize_game(feed(decisions=decisions), schedule_game())
    assert (game.winning_pitcher, game.losing_pitcher, game.save_pitcher) == expected


def test_decision_records_and_save_total_use_boxscore_season_stats():
    decisions = {
        "winner": {"id": 1, "fullName": "Spencer Strider"},
        "loser": {"id": 2, "fullName": "Gerrit Cole"},
        "save": {"id": 3, "fullName": "Raisel Iglesias"},
    }
    game = normalize_game(with_decision_stats(feed(), decisions), schedule_game())
    assert (game.winning_pitcher_wins, game.winning_pitcher_losses) == (14, 5)
    assert (game.losing_pitcher_wins, game.losing_pitcher_losses) == (11, 8)
    assert game.save_pitcher_saves == 31


def test_missing_decision_season_stats_remain_optional():
    decisions = {
        "winner": {"id": 1, "fullName": "Winner"},
        "loser": {"id": 2, "fullName": "Loser"},
        "save": {"id": 3, "fullName": "Saver"},
    }
    game = normalize_game(feed(decisions=decisions), schedule_game())
    assert game.winning_pitcher_wins is None
    assert game.winning_pitcher_losses is None
    assert game.losing_pitcher_wins is None
    assert game.losing_pitcher_losses is None
    assert game.save_pitcher_saves is None


def test_missing_optional_live_fields_and_stats_do_not_crash():
    scheduled = schedule_game()
    scheduled["teams"]["away"].pop("score")
    scheduled["teams"]["home"].pop("score")
    game = normalize_game(feed(pitcher=None, batter=None, include_stats=False), scheduled)
    assert game.pitcher_name is None
    assert game.batter_name is None
    assert (game.away_runs, game.away_hits, game.away_errors) == (None, None, None)


def test_doubleheader_selection_live_then_most_recent_final_then_upcoming():
    final_early = schedule_game(1, hour=17, game_status=status("Final", "Final", "F"))
    upcoming = schedule_game(2, hour=23, game_status=status("Preview", "Scheduled", "S"))
    live = schedule_game(3, hour=20, game_status=status("Live", "In Progress", "I"))
    selected, no_today = select_relevant_game([final_early, upcoming, live], date(2026, 9, 12))
    assert selected["gamePk"] == 3 and not no_today
    selected, _ = select_relevant_game([final_early, upcoming], date(2026, 9, 12))
    assert selected["gamePk"] == 1


def test_no_game_today_selects_next_scheduled_game():
    later = schedule_game(2, "2026-09-14", status("Preview", "Scheduled", "S"))
    sooner = schedule_game(1, "2026-09-13", status("Preview", "Scheduled", "S"))
    selected, no_today = select_relevant_game([later, sooner], date(2026, 9, 12))
    assert selected["gamePk"] == 1
    assert no_today


def test_http_timeout_has_clear_runtime_error():
    client = MlbDataClient(FakeSession(fail_paths={"/schedule"}), lambda: NOW)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        client.get_presentation(144)


def test_malformed_json_has_clear_runtime_error():
    session = FakeSession()
    session.schedule = ValueError("bad json")
    client = MlbDataClient(session, lambda: NOW)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        client.get_presentation(144)


def test_presentation_standings_and_games_back_values():
    scheduled_status = status("Preview", "Scheduled", "S")
    client = MlbDataClient(
        FakeSession(game_feed=feed(game_status=scheduled_status)), lambda: NOW
    )
    result = client.get_presentation(144)
    assert result.standings.division_name == "NL EAST"
    assert [row.abbreviation for row in result.standings.rows] == ["ATL", "PHI", "NYM", "MIA", "WSH"]
    assert result.standings.rows[0].is_selected_team
    assert result.standings.rows[0].games_back == "-"
    result.standings.rows[1].games_back = "1.5"
    assert result.standings.rows[1].games_back == "1.5"


def test_standings_failure_does_not_discard_game():
    scheduled_status = status("Preview", "Scheduled", "S")
    session = FakeSession(game_feed=feed(game_status=scheduled_status), fail_paths={"/standings"})
    result = MlbDataClient(session, lambda: NOW).get_presentation(144)
    assert result.game.game_pk == 12345
    assert result.standings is None
    assert result.standings_unavailable


def test_no_game_today_keeps_next_game_probable_pitchers():
    future = schedule_game(
        official_date="2026-09-14", game_status=status("Preview", "Scheduled", "S")
    )
    client = MlbDataClient(
        FakeSession(schedule=schedule_payload(future), game_feed=feed(game_status=future["status"])),
        lambda: NOW,
    )
    result = client.get_presentation(144)
    assert result.no_game_today
    assert result.game.away_starting_pitcher == "Gerrit Cole"
    assert result.game.home_starting_pitcher == "Spencer Strider"


def test_standings_cache_fresh_and_stale_fallback():
    clock = [NOW]
    session = FakeSession()
    client = MlbDataClient(session, lambda: clock[0])
    first = client.get_division_standings(144, 2026)
    second = client.get_division_standings(144, 2026)
    assert first.rows == second.rows
    assert sum("/standings" in call[0] for call in session.calls) == 1

    clock[0] += timedelta(minutes=21)
    session.fail_paths.add("/standings")
    scheduled_status = status("Preview", "Scheduled", "S")
    session.game_feed = feed(game_status=scheduled_status)
    result = client.get_presentation(144)
    assert result.standings.is_stale
    assert not result.standings_unavailable


@pytest.mark.parametrize("name", list(preview_states()))
def test_every_preview_fixture_renders_at_800x480(name):
    renderer = ScoreboardRenderer()
    image = renderer.render(preview_states()[name], (800, 480))
    assert isinstance(image, Image.Image)
    assert image.size == (800, 480)


def test_primary_panel_split_is_60_40_at_800x480():
    metrics = RenderMetrics.for_canvas(800, 480)
    assert LEFT_PANEL_RATIO == 0.60
    assert metrics.divider_x == 480

    renderer = ScoreboardRenderer()
    renderer.render(presentation(), (800, 480))
    assert renderer.last_layout.left_panel[2] < metrics.divider_x
    assert renderer.last_layout.right_panel[0] > metrics.divider_x


@pytest.mark.parametrize("dimensions", [(640, 400), (480, 800), (400, 300)])
def test_alternate_display_resolutions_render(dimensions):
    image = ScoreboardRenderer().render(presentation(), dimensions)
    assert image.size == dimensions


@pytest.mark.parametrize("state,expects_live", [("live_bases_empty", True), ("pregame_starters", False), ("final_wp_lp", False)])
def test_layout_switches_between_diamond_and_standings(state, expects_live):
    fixture = preview_states()[state]
    renderer = ScoreboardRenderer()
    renderer.render(fixture, (800, 480))
    assert fixture.game.uses_live_layout is expects_live
    assert renderer.last_layout.right_panel[0] > renderer.last_layout.left_panel[0]


@pytest.mark.parametrize("division", ["AL EAST", "AL CENTRAL", "AL WEST", "NL EAST", "NL CENTRAL", "NL WEST"])
def test_all_six_division_names_and_five_teams_fit(division):
    fixture = presentation(base_game(uses_live_layout=False, state=GameState.PREGAME, is_pregame=True))
    fixture.standings = standings()
    fixture.standings.division_name = division
    renderer = ScoreboardRenderer()
    image = renderer.render(fixture, (800, 480))
    assert image.size == (800, 480)


def test_selected_team_highlight_uses_inverse_fill():
    fixture = presentation(base_game(uses_live_layout=False, state=GameState.PREGAME, is_pregame=True))
    image = ScoreboardRenderer().render(fixture, (800, 480))
    assert CREAM in {"#%02x%02x%02x" % pixel for pixel in image.get_flattened_data()}
    assert fixture.standings.rows[0].is_selected_team


def test_standings_content_preserves_right_safe_margin():
    fixture = presentation(base_game(uses_live_layout=False, state=GameState.FINAL, is_final=True))
    renderer = ScoreboardRenderer()
    image = renderer.render(fixture, (800, 480))
    metrics = RenderMetrics.for_canvas(*image.size)
    content_right = renderer.last_layout.right_panel[2] - metrics.panel_padding
    background = Image.new("RGB", (image.width - content_right - 1, image.height), GREEN)
    edge = image.crop((content_right + 1, 0, image.width, image.height))
    assert ImageChops.difference(edge, background).getbbox() is None


def test_selected_standings_row_has_large_inverse_fill():
    fixture = presentation(base_game(uses_live_layout=False, state=GameState.FINAL, is_final=True))
    renderer = ScoreboardRenderer()
    image = renderer.render(fixture, (800, 480))
    right_x = renderer.last_layout.right_panel[0]
    cream = (242, 230, 189)
    widest_cream_run = max(
        sum(image.getpixel((x, y)) == cream for x in range(right_x, image.width))
        for y in range(image.height)
    )
    assert widest_cream_run >= 200


def test_live_diamond_is_horizontally_centered_in_right_content():
    renderer = ScoreboardRenderer()
    image = renderer.render(presentation(), (800, 480))
    metrics = RenderMetrics.for_canvas(*image.size)
    panel = renderer.last_layout.right_panel
    content = (
        panel[0] + metrics.panel_padding,
        panel[1] + 70,
        panel[2] - metrics.panel_padding + 1,
        panel[3] - metrics.panel_padding + 1,
    )
    crop = image.crop(content)
    background = Image.new("RGB", crop.size, GREEN)
    bbox = ImageChops.difference(crop, background).getbbox()
    assert bbox is not None
    rendered_center = content[0] + (bbox[0] + bbox[2] - 1) / 2
    panel_center = (content[0] + content[2] - 1) / 2
    assert abs(rendered_center - panel_center) <= 1


def test_text_box_uses_glyph_bounds_for_vertical_centering():
    image = Image.new("RGB", (120, 80), GREEN)
    draw = ImageDraw.Draw(image)
    renderer = ScoreboardRenderer()
    _, font = fit_text(draw, "88", 100, 38, bold=True)
    renderer._text_in_box(draw, "88", (10, 10, 110, 70), font)
    background = Image.new("RGB", image.size, GREEN)
    bbox = ImageChops.difference(image, background).getbbox()
    assert bbox is not None
    assert abs((bbox[1] + bbox[3] - 1) / 2 - 40) <= 1


@pytest.mark.parametrize("field", ["pitcher_name", "batter_name"])
def test_long_player_names_are_fitted(field):
    game = base_game(**{field: "A Very Long Compound Baseball Player Name That Must Never Overflow"})
    image = Image.new("RGB", (800, 480))
    draw = ImageDraw.Draw(image)
    text, font = fit_text(draw, getattr(game, field), 380, 32, bold=True)
    assert draw.textlength(text, font=font) <= 380
    assert text.endswith("…") or font.size < 32


def test_important_layout_bounds_stay_inside_panels_at_800x480():
    renderer = ScoreboardRenderer()
    renderer.render(presentation(), (800, 480))
    layout = renderer.last_layout
    assert layout.left_panel[0] >= 0 and layout.left_panel[2] < layout.right_panel[0]
    assert layout.right_panel[2] <= 800 and layout.right_panel[3] <= 480
    assert layout.scoreboard[0] >= layout.left_panel[0]
    assert layout.scoreboard[2] <= layout.left_panel[2]
    assert layout.details[3] <= layout.left_panel[3]


@pytest.mark.parametrize("dimensions", [(800, 480), (640, 400), (480, 800), (400, 300)])
def test_scoreboard_run_hit_and_error_columns_have_equal_widths(dimensions):
    renderer = ScoreboardRenderer()
    renderer.render(presentation(), dimensions)
    x0, _, x1, _ = renderer.last_layout.scoreboard
    boundaries = renderer._scoreboard_column_boundaries(renderer.last_layout.scoreboard)
    stat_widths = [right - left for left, right in zip(boundaries, boundaries[1:])]

    assert stat_widths[0] == stat_widths[1] == stat_widths[2]
    assert x0 < boundaries[0] < boundaries[-1] == x1


def test_scoreboard_headers_and_values_share_exact_column_centers_and_role_sizes():
    renderer = ScoreboardRenderer()
    calls = []
    original = renderer._text_in_box

    def record(draw, text, box, font, *args, **kwargs):
        calls.append((str(text), box, font.size))
        return original(draw, text, box, font, *args, **kwargs)

    renderer._text_in_box = record
    renderer.render(presentation(), (800, 480))
    columns = renderer.last_scoreboard_columns
    expected_centers = [(columns[i] + columns[i + 1]) / 2 for i in range(1, 4)]
    header_calls = [call for call in calls if call[0] in {"R", "H", "E"}]
    assert [((box[0] + box[2]) / 2) for _, box, _ in header_calls] == expected_centers
    assert {size for _, _, size in header_calls} == {renderer.typography.scoreboard_header}

    scoreboard = renderer.last_layout.scoreboard
    numeric_calls = [
        call for call in calls
        if call[1][1] > scoreboard[1] and call[1][0] in columns[1:-1]
    ]
    assert len(numeric_calls) == 6
    assert {size for _, _, size in numeric_calls} == {renderer.typography.scoreboard_primary}
    assert [((box[0] + box[2]) / 2) for _, box, _ in numeric_calls[:3]] == expected_centers
    assert [((box[0] + box[2]) / 2) for _, box, _ in numeric_calls[3:]] == expected_centers


def test_live_inning_and_outs_use_the_same_font_size_on_status_line():
    renderer = ScoreboardRenderer()
    calls = []
    original = renderer._text_in_box

    def record(draw, text, box, font, *args, **kwargs):
        calls.append((str(text), font.size))
        return original(draw, text, box, font, *args, **kwargs)

    renderer._text_in_box = record
    renderer.render(presentation(), (800, 480), show_outs=True)
    status_sizes = {text: size for text, size in calls if text in {"TOP 7TH", "2 OUTS"}}
    assert status_sizes["TOP 7TH"] == status_sizes["2 OUTS"]
    assert status_sizes["TOP 7TH"] == renderer.typography.game_status_primary


def test_typography_scale_has_stable_semantic_roles():
    scale = TypographyScale.for_canvas(800, 480)
    assert scale.scoreboard_primary == 29
    assert scale.scoreboard_header == 25
    assert scale.game_status_primary == 28
    assert scale.player_primary == 25
    assert scale.label == 17
    assert TypographyScale.for_canvas(400, 240).scoreboard_primary < scale.scoreboard_primary


@pytest.mark.parametrize(
    "state,expected_rows,expected_result",
    [
        ("final_wp_lp", 2, "14-5"),
        ("final_wp_lp_sv", 3, "31 SV"),
    ],
)
def test_final_pitcher_rows_render_records_and_optional_save(state, expected_rows, expected_result):
    renderer = ScoreboardRenderer()
    calls = []
    original = renderer._text_in_box

    def record(draw, text, box, font, *args, **kwargs):
        calls.append((str(text), box))
        return original(draw, text, box, font, *args, **kwargs)

    renderer._text_in_box = record
    renderer.render(preview_states()[state], (800, 480))
    assert len(renderer.last_final_rows) == expected_rows
    assert expected_result in {text for text, _ in calls}
    assert ({"WP:", "LP:", "SV:"} if expected_rows == 3 else {"WP:", "LP:"}) <= {
        text for text, _ in calls
    }
    if expected_rows == 2:
        assert "SV:" not in {text for text, _ in calls}


def test_final_save_with_missing_total_keeps_row_without_placeholder_total():
    game = base_game(
        state=GameState.FINAL, status="FINAL", uses_live_layout=False, is_final=True,
        winning_pitcher="Winner", losing_pitcher="Loser", save_pitcher="Saver",
        save_pitcher_saves=None,
    )
    renderer = ScoreboardRenderer()
    renderer.render(presentation(game), (800, 480))
    assert len(renderer.last_final_rows) == 3


def test_long_final_pitcher_name_cannot_overlap_record_column():
    game = base_game(
        state=GameState.FINAL, status="FINAL", uses_live_layout=False, is_final=True,
        winning_pitcher="A Very Long Compound Baseball Player Name That Must Fit",
        winning_pitcher_wins=14, winning_pitcher_losses=5,
        losing_pitcher="Gerrit Cole", losing_pitcher_wins=11, losing_pitcher_losses=8,
    )
    renderer = ScoreboardRenderer()
    calls = []
    original = renderer._text_in_box

    def record(draw, text, box, font, *args, **kwargs):
        calls.append((str(text), box, font))
        return original(draw, text, box, font, *args, **kwargs)

    renderer._text_in_box = record
    renderer.render(presentation(game), (800, 480))
    name_call = next(call for call in calls if call[0].startswith("A."))
    result_call = next(call for call in calls if call[0] == "14-5")
    assert name_call[1][2] <= result_call[1][0]
    assert ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(
        name_call[0], font=name_call[2]
    ) <= name_call[1][2] - name_call[1][0] - 4


@pytest.mark.parametrize("state", ["final_wp_lp", "final_wp_lp_sv"])
def test_all_postgame_row_allocations_stay_inside_left_panel(state):
    renderer = ScoreboardRenderer()
    renderer.render(preview_states()[state], (800, 480))
    left = renderer.last_layout.left_panel
    for row in renderer.last_final_rows:
        assert left[0] <= row[0] < row[2] <= left[2]
        assert left[1] <= row[1] < row[3] <= left[3]


def test_base_diamond_stays_inside_wider_right_panel_content():
    renderer = ScoreboardRenderer()
    renderer.render(presentation(), (800, 480))
    metrics = RenderMetrics.for_canvas(800, 480)
    right = renderer.last_layout.right_panel
    content = (
        right[0] + metrics.panel_padding,
        right[1] + metrics.panel_padding,
        right[2] - metrics.panel_padding,
        right[3] - metrics.panel_padding,
    )
    diamond = renderer.last_diamond_bounds
    assert content[0] <= diamond[0] < diamond[2] <= content[2]
    assert content[1] <= diamond[1] < diamond[3] <= content[3]


def test_plugin_generate_image_uses_settings_and_device_resolution():
    fake_client = MagicMock()
    fake_client.get_presentation.return_value = presentation()
    plugin = MlbLiveScore(
        {"id": "mlb_live_score"}, data_client=fake_client, renderer=ScoreboardRenderer()
    )
    device = MagicMock()
    device.get_resolution.return_value = (800, 480)
    device.get_config.return_value = "horizontal"
    image = plugin.generate_image(
        {"mlbTeamId": "144", "showOuts": "true", "showLastUpdated": "false"}, device
    )
    fake_client.get_presentation.assert_called_once_with(144)
    assert image.size == (800, 480)


def test_settings_defaults_and_saved_prepopulation_script():
    html = (Path(__file__).resolve().parents[1] / "src/plugins/mlb_live_score/settings.html").read_text()
    assert 'name="mlbTeamId"' in html
    assert 'value="144" selected' in html
    assert "loadPluginSettings" in html and "pluginSettings.mlbTeamId" in html
    assert 'id="showOuts"' in html and "showLastUpdated" in html
    assert setting_enabled(None, default=True)
    assert setting_enabled("true")
    assert not setting_enabled("false")


def test_team_dropdown_contains_all_30_ids_and_is_alphabetized():
    from html.parser import HTMLParser

    class OptionParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_team = False
            self.current = None
            self.options = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select" and attrs.get("id") == "mlbTeamId":
                self.in_team = True
            elif tag == "option" and self.in_team:
                self.current = [attrs.get("value"), ""]

        def handle_data(self, data):
            if self.current:
                self.current[1] += data

        def handle_endtag(self, tag):
            if tag == "option" and self.current:
                self.options.append((self.current[0], self.current[1].strip()))
                self.current = None
            elif tag == "select":
                self.in_team = False

    parser = OptionParser()
    parser.feed((Path(__file__).resolve().parents[1] / "src/plugins/mlb_live_score/settings.html").read_text())
    assert len(parser.options) == 30
    assert [name for _, name in parser.options] == sorted(name for _, name in parser.options)
    assert len({team_id for team_id, _ in parser.options}) == 30
