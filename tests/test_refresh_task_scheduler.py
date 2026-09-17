import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import refresh_task as refresh_module
from model import Playlist, PlaylistManager, PluginInstance, RefreshInfo
from refresh_task import DynamicPlaylistRefresh, ManualRefresh, PlaylistRefresh, RefreshTask, SchedulerReturnRefresh


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class FakePlugin:
    def __init__(self):
        self.config = {"id": "fake", "image_settings": []}
        self.states = {}
        self.generated = []

    def get_scheduler_state(self, settings, device_config, current_dt):
        value = self.states.get(settings["key"], False)
        if isinstance(value, Exception):
            raise value
        return {"active": value}

    def generate_image(self, settings, device_config):
        self.generated.append(settings["key"])
        color = {"one": "red", "two": "blue", "three": "green"}[settings["key"]]
        return Image.new("RGB", (8, 8), color)


class FakeDisplayManager:
    def __init__(self):
        self.images = []

    def display_image(self, image, image_settings=None):
        self.images.append(image.copy())


class FakeConfig:
    def __init__(self, tmp_path, scheduler_config=None):
        instances = [
            PluginInstance("fake", "One", {"key": "one"}, {"interval": 60}, instance_id="one-id"),
            PluginInstance("fake", "Two", {"key": "two"}, {"interval": 60}, instance_id="two-id"),
            PluginInstance("fake", "Three", {"key": "three"}, {"interval": 60}, instance_id="three-id"),
        ]
        self.playlist_manager = PlaylistManager([
            Playlist("Default", "00:00", "24:00", [instance.to_dict() for instance in instances], current_plugin_index=0)
        ], active_playlist="Default")
        self.refresh_info = RefreshInfo("Playlist", "fake", (NOW - timedelta(minutes=30)).isoformat(), "old", "Default", "One")
        self.config = {
            "plugin_cycle_interval_seconds": 3600,
            "timezone": "UTC",
            "log_system_stats": False,
        }
        if scheduler_config is not None:
            self.config["dynamic_scheduler"] = scheduler_config
        self.plugin_image_dir = str(tmp_path)
        self.writes = 0

    def get_config(self, key=None, default=None):
        return self.config if key is None else self.config.get(key, default)

    def get_playlist_manager(self):
        return self.playlist_manager

    def get_refresh_info(self):
        return self.refresh_info

    def get_plugin(self, plugin_id):
        return {"id": plugin_id, "image_settings": []} if plugin_id == "fake" else None

    def write_config(self):
        self.writes += 1


def time_rule(target="two-id", *, return_behavior="resume_previous"):
    return {
        "id": "morning",
        "priority": 30,
        "target": {"instance_id": target},
        "conditions": {"time": {"days": ["mon"], "start": "11:00", "end": "13:00"}},
        "duration": {"mode": "while_true", "min_seconds": 0},
        "return_behavior": return_behavior,
    }


def state_rule(target="two-id", source="two-id", priority=80, rule_id="live", return_behavior="resume_previous"):
    return {
        "id": rule_id,
        "priority": priority,
        "target": {"instance_id": target},
        "conditions": {"source_instance_id": source, "path": "active", "operator": "truthy"},
        "duration": {"mode": "while_true", "min_seconds": 0},
        "return_behavior": return_behavior,
    }


def scheduler_config(*rules, enabled=True):
    return {
        "enabled": enabled,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {"fallback": "playlist", "minimum_display_seconds": 0},
        "rules": list(rules),
    }


def task(tmp_path, monkeypatch, config=None):
    plugin = FakePlugin()
    monkeypatch.setattr(refresh_module, "get_plugin_instance", lambda _config: plugin)
    device = FakeConfig(tmp_path, config)
    result = RefreshTask(device, FakeDisplayManager())
    # StateProvider captures the imported loader at construction, which now is
    # the monkeypatched fake registry function.
    return result, device, plugin


def test_scheduler_off_uses_exact_normal_playlist_path(tmp_path, monkeypatch):
    value, device, _plugin = task(tmp_path, monkeypatch, scheduler_config(enabled=False))
    action = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=31))
    assert isinstance(action, PlaylistRefresh)
    assert action.plugin_instance.name == "Two"
    assert device.playlist_manager.playlists[0].current_plugin_index == 1


