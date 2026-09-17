"""InkyPi Sleeper Fantasy Matchups plugin entry point."""

from __future__ import annotations

import logging
import threading
import time

from plugins.base_plugin.base_plugin import BasePlugin

from .renderer import MatchupDashboardRenderer
from .sleeper_client import SleeperClient, selected_league_ids

logger = logging.getLogger(__name__)


def setting_enabled(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


class SleeperFantasyMatchups(BasePlugin):
    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        self.client = dependencies.get("client") or SleeperClient()
        self.renderer = dependencies.get("renderer") or MatchupDashboardRenderer()
        self.monotonic = dependencies.get("monotonic") or time.monotonic
        self._dashboard_cache = {}
        self._dashboard_cache_lock = threading.Lock()

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        settings = settings or {}
        league_ids = selected_league_ids(settings)
        if any(not league_id.isdigit() for league_id in league_ids):
            raise RuntimeError("Sleeper league IDs must contain numbers only.")
        user_reference = str(
            settings.get("resolvedUserId") or settings.get("sleeperUser") or ""
        ).strip()
        dashboard = self._get_dashboard(user_reference, league_ids)
        # Plugin settings are mutable in InkyPi; retain the stable ID in memory,
        # while the settings page hidden field persists it on the next save.
        settings["resolvedUserId"] = dashboard.sleeper_user.user_id
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        logger.info(
            "Rendering %s Sleeper matchup card(s) for user %s",
            len(dashboard.matchups), dashboard.sleeper_user.user_id,
        )
        return self.renderer.render(
            dashboard,
            dimensions,
            show_projected=setting_enabled(settings.get("showProjectedFinal"), True),
            show_probability=setting_enabled(settings.get("showWinProbability"), True),
            show_records=setting_enabled(settings.get("showRecords"), True),
            show_context=setting_enabled(settings.get("showContextRow"), True),
        )

    def get_scheduler_state(self, settings, device_config, current_dt):
        settings = settings or {}
        league_ids = selected_league_ids(settings)
        user_reference = str(
            settings.get("resolvedUserId") or settings.get("sleeperUser") or ""
        ).strip()
        dashboard = self._get_dashboard(user_reference, league_ids)
        live = [matchup for matchup in dashboard.matchups if matchup.scheduler_is_live]
        complete = [
            matchup for matchup in dashboard.matchups
            if matchup.user_side is not None and matchup.opponent_side is not None
        ]
        probabilities = [
            matchup.user_side.win_probability
            for matchup in complete
            if matchup.user_side.win_probability is not None
        ]
        close_count = sum(1 for matchup in live if self._is_close(matchup))
        return {
            "matchups": {
                "total_count": len(dashboard.matchups),
                "live_count": len(live),
                "any_live": bool(live),
                "close_game_count": close_count,
                "user_leading_count": sum(
                    matchup.user_side.current_score > matchup.opponent_side.current_score
                    for matchup in complete
                ),
                "user_trailing_count": sum(
                    matchup.user_side.current_score < matchup.opponent_side.current_score
                    for matchup in complete
                ),
                "min_user_win_probability": min(probabilities) if probabilities else None,
                "max_user_win_probability": max(probabilities) if probabilities else None,
                "closest_win_probability_to_50": (
                    min(abs(probability - 0.5) for probability in probabilities)
                    if probabilities else None
                ),
                "live_starters_count": sum(
                    matchup.user_live_starters_count + matchup.opponent_live_starters_count
                    for matchup in dashboard.matchups
                ),
            }
        }

    def get_scheduler_state_schema(self):
        return {
            "matchups.total_count": {"label": "Displayed matchups", "type": "number"},
            "matchups.live_count": {
                "label": "Live matchups", "type": "number",
                "description": "Matchups with at least one starting player in a live NFL game.",
            },
            "matchups.any_live": {
                "label": "Any matchup has a starting player currently playing",
                "type": "boolean",
            },
            "matchups.close_game_count": {
                "label": "Close live matchups", "type": "number",
                "description": "Live matchups with win probability from 35% through 65%.",
            },
            "matchups.user_leading_count": {"label": "Matchups I'm leading", "type": "number"},
            "matchups.user_trailing_count": {"label": "Matchups I'm trailing", "type": "number"},
            "matchups.min_user_win_probability": {"label": "Lowest win probability", "type": "probability"},
            "matchups.max_user_win_probability": {"label": "Highest win probability", "type": "probability"},
            "matchups.closest_win_probability_to_50": {
                "label": "Closest win probability to 50%", "type": "probability",
                "description": "Absolute distance from an even 50% probability.",
            },
            "matchups.live_starters_count": {
                "label": "Starting players currently playing", "type": "number",
            },
        }

    def _get_dashboard(self, user_reference, league_ids):
        """Share a brief normalized-model cache between scheduling and rendering."""
        key = (user_reference, tuple(league_ids))
        with self._dashboard_cache_lock:
            now = self.monotonic()
            cached = self._dashboard_cache.get(key)
            if cached and now - cached[0] <= 30:
                return cached[1]
            dashboard = self.client.get_dashboard(user_reference, league_ids)
            self._dashboard_cache[key] = (now, dashboard)
            return dashboard

    @staticmethod
    def _is_close(matchup):
        if not matchup.user_side or not matchup.opponent_side:
            return False
        probability = matchup.user_side.win_probability
        if probability is not None:
            return 0.35 <= probability <= 0.65
        return False
