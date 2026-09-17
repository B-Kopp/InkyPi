from flask import Blueprint, request, jsonify, current_app, render_template, Response
from utils.time_utils import calculate_seconds
from datetime import datetime, timedelta
import os
import pytz
import logging
import io
import json
from plugins.plugin_registry import get_plugin_instance
from scheduler.config import SchedulerConfigError, load_scheduler_config
from scheduler.state_provider import StateProvider

# Try to import cysystemd for journal reading (Linux only)
try:
    from cysystemd.reader import JournalReader, JournalOpenMode, Rule
    JOURNAL_AVAILABLE = True
except ImportError:
    JOURNAL_AVAILABLE = False
    # Define dummy classes for when cysystemd is not available
    class JournalOpenMode:
        SYSTEM = None
    class Rule:
        pass
    class JournalReader:
        def __init__(self, *args, **kwargs):
            pass


logger = logging.getLogger(__name__)
settings_bp = Blueprint("settings", __name__)

@settings_bp.route('/settings')
def settings_page():
    device_config = current_app.config['DEVICE_CONFIG']
    timezones = sorted(pytz.all_timezones_set)
    scheduler_config = device_config.get_config("dynamic_scheduler", default={
        "enabled": False,
        "version": 1,
        "evaluation_interval_seconds": 60,
        "defaults": {
            "fallback": "playlist",
            "return_behavior": "resume_previous",
            "minimum_display_seconds": 60,
        },
        "rules": [],
    })
    return render_template(
        'settings.html',
        device_settings=device_config.get_config(),
        timezones=timezones,
        dynamic_scheduler_json=json.dumps(scheduler_config, indent=2),
    )

@settings_bp.route('/save_settings', methods=['POST'])
def save_settings():
    device_config = current_app.config['DEVICE_CONFIG']

    try:
        form_data = request.form.to_dict()

        unit, interval, time_format = form_data.get('unit'), form_data.get("interval"), form_data.get("timeFormat")
        if not unit or unit not in ["minute", "hour"]:
            return jsonify({"error": "Plugin cycle interval unit is required"}), 400
        if not interval or not interval.isnumeric():
            return jsonify({"error": "Refresh interval is required"}), 400
        if not form_data.get("timezoneName"):
            return jsonify({"error": "Time Zone is required"}), 400
        if not time_format or time_format not in ["12h", "24h"]:
            return jsonify({"error": "Time format is required"}), 400
        previous_interval_seconds = device_config.get_config("plugin_cycle_interval_seconds")
        previous_scheduler_config = device_config.get_config("dynamic_scheduler", default=None)
        plugin_cycle_interval_seconds = calculate_seconds(int(interval), unit)
        if plugin_cycle_interval_seconds > 86400 or plugin_cycle_interval_seconds <= 0:
            return jsonify({"error": "Plugin cycle interval must be less than 24 hours"}), 400

        scheduler_json = form_data.get("dynamicSchedulerConfig", "").strip()
        try:
            scheduler_data = json.loads(scheduler_json) if scheduler_json else {}
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"Dynamic Scheduler JSON is invalid: {exc.msg} at line {exc.lineno}"}), 400
        if not isinstance(scheduler_data, dict):
            return jsonify({"error": "Dynamic Scheduler configuration must be a JSON object"}), 400
        scheduler_data["enabled"] = "dynamicSchedulerEnabled" in form_data
        scheduler_data.setdefault("version", 1)
        scheduler_data.setdefault("evaluation_interval_seconds", 60)
        scheduler_data.setdefault("defaults", {})
        scheduler_data.setdefault("rules", [])
        state_provider = StateProvider(
            device_config.get_playlist_manager(), device_config, get_plugin_instance
        )
        try:
            load_scheduler_config(scheduler_data, target_exists=state_provider.target_exists)
        except SchedulerConfigError as exc:
            return jsonify({"error": f"Dynamic Scheduler configuration is invalid: {exc}"}), 400

        settings = {
            "name": form_data.get("deviceName"),
            "orientation": form_data.get("orientation"),
            "inverted_image": form_data.get("invertImage"),
            "log_system_stats": form_data.get("logSystemStats"),
            "timezone": form_data.get("timezoneName"),
            "time_format": form_data.get("timeFormat"),
            "plugin_cycle_interval_seconds": plugin_cycle_interval_seconds,
            "dynamic_scheduler": scheduler_data,
            "image_settings": {
                "saturation": float(form_data.get("saturation", "1.0")),
                "brightness": float(form_data.get("brightness", "1.0")),
                "sharpness": float(form_data.get("sharpness", "1.0")),
                "contrast": float(form_data.get("contrast", "1.0"))
            }
        }
        if "inky_saturation" in form_data:
            settings["image_settings"]["inky_saturation"] = float(form_data.get("inky_saturation", "0.5"))
        device_config.update_config(settings)

        if (plugin_cycle_interval_seconds != previous_interval_seconds or
                scheduler_data != previous_scheduler_config):
            # Reload scheduler state and wake the background thread when either
            # decision cadence changed.
            refresh_task = current_app.config['REFRESH_TASK']
            refresh_task.signal_config_change()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"success": True, "message": "Saved settings."})

@settings_bp.route('/shutdown', methods=['POST'])
def shutdown():
    data = request.get_json() or {}
    if data.get("reboot"):
        logger.info("Reboot requested")
        os.system("sudo reboot")
    else:
        logger.info("Shutdown requested")
        os.system("sudo shutdown -h now")
    return jsonify({"success": True})

@settings_bp.route('/download-logs')
def download_logs():
    try:
        buffer = io.StringIO()
        
        # Get 'hours' from query parameters, default to 2 if not provided or invalid
        hours_str = request.args.get('hours', '2')
        try:
            hours = int(hours_str)
        except ValueError:
            hours = 2
        since = datetime.now() - timedelta(hours=hours)

        if not JOURNAL_AVAILABLE:
            # Return a message when running in development mode without systemd
            buffer.write(f"Log download not available in development mode (cysystemd not installed).\n")
            buffer.write(f"Logs would normally show InkyPi service logs from the last {hours} hours.\n")
            buffer.write(f"\nTo see Flask development logs, check your terminal output.\n")
        else:
            reader = JournalReader()
            reader.open(JournalOpenMode.SYSTEM)
            reader.add_filter(Rule("_SYSTEMD_UNIT", "inkypi.service"))
            reader.seek_realtime_usec(int(since.timestamp() * 1_000_000))

            for record in reader:
                try:
                    ts = datetime.fromtimestamp(record.get_realtime_usec() / 1_000_000)
                    formatted_ts = ts.strftime("%b %d %H:%M:%S")
                except Exception:
                    formatted_ts = "??? ?? ??:??:??"

                data = record.data
                hostname = data.get("_HOSTNAME", "unknown-host")
                identifier = data.get("SYSLOG_IDENTIFIER") or data.get("_COMM", "?")
                pid = data.get("_PID", "?")
                msg = data.get("MESSAGE", "").rstrip()

                # Format the log entry similar to the journalctl default output
                buffer.write(f"{formatted_ts} {hostname} {identifier}[{pid}]: {msg}\n")

        buffer.seek(0)
        # Add date and time to the filename
        now_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"inkypi_{now_str}.log"
        return Response(
            buffer.read(),
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        return Response(f"Error reading logs: {e}", status=500, mimetype="text/plain")
