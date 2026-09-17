"""InkyPi MLB Live Score plugin entry point."""

from __future__ import annotations

import logging
import threading
import time

from plugins.base_plugin.base_plugin import BasePlugin

from .mlb_data import MlbDataClient
from .renderer import ScoreboardRenderer

logger = logging.getLogger(__name__)


def setting_enabled(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


class MlbLiveScore(BasePlugin):
    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        self.data_client = dependencies.get("data_client") or MlbDataClient()
        self.renderer = dependencies.get("renderer") or ScoreboardRenderer()
        self.monotonic = dependencies.get("monotonic") or time.monotonic
        self._presentation_cache = {}
        self._presentation_cache_lock = threading.Lock()

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        settings = settings or {}
        try:
            team_id = int(settings.get("mlbTeamId", 144))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Please select a valid MLB team.") from exc

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        logger.info("Generating MLB Live Score for team ID %s", team_id)
        presentation = self._get_presentation(team_id)
        return self.renderer.render(
            presentation,
            dimensions,
            show_outs=setting_enabled(settings.get("showOuts"), default=True),
            show_updated=setting_enabled(settings.get("showLastUpdated"), default=False),
        )

    def get_scheduler_state(self, settings, device_config, current_dt):
        settings = settings or {}
        try:
            team_id = int(settings.get("mlbTeamId", 144))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Please select a valid MLB team.") from exc
        presentation = self._get_presentation(team_id)
        game = presentation.game
        selected_is_away = bool(game and game.away_team_id == presentation.selected_team_id)
        selected_score = None
        opponent_score = None
        # MLB may serialize pregame linescores as 0-0 with inning 1. Those are
        # placeholders, not meaningful scheduler facts.
        if game and not game.is_pregame:
            selected_score = game.away_runs if selected_is_away else game.home_runs
            opponent_score = game.home_runs if selected_is_away else game.away_runs
        scores_available = selected_score is not None and opponent_score is not None
        score_diff = abs(selected_score - opponent_score) if scores_available else None
        selected_leading = bool(scores_available and selected_score > opponent_score)
        selected_trailing = bool(scores_available and selected_score < opponent_score)
        tied = bool(scores_available and selected_score == opponent_score)
        is_live = bool(game and game.uses_live_layout)
        return {
            "game": {
                "status": game.state.value if game else "unavailable",
                "is_live": is_live,
                "is_pregame": bool(game and game.is_pregame),
                "is_final": bool(game and game.is_final),
                "inning": game.inning if game and not game.is_pregame else None,
                "inning_half": (
                    game.inning_half.lower()
                    if game and not game.is_pregame and game.inning_half else None
                ),
                "selected_team": presentation.selected_team_abbreviation,
                "selected_team_score": selected_score,
                "opponent_score": opponent_score,
                "score_diff": score_diff,
                "selected_team_leading": selected_leading,
                "selected_team_trailing": selected_trailing,
                "tied": tied,
                "is_close": bool(is_live and score_diff is not None and score_diff <= 2),
                "start_time": game.scheduled_time.isoformat() if game and game.scheduled_time else None,
                "has_game_today": bool(game and not presentation.no_game_today),
            }
        }

    def get_scheduler_state_schema(self):
        return {
            "game.status": {
                "label": "Game status", "type": "enum",
                "allowed_values": [
                    "pregame", "live", "final", "delayed_pregame",
                    "delayed_live", "postponed", "suspended",
                    "suspended_live", "cancelled", "unknown", "unavailable",
                ],
            },
            "game.is_live": {"label": "Game is live", "type": "boolean"},
            "game.is_pregame": {"label": "Game is pregame", "type": "boolean"},
            "game.is_final": {"label": "Game is final", "type": "boolean"},
            "game.selected_team": {"label": "Selected team", "type": "string"},
            "game.inning": {"label": "Inning", "type": "number"},
            "game.inning_half": {
                "label": "Inning half", "type": "enum",
                "allowed_values": ["top", "bottom"],
            },
            "game.selected_team_score": {"label": "Selected team score", "type": "number"},
            "game.opponent_score": {"label": "Opponent score", "type": "number"},
            "game.score_diff": {
                "label": "Score difference", "type": "number",
                "description": "Absolute run difference.",
            },
            "game.selected_team_leading": {"label": "Selected team is leading", "type": "boolean"},
            "game.selected_team_trailing": {"label": "Selected team is trailing", "type": "boolean"},
            "game.tied": {"label": "Game is tied", "type": "boolean"},
            "game.is_close": {
                "label": "Game is close", "type": "boolean",
                "description": "Live game with a score difference of two runs or fewer.",
            },
            "game.start_time": {"label": "Game start time", "type": "datetime"},
            "game.has_game_today": {"label": "Team has a game today", "type": "boolean"},
        }

    def _get_presentation(self, team_id):
        """Share a brief domain-model cache between scheduling and rendering."""
        with self._presentation_cache_lock:
            now = self.monotonic()
            cached = self._presentation_cache.get(team_id)
            if cached and now - cached[0] <= 30:
                return cached[1]
            presentation = self.data_client.get_presentation(team_id)
            self._presentation_cache[team_id] = (now, presentation)
            return presentation
