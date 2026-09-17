import pytest

from src.scheduler.config import SchedulerConfigError, load_scheduler_config


def valid_rule(**changes):
    rule = {
        "id": "live",
        "enabled": True,
        "priority": 80,
        "target": {"instance_id": "score-id"},
        "conditions": {
            "source_instance_id": "score-id",
            "path": "game.status",
            "operator": "eq",
            "value": "live",
        },
        "duration": {"mode": "while_true", "min_seconds": 120, "max_seconds": 3600},
        "cooldown_seconds": 30,
        "return_behavior": "resume_previous",
    }
    rule.update(changes)
    return rule


def config(*rules, **changes):
    value = {
        "enabled": True,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {"fallback": "playlist", "return_behavior": "resume_previous", "minimum_display_seconds": 60},
        "rules": list(rules),
    }
    value.update(changes)
    return value


def test_missing_config_is_disabled_with_safe_defaults():
    parsed = load_scheduler_config(None)
    assert not parsed.enabled
    assert parsed.evaluation_interval_seconds == 60
    assert parsed.defaults.fallback == "playlist"


def test_valid_config_is_typed():
    parsed = load_scheduler_config(config(valid_rule()))
    assert parsed.enabled and parsed.rules[0].target.instance_id == "score-id"
    assert parsed.rules[0].duration.min_seconds == 120


@pytest.mark.parametrize(
    "rule,error",
    [
        (valid_rule(id=""), ".id is required"),
        (valid_rule(priority="high"), ".priority must be an integer"),
        (valid_rule(target={}), "requires instance_id or plugin_instance"),
        (valid_rule(conditions={"path": "x", "source": "x", "operator": "wat"}), "operator is unknown"),
        (valid_rule(conditions={"all": []}), "must be a non-empty list"),
        (valid_rule(duration={"mode": "fixed"}), ".seconds is required"),
        (valid_rule(return_behavior="rewind"), ".return_behavior is invalid"),
    ],
)
def test_invalid_rule_has_actionable_error(rule, error):
    with pytest.raises(SchedulerConfigError, match=error):
        load_scheduler_config(config(rule))


def test_duplicate_ids_are_rejected():
    with pytest.raises(SchedulerConfigError, match="duplicates"):
        load_scheduler_config(config(valid_rule(), valid_rule()))


def test_target_can_be_checked_against_available_instances():
    with pytest.raises(SchedulerConfigError, match="does not identify"):
        load_scheduler_config(config(valid_rule()), target_exists=lambda target: False)


@pytest.mark.parametrize("interval", [0, -1, "60", True])
def test_evaluation_interval_must_be_positive_integer(interval):
    with pytest.raises(SchedulerConfigError, match="evaluation_interval_seconds"):
        load_scheduler_config(config(valid_rule(), evaluation_interval_seconds=interval))

