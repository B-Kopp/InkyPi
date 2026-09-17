"""Typed configuration and runtime models for the dynamic scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


RETURN_BEHAVIORS = {
    "resume_previous",
    "resume_playlist",
    "stay_until_next_cycle",
}


@dataclass(frozen=True)
class PluginReference:
    """A stable-first reference to a configured plugin instance."""

    instance_id: str | None = None
    plugin_id: str | None = None
    plugin_instance: str | None = None
    playlist: str | None = None

    @property
    def cache_key(self) -> str:
        if self.instance_id:
            return f"instance_id:{self.instance_id}"
        return "legacy:" + "|".join(
            value or "" for value in (self.playlist, self.plugin_id, self.plugin_instance)
        )


@dataclass(frozen=True)
class DurationConfig:
    mode: str = "while_true"
    seconds: int | None = None
    min_seconds: int = 0
    max_seconds: int | None = None


@dataclass(frozen=True)
class SchedulerRule:
    id: str
    enabled: bool
    priority: int
    target: PluginReference
    conditions: dict[str, Any]
    duration: DurationConfig
    cooldown_seconds: int = 0
    return_behavior: str = "resume_previous"
    order: int = 0
    name: str | None = None
    schedule: dict[str, Any] | None = None


@dataclass(frozen=True)
class SchedulerDefaults:
    fallback: str = "playlist"
    return_behavior: str = "resume_previous"
    minimum_display_seconds: int = 60


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    version: int = 1
    evaluation_interval_seconds: int = 60
    defaults: SchedulerDefaults = field(default_factory=SchedulerDefaults)
    rules: tuple[SchedulerRule, ...] = ()


@dataclass
class RuleActivation:
    rule_id: str
    activated_at: datetime
    expires_at: datetime | None = None


@dataclass
class SchedulerState:
    active_rule_id: str | None = None
    activations: dict[str, RuleActivation] = field(default_factory=dict)
    cooldown_until: dict[str, datetime] = field(default_factory=dict)
    blocked_until_false: set[str] = field(default_factory=set)
    last_evaluated_at: datetime | None = None
    occurrence_keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulingDecision:
    target: PluginReference
    source: str
    rule_id: str
    priority: int
    activated_at: datetime
    expires_at: datetime | None
    return_behavior: str
    reason: str
    changed: bool = False


@dataclass(frozen=True)
class SchedulingOutcome:
    decision: SchedulingDecision | None = None
    ended_rule_id: str | None = None
    return_behavior: str | None = None
