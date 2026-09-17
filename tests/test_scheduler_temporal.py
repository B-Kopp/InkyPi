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


@pytest.mark.parametrize("minute,second", [(15, 0), (15, 27), (16, 10)])
def test_fixed_occurrence_expiration_uses_scheduled_time(minute, second, caplog):
    value = engine({"type": "minute_of_hour", "minutes": [15]},
                   duration={"mode": "fixed", "seconds": 180})
    value.evaluate(dt(hour=15, minute=14, second=50), no_state)
    detected = dt(hour=15, minute=minute, second=second)
    with caplog.at_level("INFO"):
        decision = value.evaluate(detected, no_state).decision
    assert decision.occurrence_at == dt(hour=15, minute=15)
    assert decision.activated_at == detected
    assert decision.expires_at == dt(hour=15, minute=18)
    assert value.state.activations["timed"].occurrence_at == decision.occurrence_at
    assert "scheduled_at=" + decision.occurrence_at.isoformat() in caplog.text
    assert "activated_at=" + detected.isoformat() in caplog.text
    assert "expires_at=" + decision.expires_at.isoformat() in caplog.text
    assert f"remaining_seconds={(decision.expires_at - detected).total_seconds()}" in caplog.text
    assert value.evaluate(dt(hour=15, minute=18), no_state).decision is None


@pytest.mark.parametrize("second", [0, 10])
def test_already_expired_occurrence_is_consumed_without_activation(second, caplog):
    value = engine({"type": "minute_of_hour", "minutes": [15]},
                   duration={"mode": "fixed", "seconds": 180})
    value.evaluate(dt(hour=15, minute=14), no_state)
    with caplog.at_level("INFO"):
        assert value.evaluate(dt(hour=15, minute=18, second=second), no_state).decision is None
    assert not value.state.activations
    assert not value.state.cooldown_until
    assert "skipped as expired" in caplog.text
    assert value.state.occurrence_keys["timed"] == "2026-09-17T15:15"


@pytest.mark.parametrize("release_minute", [20, 24])
def test_blocked_occurrences_remain_skipped_without_replay(release_minute, caplog):
    high = {"id": "high", "priority": 80, "target": {"instance_id": "high"},
            "conditions": {"source_instance_id": "high", "path": "live", "operator": "truthy"},
            "duration": {"mode": "while_true", "min_seconds": 0}}
    value = engine({"type": "minute_of_hour", "minutes": [18]}, high,
                   duration={"mode": "fixed", "seconds": 300})
    value.evaluate(dt(hour=15, minute=17), lambda _: (True, {"live": True}))
    with caplog.at_level("INFO"):
        assert value.evaluate(dt(hour=15, minute=18, second=27), lambda _: (True, {"live": True})).decision.rule_id == "high"
    assert "skipped while blocked" in caplog.text
    assert "timed" not in value.state.activations
    assert value.evaluate(dt(hour=15, minute=release_minute), lambda _: (True, {"live": False})).decision is None


def test_expiring_clock_no_longer_blocks_countdown_occurrence():
    countdown = raw({"type": "minute_of_hour", "minutes": [18]}, id="countdown", priority=10)
    value = engine({"type": "minute_of_hour", "minutes": [15]}, countdown,
                   duration={"mode": "fixed", "seconds": 180})
    clock = value.evaluate(dt(hour=16, minute=15, second=27), no_state).decision
    assert clock.expires_at == dt(hour=16, minute=18)
    result = value.evaluate(dt(hour=16, minute=18, second=10), no_state).decision
    assert result.rule_id == "countdown"
    assert result.expires_at == dt(hour=16, minute=20)


def test_consecutive_occurrences_across_hour_and_day_boundaries():
    value = engine({"type": "minute_of_hour", "minutes": [0, 15, 55]},
                   duration={"mode": "fixed", "seconds": 180})
    for scheduled in (dt(hour=23, minute=55), dt(day=18, hour=0, minute=0), dt(day=18, hour=0, minute=15)):
        result = value.evaluate(scheduled + timedelta(seconds=27), no_state).decision
        assert result.occurrence_at == scheduled
        assert result.expires_at == scheduled + timedelta(seconds=180)


def test_occurrence_while_true_maximum_remains_activation_anchored():
    value = engine({"type": "minute_of_hour", "minutes": [15]},
                   duration={"mode": "while_true", "min_seconds": 180, "max_seconds": 300})
    detected = dt(hour=15, minute=15, second=27)
    result = value.evaluate(detected, no_state).decision
    assert result.activated_at == detected
    assert result.expires_at == detected + timedelta(seconds=300)
    assert value.evaluate(dt(hour=15, minute=18), no_state).decision


def test_non_occurrence_fixed_duration_remains_activation_anchored():
    rule = raw({}, conditions={"time": {}}, duration={"mode": "fixed", "seconds": 180})
    rule.pop("schedule")
    value = DynamicScheduler(load_scheduler_config({"enabled": True, "rules": [rule]}))
    detected = dt(hour=15, minute=15, second=27)
    result = value.evaluate(detected, no_state).decision
    assert result.occurrence_at is None
    assert result.expires_at == detected + timedelta(seconds=180)
    assert value.evaluate(dt(hour=15, minute=18), no_state).decision


def test_fixed_occurrence_duration_across_dst_is_elapsed_seconds():
    value = engine({"type": "daily", "times": ["01:55"]},
                   duration={"mode": "fixed", "seconds": 600})
    scheduled = datetime(2026, 11, 1, 1, 55, tzinfo=TZ)
    detected = scheduled + timedelta(seconds=27)
    result = value.evaluate(detected, no_state).decision
    assert result.expires_at.hour == 1 and result.expires_at.minute == 5
    assert result.expires_at.fold == 1
    assert value.evaluate(datetime(2026, 11, 1, 1, 4, tzinfo=TZ, fold=1), no_state).decision
    assert value.evaluate(datetime(2026, 11, 1, 1, 5, tzinfo=TZ, fold=1), no_state).decision is None
