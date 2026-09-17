from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytz

from src.scheduler.config import SchedulerConfigError, load_scheduler_config
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.temporal import latest_occurrence, next_occurrence, restrictions_match


TZ = ZoneInfo("America/New_York")


def dt(day=17, hour=10, minute=5, second=0, month=9):
    return datetime(2026, month, day, hour, minute, second, tzinfo=TZ)


def raw(schedule, **extra):
    return {"id": "timed", "priority": 20, "target": {"instance_id": "clock"},
            "schedule": schedule, "duration": {"mode": "fixed", "seconds": 120}, **extra}


def engine(schedule, *others, **extra):
    return DynamicScheduler(load_scheduler_config({"enabled": True, "version": 2,
        "rules": [raw(schedule, **extra), *others]}))


def no_state(_ref):
    raise AssertionError("Time scheduling must not query a plugin")


@pytest.mark.parametrize("minute,expected", [(4, False), (5, True), (6, False)])
def test_hourly_cold_start_current_minute_only(minute, expected):
    result = engine({"type": "minute_of_hour", "minutes": [5]}).evaluate(dt(minute=minute), no_state)
    assert bool(result.decision) is expected


def test_crossed_occurrence_deduplicates_and_next_hour_fires():
    value = engine({"type": "minute_of_hour", "minutes": [5]})
    assert value.evaluate(dt(minute=4, second=50), no_state).decision is None
    activated = value.evaluate(dt(second=40), no_state).decision
    assert activated and activated.activated_at == dt(second=40)
    again = value.evaluate(dt(second=50), no_state).decision
    assert not again.changed and again.activated_at == activated.activated_at
    assert value.evaluate(dt(minute=8), no_state).decision is None
    assert value.evaluate(dt(hour=11, second=30), no_state).decision


@pytest.mark.parametrize("schedule,minutes", [
    ({"type": "minute_of_hour", "minutes": [0, 20, 40]}, [0, 20, 40]),
    ({"type": "interval", "every_minutes": 15}, [0, 15, 30, 45]),
    ({"type": "interval", "every_minutes": 30}, [0, 30]),
    ({"type": "interval", "every_minutes": 60}, [0]),
])
def test_wall_clock_alignment_and_multiple_occurrences(schedule, minutes):
    for minute in range(60):
        assert bool(latest_occurrence(schedule, dt(minute=minute))) == (minute in minutes)


def test_multiple_daily_times_independent_occurrences():
    schedule = {"type": "daily", "times": ["08:00", "12:00", "18:00"]}
    value = engine(schedule)
    for hour in (8, 12, 18):
        assert value.evaluate(dt(hour=hour, minute=0), no_state).decision
        assert value.evaluate(dt(hour=hour, minute=3), no_state).decision is None


@pytest.mark.parametrize("day,days,expected", [
    (17, ["mon", "tue", "wed", "thu", "fri"], True),
    (19, ["sat", "sun"], True), (17, ["sat", "sun"], False),
    (17, ["thu"], True),
])
def test_day_restrictions(day, days, expected):
    schedule = {"type": "minute_of_hour", "minutes": [5], "days": days}
    assert bool(latest_occurrence(schedule, dt(day=day))) is expected


@pytest.mark.parametrize("hour,expected", [(7, False), (8, True), (16, True), (17, False)])
def test_start_inclusive_end_exclusive_occurrence_window(hour, expected):
    schedule = {"type": "minute_of_hour", "minutes": [5], "start": "08:00", "end": "17:00"}
    assert bool(latest_occurrence(schedule, dt(hour=hour))) is expected


def test_overnight_occurrence_uses_starting_calendar_day():
    schedule = {"type": "minute_of_hour", "minutes": [15], "start": "22:00", "end": "02:00", "days": ["thu"]}
    assert latest_occurrence(schedule, dt(hour=22, minute=15))
    assert latest_occurrence(schedule, dt(day=18, hour=0, minute=15))
    assert not latest_occurrence(schedule, dt(day=18, hour=2, minute=15))


def test_hours_month_day_of_month_and_nonexistent_dates():
    schedule = {"type": "daily", "times": ["08:00", "12:00"], "days_of_month": [1], "months": [12], "hours": [8]}
    assert latest_occurrence(schedule, dt(day=1, month=12, hour=8, minute=0))
    assert not latest_occurrence(schedule, dt(day=2, month=12, hour=8, minute=0))
    assert not latest_occurrence(schedule, dt(day=1, month=9, hour=8, minute=0))
    assert not latest_occurrence(schedule, dt(day=1, month=12, hour=12, minute=0))
    february = {"type": "daily", "times": ["08:00"], "months": [2], "days_of_month": [31]}
    assert next_occurrence(february, dt()) is None


def test_date_range_condition_inclusive():
    window = {"date_start": "2026-12-01", "date_end": "2026-12-25"}
    assert restrictions_match(window, dt(day=25, month=12))
    assert not restrictions_match(window, dt(day=26, month=12))


