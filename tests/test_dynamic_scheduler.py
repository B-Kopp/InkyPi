from datetime import datetime, timedelta, timezone

from src.scheduler.config import load_scheduler_config
from src.scheduler.dynamic_scheduler import DynamicScheduler


START = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def rule(rule_id, priority, source=None, *, mode="while_true", minimum=0, maximum=None, seconds=None, cooldown=0, return_behavior="resume_previous"):
    duration = {"mode": mode, "min_seconds": minimum}
    if maximum is not None:
        duration["max_seconds"] = maximum
    if seconds is not None:
        duration["seconds"] = seconds
    return {
        "id": rule_id,
        "priority": priority,
        "target": {"instance_id": f"{rule_id}-target"},
        "conditions": {"source_instance_id": source or rule_id, "path": "active", "operator": "truthy"},
        "duration": duration,
        "cooldown_seconds": cooldown,
        "return_behavior": return_behavior,
    }


def scheduler(*rules):
    return DynamicScheduler(load_scheduler_config({
        "enabled": True,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {"minimum_display_seconds": 0},
        "rules": list(rules),
    }))


def lookup(values):
    return lambda ref: (True, {"active": values.get(ref.instance_id, False)})


def test_no_match_returns_no_decision():
    assert scheduler(rule("one", 1)).evaluate(START, lookup({})).decision is None


def test_highest_priority_wins_and_active_equal_priority_is_stable():
    value = scheduler(rule("first", 60), rule("second", 80), rule("third", 80))
    first = value.evaluate(START, lookup({"first": True, "second": True, "third": True}))
    assert first.decision.rule_id == "second"
    again = value.evaluate(START + timedelta(seconds=5), lookup({"second": True, "third": True}))
    assert again.decision.rule_id == "second" and not again.decision.changed


def test_configuration_order_breaks_equal_priority_tie():
    value = scheduler(rule("z-rule", 80), rule("a-rule", 80))
    assert value.evaluate(START, lookup({"z-rule": True, "a-rule": True})).decision.rule_id == "z-rule"


def test_while_true_ends_with_return_behavior():
    value = scheduler(rule("live", 80, return_behavior="resume_playlist"))
    value.evaluate(START, lookup({"live": True}))
    outcome = value.evaluate(START + timedelta(seconds=1), lookup({"live": False}))
    assert outcome.decision is None
    assert outcome.ended_rule_id == "live" and outcome.return_behavior == "resume_playlist"


def test_minimum_duration_resists_false_flap():
    value = scheduler(rule("live", 80, minimum=120, cooldown=30))
    value.evaluate(START, lookup({"live": True}))
    held = value.evaluate(START + timedelta(seconds=60), lookup({"live": False}))
    assert held.decision.rule_id == "live"
    ended = value.evaluate(START + timedelta(seconds=120), lookup({"live": False}))
    assert ended.decision is None


def test_maximum_duration_ends_and_requires_false_before_rearming():
    value = scheduler(rule("live", 80, maximum=120))
    value.evaluate(START, lookup({"live": True}))
    assert value.evaluate(START + timedelta(seconds=120), lookup({"live": True})).decision is None
    assert value.evaluate(START + timedelta(seconds=121), lookup({"live": True})).decision is None
    value.evaluate(START + timedelta(seconds=122), lookup({"live": False}))
    assert value.evaluate(START + timedelta(seconds=123), lookup({"live": True})).decision.rule_id == "live"


def test_fixed_duration_survives_trigger_disappearing():
    value = scheduler(rule("alert", 100, mode="fixed", seconds=300))
    activated = value.evaluate(START, lookup({"alert": True})).decision
    assert activated.expires_at == START + timedelta(seconds=300)
    assert value.evaluate(START + timedelta(seconds=60), lookup({"alert": False})).decision.rule_id == "alert"
    assert value.evaluate(START + timedelta(seconds=300), lookup({"alert": False})).decision is None


def test_cooldown_prevents_immediate_retrigger():
    value = scheduler(rule("live", 80, cooldown=60))
    value.evaluate(START, lookup({"live": True}))
    value.evaluate(START + timedelta(seconds=1), lookup({"live": False}))
    assert value.evaluate(START + timedelta(seconds=30), lookup({"live": True})).decision is None
    assert value.evaluate(START + timedelta(seconds=61), lookup({"live": True})).decision.rule_id == "live"


def test_higher_priority_preempts_then_lower_rule_resumes():
    value = scheduler(rule("fantasy", 60), rule("baseball", 80))
    assert value.evaluate(START, lookup({"fantasy": True})).decision.rule_id == "fantasy"
    assert value.evaluate(START + timedelta(seconds=1), lookup({"fantasy": True, "baseball": True})).decision.rule_id == "baseball"
    resumed = value.evaluate(START + timedelta(seconds=2), lookup({"fantasy": True, "baseball": False}))
    assert resumed.decision.rule_id == "fantasy" and resumed.decision.changed
    ended = value.evaluate(START + timedelta(seconds=3), lookup({})).decision
    assert ended is None


def test_disabled_scheduler_never_queries_state():
    value = DynamicScheduler(load_scheduler_config(None))
    assert value.evaluate(START, lambda _ref: (_ for _ in ()).throw(AssertionError())).decision is None

