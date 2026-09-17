"""Normalized models for the Sleeper Fantasy Matchups plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MatchupStatus(str, Enum):
    PREGAME = "PREGAME"
    LIVE = "LIVE"
    FINAL = "FINAL"
    BYE = "BYE"
    PLAYOFF = "PLAYOFF"
    NO_MATCHUP = "NO MATCHUP"
    SEASON_COMPLETE = "SEASON COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SleeperUser:
    user_id: str
    username: str
    display_name: str


@dataclass(frozen=True)
class LeagueInfo:
    league_id: str
    name: str
    season: str
    status: str
    scoring_settings: dict[str, float] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FantasyRoster:
    roster_id: int
    owner_user_id: str | None
    team_name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    starters: tuple[str, ...] = ()
    players: tuple[str, ...] = ()

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base


@dataclass
class MatchupSide:
    roster_id: int
    team_name: str
    owner_name: str
    record: str
    current_score: float = 0.0
    projected_final: float | None = None
    estimated_remaining_points: float | None = None
    win_probability: float | None = None
    top_scorer: str | None = None
    starters_remaining: int | None = None
    remaining_variance: float | None = None


@dataclass
class FantasyMatchup:
    league_id: str
    league_name: str
    week: int
    status: MatchupStatus
    user_side: MatchupSide | None = None
    opponent_side: MatchupSide | None = None
    last_updated: datetime | None = None
    projections_available: bool = False
    error_state: str | None = None
    is_playoff: bool = False
    scheduler_is_live: bool = False
    user_live_starters_count: int = 0
    opponent_live_starters_count: int = 0

    @property
    def status_label(self) -> str:
        if self.is_playoff and self.status in {
            MatchupStatus.PREGAME, MatchupStatus.LIVE, MatchupStatus.FINAL
        }:
            return f"PLAYOFF · {self.status.value}"
        return self.status.value


@dataclass
class DashboardModel:
    sleeper_user: SleeperUser
    season: str
    week: int
    matchups: list[FantasyMatchup] = field(default_factory=list)


@dataclass(frozen=True)
class PlayerProjection:
    player_id: str
    position: str
    game_id: str | None
    stats: dict[str, float]


@dataclass(frozen=True)
class GameProgress:
    game_id: str
    state: str
    remaining_fraction: float | None
