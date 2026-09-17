import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flask import Flask

from blueprints.plugin import plugin_bp
from model import Playlist, PlaylistManager, PluginInstance
from refresh_task import ManualRefresh, PlaylistRefresh


class Device:
    def __init__(self):
        instance = PluginInstance("fake", "Saved", {"message": "saved"}, {"interval": 60}, instance_id="saved-id")
        self.manager = PlaylistManager([
            Playlist("Default", "00:00", "24:00", [instance.to_dict()])
        ])

    def get_playlist_manager(self):
        return self.manager


class Refresh:
    running = True

    def __init__(self):
        self.actions = []

    def manual_update(self, action):
        self.actions.append(action)


def client():
    app = Flask(__name__)
    app.register_blueprint(plugin_bp)
    refresh = Refresh()
    app.config.update(DEVICE_CONFIG=Device(), REFRESH_TASK=refresh, DISPLAY_MANAGER=object())
    return app.test_client(), refresh


def test_display_now_for_saved_instance_still_forces_playlist_execution():
    browser, refresh = client()
    response = browser.post("/display_plugin_instance", json={
        "playlist_name": "Default",
        "plugin_id": "fake",
        "plugin_instance": "Saved",
    })
    assert response.status_code == 200
    assert len(refresh.actions) == 1
    assert isinstance(refresh.actions[0], PlaylistRefresh)
    assert refresh.actions[0].force is True


def test_unsaved_display_now_still_uses_manual_refresh():
    browser, refresh = client()
    response = browser.post("/update_now", data={"plugin_id": "fake", "message": "preview"})
    assert response.status_code == 200
    assert len(refresh.actions) == 1
    assert isinstance(refresh.actions[0], ManualRefresh)
    assert refresh.actions[0].plugin_settings["message"] == "preview"

