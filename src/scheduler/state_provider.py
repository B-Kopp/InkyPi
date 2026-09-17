"""Resolve configured instances and retrieve optional normalized plugin state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .models import PluginReference

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPluginInstance:
    playlist: Any
    instance: Any


class StateProvider:
    """Lazy state access with one provider call per source per evaluation."""

    def __init__(self, playlist_manager, device_config, plugin_loader: Callable[[dict], Any]):
        self.playlist_manager = playlist_manager
        self.device_config = device_config
        self.plugin_loader = plugin_loader
        self._cache: dict[str, tuple[bool, dict[str, Any]]] = {}
        self._schema_cache: dict[str, tuple[bool, dict[str, Any]]] = {}
        self._current_dt: datetime | None = None

    def begin_evaluation(self, current_dt: datetime) -> None:
        self._current_dt = current_dt
        self._cache = {}

    def resolve(self, reference: PluginReference) -> ResolvedPluginInstance | None:
        matches: list[ResolvedPluginInstance] = []
        for playlist in self.playlist_manager.playlists:
            if reference.playlist and playlist.name != reference.playlist:
                continue
            for instance in playlist.plugins:
                if reference.instance_id:
                    if instance.instance_id == reference.instance_id:
                        return ResolvedPluginInstance(playlist, instance)
                    continue
                if reference.plugin_id and instance.plugin_id != reference.plugin_id:
                    continue
                if reference.plugin_instance and instance.name != reference.plugin_instance:
                    continue
                matches.append(ResolvedPluginInstance(playlist, instance))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("Dynamic scheduler plugin reference is ambiguous: %s", reference.cache_key)
        else:
            logger.warning("Dynamic scheduler plugin instance was not found: %s", reference.cache_key)
        return None

    def target_exists(self, reference: PluginReference) -> bool:
        return self.resolve(reference) is not None

    def get_state(self, reference: PluginReference) -> tuple[bool, dict[str, Any]]:
        key = reference.cache_key
        if key in self._cache:
            return self._cache[key]
        resolved = self.resolve(reference)
        if resolved is None:
            result = (False, {})
            self._cache[key] = result
            return result
        instance = resolved.instance
        plugin_config = self.device_config.get_plugin(instance.plugin_id)
        if plugin_config is None:
            logger.warning("Scheduler state plugin config not found: %s", instance.plugin_id)
            result = (False, {})
            self._cache[key] = result
            return result
        try:
            plugin = self.plugin_loader(plugin_config)
            state = plugin.get_scheduler_state(
                instance.settings, self.device_config, self._current_dt
            )
            if not isinstance(state, dict):
                raise TypeError("get_scheduler_state must return a mapping")
            result = (True, state)
        except Exception:
            logger.exception("Scheduler state provider failed: %s", instance.name)
            result = (False, {})
        self._cache[key] = result
        return result

    def get_schema(self, reference: PluginReference) -> tuple[bool, dict[str, Any]]:
        """Return optional field metadata without retrieving live plugin state."""
        key = reference.cache_key
        if key in self._schema_cache:
            return self._schema_cache[key]
        resolved = self.resolve(reference)
        if resolved is None:
            result = (False, {})
        else:
            plugin_config = self.device_config.get_plugin(resolved.instance.plugin_id)
            try:
                if plugin_config is None:
                    raise LookupError("plugin configuration is unavailable")
                schema = self.plugin_loader(plugin_config).get_scheduler_state_schema()
                if not isinstance(schema, dict):
                    raise TypeError("get_scheduler_state_schema must return a mapping")
                result = (True, schema)
            except Exception:
                logger.exception("Scheduler state schema provider failed: %s", resolved.instance.name)
                result = (False, {})
        self._schema_cache[key] = result
        return result
