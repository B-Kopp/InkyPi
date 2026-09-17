"""HTTP API for Dynamic Scheduler configuration and read-only diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytz
from flask import Blueprint, current_app, jsonify, request

from plugins.plugin_registry import get_plugin_instance
from scheduler.config import SchedulerConfigError, load_scheduler_config
from scheduler.diagnostics import SchedulerDiagnostics
from scheduler.models import PluginReference
from scheduler.state_provider import StateProvider

scheduler_bp = Blueprint("scheduler", __name__, url_prefix="/scheduler")


def _provider():
    device = current_app.config["DEVICE_CONFIG"]
    return StateProvider(device.get_playlist_manager(), device, get_plugin_instance)


def _now():
    device = current_app.config["DEVICE_CONFIG"]
    return datetime.now(pytz.timezone(device.get_config("timezone", default="UTC")))


def _load(raw):
    provider = _provider()
    return load_scheduler_config(raw, target_exists=provider.target_exists), provider


def _diagnostics(provider):
    refresh = current_app.config["REFRESH_TASK"]
    engine = getattr(refresh, "dynamic_scheduler", None)
    return SchedulerDiagnostics(provider, temporal_state=engine.state if engine else None)


def _instance_catalog(provider):
    device = current_app.config["DEVICE_CONFIG"]
    values = []
    for playlist in device.get_playlist_manager().playlists:
        for instance in playlist.plugins:
            plugin_config = device.get_plugin(instance.plugin_id) or {}
            _available, schema = provider.get_schema(
                PluginReference(instance_id=instance.instance_id)
            )
            values.append({
                "instance_id": instance.instance_id,
                "instance_name": instance.name,
                "plugin_id": instance.plugin_id,
                "plugin_name": plugin_config.get("display_name", instance.plugin_id),
                "playlist": playlist.name,
                "schema": schema,
            })
    return values


def _flatten_state(value: Any, prefix="", limit=100):
    flattened = {}
    if not isinstance(value, dict):
        return flattened
    for key, item in value.items():
        if len(flattened) >= limit:
            break
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            remaining = limit - len(flattened)
            flattened.update(_flatten_state(item, path, remaining))
        elif isinstance(item, (str, int, float, bool)) or item is None:
            flattened[path] = item
    return flattened


@scheduler_bp.get("/config")
def get_config():
    device = current_app.config["DEVICE_CONFIG"]
    provider = _provider()
    return jsonify({
        "timezone": device.get_config("timezone", default="UTC"),
        "config": device.get_config("dynamic_scheduler", default={
            "enabled": False, "version": 1, "evaluation_interval_seconds": 60,
            "defaults": {"fallback": "playlist", "return_behavior": "resume_previous", "minimum_display_seconds": 60},
            "rules": [],
        }),
        "instances": _instance_catalog(provider),
    })


@scheduler_bp.put("/config")
def save_config():
    device = current_app.config["DEVICE_CONFIG"]
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "Scheduler configuration must be a JSON object"}), 400
    try:
        _load(raw)
    except SchedulerConfigError as exc:
        return jsonify({"error": str(exc), "errors": exc.errors}), 400
    device.update_value("dynamic_scheduler", raw, write=True)
    current_app.config["REFRESH_TASK"].signal_config_change()
    return jsonify({"success": True, "message": "Dynamic Scheduler saved."})


@scheduler_bp.get("/status")
def status():
    device = current_app.config["DEVICE_CONFIG"]
    refresh = current_app.config["REFRESH_TASK"]
    result = refresh.get_scheduler_status(_now())
    raw = device.get_config("dynamic_scheduler", default=None)
    if result["enabled"] and raw:
        try:
            config, provider = _load(raw)
            diagnostic = _diagnostics(provider).test_all(config, _now())
            result["temporal_rules"] = [
                {"rule_id": item["rule_id"], "name": item["name"], **item["schedule"]}
                for item in diagnostic["rules"] if item["schedule"]
            ]
            result["matching_rules"] = [
                {"rule_id": item["rule_id"], "name": item["name"], "priority": item["priority"]}
                for item in diagnostic["rules"]
                if item["matched"] and item["rule_id"] != result.get("rule_id")
            ]
            result["observed_winner_rule_id"] = diagnostic["winner_rule_id"]
        except SchedulerConfigError:
            result["matching_rules"] = []
    else:
        result["matching_rules"] = []
    return jsonify(result)


@scheduler_bp.get("/state")
def current_state():
    provider = _provider()
    provider.begin_evaluation(_now())
    instances = []
    for item in _instance_catalog(provider):
        if not item["schema"]:
            continue
        available, state = provider.get_state(
            PluginReference(instance_id=item["instance_id"])
        )
        instances.append({
            **{key: value for key, value in item.items() if key != "schema"},
            "available": available,
            "state": _flatten_state(state),
        })
    return jsonify({"instances": instances})


def _diagnostic_config(raw):
    if "rule" in raw:
        device_config = current_app.config["DEVICE_CONFIG"].get_config(
            "dynamic_scheduler", default={}
        )
        candidate = {
            "enabled": True,
            "version": device_config.get("version", 1),
            "evaluation_interval_seconds": device_config.get("evaluation_interval_seconds", 60),
            "defaults": device_config.get("defaults", {}),
            "rules": [raw["rule"]],
        }
    else:
        candidate = raw.get("config", raw)
    return _load(candidate)


@scheduler_bp.post("/test-rule")
def test_rule():
    raw = request.get_json(silent=True) or {}
    try:
        config, provider = _diagnostic_config(raw)
    except SchedulerConfigError as exc:
        return jsonify({"error": str(exc), "errors": exc.errors}), 400
    if len(config.rules) != 1:
        return jsonify({"error": "Test Rule requires exactly one rule"}), 400
    return jsonify(_diagnostics(provider).test_rule(config.rules[0], _now()))


@scheduler_bp.post("/test-all")
def test_all():
    raw = request.get_json(silent=True) or {}
    try:
        config, provider = _diagnostic_config(raw)
    except SchedulerConfigError as exc:
        return jsonify({"error": str(exc), "errors": exc.errors}), 400
    return jsonify(_diagnostics(provider).test_all(config, _now()))
