"""InkyPi MLB Live Score plugin entry point."""

from __future__ import annotations

import logging

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
        presentation = self.data_client.get_presentation(team_id)
        return self.renderer.render(
            presentation,
            dimensions,
            show_outs=setting_enabled(settings.get("showOuts"), default=True),
            show_updated=setting_enabled(settings.get("showLastUpdated"), default=False),
        )
