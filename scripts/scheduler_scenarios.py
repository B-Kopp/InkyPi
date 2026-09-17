#!/usr/bin/env python3
"""Exercise deterministic Dynamic Scheduler scenarios without APIs or hardware."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheduler.config import load_scheduler_config
from scheduler.dynamic_scheduler import DynamicScheduler


def rule(rule_id, priority, *, mode="while_true", seconds=None):
    duration = {"mode": mode, "min_seconds": 0}
    if seconds is not None:
        duration["seconds"] = seconds
    return {
        "id": rule_id,
        "priority": priority,
        "target": {"instance_id": f"fake-{rule_id}"},
        "conditions": {
            "source_instance_id": f"fake-{rule_id}",
            "path": "active",
            "operator": "truthy",
        },
        "duration": duration,
    }


def make_scheduler():
    return DynamicScheduler(load_scheduler_config({
        "enabled": True,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {"minimum_display_seconds": 0},
        "rules": [
            rule("sleeper_live", 60),
            rule("mlb_live", 80),
            rule("fixed_alert", 100, mode="fixed", seconds=300),
        ],
    }))


def timeline_for(name):
    scenarios = {
        "none": [({}, 0)],
        "mlb_live": [({"mlb_live": True}, 0)],
        "sleeper_live": [({"sleeper_live": True}, 0)],
        "both_live": [({"mlb_live": True, "sleeper_live": True}, 0)],
        "higher_priority_interrupt": [
            ({"sleeper_live": True}, 0),
            ({"sleeper_live": True, "mlb_live": True}, 60),
            ({"sleeper_live": True}, 120),
        ],
        "condition_expires": [({"mlb_live": True}, 0), ({}, 60)],
        "fixed_duration": [
            ({"fixed_alert": True}, 0),
            ({}, 60),
            ({}, 299),
            ({}, 300),
        ],
        "provider_failure": [({"provider_failure": True}, 0)],
    }
    return scenarios[name]


def run(name):
    scheduler = make_scheduler()
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    for states, seconds in timeline_for(name):
        def lookup(reference):
            if states.get("provider_failure"):
                return False, {}
            key = reference.instance_id.removeprefix("fake-")
            return True, {"active": states.get(key, False)}

        outcome = scheduler.evaluate(start + timedelta(seconds=seconds), lookup)
        selected = outcome.decision.rule_id if outcome.decision else "playlist"
        transition = " changed" if outcome.decision and outcome.decision.changed else ""
        print(f"+{seconds:>3}s -> {selected}{transition}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=[
        "none", "mlb_live", "sleeper_live", "both_live",
        "higher_priority_interrupt", "condition_expires",
        "fixed_duration", "provider_failure",
    ])
    run(parser.parse_args().scenario)

