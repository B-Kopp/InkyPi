"""Backward-compatible validation for dynamic scheduler versions 1 and 2."""

from __future__ import annotations

from typing import Any, Callable
from datetime import date, datetime

from .condition_evaluator import CONDITION_OPERATORS, source_reference
from .temporal import SCHEDULE_TYPES
from .models import (
    RETURN_BEHAVIORS,
    DurationConfig,
    PluginReference,
    SchedulerConfig,
    SchedulerDefaults,
    SchedulerRule,
)


class SchedulerConfigError(ValueError):
    """Raised when scheduler configuration cannot be used safely."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _non_negative_int(value: Any, path: str, errors: list[str], *, minimum=0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{path} must be an integer >= {minimum}")
        return None
    return value


def _plugin_reference(raw: Any, path: str, errors: list[str]) -> PluginReference:
    if not isinstance(raw, dict):
        errors.append(f"{path} must be an object")
        return PluginReference()
    reference = PluginReference(
        instance_id=_text(raw.get("instance_id")),
        plugin_id=_text(raw.get("plugin_id")),
        plugin_instance=_text(raw.get("plugin_instance")),
        playlist=_text(raw.get("playlist")),
    )
    if not reference.instance_id and not reference.plugin_instance:
        errors.append(f"{path} requires instance_id or plugin_instance")
    return reference


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_time(raw: Any, path: str, errors: list[str]) -> None:
    if not isinstance(raw, dict):
        errors.append(f"{path} must be an object")
        return
    days = raw.get("days")
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if days is not None:
        if not isinstance(days, list) or not days or any(
            not isinstance(day, str) or day.lower() not in valid_days for day in days
        ):
            errors.append(f"{path}.days must be a non-empty list of mon..sun values")
    for name in ("start", "end"):
        value = raw.get(name)
        if value is not None:
            try:
                hour, minute = value.split(":")
                valid = len(value) == 5 and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
            except (AttributeError, ValueError):
                valid = False
            if not valid:
                errors.append(f"{path}.{name} must use HH:MM")
    if (raw.get("start") is None) != (raw.get("end") is None):
        errors.append(f"{path} requires both start and end")
    for key, minimum, maximum in (("hours", 0, 23), ("months", 1, 12), ("days_of_month", 1, 31)):
        if key in raw:
            _integer_list(raw[key], f"{path}.{key}", errors, minimum, maximum)
    for key in ("date_start", "date_end"):
        if key in raw:
            try:
                value = raw[key]
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append(f"{path}.{key} must use YYYY-MM-DD")
    if isinstance(raw.get("date_start"), str) and isinstance(raw.get("date_end"), str) and raw["date_start"] > raw["date_end"]:
        errors.append(f"{path}.date_start cannot exceed date_end")


def _integer_list(raw, path, errors, minimum, maximum):
    if not isinstance(raw, list) or not raw or any(
        isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
        for value in raw
    ):
        errors.append(f"{path} must be a non-empty list of integers {minimum}..{maximum}")


def _validate_schedule(raw, path, errors):
    if not isinstance(raw, dict):
        errors.append(f"{path} must be an object")
        return
    _validate_time(raw, path, errors)
    kind = raw.get("type")
    if kind not in SCHEDULE_TYPES:
        errors.append(f"{path}.type must be minute_of_hour, interval, daily, or once")
    elif kind == "minute_of_hour":
        _integer_list(raw.get("minutes"), f"{path}.minutes", errors, 0, 59)
    elif kind == "interval":
        value = raw.get("every_minutes")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
            errors.append(f"{path}.every_minutes must be an integer 1..1440")
    elif kind == "daily":
        values = raw.get("times")
        if not isinstance(values, list) or not values:
            errors.append(f"{path}.times must be a non-empty list of HH:MM times")
        else:
            for index, value in enumerate(values):
                _validate_time({"start": value, "end": value}, f"{path}.times[{index}]", errors)
                if value is None:
                    errors.append(f"{path}.times[{index}] must use HH:MM")
    else:
        try:
            value = datetime.fromisoformat(raw.get("at", ""))
            if value.second or value.microsecond or "T" not in raw["at"]:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            errors.append(f"{path}.at must use YYYY-MM-DDTHH:MM (optional UTC offset)")


def _validate_conditions(
    raw: Any,
    path: str,
    errors: list[str],
    source_exists: Callable[[PluginReference], bool] | None = None,
) -> None:
    if not isinstance(raw, dict):
        errors.append(f"{path} must be an object")
        return
    node_keys = [key for key in ("all", "any", "not", "time", "path") if key in raw]
    if len(node_keys) != 1:
        errors.append(f"{path} must contain exactly one of all, any, not, time, or path")
        return
    kind = node_keys[0]
    if kind in {"all", "any"}:
        children = raw[kind]
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{kind} must be a non-empty list")
            return
        for index, child in enumerate(children):
            _validate_conditions(child, f"{path}.{kind}[{index}]", errors, source_exists)
    elif kind == "not":
        _validate_conditions(raw[kind], f"{path}.not", errors, source_exists)
    elif kind == "time":
        _validate_time(raw[kind], f"{path}.time", errors)
    else:
        if not _text(raw.get("path")):
            errors.append(f"{path}.path must be a non-empty string")
        operator = raw.get("operator")
        if operator not in CONDITION_OPERATORS:
            errors.append(f"{path}.operator is unknown: {operator!r}")
        if operator not in {"exists", "truthy", "falsy"} and "value" not in raw:
            errors.append(f"{path}.value is required for operator {operator!r}")
        source = raw.get("source")
        source_id = raw.get("source_instance_id")
        if isinstance(source, dict):
            _plugin_reference(source, f"{path}.source", errors)
        elif not _text(source) and not _text(source_id):
            errors.append(f"{path} requires source or source_instance_id")
        if source_exists and (isinstance(source, dict) or _text(source) or _text(source_id)) and not source_exists(source_reference(raw)):
            errors.append(f"{path} source does not identify an available plugin instance")


def _duration(raw: Any, path: str, errors: list[str], default_minimum: int) -> DurationConfig:
    if raw is None:
        return DurationConfig(min_seconds=default_minimum)
    if not isinstance(raw, dict):
        errors.append(f"{path} must be an object")
        return DurationConfig(min_seconds=default_minimum)
    mode = raw.get("mode", "while_true")
    if mode not in {"while_true", "fixed"}:
        errors.append(f"{path}.mode must be while_true or fixed")
    minimum = _non_negative_int(raw.get("min_seconds", default_minimum), f"{path}.min_seconds", errors)
    maximum_raw = raw.get("max_seconds")
    maximum = None if maximum_raw is None else _non_negative_int(maximum_raw, f"{path}.max_seconds", errors, minimum=1)
    seconds_raw = raw.get("seconds")
    seconds = None if seconds_raw is None else _non_negative_int(seconds_raw, f"{path}.seconds", errors, minimum=1)
    if mode == "fixed" and seconds is None:
        errors.append(f"{path}.seconds is required for fixed duration")
    if minimum is not None and maximum is not None and minimum > maximum:
        errors.append(f"{path}.min_seconds cannot exceed max_seconds")
    return DurationConfig(mode, seconds, minimum or 0, maximum)


def load_scheduler_config(
    raw: Any,
    target_exists: Callable[[PluginReference], bool] | None = None,
) -> SchedulerConfig:
    """Parse scheduler config or raise one error containing all useful diagnostics."""

    if raw is None:
        return SchedulerConfig()
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise SchedulerConfigError(["dynamic_scheduler must be an object"])
    version = raw.get("version", 1)
    if isinstance(version, bool) or version not in (1, 2):
        errors.append("dynamic_scheduler.version must be 1 or 2")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        errors.append("dynamic_scheduler.enabled must be a boolean")
        enabled = False
    interval = _non_negative_int(
        raw.get("evaluation_interval_seconds", 60),
        "dynamic_scheduler.evaluation_interval_seconds",
        errors,
        minimum=1,
    )
    defaults_raw = raw.get("defaults", {})
    if not isinstance(defaults_raw, dict):
        errors.append("dynamic_scheduler.defaults must be an object")
        defaults_raw = {}
    fallback = defaults_raw.get("fallback", "playlist")
    if fallback != "playlist":
        errors.append("dynamic_scheduler.defaults.fallback must be playlist")
    return_default = defaults_raw.get("return_behavior", "resume_previous")
    if return_default not in RETURN_BEHAVIORS:
        errors.append("dynamic_scheduler.defaults.return_behavior is invalid")
        return_default = "resume_previous"
    minimum = _non_negative_int(
        defaults_raw.get("minimum_display_seconds", 60),
        "dynamic_scheduler.defaults.minimum_display_seconds",
        errors,
    )
    defaults = SchedulerDefaults("playlist", return_default, minimum or 0)

    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        errors.append("dynamic_scheduler.rules must be a list")
        rules_raw = []
    rules: list[SchedulerRule] = []
    identifiers: set[str] = set()
    for index, item in enumerate(rules_raw):
        path = f"dynamic_scheduler.rules[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        rule_id = _text(item.get("id"))
        if not rule_id:
            errors.append(f"{path}.id is required")
            rule_id = f"invalid-{index}"
        elif rule_id in identifiers:
            errors.append(f"{path}.id duplicates {rule_id!r}")
        identifiers.add(rule_id)
        rule_enabled = item.get("enabled", True)
        if not isinstance(rule_enabled, bool):
            errors.append(f"{path}.enabled must be a boolean")
            rule_enabled = False
        priority = item.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            errors.append(f"{path}.priority must be an integer")
            priority = 0
        target = _plugin_reference(item.get("target"), f"{path}.target", errors)
        if target_exists and (target.instance_id or target.plugin_instance) and not target_exists(target):
            errors.append(f"{path}.target does not identify an available plugin instance")
        schedule = item.get("schedule")
        if "schedule" in item:
            _validate_schedule(schedule, f"{path}.schedule", errors)
        conditions = item.get("conditions", {"time": {}} if schedule else None)
        _validate_conditions(conditions, f"{path}.conditions", errors, target_exists)
        duration = _duration(
            item.get("duration", {"mode": "fixed", "seconds": 120} if schedule else None),
            f"{path}.duration", errors, defaults.minimum_display_seconds,
        )
        cooldown = _non_negative_int(item.get("cooldown_seconds", 0), f"{path}.cooldown_seconds", errors)
        return_behavior = item.get("return_behavior", defaults.return_behavior)
        if return_behavior not in RETURN_BEHAVIORS:
            errors.append(f"{path}.return_behavior is invalid")
            return_behavior = defaults.return_behavior
        rules.append(SchedulerRule(
            rule_id, rule_enabled, priority, target, conditions, duration,
            cooldown or 0, return_behavior, index, _text(item.get("name")) or rule_id,
            schedule,
        ))
    if errors:
        raise SchedulerConfigError(errors)
    return SchedulerConfig(bool(enabled), version, interval or 60, defaults, tuple(rules))