def test_scheduler_on_no_match_uses_normal_playlist_clock(tmp_path, monkeypatch):
    value, device, _plugin = task(tmp_path, monkeypatch, scheduler_config(time_rule()))
    after_window = NOW + timedelta(hours=2)
    action = value._determine_refresh_action(device.playlist_manager, device.refresh_info, after_window)
    assert isinstance(action, PlaylistRefresh)
    assert action.plugin_instance.name == "Two"


def test_time_rule_selects_target_without_advancing_playlist_and_uses_shared_execution(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(time_rule()))
    index = device.playlist_manager.playlists[0].current_plugin_index
    action = value._run_cycle(NOW)
    assert isinstance(action, DynamicPlaylistRefresh)
    assert action.plugin_instance.instance_id == "two-id"
    assert device.playlist_manager.playlists[0].current_plugin_index == index
    assert plugin.generated == ["two"]
    assert len(value.display_manager.images) == 1
    assert device.refresh_info.refresh_type == "Dynamic Scheduler"


def test_invalid_scheduler_config_safely_falls_back(tmp_path, monkeypatch):
    invalid = scheduler_config(time_rule())
    invalid["rules"][0]["priority"] = "high"
    value, device, _plugin = task(tmp_path, monkeypatch, invalid)
    assert not value.scheduler_config_valid
    action = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=31))
    assert isinstance(action, PlaylistRefresh)


def test_provider_failure_falls_back_without_advancing_for_failed_rule_only(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = RuntimeError("offline")
    action = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=31))
    assert isinstance(action, PlaylistRefresh)
    assert action.plugin_instance.name == "Two"


def test_resume_previous_pauses_normal_clock_and_does_not_advance_index(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    override = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW)
    assert isinstance(override, DynamicPlaylistRefresh)
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
    plugin.states["two"] = False
    returned = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=30))
    assert isinstance(returned, SchedulerReturnRefresh)
    assert returned.plugin_instance.name == "One"
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
    assert value.normal_refresh_time == NOW
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=59)) is None
    next_action = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(hours=1))
    assert next_action.plugin_instance.name == "Two"


def test_resume_playlist_advances_only_when_override_ends(tmp_path, monkeypatch):
    value, device, plugin = task(
        tmp_path, monkeypatch, scheduler_config(state_rule(return_behavior="resume_playlist"))
    )
    plugin.states["two"] = True
    value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW)
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
    plugin.states["two"] = False
    returned = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=1))
    assert isinstance(returned, PlaylistRefresh)
    assert returned.plugin_instance.name == "Two"
    assert device.playlist_manager.playlists[0].current_plugin_index == 1


def test_stay_until_next_cycle_returns_no_action(tmp_path, monkeypatch):
    value, device, plugin = task(
        tmp_path, monkeypatch, scheduler_config(state_rule(return_behavior="stay_until_next_cycle"))
    )
    plugin.states["two"] = True
    value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW)
    plugin.states["two"] = False
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=1)) is None
    assert device.playlist_manager.playlists[0].current_plugin_index == 0


def test_higher_override_ends_and_lower_valid_override_resumes(tmp_path, monkeypatch):
    rules = scheduler_config(
        state_rule(target="two-id", source="two-id", priority=60, rule_id="low"),
        state_rule(target="three-id", source="three-id", priority=80, rule_id="high"),
    )
    value, device, plugin = task(tmp_path, monkeypatch, rules)
    plugin.states.update(two=True, three=False)
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW).plugin_instance.name == "Two"
    plugin.states["three"] = True
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(seconds=1)).plugin_instance.name == "Three"
    plugin.states["three"] = False
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(seconds=2)).plugin_instance.name == "Two"
    assert device.playlist_manager.playlists[0].current_plugin_index == 0


def test_manual_display_precedes_scheduler_and_holds_until_normal_cycle(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    value.manual_update_request = ManualRefresh("fake", {"key": "three"})
    action = value._run_cycle(NOW)
    assert isinstance(action, ManualRefresh) and plugin.generated == ["three"]
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=1)) is None
    after_hold = value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW + timedelta(hours=1))
    assert isinstance(after_hold, DynamicPlaylistRefresh)


