import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheduler.config import load_scheduler_config


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/templates/settings.html").read_text()
JAVASCRIPT = (ROOT / "src/static/scheduler_settings.js").read_text()


def visual_config(condition):
    return {
        "enabled": True,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {
            "fallback": "playlist",
            "return_behavior": "resume_previous",
            "minimum_display_seconds": 60,
        },
        "rules": [{
            "id": "visual_rule",
            "name": "Visual Rule",
            "enabled": True,
            "priority": 80,
            "target": {"instance_id": "target-id"},
            "conditions": {"all": [condition]},
            "duration": {"mode": "while_true", "min_seconds": 120},
            "cooldown_seconds": 0,
            "return_behavior": "resume_previous",
        }],
    }


def test_settings_uses_visual_builder_as_primary_and_one_advanced_json_model():
    assert 'id="schedulerRuleList"' in HTML
    assert 'id="schedulerAddRule"' in HTML
    assert 'id="schedulerStatus"' in HTML
    assert 'id="schedulerViewState"' in HTML
    assert '<summary>Advanced JSON</summary>' in HTML
    assert HTML.count('id="dynamicSchedulerConfig"') == 1
    assert 'scheduler_settings.js' in HTML


def test_builder_has_accessible_stable_id_and_type_aware_controls():
    assert 'source_instance_id' in JAVASCRIPT
    assert 'target: { instance_id:' in JAVASCRIPT
    assert 'type === "boolean"' in JAVASCRIPT
    assert 'type === "probability"' in JAVASCRIPT
    assert 'type="time"' in JAVASCRIPT
    for action in ("test", "edit", "duplicate", "toggle", "delete"):
        assert f'data-action="{action}"' in JAVASCRIPT


@pytest.mark.parametrize("condition", [
    {"source_instance_id": "source-id", "path": "game.is_live", "operator": "eq", "value": True},
    {"source_instance_id": "source-id", "path": "game.inning", "operator": "gte", "value": 7},
    {"source_instance_id": "source-id", "path": "game.status", "operator": "eq", "value": "live"},
    {"source_instance_id": "source-id", "path": "matchups.min_user_win_probability", "operator": "lte", "value": 0.35},
    {"time": {"days": ["mon", "tue"], "start": "22:00", "end": "02:00"}},
])
def test_ui_generated_condition_shapes_pass_existing_v1_validation(condition):
    parsed = load_scheduler_config(
        visual_config(condition),
        target_exists=lambda reference: reference.instance_id in {"target-id", "source-id"},
    )
    assert parsed.rules[0].name == "Visual Rule"
    assert parsed.rules[0].target.instance_id == "target-id"


def test_existing_v1_rule_without_display_name_still_loads():
    value = visual_config({"time": {"start": "07:00", "end": "09:00"}})
    del value["rules"][0]["name"]
    parsed = load_scheduler_config(value, target_exists=lambda _reference: True)
    assert parsed.rules[0].name == "visual_rule"


def test_builder_keeps_json_as_single_source_and_rejects_malformed_input_locally():
    assert "JSON.stringify(state.config" in JAVASCRIPT
    assert "JSON.parse(el(\"dynamicSchedulerConfig\").value)" in JAVASCRIPT
    assert "Invalid JSON:" in JAVASCRIPT
    assert "state.config = previous" in JAVASCRIPT


def test_test_actions_use_observational_endpoints():
    assert 'api("/scheduler/test-rule"' in JAVASCRIPT
    assert 'api("/scheduler/test-all"' in JAVASCRIPT
    assert 'api("/scheduler/state"' in JAVASCRIPT
    assert 'api("/scheduler/status"' in JAVASCRIPT


@pytest.mark.parametrize("schedule", [
    {"type":"minute_of_hour", "minutes":[5]},
    {"type":"minute_of_hour", "minutes":[0,30], "days":["mon","tue","wed","thu","fri"], "start":"08:00", "end":"17:00"},
    {"type":"interval", "every_minutes":30},
    {"type":"daily", "times":["08:00","12:00","18:00"]},
    {"type":"once", "at":"2026-12-31T23:55"},
])
def test_visual_temporal_json_round_trips(schedule):
    import json
    value = visual_config({"time":{}})
    value["version"] = 2
    value["rules"][0]["schedule"] = schedule
    value["rules"][0]["duration"] = {"mode":"fixed","seconds":120}
    parsed = load_scheduler_config(json.loads(json.dumps(value)))
    assert parsed.rules[0].schedule == schedule
    assert parsed.rules[0].duration.seconds == 120


def test_temporal_editor_includes_day_shortcuts_and_structured_schedule_inputs():
    for label in ("At Specific Times", "Minutes Past the Hour", "Every N Minutes", "One-Time Date/Time", "Weekdays", "Weekends"):
        assert label in JAVASCRIPT
    assert 'data-schedule-value' in JAVASCRIPT
    assert 'type="date"' in JAVASCRIPT
    assert 'candidate.version = 2' in JAVASCRIPT
    assert 'temporalDiagnostic(result.schedule)' in JAVASCRIPT
