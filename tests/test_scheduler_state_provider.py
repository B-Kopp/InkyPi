from datetime import datetime, timezone
from types import SimpleNamespace

from src.model import Playlist, PlaylistManager, PluginInstance
from src.plugins.base_plugin.base_plugin import BasePlugin
from src.scheduler.models import PluginReference
from src.scheduler.state_provider import StateProvider


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


class DeviceConfig:
    def get_plugin(self, plugin_id):
        return {"id": plugin_id}


class StatePlugin:
    def __init__(self, state=None, error=None):
        self.state = state or {}
        self.error = error
        self.calls = []

    def get_scheduler_state(self, settings, device_config, current_dt):
        self.calls.append((settings, device_config, current_dt))
        if self.error:
            raise self.error
        return self.state

    def get_scheduler_state_schema(self):
        return {"ready": {"label": "Ready", "type": "boolean"}}


def manager_with(*instances):
    return PlaylistManager([Playlist("Default", "00:00", "24:00", [value.to_dict() for value in instances])])


def test_base_plugin_scheduler_state_is_empty():
    plugin = object.__new__(BasePlugin)
    assert plugin.get_scheduler_state({}, DeviceConfig(), NOW) == {}
    assert plugin.get_scheduler_state_schema() == {}


def test_plugin_instance_id_round_trips_and_legacy_gets_id():
    instance = PluginInstance("clock", "Morning", {}, {"interval": 60})
    restored = PluginInstance.from_dict(instance.to_dict())
    legacy = PluginInstance.from_dict({
        "plugin_id": "clock", "name": "Legacy", "plugin_settings": {}, "refresh": {"interval": 60}
    })
    assert restored.instance_id == instance.instance_id
    assert legacy.instance_id
    assert PluginInstance.from_dict(legacy.to_dict()).instance_id == legacy.instance_id


def test_state_retrieval_uses_instance_settings_and_injected_time():
    original = PluginInstance("fake", "Score", {"team": "ATL"}, {"interval": 60}, instance_id="stable")
    plugin = StatePlugin({"game": {"status": "live"}})
    provider = StateProvider(manager_with(original), DeviceConfig(), lambda _config: plugin)
    provider.begin_evaluation(NOW)
    available, state = provider.get_state(PluginReference(instance_id="stable"))
    assert available and state["game"]["status"] == "live"
    assert plugin.calls == [({"team": "ATL"}, provider.device_config, NOW)]


def test_state_retrieval_is_deduplicated_within_evaluation():
    instance = PluginInstance("fake", "Score", {}, {}, instance_id="stable")
    plugin = StatePlugin({"ready": True})
    provider = StateProvider(manager_with(instance), DeviceConfig(), lambda _config: plugin)
    provider.begin_evaluation(NOW)
    reference = PluginReference(instance_id="stable")
    assert provider.get_state(reference) == provider.get_state(reference)
    assert len(plugin.calls) == 1
    provider.begin_evaluation(NOW)
    provider.get_state(reference)
    assert len(plugin.calls) == 2


def test_provider_exception_is_isolated():
    instance = PluginInstance("fake", "Broken", {}, {}, instance_id="broken")
    provider = StateProvider(
        manager_with(instance), DeviceConfig(), lambda _config: StatePlugin(error=RuntimeError("offline"))
    )
    provider.begin_evaluation(NOW)
    assert provider.get_state(PluginReference(instance_id="broken")) == (False, {})


def test_only_requested_provider_is_queried():
    first = PluginInstance("fake", "First", {}, {}, instance_id="first")
    second = PluginInstance("fake", "Second", {}, {}, instance_id="second")
    plugins = {"first": StatePlugin({"ready": True}), "second": StatePlugin({"ready": True})}
    manager = manager_with(first, second)
    provider = StateProvider(
        manager,
        DeviceConfig(),
        lambda config: plugins[manager.find_plugin(config["id"], "First").instance_id]
        if config["id"] == "fake" else None,
    )
    # Use a loader whose returned plugin can be observed without requesting the
    # second source. Both instances intentionally share the same plugin type,
    # matching the production singleton registry.
    singleton = StatePlugin({"ready": True})
    provider.plugin_loader = lambda _config: singleton
    provider.begin_evaluation(NOW)
    provider.get_state(PluginReference(instance_id="first"))
    assert len(singleton.calls) == 1


def test_legacy_name_resolution_rejects_ambiguity():
    one = PluginInstance("one", "Morning", {}, {}, instance_id="one")
    two = PluginInstance("two", "Morning", {}, {}, instance_id="two")
    provider = StateProvider(manager_with(one, two), DeviceConfig(), lambda _config: StatePlugin())
    assert provider.resolve(PluginReference(plugin_instance="Morning")) is None
    resolved = provider.resolve(PluginReference(plugin_id="one", plugin_instance="Morning"))
    assert resolved.instance.instance_id == "one"


def test_schema_retrieval_is_cached_and_does_not_fetch_state():
    instance = PluginInstance("fake", "Score", {}, {}, instance_id="stable")
    plugin = StatePlugin({"ready": True})
    provider = StateProvider(manager_with(instance), DeviceConfig(), lambda _config: plugin)
    assert provider.get_schema(PluginReference(instance_id="stable")) == (
        True, {"ready": {"label": "Ready", "type": "boolean"}}
    )
    assert provider.get_schema(PluginReference(instance_id="stable"))[0]
    assert plugin.calls == []
