import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flask import Flask

from blueprints.settings import settings_bp
from model import Playlist, PlaylistManager, PluginInstance


class FakeDeviceConfig:
    def __init__(self):
        instance = PluginInstance("clock", "Morning Clock", {}, {"interval": 60}, instance_id="clock-stable")
        self.playlist_manager = PlaylistManager([
            Playlist("Default", "00:00", "24:00", [instance.to_dict()])
        ])
        self.config = {
            "name": "Test",
            "orientation": "horizontal",
            "inverted_image": False,
            "log_system_stats": False,
            "timezone": "UTC",
            "time_format": "24h",
            "plugin_cycle_interval_seconds": 3600,
            "image_settings": {},
        }
        self.updated = False

    def get_config(self, key=None, default=None):
        return self.config if key is None else self.config.get(key, default)

    def get_playlist_manager(self):
        return self.playlist_manager

    def get_plugin(self, plugin_id):
        return {"id": plugin_id}

    def update_config(self, values):
        self.config.update(values)
        self.updated = True


class FakeRefreshTask:
    def __init__(self):
        self.signals = 0

    def signal_config_change(self):
        self.signals += 1


def app_with_config():
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "src" / "templates"))
    app.register_blueprint(settings_bp)
    device = FakeDeviceConfig()
    refresh = FakeRefreshTask()
    app.config.update(DEVICE_CONFIG=device, REFRESH_TASK=refresh)
    return app, device, refresh


def form(scheduler):
    return {
        "deviceName": "Test",
        "orientation": "horizontal",
        "timezoneName": "UTC",
        "timeFormat": "24h",
        "unit": "hour",
        "interval": "1",
        "saturation": "1",
        "brightness": "1",
        "sharpness": "1",
        "contrast": "1",
        "dynamicSchedulerEnabled": "on",
        "dynamicSchedulerConfig": json.dumps(scheduler),
    }


def valid_scheduler():
    return {
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {"minimum_display_seconds": 60},
        "rules": [{
            "id": "morning",
            "priority": 30,
            "target": {"instance_id": "clock-stable"},
            "conditions": {"time": {"days": ["mon"], "start": "07:00", "end": "09:00"}},
            "duration": {"mode": "while_true"},
        }],
    }


def test_settings_page_loads_visual_builder_and_default_scheduler():
    app, _device, _refresh = app_with_config()
    response = app.test_client().get("/settings")
    assert response.status_code == 200
    assert b'id="schedulerRuleList"' in response.data
    assert b"Advanced JSON" in response.data
    assert b"evaluation_interval_seconds" in response.data


def test_valid_scheduler_is_persisted_and_signaled():
    app, device, refresh = app_with_config()
    response = app.test_client().post("/save_settings", data=form(valid_scheduler()))
    assert response.status_code == 200
    assert device.config["dynamic_scheduler"]["enabled"] is True
    assert device.config["dynamic_scheduler"]["evaluation_interval_seconds"] == 60
    assert refresh.signals == 1


def test_invalid_scheduler_json_does_not_change_settings():
    app, device, _refresh = app_with_config()
    values = form(valid_scheduler())
    values["dynamicSchedulerConfig"] = "{broken"
    response = app.test_client().post("/save_settings", data=values)
    assert response.status_code == 400
    assert not device.updated


def test_missing_target_is_rejected_without_writing():
    app, device, _refresh = app_with_config()
    scheduler = valid_scheduler()
    scheduler["rules"][0]["target"]["instance_id"] = "missing"
    response = app.test_client().post("/save_settings", data=form(scheduler))
    assert response.status_code == 400
    assert b"does not identify" in response.data
    assert not device.updated