def test_once_and_restart_stale_event():
    schedule = {"type": "once", "at": "2026-09-17T10:05"}
    value = engine(schedule)
    assert not value.evaluate(dt(minute=4), no_state).decision
    assert value.evaluate(dt(second=31), no_state).decision
    assert not value.evaluate(dt(minute=8), no_state).decision
    assert not value.evaluate(dt(hour=11), no_state).decision
    assert not engine(schedule).evaluate(dt(minute=6), no_state).decision
    assert next_occurrence(schedule, dt(minute=4)) == dt()


def test_priority_blocked_occurrence_is_missed_not_queued():
    high = {"id": "high", "priority": 80, "target": {"instance_id": "high"},
        "conditions": {"source_instance_id": "high", "path": "live", "operator": "truthy"},
        "duration": {"mode": "while_true", "min_seconds": 0}}
    value = engine({"type": "minute_of_hour", "minutes": [5]}, high)
    assert value.evaluate(dt(), lambda _ref: (True, {"live": True})).decision.rule_id == "high"
    assert value.evaluate(dt(second=30), lambda _ref: (True, {"live": False})).decision is None
    assert value.evaluate(dt(hour=11), lambda _ref: (True, {"live": False})).decision.rule_id == "timed"


def test_manual_hold_consumes_occurrences_without_state_or_activation():
    value = engine({"type": "minute_of_hour", "minutes": [5]})
    value.skip_occurrences(dt(second=30))
    assert not value.state.activations
    assert value.evaluate(dt(minute=6), no_state).decision is None


def test_not_due_does_not_query_application_state():
    value = engine({"type": "minute_of_hour", "minutes": [5]},
        conditions={"source_instance_id": "source", "path": "live", "operator": "truthy"})
    assert value.evaluate(dt(minute=4), no_state).decision is None


def test_higher_timed_occurrence_preempts_and_returns_to_valid_lower_rule():
    lower = {"id":"lower", "priority":10, "target":{"instance_id":"lower"},
        "conditions":{"time":{}}, "duration":{"mode":"while_true","min_seconds":0}}
    value = engine({"type":"minute_of_hour","minutes":[5]}, lower, return_behavior="resume_previous")
    assert value.evaluate(dt(minute=4), no_state).decision.rule_id == "lower"
    assert value.evaluate(dt(second=30), no_state).decision.rule_id == "timed"
    result = value.evaluate(dt(minute=8), no_state)
    assert result.decision.rule_id == "lower"


def test_next_occurrence_and_diagnostics_do_not_consume_engine_history():
    from src.scheduler.diagnostics import SchedulerDiagnostics
    from tests.test_scheduler_diagnostics import Provider
    value = engine({"type":"minute_of_hour","minutes":[5]})
    value.evaluate(dt(minute=4,second=48), no_state)
    before = deepcopy(value.state)
    diagnostic = SchedulerDiagnostics(Provider({}), temporal_state=value.state)
    for _ in range(2):
        result = diagnostic.test_rule(value.config.rules[0], dt(second=31))
        assert result["matched"] and result["schedule"]["scheduled_at"] == dt().isoformat()
        assert result["schedule"]["next_occurrence"] == dt(hour=11).isoformat()
        assert value.state == before
    assert value.evaluate(dt(second=31), no_state).decision


def test_interval_is_midnight_aligned_not_evaluation_aligned():
    value = engine({"type":"interval","every_minutes":30})
    assert not value.evaluate(dt(minute=13),no_state).decision
    assert value.evaluate(dt(minute=30,second=25),no_state).decision


@pytest.mark.parametrize("tz", [TZ, pytz.timezone("America/New_York")])
def test_dst_nonexistent_skipped_ambiguous_once_per_local_minute(tz):
    schedule = {"type": "daily", "times": ["02:30"]}
    start = datetime(2026, 3, 8, 0, 0)
    start = tz.localize(start) if hasattr(tz, "localize") else start.replace(tzinfo=tz)
    assert next_occurrence(schedule, start).date().isoformat() == "2026-03-09"
    schedule = {"type": "daily", "times": ["01:30"]}
    fall = datetime(2026, 11, 1, 0, 0)
    fall = tz.localize(fall) if hasattr(tz, "localize") else fall.replace(tzinfo=tz)
    first = next_occurrence(schedule, fall)
    assert first.utcoffset() == timedelta(hours=-4)
    assert next_occurrence(schedule, first).date().isoformat() == "2026-11-02"


@pytest.mark.parametrize("schedule", [
    {"type": "minute_of_hour", "minutes": [60]},
    {"type": "minute_of_hour", "minutes": [True]},
    {"type": "interval", "every_minutes": 0},
    {"type": "daily", "times": ["25:00"]},
    {"type": "once", "at": "not-a-date"},
    {"type": "daily", "times": ["08:00"], "months": [13]},
    {"type": "daily", "times": ["08:00"], "date_start": "2026-02-31"},
])
def test_invalid_schedule_rejected(schedule):
    with pytest.raises(SchedulerConfigError):
        engine(schedule)
