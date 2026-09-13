"""InkyPi Sleeper Fantasy Matchups plugin entry point."""

from __future__ import annotations

import logging

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
        dashboard = self.client.get_dashboard(user_reference, league_ids)
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
