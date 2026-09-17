from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.scheduler.condition_evaluator import ConditionEvaluator
from src.scheduler.models import PluginReference


NOW = datetime(2026, 9, 14, 8, 0, tzinfo=ZoneInfo("America/New_York"))


def lookup(state, available=True):
    return lambda _source: (available, state)


@pytest.mark.parametrize(
    "operator,actual,expected,matched",
    [
        ("eq", "live", "live", True),
        ("ne", "live", "final", True),
        ("gt", 3, 2, True),
        ("gte", 3, 3, True),
        ("lt", 2, 3, True),
        ("lte", 3, 3, True),
        ("in", "live", ["live", "delayed"], True),
        ("not_in", "final", ["live", "delayed"], True),
        ("truthy", [1], None, True),
        ("falsy", 0, None, True),
        ("contains", ["live", "final"], "live", True),
    ],
)
def test_state_operators(operator, actual, expected, matched):
    condition = {"source": "score", "path": "game.value", "operator": operator}
    if operator not in {"truthy", "falsy"}:
        condition["value"] = expected
    assert ConditionEvaluator().evaluate(
        condition, NOW, lookup({"game": {"value": actual}})
    ).matched is matched


def test_exists_distinguishes_present_none_from_missing():
    evaluator = ConditionEvaluator()
    condition = {"source": "score", "path": "game.value", "operator": "exists"}
    assert evaluator.evaluate(condition, NOW, lookup({"game": {"value": None}})).matched
    assert not evaluator.evaluate(condition, NOW, lookup({"game": {}})).matched


def test_nested_all_any_and_not():
    condition = {
        "all": [
            {"source": "score", "path": "game.live", "operator": "truthy"},
            {"any": [
                {"source": "score", "path": "game.inning", "operator": "gt", "value": 6},
                {"not": {"source": "score", "path": "game.status", "operator": "eq", "value": "final"}},
            ]},
        ]
    }
    assert ConditionEvaluator().evaluate(
        condition, NOW, lookup({"game": {"live": True, "inning": 4, "status": "live"}})
    ).matched


def test_missing_or_failed_state_stays_unavailable_under_not():
    condition = {"not": {"source": "score", "path": "missing", "operator": "truthy"}}
    missing = ConditionEvaluator().evaluate(condition, NOW, lookup({}))
    failed = ConditionEvaluator().evaluate(condition, NOW, lookup({}, available=False))
    assert not missing.matched and not missing.available
    assert not failed.matched and not failed.available


def test_unknown_any_branch_cannot_be_inverted_into_a_match():
    condition = {"not": {"any": [
        {"source": "score", "path": "missing", "operator": "truthy"},
        {"source": "score", "path": "game.live", "operator": "truthy"},
    ]}}
    result = ConditionEvaluator().evaluate(
        condition, NOW, lookup({"game": {"live": False}})
    )
    assert not result.matched and not result.available


def test_type_mismatch_is_false_and_unavailable():
    condition = {"source": "score", "path": "game.status", "operator": "gt", "value": 1}
    result = ConditionEvaluator().evaluate(condition, NOW, lookup({"game": {"status": "live"}}))
    assert not result.matched and not result.available


@pytest.mark.parametrize(
    "current,condition,expected",
    [
        (datetime(2026, 9, 14, 8, 0, tzinfo=ZoneInfo("America/New_York")), {"days": ["mon"], "start": "07:00", "end": "09:00"}, True),
        (datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("America/New_York")), {"start": "07:00", "end": "09:00"}, False),
        (datetime(2026, 9, 19, 23, 0, tzinfo=ZoneInfo("America/New_York")), {"days": ["sat"], "start": "22:00", "end": "02:00"}, True),
        (datetime(2026, 9, 20, 1, 0, tzinfo=ZoneInfo("America/New_York")), {"days": ["sat"], "start": "22:00", "end": "02:00"}, True),
        (datetime(2026, 9, 20, 3, 0, tzinfo=ZoneInfo("America/New_York")), {"start": "22:00", "end": "02:00"}, False),
    ],
)
def test_time_conditions(current, condition, expected):
    assert ConditionEvaluator().evaluate({"time": condition}, current, lookup({})).matched is expected


def test_source_reference_prefers_stable_id():
    seen = []
    condition = {
        "source": "Friendly Name",
        "source_instance_id": "stable-id",
        "path": "ready",
        "operator": "truthy",
    }
    ConditionEvaluator().evaluate(condition, NOW, lambda ref: (seen.append(ref) is None, {"ready": True}))
    assert seen == [PluginReference(instance_id="stable-id", plugin_instance="Friendly Name")]