def test_scheduler_uses_explicit_evaluation_interval_only_when_enabled(tmp_path, monkeypatch):
    enabled, _device, _plugin = task(tmp_path, monkeypatch, scheduler_config(time_rule()))
    disabled, _device2, _plugin2 = task(tmp_path, monkeypatch, scheduler_config(time_rule(), enabled=False))
    assert enabled._get_loop_sleep_time() == 60
    assert disabled._get_loop_sleep_time() == 3600


def test_restart_uses_persisted_normal_clock_not_dynamic_refresh_time(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    value._run_cycle(NOW)
    persisted = device.playlist_manager.normal_refresh_time
    assert persisted == (NOW - timedelta(minutes=30)).isoformat()
    device.refresh_info = RefreshInfo(
        "Dynamic Scheduler", "fake", NOW.isoformat(), "dynamic", "Default", "Two"
    )
    restarted = RefreshTask(device, FakeDisplayManager())
    assert restarted.normal_refresh_time.isoformat() == persisted
    assert restarted.dynamic_scheduler.state.active_rule_id is None


def test_restart_reselects_live_rule_and_drops_stale_override(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    assert value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW).plugin_instance.name == "Two"
    restarted = RefreshTask(device, FakeDisplayManager())
    restarted.state_provider.plugin_loader = lambda _config: plugin
    assert restarted._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW).plugin_instance.name == "Two"
    plugin.states["two"] = False
    second_restart = RefreshTask(device, FakeDisplayManager())
    second_restart.state_provider.plugin_loader = lambda _config: plugin
    action = second_restart._determine_refresh_action(
        device.playlist_manager, device.refresh_info, NOW
    )
    assert action is None


def test_disabling_scheduler_during_override_returns_to_playlist(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW)
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
    device.config["dynamic_scheduler"]["enabled"] = False
    value.signal_config_change()
    returned = value._determine_refresh_action(
        device.playlist_manager, device.refresh_info, NOW + timedelta(minutes=1)
    )
    assert isinstance(returned, PlaylistRefresh)
    assert returned.plugin_instance.name == "Two"
    assert device.playlist_manager.playlists[0].current_plugin_index == 1


def test_removing_active_rule_returns_safely_before_re_evaluation(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    assert isinstance(
        value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW),
        DynamicPlaylistRefresh,
    )
    device.config["dynamic_scheduler"] = scheduler_config(time_rule())
    value.signal_config_change()
    returned = value._determine_refresh_action(
        device.playlist_manager, device.refresh_info, NOW + timedelta(seconds=1)
    )
    assert isinstance(returned, PlaylistRefresh)
    assert value.override_started_at is None


def test_unrelated_config_signal_does_not_reset_active_scheduler_lifecycle(tmp_path, monkeypatch):
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(state_rule()))
    plugin.states["two"] = True
    value._determine_refresh_action(device.playlist_manager, device.refresh_info, NOW)
    scheduler = value.dynamic_scheduler
    activated = scheduler.state.activations["live"].activated_at
    value.signal_config_change()
    assert value.dynamic_scheduler is scheduler
    assert value.dynamic_scheduler.state.activations["live"].activated_at == activated


def test_occurrence_uses_shared_render_path_and_restores_without_playlist_advance(tmp_path, monkeypatch):
    timed = {"id":"hourly", "target":{"instance_id":"two-id"}, "priority":20,
        "schedule":{"type":"minute_of_hour","minutes":[5]},
        "duration":{"mode":"fixed","seconds":120}, "return_behavior":"resume_previous"}
    value, device, plugin = task(tmp_path, monkeypatch, scheduler_config(timed))
    original_time = value.normal_refresh_time
    assert value._run_cycle(NOW + timedelta(minutes=4,seconds=50)) is None
    selected = value._run_cycle(NOW + timedelta(minutes=5,seconds=40))
    assert isinstance(selected, DynamicPlaylistRefresh)
    assert plugin.generated == ["two"]
    assert value.normal_refresh_time == original_time
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
    returned = value._run_cycle(NOW + timedelta(minutes=7,seconds=40))
    assert isinstance(returned, SchedulerReturnRefresh)
    assert returned.plugin_instance.name == "One"
    assert device.playlist_manager.playlists[0].current_plugin_index == 0
