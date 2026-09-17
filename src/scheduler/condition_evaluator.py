"""Generic state-path and scheduler-native time condition evaluation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .models import PluginReference
from .temporal import restrictions_match

logger = logging.getLogger(__name__)

CONDITION_OPERATORS = {
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in",
    "exists", "truthy", "falsy", "contains",
}
MISSING = object()


@dataclass(frozen=True)
class ConditionResult:
    matched: bool
    available: bool = True


StateLookup = Callable[[PluginReference], tuple[bool, dict[str, Any]]]


def source_reference(condition: dict[str, Any]) -> PluginReference:
    source = condition.get("source")
    if isinstance(source, dict):
        return PluginReference(
            instance_id=source.get("instance_id"),
            plugin_id=source.get("plugin_id"),
            plugin_instance=source.get("plugin_instance"),
            playlist=source.get("playlist"),
        )
    return PluginReference(
        instance_id=condition.get("source_instance_id"),
        plugin_instance=source if isinstance(source, str) else None,
    )


def lookup_path(state: Any, path: str) -> Any:
    value = state
    for component in path.split("."):
        if not component or not isinstance(value, dict) or component not in value:
            return MISSING
        value = value[component]
    return value


class ConditionEvaluator:
    def evaluate(self, condition: dict[str, Any], current_dt: datetime, state_lookup: StateLookup) -> ConditionResult:
        try:
            if "all" in condition:
                results = [self.evaluate(child, current_dt, state_lookup) for child in condition["all"]]
                if any(result.available and not result.matched for result in results):
                    return ConditionResult(False, True)
                if all(result.available and result.matched for result in results):
                    return ConditionResult(True, True)
                return ConditionResult(False, False)
            if "any" in condition:
                results = [self.evaluate(child, current_dt, state_lookup) for child in condition["any"]]
                if any(result.available and result.matched for result in results):
                    return ConditionResult(True, True)
                if all(result.available and not result.matched for result in results):
                    return ConditionResult(False, True)
                return ConditionResult(False, False)
            if "not" in condition:
                result = self.evaluate(condition["not"], current_dt, state_lookup)
                return ConditionResult(not result.matched if result.available else False, result.available)
            if "time" in condition:
                return ConditionResult(self._time_matches(condition["time"], current_dt))
            return self._state_matches(condition, state_lookup)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning("Dynamic scheduler condition could not be evaluated: %s", exc)
            return ConditionResult(False, False)

    def _state_matches(self, condition: dict[str, Any], state_lookup: StateLookup) -> ConditionResult:
        available, state = state_lookup(source_reference(condition))
        if not available:
            return ConditionResult(False, False)
        actual = lookup_path(state, condition["path"])
        operator = condition["operator"]
        if operator == "exists":
            return ConditionResult(actual is not MISSING)
        if actual is MISSING:
            return ConditionResult(False, False)
        expected = condition.get("value")
        try:
            if operator == "eq":
                matched = actual == expected
            elif operator == "ne":
                matched = actual != expected
            elif operator == "gt":
                matched = actual > expected
            elif operator == "gte":
                matched = actual >= expected
            elif operator == "lt":
                matched = actual < expected
            elif operator == "lte":
                matched = actual <= expected
            elif operator == "in":
                matched = actual in expected
            elif operator == "not_in":
                matched = actual not in expected
            elif operator == "truthy":
                matched = bool(actual)
            elif operator == "falsy":
                matched = not bool(actual)
            elif operator == "contains":
                matched = expected in actual
            else:
                return ConditionResult(False, False)
        except (TypeError, ValueError):
            logger.debug("Scheduler condition type mismatch for path %s", condition["path"])
            return ConditionResult(False, False)
        return ConditionResult(bool(matched))

    @staticmethod
    def _time_matches(config: dict[str, Any], current_dt: datetime) -> bool:
        return restrictions_match(config, current_dt)
