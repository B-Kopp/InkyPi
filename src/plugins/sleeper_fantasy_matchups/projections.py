"""Optional adapter for Sleeper's undocumented projection services.

These endpoints are useful but are not part of Sleeper's supported public API.
Every failure is intentionally converted to unavailable projection data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from utils.http_client import get_http_session

from .models import GameProgress, PlayerProjection

logger = logging.getLogger(__name__)

PROJECTION_API = "https://api.sleeper.com"
REQUEST_TIMEOUT = (5, 15)
PROJECTION_TTL = timedelta(minutes=20)
SCHEDULE_TTL = timedelta(seconds=60)


class SleeperProjectionAdapter:
    def __init__(self, session=None, now_provider: Callable[[], datetime] | None = None):
        self.session = session or get_http_session()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[Any, ...], tuple[datetime, Any]] = {}

    def _get_list(self, kind: str, season: str, week: int, season_type: str) -> list:
        key = (kind, str(season), int(week), str(season_type))
        cached = self._cache.get(key)
        if cached and self.now_provider() - cached[0] <= PROJECTION_TTL:
            return cached[1]
        url = f"{PROJECTION_API}/{kind}/nfl/{season}/{week}"
        response = self.session.get(
            url, params={"season_type": season_type}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"{kind} response was not a JSON list")
        self._cache[key] = (self.now_provider(), payload)
        return payload

    def _get_schedule(self, season: str, season_type: str) -> list:
        key = ("schedule", str(season), str(season_type))
        cached = self._cache.get(key)
        if cached and self.now_provider() - cached[0] <= SCHEDULE_TTL:
            return cached[1]
        response = self.session.get(
            f"{PROJECTION_API}/schedule/nfl/{season_type}/{season}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("schedule response was not a JSON list")
        self._cache[key] = (self.now_provider(), payload)
        return payload

    def get_week(
        self, season: str, week: int, season_type: str = "regular"
    ) -> tuple[dict[str, PlayerProjection], dict[str, GameProgress]]:
        try:
            raw_projections = self._get_list("projections", season, week, season_type)
            raw_schedule = self._get_schedule(season, season_type)
            projections = {
                value.player_id: value
                for item in raw_projections
                if (value := parse_projection(item)) is not None
            }
            games = {
                value.game_id: value
                for item in raw_schedule
                if isinstance(item, dict) and int(item.get("week") or -1) == int(week)
                if (value := parse_game_progress(item)) is not None
            }
            return projections, games
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            logger.warning("Optional Sleeper projections unavailable: %s", exc)
            return {}, {}


def parse_projection(payload: Any) -> PlayerProjection | None:
    if not isinstance(payload, dict) or payload.get("player_id") is None:
        return None
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    numeric: dict[str, float] = {}
    for key, value in stats.items():
        try:
            numeric[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
    positions = player.get("fantasy_positions") or []
    position = str(player.get("position") or (positions[0] if positions else ""))
    game_id = payload.get("game_id")
    return PlayerProjection(
        player_id=str(payload["player_id"]),
        position=position.upper(),
        game_id=str(game_id) if game_id is not None else None,
        stats=numeric,
    )


def parse_game_progress(payload: Any) -> GameProgress | None:
    if not isinstance(payload, dict):
        return None
    game_id = payload.get("game_id") or payload.get("gameId") or payload.get("id")
    if game_id is None:
        return None
    raw_status = str(
        payload.get("status") or payload.get("game_status") or payload.get("state") or ""
    ).lower()
    if any(token in raw_status for token in ("final", "complete", "post")):
        return GameProgress(str(game_id), "final", 0.0)
    if any(token in raw_status for token in ("pre", "schedule", "not_started")):
        return GameProgress(str(game_id), "pregame", 1.0)
    if any(token in raw_status for token in ("progress", "live", "half", "quarter", "overtime")):
        return GameProgress(str(game_id), "live", _remaining_fraction(payload))
    if "delay" in raw_status and (payload.get("quarter") or payload.get("period")):
        # A delayed game which has already entered a period is an interrupted
        # in-progress game, not a future kickoff.
        return GameProgress(str(game_id), "live", _remaining_fraction(payload))
    # Sleeper commonly leaves ``status`` null on otherwise valid future
    # schedule rows.  The row's presence in the requested week is enough to
    # identify a not-yet-started game.  Once scoring begins, the matchup feed
    # is used as an additional guard so this fallback cannot hold a matchup in
    # PREGAME.
    return GameProgress(str(game_id), "pregame", 1.0)


def _remaining_fraction(payload: dict[str, Any]) -> float | None:
    try:
        quarter = int(payload.get("quarter") or payload.get("period") or 0)
    except (TypeError, ValueError):
        quarter = 0
    clock = str(payload.get("time") or payload.get("clock") or "")
    if quarter <= 0:
        return None
    if quarter > 4:
        return 0.05
    try:
        minutes, seconds = clock.split(":", 1)
        in_period = float(minutes) + float(seconds) / 60.0
    except (TypeError, ValueError):
        return None
    remaining = (4 - quarter) * 15.0 + min(15.0, max(0.0, in_period))
    return min(1.0, max(0.0, remaining / 60.0))
