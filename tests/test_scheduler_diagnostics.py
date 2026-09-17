from copy import deepcopy
from datetime import datetime, timezone

from src.scheduler.config import load_scheduler_config
from src.scheduler.diagnostics import SchedulerDiagnostics
from src.scheduler.models import PluginReference


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class Provider:
    def __init__(self, values):
        self.values = values
        self.begins = 0

    def begin_evaluation(self, current_dt):
        self.begins += 1

    def get_state(self, reference):
        return True, self.values[reference.instance_id]

    def resolve(self, reference):
        return type("Resolved", (), {
            "instance": type("Instance", (), {"name": reference.instance_id, "plugin_id": "fake"})(),
            "playlist": type("Playlist", (), {"name": "Default"})(),
        })()


def raw_rule(rule_id="live", priority=80):
    return {
        "id": rule_id,
        "name": "Live game",
        "enabled": True,
        "priority": priority,
        "target": {"instance_id": "source"},
        "conditions": {"all": [
            {"source_instance_id": "source", "path": "game.live", "operator": "eq", "value": True},
            {"source_instance_id": "source", "path": "game.inning", "operator": "gte", "value": 7},
        ]},
        "duration": {"mode": "while_true", "min_seconds": 0},
    }


def config(*rules):
    return load_scheduler_config({
        "enabled": True, "version": 1, "rules": list(rules),
        "defaults": {"minimum_display_seconds": 0},
    })


def test_rule_diagnostics_include_current_values_without_mutating_config():
    parsed = config(raw_rule())
    before = deepcopy(parsed)
    result = SchedulerDiagnostics(Provider({"source": {"game": {"live": True, "inning": 8}}})).test_rule(parsed.rules[0], NOW)
    assert result["matched"] is True
    assert [child["current_value"] for child in result["condition"]["children"]] == [True, 8]
    assert parsed == before


def test_rule_diagnostics_show_failing_leaf():
    parsed = config(raw_rule())
    result = SchedulerDiagnostics(Provider({"source": {"game": {"live": True, "inning": 5}}})).test_rule(parsed.rules[0], NOW)
    assert result["matched"] is False
    assert result["condition"]["children"][1]["matched"] is False


def test_all_reports_matching_rules_and_deterministic_winner():
    parsed = config(raw_rule("low", 60), raw_rule("high", 80))
    result = SchedulerDiagnostics(Provider({"source": {"game": {"live": True, "inning": 8}}})).test_all(parsed, NOW)
    assert {item["rule_id"] for item in result["rules"] if item["matched"]} == {"low", "high"}
    assert result["winner_rule_id"] == "high"
