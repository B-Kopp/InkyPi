"""Sleeper public API access and response normalization."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from utils.http_client import get_http_session

from .analytics import aggregate_remaining, estimate_win_probability, remaining_projection, score_projected_stats
from .models import (
    DashboardModel,
    FantasyMatchup,
    FantasyRoster,
    LeagueInfo,
    MatchupSide,
    MatchupStatus,
    SleeperUser,
)
from .projections import SleeperProjectionAdapter

logger = logging.getLogger(__name__)

SLEEPER_API = "https://api.sleeper.app/v1"
REQUEST_TIMEOUT = (5, 15)
CACHE_TTLS = {
    "state": timedelta(minutes=10),
    "leagues": timedelta(minutes=10),
    "league": timedelta(hours=1),
    "users": timedelta(minutes=30),
    "rosters": timedelta(minutes=30),
    "matchups": timedelta(seconds=90),
    "players": timedelta(hours=24),
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def selected_league_ids(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("selectedLeagueIds[]")
    if raw is None:
        raw = [settings.get(f"leagueId{index}") for index in range(1, 5)]
    elif not isinstance(raw, list):
        raw = [raw]
    values: list[str] = []
    for value in raw:
        league_id = str(value or "").strip()
        if league_id:
            if league_id in values:
                raise RuntimeError("Select different Sleeper leagues in each slot.")
            values.append(league_id)
    if not 1 <= len(values) <= 4:
        raise RuntimeError("Select between 1 and 4 Sleeper leagues.")
    return values


def score_from_matchup(payload: dict[str, Any]) -> float:
    custom = payload.get("custom_points")
    return _number(custom) if custom is not None else _number(payload.get("points"))


class SleeperClient:
    def __init__(
        self,
        session=None,
        projection_adapter=None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.session = session or get_http_session()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.projections = projection_adapter or SleeperProjectionAdapter(
            self.session, self.now_provider
        )
        self._cache: dict[tuple[Any, ...], tuple[datetime, Any]] = {}

    def _request_json(self, path: str, cache_key: tuple[Any, ...]) -> Any:
        kind = str(cache_key[0])
        cached = self._cache.get(cache_key)
        if cached and self.now_provider() - cached[0] <= CACHE_TTLS[kind]:
            return cached[1]
        try:
            response = self.session.get(f"{SLEEPER_API}{path}", timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (dict, list)):
                raise ValueError("response was not JSON data")
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("Sleeper request failed for %s: %s", path, exc)
            raise RuntimeError("Sleeper is temporarily unavailable.") from exc
        self._cache[cache_key] = (self.now_provider(), payload)
        return payload

    def get_nfl_state(self) -> dict[str, Any]:
        payload = self._request_json("/state/nfl", ("state", "nfl"))
        if not isinstance(payload, dict) or not payload.get("season"):
            raise RuntimeError("Sleeper NFL season information is unavailable.")
        return payload

    def resolve_user(self, username_or_id: str) -> SleeperUser:
        reference = str(username_or_id or "").strip()
        if not reference:
            raise RuntimeError("Enter a Sleeper username or user ID.")
        payload = self._request_json(f"/user/{reference}", ("users", "identity", reference.lower()))
        if not isinstance(payload, dict) or not payload.get("user_id"):
            raise RuntimeError(f"Sleeper user '{reference}' was not found.")
        return SleeperUser(
            str(payload["user_id"]),
            str(payload.get("username") or reference),
            str(payload.get("display_name") or payload.get("username") or reference),
        )

    def discover_leagues(self, user_id: str, season: str) -> list[LeagueInfo]:
        payload = self._request_json(
            f"/user/{user_id}/leagues/nfl/{season}", ("leagues", str(user_id), str(season))
        )
        if not isinstance(payload, list):
            raise RuntimeError("Sleeper leagues are unavailable.")
        return [normalize_league(item) for item in payload if isinstance(item, dict)]

    def get_dashboard(self, username_or_id: str, league_ids: list[str]) -> DashboardModel:
        if not 1 <= len(league_ids) <= 4:
            raise RuntimeError("Select between 1 and 4 Sleeper leagues.")
        user = self.resolve_user(username_or_id)
        state = self.get_nfl_state()
        season = str(state.get("league_season") or state["season"])
        week = _integer(state.get("week") or state.get("display_week"), 1)
        season_type = str(state.get("season_type") or "regular")
        discovered = {league.league_id: league for league in self.discover_leagues(user.user_id, season)}
        projections, games = self.projections.get_week(season, week, season_type)
        matchups: list[FantasyMatchup] = []
        loaded = 0
        for league_id in league_ids:
            known = discovered.get(str(league_id))
            try:
                if known is None:
                    raise RuntimeError("Not one of this user's current-season NFL leagues.")
                matchup = self._get_matchup(known, user, week, projections, games)
                loaded += 1
            except RuntimeError as exc:
                logger.warning("League %s unavailable: %s", league_id, exc)
                matchup = FantasyMatchup(
                    league_id=str(league_id),
                    league_name=known.name if known else f"LEAGUE {league_id}",
                    week=week,
                    status=MatchupStatus.UNAVAILABLE,
                    last_updated=self.now_provider(),
                    error_state=str(exc),
                )
            matchups.append(matchup)
        if loaded == 0:
            raise RuntimeError("None of the selected Sleeper leagues could be loaded.")
        return DashboardModel(user, season, week, matchups)

    def _get_matchup(self, league: LeagueInfo, user: SleeperUser, week: int, projections, games):
        league_payload = self._request_json(
            f"/league/{league.league_id}", ("league", league.league_id)
        )
        if not isinstance(league_payload, dict) or not league_payload.get("league_id"):
            raise RuntimeError("League metadata is invalid.")
        league = normalize_league(league_payload)
        roster_payload = self._request_json(
            f"/league/{league.league_id}/rosters", ("rosters", league.league_id)
        )
        user_payload = self._request_json(
            f"/league/{league.league_id}/users", ("users", "league", league.league_id)
        )
        matchup_payload = self._request_json(
            f"/league/{league.league_id}/matchups/{week}",
            ("matchups", league.league_id, week),
        )
        if not isinstance(roster_payload, list) or not isinstance(user_payload, list) or not isinstance(matchup_payload, list):
            raise RuntimeError("League matchup data is malformed.")
        users = {str(item.get("user_id")): item for item in user_payload if isinstance(item, dict)}
        rosters = {
            roster.roster_id: roster
            for item in roster_payload
            if isinstance(item, dict)
            and (roster := normalize_roster(item, users)) is not None
        }
        user_roster = next(
            (item for item in rosters.values() if item.owner_user_id == user.user_id), None
        )
        if user_roster is None:
            raise RuntimeError("The selected user does not own a roster in this league.")
        user_row = next(
            (item for item in matchup_payload if _integer(item.get("roster_id"), -1) == user_roster.roster_id),
            None,
        )
        playoff_start = _integer(league.settings.get("playoff_week_start"), 99)
        if user_row is None:
            status = MatchupStatus.SEASON_COMPLETE if league.status == "complete" else MatchupStatus.NO_MATCHUP
            return FantasyMatchup(
                league.league_id, league.name, week, status,
                last_updated=self.now_provider(), is_playoff=week >= playoff_start,
            )
        matchup_id = user_row.get("matchup_id")
        opponent_row = next(
            (
                item for item in matchup_payload
                if item is not user_row and item.get("matchup_id") == matchup_id
            ),
            None,
        )
        user_side = self._side(user_roster, user_row, league, projections, games)
        if opponent_row is None:
            return FantasyMatchup(
                league.league_id, league.name, week, MatchupStatus.BYE,
                user_side=user_side, last_updated=self.now_provider(),
                is_playoff=week >= playoff_start,
            )
        opponent_roster = rosters.get(_integer(opponent_row.get("roster_id"), -1))
        if opponent_roster is None:
            raise RuntimeError("The opponent roster is unavailable.")
        opponent_side = self._side(opponent_roster, opponent_row, league, projections, games)
        status = classify_matchup_status(
            user_row, opponent_row, games, projections, league.status
        )
        user_live_starters = count_live_starters(user_row, projections, games)
        opponent_live_starters = count_live_starters(opponent_row, projections, games)
        projections_available = (
            user_side.projected_final is not None and opponent_side.projected_final is not None
        )
        if projections_available:
            probability = estimate_win_probability(
                user_side.current_score,
                opponent_side.current_score,
                user_side.estimated_remaining_points,
                opponent_side.estimated_remaining_points,
                user_side.remaining_variance,
                opponent_side.remaining_variance,
                completed=status is MatchupStatus.FINAL,
            )
            user_side.win_probability = probability
            opponent_side.win_probability = None if probability is None else 1.0 - probability
        return FantasyMatchup(
            league.league_id, league.name, week, status, user_side, opponent_side,
            self.now_provider(), projections_available, is_playoff=week >= playoff_start,
            scheduler_is_live=(user_live_starters + opponent_live_starters) > 0,
            user_live_starters_count=user_live_starters,
            opponent_live_starters_count=opponent_live_starters,
        )

    @staticmethod
    def _side(roster, row, league, projections, games) -> MatchupSide:
        score = score_from_matchup(row)
        side = MatchupSide(
            roster.roster_id, roster.team_name, roster.team_name, roster.record, score
        )
        starters = tuple(str(value) for value in (row.get("starters") or roster.starters) if str(value) not in {"0", "None", ""})
        remaining_values: list[tuple[float, str]] = []
        remaining_count = 0
        reliable = bool(starters)
        for player_id in starters:
            projection = projections.get(player_id)
            if projection is None or projection.game_id is None:
                reliable = False
                break
            progress = games.get(projection.game_id)
            if progress is None:
                reliable = False
                break
            full = score_projected_stats(projection.stats, league.scoring_settings)
            remaining = remaining_projection(full, progress.state, progress.remaining_fraction)
            if remaining is None:
                reliable = False
                break
            remaining_values.append((remaining, projection.position))
            if progress.state != "final":
                remaining_count += 1
        if reliable:
            expected, variance = aggregate_remaining(remaining_values)
            side.estimated_remaining_points = expected
            side.remaining_variance = variance
            side.projected_final = score + expected
            side.starters_remaining = remaining_count
        return side


def normalize_league(payload: dict[str, Any]) -> LeagueInfo:
    scoring = {
        str(key): _number(value)
        for key, value in (payload.get("scoring_settings") or {}).items()
    }
    return LeagueInfo(
        str(payload.get("league_id") or ""),
        str(payload.get("name") or "Unnamed League"),
        str(payload.get("season") or ""),
        str(payload.get("status") or "unknown").lower(),
        scoring,
        payload.get("settings") if isinstance(payload.get("settings"), dict) else {},
    )


def normalize_roster(payload: dict[str, Any], users: dict[str, dict[str, Any]]) -> FantasyRoster | None:
    roster_id = _integer(payload.get("roster_id"), -1)
    if roster_id < 0:
        return None
    owner_id = str(payload.get("owner_id")) if payload.get("owner_id") is not None else None
    owner = users.get(owner_id or "", {})
    metadata = owner.get("metadata") if isinstance(owner.get("metadata"), dict) else {}
    team_name = str(
        metadata.get("team_name")
        or owner.get("display_name")
        or owner.get("username")
        or f"TEAM {roster_id}"
    )
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    return FantasyRoster(
        roster_id,
        owner_id,
        team_name,
        _integer(settings.get("wins")),
        _integer(settings.get("losses")),
        _integer(settings.get("ties")),
        tuple(str(value) for value in (payload.get("starters") or [])),
        tuple(str(value) for value in (payload.get("players") or [])),
    )


def classify_matchup_status(
    user_row, opponent_row, games, projections, league_status: str
) -> MatchupStatus:
    starter_ids = {
        str(player_id)
        for row in (user_row, opponent_row)
        for player_id in (row.get("starters") or [])
        if str(player_id) not in {"", "0", "None"}
    }
    game_ids = {
        projection.game_id
        for player_id in starter_ids
        if (projection := projections.get(player_id)) is not None
        and projection.game_id is not None
    }
    states = {games[game_id].state for game_id in game_ids if game_id in games}
    all_week_final = bool(games) and all(game.state == "final" for game in games.values())
    full_starter_coverage = bool(starter_ids) and len(game_ids) == len(starter_ids) and all(
        game_id in games for game_id in game_ids
    )
    scoring_started = any(
        _number(row.get("custom_points") if row.get("custom_points") is not None else row.get("points"))
        or any(_number(points) for points in (row.get("players_points") or {}).values())
        for row in (user_row, opponent_row)
    )
    if all_week_final or (full_starter_coverage and states <= {"final"}):
        return MatchupStatus.FINAL
    if "live" in states:
        return MatchupStatus.LIVE
    # A mix of completed and scheduled starter games means the fantasy
    # matchup is underway even when no NFL game happens to be live at this
    # instant.  Matchup scoring is also authoritative evidence that kickoff
    # has occurred, including when optional schedule data is stale/partial.
    if "final" in states or scoring_started:
        return MatchupStatus.LIVE
    if str(league_status).lower() == "complete":
        return MatchupStatus.FINAL
    return MatchupStatus.PREGAME


def count_live_starters(row, projections, games) -> int:
    """Count only fantasy starters whose mapped NFL game is in progress."""
    starter_ids = {
        str(player_id)
        for player_id in (row.get("starters") or [])
        if str(player_id) not in {"", "0", "None"}
    }
    count = 0
    for player_id in starter_ids:
        projection = projections.get(player_id)
        if projection is None or projection.game_id is None:
            continue
        progress = games.get(projection.game_id)
        if progress is not None and progress.state == "live":
            count += 1
    return count
