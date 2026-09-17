"""Normalized presentation models for the MLB Live Score plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class GameState(str, Enum):
    PREGAME = "pregame"
    LIVE = "live"
    FINAL = "final"
    DELAYED_PREGAME = "delayed_pregame"
    DELAYED_LIVE = "delayed_live"
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    SUSPENDED_LIVE = "suspended_live"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedGameState:
    state: GameState
    display_status: str
    uses_live_layout: bool = False
    is_final: bool = False
    is_pregame: bool = False


def classify_game_state(status: dict[str, Any] | None) -> ClassifiedGameState:
    """Classify one game solely from MLB's official status fields."""
    status = status or {}
    abstract = str(status.get("abstractGameState") or "").strip().lower()
    detailed_raw = str(status.get("detailedState") or "").strip()
    detailed = detailed_raw.lower()
    coded = str(status.get("codedGameState") or status.get("statusCode") or "").upper()
    reason = str(status.get("reason") or "").strip()
    label = (reason or detailed_raw or abstract or "Status unavailable").upper()

    active = abstract == "live" or coded in {"I", "M"}
    if "cancel" in detailed or coded == "C":
        return ClassifiedGameState(GameState.CANCELLED, label)
    if "postpon" in detailed or coded == "D":
        return ClassifiedGameState(GameState.POSTPONED, label)
    if "suspend" in detailed:
        state = GameState.SUSPENDED_LIVE if active else GameState.SUSPENDED
        return ClassifiedGameState(state, label, uses_live_layout=active)
    if "delay" in detailed:
        state = GameState.DELAYED_LIVE if active else GameState.DELAYED_PREGAME
        return ClassifiedGameState(
            state, label, uses_live_layout=active, is_pregame=not active
        )
    if abstract == "final" or coded in {"F", "O", "R"} or any(
        word in detailed for word in ("final", "completed", "game over")
    ):
        return ClassifiedGameState(GameState.FINAL, label, is_final=True)
    if active:
        return ClassifiedGameState(GameState.LIVE, label, uses_live_layout=True)
    if abstract == "preview" or coded in {"P", "S", "W"} or any(
        word in detailed for word in ("scheduled", "pre-game", "pregame", "warmup")
    ):
        return ClassifiedGameState(GameState.PREGAME, label, is_pregame=True)
    return ClassifiedGameState(GameState.UNKNOWN, label)


@dataclass
class GameSummary:
    game_pk: int | None = None
    state: GameState = GameState.UNKNOWN
    status: str = "STATUS UNAVAILABLE"
    uses_live_layout: bool = False
    is_final: bool = False
    is_pregame: bool = False
    away_team_id: int | None = None
    away_team_name: str = "Away"
    away_abbreviation: str = "AWAY"
    home_team_id: int | None = None
    home_team_name: str = "Home"
    home_abbreviation: str = "HOME"
    away_runs: int | None = None
    away_hits: int | None = None
    away_errors: int | None = None
    home_runs: int | None = None
    home_hits: int | None = None
    home_errors: int | None = None
    inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    balls: int | None = None
    strikes: int | None = None
    pitcher_name: str | None = None
    batter_name: str | None = None
    runner_on_first: bool = False
    runner_on_second: bool = False
    runner_on_third: bool = False
    scheduled_time: datetime | None = None
    # Completed terminal play endTime, not feed-update/first-observed-final time.
    ended_at: datetime | None = None
    official_date: str | None = None
    venue: str | None = None
    away_starting_pitcher: str | None = None
    away_starting_pitcher_wins: int | None = None
    away_starting_pitcher_losses: int | None = None
    home_starting_pitcher: str | None = None
    home_starting_pitcher_wins: int | None = None
    home_starting_pitcher_losses: int | None = None
    winning_pitcher: str | None = None
    winning_pitcher_wins: int | None = None
    winning_pitcher_losses: int | None = None
    losing_pitcher: str | None = None
    losing_pitcher_wins: int | None = None
    losing_pitcher_losses: int | None = None
    save_pitcher: str | None = None
    save_pitcher_saves: int | None = None
    season: int | None = None


@dataclass
class DivisionStandingRow:
    abbreviation: str
    wins: int
    losses: int
    games_back: str
    is_selected_team: bool = False


@dataclass
class DivisionStandings:
    division_name: str
    rows: list[DivisionStandingRow] = field(default_factory=list)
    is_stale: bool = False


@dataclass
class ScoreboardPresentation:
    selected_team_id: int
    selected_team_name: str
    selected_team_abbreviation: str
    game: GameSummary | None
    no_game_today: bool = False
    standings: DivisionStandings | None = None
    standings_unavailable: bool = False
    updated_at: datetime | None = None
