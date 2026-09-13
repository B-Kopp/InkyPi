"""MLB StatsAPI access and response normalization."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from utils.http_client import get_http_session

from .models import (
    DivisionStandingRow,
    DivisionStandings,
    GameSummary,
    ScoreboardPresentation,
    classify_game_state,
)

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api"
REQUEST_TIMEOUT = (5, 15)
STANDINGS_CACHE_TTL = timedelta(minutes=20)

TEAM_ABBREVIATIONS = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def _dig(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _game_time(game: dict[str, Any]) -> datetime:
    parsed = _parse_datetime(game.get("gameDate"))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _official_date(game: dict[str, Any]) -> str:
    return str(game.get("officialDate") or game.get("gameDate") or "")[:10]


def select_relevant_game(
    games: list[dict[str, Any]], today: date
) -> tuple[dict[str, Any] | None, bool]:
    """Apply the deterministic live/today/future selection policy."""
    live = [g for g in games if classify_game_state(g.get("status")).uses_live_layout]
    if live:
        return sorted(live, key=_game_time)[0], _official_date(live[0]) != today.isoformat()

    todays_games = [g for g in games if _official_date(g) == today.isoformat()]
    if todays_games:
        finals = [g for g in todays_games if classify_game_state(g.get("status")).is_final]
        if finals:
            return sorted(finals, key=_game_time, reverse=True)[0], False
        upcoming = [
            g for g in todays_games
            if classify_game_state(g.get("status")).state.value not in {"cancelled", "postponed"}
        ]
        if upcoming:
            return sorted(upcoming, key=_game_time)[0], False
        return sorted(todays_games, key=_game_time)[0], False

    future = [
        g for g in games
        if _official_date(g) > today.isoformat()
        and classify_game_state(g.get("status")).state.value not in {"cancelled", "postponed", "final"}
    ]
    if future:
        return sorted(future, key=_game_time)[0], True
    return None, True


class MlbDataClient:
    """Small data-access layer for MLB schedule, live feed and standings."""

    def __init__(self, session=None, now_provider=None):
        self.session = session or get_http_session()
        self.now_provider = now_provider or (lambda: datetime.now().astimezone())
        self._standings_cache: dict[tuple[int, int], tuple[datetime, DivisionStandings]] = {}

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{MLB_API}{path}", params=params or {}, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response was not a JSON object")
            return payload
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("MLB StatsAPI request failed for %s: %s", path, exc)
            raise RuntimeError("MLB StatsAPI is temporarily unavailable.") from exc

    def discover_games(self, team_id: int, today: date) -> list[dict[str, Any]]:
        data = self._request_json(
            "/v1/schedule",
            {
                "sportId": 1,
                "teamId": team_id,
                # Include recent dates so a delayed/suspended contest that crossed
                # midnight can still win the live-game selection rule.
                "startDate": (today - timedelta(days=7)).isoformat(),
                "endDate": (today + timedelta(days=30)).isoformat(),
                "hydrate": "team,probablePitcher,linescore",
            },
        )
        return [
            game
            for day_data in data.get("dates", []) or []
            for game in (day_data.get("games", []) or [])
            if isinstance(game, dict)
        ]

    def get_presentation(self, team_id: int) -> ScoreboardPresentation:
        if team_id not in TEAM_ABBREVIATIONS:
            raise RuntimeError("Please select a valid MLB team.")
        now = self.now_provider()
        games = self.discover_games(team_id, now.date())
        selected, no_game_today = select_relevant_game(games, now.date())
        if selected is None:
            team_name = TEAM_ABBREVIATIONS[team_id]
            return ScoreboardPresentation(
                team_id, team_name, team_name, None, no_game_today=True, updated_at=now
            )

        game_pk = _int_or_none(selected.get("gamePk"))
        if game_pk is None:
            raise RuntimeError("MLB returned a game without an identifier.")
        feed = self._request_json(f"/v1.1/game/{game_pk}/feed/live")
        game = normalize_game(feed, selected)

        selected_side = "away" if game.away_team_id == team_id else "home"
        selected_team = _dig(feed, "gameData", "teams", selected_side, default={})
        selected_name = selected_team.get("name") or TEAM_ABBREVIATIONS[team_id]
        selected_abbr = team_abbreviation(selected_team, team_id)
        presentation = ScoreboardPresentation(
            selected_team_id=team_id,
            selected_team_name=selected_name,
            selected_team_abbreviation=selected_abbr,
            game=game,
            no_game_today=no_game_today,
            updated_at=now,
        )
        if not game.uses_live_layout:
            try:
                presentation.standings = self.get_division_standings(
                    team_id, game.season or now.year
                )
            except RuntimeError:
                cached = self._cached_standings(team_id, game.season or now.year, allow_stale=True)
                if cached:
                    cached.is_stale = True
                    presentation.standings = cached
                else:
                    presentation.standings_unavailable = True
        return presentation

    def _cached_standings(
        self, team_id: int, season: int, allow_stale: bool = False
    ) -> DivisionStandings | None:
        cached = self._standings_cache.get((team_id, season))
        if not cached:
            return None
        cached_at, value = cached
        if not allow_stale and self.now_provider() - cached_at > STANDINGS_CACHE_TTL:
            return None
        return deepcopy(value)

    def get_division_standings(self, team_id: int, season: int) -> DivisionStandings:
        cached = self._cached_standings(team_id, season)
        if cached:
            return cached
        team_data = self._request_json(f"/v1/teams/{team_id}", {"hydrate": "division"})
        team = (team_data.get("teams") or [{}])[0]
        division = team.get("division") or {}
        division_id = _int_or_none(division.get("id"))
        if division_id is None:
            raise RuntimeError("MLB division information is unavailable.")
        division_name = str(division.get("nameShort") or division.get("name") or "DIVISION")
        data = self._request_json(
            "/v1/standings",
            {
                "leagueId": _dig(team, "league", "id", default="103,104"),
                "divisionId": division_id,
                "season": season,
                "standingsTypes": "regularSeason",
                "hydrate": "team",
            },
        )
        records = data.get("records") or []
        team_records = (records[0].get("teamRecords") or []) if records else []
        rows = []
        for record in team_records:
            record_team = record.get("team") or {}
            record_id = _int_or_none(record_team.get("id"))
            gb = record.get("gamesBack")
            rows.append(
                DivisionStandingRow(
                    abbreviation=team_abbreviation(record_team, record_id),
                    wins=_int_or_none(record.get("wins")) or 0,
                    losses=_int_or_none(record.get("losses")) or 0,
                    games_back="-" if str(gb or "-") in {"-", "0", "0.0"} else str(gb),
                    is_selected_team=record_id == team_id,
                )
            )
        if not rows:
            raise RuntimeError("MLB standings are unavailable.")
        result = DivisionStandings(division_name.upper(), rows)
        self._standings_cache[(team_id, season)] = (self.now_provider(), deepcopy(result))
        return result


def team_abbreviation(team: dict[str, Any], team_id: int | None) -> str:
    return str(team.get("abbreviation") or TEAM_ABBREVIATIONS.get(team_id, "---")).upper()


def _team_values(feed: dict[str, Any], schedule: dict[str, Any], side: str) -> tuple:
    team = _dig(feed, "gameData", "teams", side, default={})
    scheduled = _dig(schedule, "teams", side, "team", default={})
    team_id = _int_or_none(team.get("id") or scheduled.get("id"))
    name = str(team.get("name") or scheduled.get("name") or side.title())
    abbreviation = team_abbreviation(team or scheduled, team_id)
    box_stats = _dig(feed, "liveData", "boxscore", "teams", side, "teamStats", "batting", default={})
    line = _dig(feed, "liveData", "linescore", "teams", side, default={})
    scheduled_side = _dig(schedule, "teams", side, default={})
    runs = _int_or_none(box_stats.get("runs"))
    if runs is None:
        runs = _int_or_none(line.get("runs"))
    if runs is None:
        runs = _int_or_none(scheduled_side.get("score"))
    hits = _int_or_none(box_stats.get("hits"))
    if hits is None:
        hits = _int_or_none(line.get("hits"))
    errors = _int_or_none(box_stats.get("errors"))
    if errors is None:
        errors = _int_or_none(line.get("errors"))
    probable = _dig(feed, "gameData", "probablePitchers", side, "fullName")
    if not probable:
        probable = _dig(schedule, "teams", side, "probablePitcher", "fullName")
    return team_id, name, abbreviation, runs, hits, errors, probable


def _decision_pitching_stats(
    live_data: dict[str, Any], decision: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the official decision pitcher's postgame season pitching line."""
    pitcher_id = _int_or_none((decision or {}).get("id"))
    if pitcher_id is None:
        return {}
    player_key = f"ID{pitcher_id}"
    boxscore_teams = _dig(live_data, "boxscore", "teams", default={})
    for side in ("away", "home"):
        player = _dig(boxscore_teams, side, "players", player_key, default={})
        pitching = _dig(player, "seasonStats", "pitching", default={})
        if pitching:
            return pitching
    return {}


