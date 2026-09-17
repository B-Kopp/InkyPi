"""Read-only rule diagnostics built on the generic scheduler contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .condition_evaluator import ConditionEvaluator, MISSING, lookup_path, source_reference
from .models import SchedulerConfig, SchedulerRule
from .temporal import latest_occurrence, next_occurrence, occurrence_key, schedule_summary


class SchedulerDiagnostics:
    def __init__(self, state_provider, condition_evaluator=None, temporal_state=None):
        self.state_provider = state_provider
        self.conditions = condition_evaluator or ConditionEvaluator()
        self.temporal_state = temporal_state

    def schedule_detail(self, rule, current_dt):
        if not rule.schedule:
            return None
        previous = self.temporal_state.last_evaluated_at if self.temporal_state else None
        occurrence = latest_occurrence(rule.schedule, current_dt, previous) or latest_occurrence(rule.schedule, current_dt)
        consumed = bool(occurrence and self.temporal_state and
                        self.temporal_state.occurrence_keys.get(rule.id) == occurrence_key(occurrence))
        upcoming = next_occurrence(rule.schedule, current_dt)
        return {
            "summary": schedule_summary(rule.schedule),
            "current_time": current_dt.isoformat(),
            "scheduled_at": occurrence.isoformat() if occurrence else None,
            "occurrence_key": occurrence_key(occurrence) if occurrence else None,
            "consumed": consumed,
            "due": bool(occurrence and not consumed),
            "next_occurrence": upcoming.isoformat() if upcoming else None,
        }

    def test_rule(self, rule: SchedulerRule, current_dt: datetime) -> dict[str, Any]:
        self.state_provider.begin_evaluation(current_dt)
        condition = self._condition_detail(rule.conditions, current_dt)
        schedule = self.schedule_detail(rule, current_dt)
        resolved = self.state_provider.resolve(rule.target)
        return {
            "rule_id": rule.id,
            "name": rule.name or rule.id,
            "enabled": rule.enabled,
            "priority": rule.priority,
            "matched": bool(rule.enabled and condition["matched"] and (not schedule or schedule["due"])),
            "condition": condition,
            "schedule": schedule,
            "target": {
                "instance_id": rule.target.instance_id,
                "name": resolved.instance.name if resolved else rule.target.plugin_instance,
                "plugin_id": resolved.instance.plugin_id if resolved else rule.target.plugin_id,
                "playlist": resolved.playlist.name if resolved else rule.target.playlist,
            },
        }

    def test_all(self, config: SchedulerConfig, current_dt: datetime) -> dict[str, Any]:
        # One evaluation cache is shared across all rules.
        self.state_provider.begin_evaluation(current_dt)
        results = []
        for rule in config.rules:
            condition = self._condition_detail(rule.conditions, current_dt)
            schedule = self.schedule_detail(rule, current_dt)
            resolved = self.state_provider.resolve(rule.target)
            results.append({
                "rule_id": rule.id,
                "name": rule.name or rule.id,
                "enabled": rule.enabled,
                "priority": rule.priority,
                "matched": bool(rule.enabled and condition["matched"] and (not schedule or schedule["due"])),
                "condition": condition,
                "schedule": schedule,
                "target": {
                    "instance_id": rule.target.instance_id,
                    "name": resolved.instance.name if resolved else rule.target.plugin_instance,
                    "plugin_id": resolved.instance.plugin_id if resolved else rule.target.plugin_id,
                    "playlist": resolved.playlist.name if resolved else rule.target.playlist,
                },
                "order": rule.order,
            })
        matching = [result for result in results if result["matched"]]
        winner = min(matching, key=lambda item: (-item["priority"], item["order"], item["rule_id"])) if matching else None
        return {"rules": results, "winner_rule_id": winner["rule_id"] if winner else None}

    def _condition_detail(self, node: dict[str, Any], current_dt: datetime) -> dict[str, Any]:
        if "all" in node or "any" in node:
            kind = "all" if "all" in node else "any"
            children = [self._condition_detail(child, current_dt) for child in node[kind]]
            result = self.conditions.evaluate(node, current_dt, self.state_provider.get_state)
            return {
                "kind": kind,
                "matched": result.matched,
                "available": result.available,
                "children": children,
            }
        if "not" in node:
            child = self._condition_detail(node["not"], current_dt)
            return {
                "kind": "not",
                "matched": not child["matched"] if child["available"] else False,
                "available": child["available"],
                "children": [child],
            }
        if "time" in node:
            result = self.conditions.evaluate(node, current_dt, self.state_provider.get_state)
            return {
                "kind": "time",
                "matched": result.matched,
                "available": result.available,
                "time": node["time"],
                "current_value": current_dt.isoformat(),
            }
        reference = source_reference(node)
        available, state = self.state_provider.get_state(reference)
        actual = lookup_path(state, node["path"]) if available else MISSING
        result = self.conditions.evaluate(node, current_dt, self.state_provider.get_state)
        return {
            "kind": "state",
            "matched": result.matched,
            "available": result.available,
            "source_instance_id": reference.instance_id,
            "source": reference.plugin_instance,
            "path": node["path"],
            "operator": node["operator"],
            "expected_value": node.get("value"),
            "current_value": None if actual is MISSING else actual,
        }
