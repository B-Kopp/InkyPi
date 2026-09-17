import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flask import Flask

import blueprints.scheduler as scheduler_routes
from blueprints.scheduler import scheduler_bp
from model import Playlist, PlaylistManager, PluginInstance


class Plugin:
    def get_scheduler_state_schema(self):
        return {
            "game.live": {"label": "Game is live", "type": "boolean"},
            "game.inning": {"label": "Inning", "type": "number"},
        }

    def get_scheduler_state(self, settings, device, current_dt):
        return {"game": {"live": settings["live"], "inning": settings["inning"]}}


class Device:
    def __init__(self):
        instance = PluginInstance(
            "fake", "Scoreboard", {"live": True, "inning": 8}, {"interval": 60}, instance_id="score-id"
        )
        self.manager = PlaylistManager([
            Playlist("Default", "00:00", "24:00", [instance.to_dict()], current_plugin_index=0)
        ])
        self.config = {
            "timezone": "UTC",
            "dynamic_scheduler": config(rule()),
        }
        self.writes = 0

    def get_playlist_manager(self):
        return self.manager

    def get_plugin(self, plugin_id):
        return {"id": plugin_id, "display_name": "Fake Scores"}

    def get_config(self, key=None, default=None):
        return self.config if key is None else self.config.get(key, default)

    def update_value(self, key, value, write=False):
        self.config[key] = value
        self.writes += int(write)


class Refresh:
    def __init__(self):
        self.signals = 0
        self.status = {
            "enabled": True, "valid": True, "currently_displaying": "Scoreboard",
            "selected_by": "Live game", "rule_id": "live", "priority": 80,
            "active_since": "2026-09-14T12:00:00+00:00",
            "return_behavior": "resume_previous", "next_evaluation_seconds": 25,
            "reason": "Selected by the highest-priority active rule",
        }

    def signal_config_change(self):
        self.signals += 1

    def get_scheduler_status(self, current_dt):
        return deepcopy(self.status)


def rule(rule_id="live", priority=80):
    return {
        "id": rule_id, "name": "Live game", "enabled": True, "priority": priority,
        "target": {"instance_id": "score-id"},
        "conditions": {"source_instance_id": "score-id", "path": "game.live", "operator": "eq", "value": True},
        "duration": {"mode": "while_true", "min_seconds": 0},
        "return_behavior": "resume_previous",
    }


def config(*rules):
    return {
        "enabled": True, "version": 1, "evaluation_interval_seconds": 60,
        "defaults": {"fallback": "playlist", "return_behavior": "resume_previous", "minimum_display_seconds": 0},
        "rules": list(rules),
    }


def client(monkeypatch):
    monkeypatch.setattr(scheduler_routes, "get_plugin_instance", lambda _config: Plugin())
    app = Flask(__name__)
    app.register_blueprint(scheduler_bp)
    device, refresh = Device(), Refresh()
    app.config.update(DEVICE_CONFIG=device, REFRESH_TASK=refresh)
    return app.test_client(), device, refresh


def test_config_catalog_contains_stable_ids_and_schema(monkeypatch):
    browser, _device, _refresh = client(monkeypatch)
    data = browser.get("/scheduler/config").get_json()
    assert data["instances"][0]["instance_id"] == "score-id"
    assert data["instances"][0]["schema"]["game.live"]["type"] == "boolean"


def test_state_inspector_returns_only_flat_scheduler_contract(monkeypatch):
    browser, _device, _refresh = client(monkeypatch)
    data = browser.get("/scheduler/state").get_json()["instances"][0]
    assert data["available"] is True
    assert data["state"] == {"game.inning": 8, "game.live": True}


def test_status_reports_active_and_other_matching_rules(monkeypatch):
    browser, device, _refresh = client(monkeypatch)
    device.config["dynamic_scheduler"] = config(rule(), rule("lower", 60))
    data = browser.get("/scheduler/status").get_json()
    assert data["selected_by"] == "Live game" and data["priority"] == 80
    assert data["next_evaluation_seconds"] == 25
    assert data["matching_rules"] == [{"name": "Live game", "priority": 60, "rule_id": "lower"}]


def test_status_playlist_fallback_is_clear(monkeypatch):
    browser, device, refresh = client(monkeypatch)
    device.manager.playlists[0].plugins[0].settings["live"] = False
    refresh.status.update({
        "currently_displaying": "Normal Playlist", "selected_by": None,
        "rule_id": None, "priority": None,
        "reason": "No dynamic rules currently match",
    })
    data = browser.get("/scheduler/status").get_json()
    assert data["currently_displaying"] == "Normal Playlist"
    assert data["matching_rules"] == []


def test_test_rule_is_observational_and_returns_current_value(monkeypatch):
    browser, device, refresh = client(monkeypatch)
    index = device.manager.playlists[0].current_plugin_index
    before = deepcopy(device.config)
    data = browser.post("/scheduler/test-rule", json={"rule": rule()}).get_json()
    assert data["matched"] is True
    assert data["condition"]["current_value"] is True
    assert device.manager.playlists[0].current_plugin_index == index
    assert device.config == before and device.writes == 0 and refresh.signals == 0