def normalize_game(feed: dict[str, Any], schedule: dict[str, Any] | None = None) -> GameSummary:
    schedule = schedule or {}
    game_data = feed.get("gameData") or {}
    live_data = feed.get("liveData") or {}
    status_payload = game_data.get("status") or schedule.get("status") or {}
    classified = classify_game_state(status_payload)
    away = _team_values(feed, schedule, "away")
    home = _team_values(feed, schedule, "home")
    linescore = live_data.get("linescore") or {}
    current_play = live_data.get("plays", {}).get("currentPlay") or {}
    matchup = current_play.get("matchup") or {}
    count = current_play.get("count") or {}
    offense = linescore.get("offense") or {}
    defense = linescore.get("defense") or {}
    decisions = live_data.get("decisions") or {}
    winner = decisions.get("winner") or {}
    loser = decisions.get("loser") or {}
    save = decisions.get("save") or {}
    winner_stats = _decision_pitching_stats(live_data, winner)
    loser_stats = _decision_pitching_stats(live_data, loser)
    save_stats = _decision_pitching_stats(live_data, save)
    scheduled_time = _parse_datetime(_dig(game_data, "datetime", "dateTime") or schedule.get("gameDate"))
    season = _int_or_none(_dig(game_data, "game", "season") or schedule.get("season"))
    inning_half = linescore.get("inningHalf")
    if not inning_half and linescore.get("currentInning") is not None:
        inning_half = "Top" if linescore.get("isTopInning") else "Bottom"

    return GameSummary(
        game_pk=_int_or_none(_dig(game_data, "game", "pk") or schedule.get("gamePk")),
        state=classified.state,
        status=classified.display_status,
        uses_live_layout=classified.uses_live_layout,
        is_final=classified.is_final,
        is_pregame=classified.is_pregame,
        away_team_id=away[0], away_team_name=away[1], away_abbreviation=away[2],
        away_runs=away[3], away_hits=away[4], away_errors=away[5],
        away_starting_pitcher=away[6],
        home_team_id=home[0], home_team_name=home[1], home_abbreviation=home[2],
        home_runs=home[3], home_hits=home[4], home_errors=home[5],
        home_starting_pitcher=home[6],
        inning=_int_or_none(linescore.get("currentInning")),
        inning_half=str(inning_half).upper() if inning_half else None,
        outs=_int_or_none(linescore.get("outs") if linescore.get("outs") is not None else count.get("outs")),
        balls=_int_or_none(count.get("balls")), strikes=_int_or_none(count.get("strikes")),
        pitcher_name=_dig(defense, "pitcher", "fullName") or _dig(matchup, "pitcher", "fullName"),
        batter_name=_dig(offense, "batter", "fullName") or _dig(matchup, "batter", "fullName"),
        runner_on_first=bool(offense.get("first")),
        runner_on_second=bool(offense.get("second")),
        runner_on_third=bool(offense.get("third")),
        scheduled_time=scheduled_time,
        official_date=str(_dig(game_data, "datetime", "officialDate") or schedule.get("officialDate") or "") or None,
        venue=_dig(game_data, "venue", "name") or _dig(schedule, "venue", "name"),
        winning_pitcher=winner.get("fullName"),
        winning_pitcher_wins=_int_or_none(winner_stats.get("wins")),
        winning_pitcher_losses=_int_or_none(winner_stats.get("losses")),
        losing_pitcher=loser.get("fullName"),
        losing_pitcher_wins=_int_or_none(loser_stats.get("wins")),
        losing_pitcher_losses=_int_or_none(loser_stats.get("losses")),
        save_pitcher=save.get("fullName"),
        save_pitcher_saves=_int_or_none(save_stats.get("saves")),
        season=season,
    )