def test_test_all_reports_winner_without_saving(monkeypatch):
    browser, device, _refresh = client(monkeypatch)
    candidate = config(rule("low", 60), rule("high", 80))
    data = browser.post("/scheduler/test-all", json={"config": candidate}).get_json()
    assert data["winner_rule_id"] == "high"
    assert device.writes == 0


def test_save_config_validates_persists_and_wakes_scheduler(monkeypatch):
    browser, device, refresh = client(monkeypatch)
    candidate = config(rule("new", 40))
    response = browser.put("/scheduler/config", json=candidate)
    assert response.status_code == 200
    assert device.config["dynamic_scheduler"] == candidate
    assert device.writes == 1 and refresh.signals == 1


def test_invalid_config_keeps_last_valid_value(monkeypatch):
    browser, device, refresh = client(monkeypatch)
    before = deepcopy(device.config["dynamic_scheduler"])
    invalid = config(rule())
    invalid["rules"][0]["priority"] = "high"
    response = browser.put("/scheduler/config", json=invalid)
    assert response.status_code == 400
    assert device.config["dynamic_scheduler"] == before
    assert device.writes == 0 and refresh.signals == 0


def test_visual_rule_crud_shapes_share_the_persisted_json_model(monkeypatch):
    browser, device, refresh = client(monkeypatch)
    candidate = config(rule("created", 60))
    candidate["rules"][0]["name"] = "Created Rule"
    assert browser.put("/scheduler/config", json=candidate).status_code == 200

    candidate["rules"][0]["name"] = "Edited Rule"
    candidate["rules"][0]["priority"] = 80
    assert browser.put("/scheduler/config", json=candidate).status_code == 200

    duplicate = deepcopy(candidate["rules"][0])
    duplicate.update(id="created_copy", name="Edited Rule Copy", enabled=False)
    candidate["rules"].append(duplicate)
    assert browser.put("/scheduler/config", json=candidate).status_code == 200
    assert device.config["dynamic_scheduler"]["rules"][1]["enabled"] is False

    candidate["rules"][1]["enabled"] = True
    assert browser.put("/scheduler/config", json=candidate).status_code == 200
    candidate["rules"].pop(0)
    assert browser.put("/scheduler/config", json=candidate).status_code == 200
    assert [item["id"] for item in device.config["dynamic_scheduler"]["rules"]] == ["created_copy"]
    assert device.writes == 5 and refresh.signals == 5


def test_temporal_test_endpoints_do_not_consume_or_activate(monkeypatch):
    from scheduler.config import load_scheduler_config
    from scheduler.dynamic_scheduler import DynamicScheduler

    browser, device, refresh = client(monkeypatch)
    timed = rule("hourly", 20)
    timed.update(schedule={"type": "minute_of_hour", "minutes": [5]},
                 conditions={"time": {}}, duration={"mode": "fixed", "seconds": 120})
    device.config["dynamic_scheduler"] = config(timed)
    refresh.dynamic_scheduler = DynamicScheduler(load_scheduler_config(config(timed)))
    now = datetime(2026, 9, 17, 10, 5, 31, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_routes, "_now", lambda: now)
    before = deepcopy(refresh.dynamic_scheduler.state)
    index = device.manager.playlists[0].current_plugin_index
    for endpoint, payload in (("test-rule", {"rule": timed}), ("test-all", {"config": config(timed)})):
        response = browser.post("/scheduler/" + endpoint, json=payload)
        assert response.status_code == 200
        data = response.get_json()
        detail = data if endpoint == "test-rule" else data["rules"][0]
        assert detail["matched"] and detail["schedule"]["due"]
        assert detail["schedule"]["scheduled_at"] == "2026-09-17T10:05:00+00:00"
        assert detail["schedule"]["next_occurrence"] == "2026-09-17T11:05:00+00:00"
    assert refresh.dynamic_scheduler.state == before
    assert device.manager.playlists[0].current_plugin_index == index
    assert device.writes == refresh.signals == 0


def test_temporal_status_reports_upcoming_schedule(monkeypatch):
    browser, device, _refresh = client(monkeypatch)
    timed = rule("hourly", 20)
    timed.update(schedule={"type": "minute_of_hour", "minutes": [5]}, conditions={"time": {}})
    device.config["dynamic_scheduler"] = config(timed)
    monkeypatch.setattr(scheduler_routes, "_now", lambda: datetime(2026, 9, 17, 10, 4, tzinfo=timezone.utc))
    data = browser.get("/scheduler/status").get_json()
    assert data["temporal_rules"][0]["next_occurrence"] == "2026-09-17T10:05:00+00:00"
    assert data["temporal_rules"][0]["due"] is False
