import threading
import time
import os
import logging
from copy import deepcopy
import psutil
import pytz
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from plugins.plugin_registry import get_plugin_instance
from utils.image_utils import compute_image_hash
from model import RefreshInfo, PlaylistManager
from PIL import Image
from scheduler.config import SchedulerConfigError, load_scheduler_config
from scheduler.dynamic_scheduler import DynamicScheduler
from scheduler.models import PluginReference
from scheduler.state_provider import StateProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalDisplayContext:
    playlist_name: str
    instance_id: str


class RefreshClock:
    """Minimal refresh-info view backed by the independent normal clock."""

    def __init__(self, refresh_time):
        self.refresh_time = refresh_time

    def get_refresh_datetime(self):
        return self.refresh_time

class RefreshTask:
    """Handles the logic for refreshing the display using a background thread."""

    def __init__(self, device_config, display_manager):
        self.device_config = device_config
        self.display_manager = display_manager

        self.thread = None
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.running = False
        self.manual_update_request = ()

        self.refresh_event = threading.Event()
        self.refresh_event.set()
        self.refresh_result = {}

        # Dynamic scheduler state is deliberately separate from persisted
        # playlist indexes and the global refresh metadata.
        self.dynamic_scheduler = None
        self.scheduler_config = None
        self.scheduler_config_valid = True
        self.state_provider = StateProvider(
            self.device_config.get_playlist_manager(), self.device_config, get_plugin_instance
        )
        latest_refresh = self.device_config.get_refresh_info()
        persisted_normal_time = self.device_config.get_playlist_manager().normal_refresh_time
        if persisted_normal_time:
            self.normal_refresh_time = datetime.fromisoformat(persisted_normal_time)
        elif latest_refresh.refresh_type in {"Playlist", "Manual Update"}:
            self.normal_refresh_time = latest_refresh.get_refresh_datetime()
        else:
            self.normal_refresh_time = None
        if self.normal_refresh_time and not persisted_normal_time:
            self.device_config.get_playlist_manager().normal_refresh_time = (
                self.normal_refresh_time.isoformat()
            )
        self.last_normal_context = self._context_from_refresh_info(latest_refresh)
        self.interrupted_normal_context = None
        self.override_started_at = None
        self.normal_cycle_remaining_seconds = None
        self.manual_hold_until = None
        self.scheduler_shutdown_pending = False
        self.scheduler_reconfigure_pending = False
        self.scheduler_raw_config = None
        self.last_scheduler_evaluation_at = None
        self._reload_scheduler_config()

    def start(self):
        """Starts the background thread for refreshing the display."""
        if not self.thread or not self.thread.is_alive():
            logger.info("Starting refresh task")
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.running = True
            self.thread.start()

    def stop(self):
        """Stops the refresh task by notifying the background thread to exit."""
        with self.condition:
            self.running = False
            self.condition.notify_all()  # Wake the thread to let it exit
        if self.thread:
            logger.info("Stopping refresh task")
            self.thread.join()

    def _run(self):
        """Background task that manages the periodic refresh of the display.

        This function runs in a loop, sleeping for a configured duration (`plugin_cycle_interval_seconds`) or until
        manually triggered via `manual_update()`. Determines the next plugin to refresh based on active playlists and
        updates the display accordingly.

        Workflow:
        1. Waits for the configured sleep duration or until notified of a manual update.
        2. Checks if a manual update has been requested:
        - If so, refreshes the specified plugin immediately.
        3. Otherwise, determines the next plugin to refresh based on the active playlist and generates an image.
        4. Compares the image hash with the last displayed image hash.
        - If the image has changed, updates the display.
        - If the image is the same, skips the refresh.
        5. Updates the refresh metadata in the device configuration.
        6. Repeats the process until `stop()` is called.

        Handles any exceptions that occur during the refresh process and ensures the refresh event is set 
        to indicate completion.

        Exceptions:
        - Captures and logs any unexpected errors during execution to prevent the thread from exiting.
        """
        evaluate_immediately = bool(
            self.scheduler_config and self.scheduler_config.enabled
        )
        while True:
            try:
                with self.condition:
                    sleep_time = self._get_loop_sleep_time()

                    # Wait for sleep_time or until notified
                    self.condition.wait(timeout=0 if evaluate_immediately else sleep_time)
                    evaluate_immediately = False
                    self.refresh_result = {}
                    self.refresh_event.clear()

                    # Exit if `stop()` is called
                    if not self.running:
                        break

                    current_dt = self._get_current_datetime()
                    self._run_cycle(current_dt)

            except Exception as e:
                logger.exception('Exception during refresh')
                self.refresh_result["exception"] = e  # Capture exception
            finally:
                self.refresh_event.set()

    def manual_update(self, refresh_action):
        """Manually triggers an update for the specified plugin id and plugin settings by notifying the background process."""
        if self.running:
            with self.condition:
                self.manual_update_request = refresh_action
                self.refresh_result = {}
                self.refresh_event.clear()

                self.condition.notify_all()  # Wake the thread to process manual update

            self.refresh_event.wait()
            if self.refresh_result.get("exception"):
                raise self.refresh_result.get("exception")
        else:
            logger.warning("Background refresh task is not running, unable to do a manual update")

    def signal_config_change(self):
        """Notify the background thread that config has changed (e.g., interval updated)."""
        if self.running:
            with self.condition:
                self._reload_scheduler_config()
                self.condition.notify_all()
        else:
            self._reload_scheduler_config()

    def _reload_scheduler_config(self):
        """Load scheduler configuration without allowing errors into the display loop."""
        was_enabled = bool(self.scheduler_config and self.scheduler_config.enabled)
        raw = self.device_config.get_config("dynamic_scheduler", default=None)
        if self.dynamic_scheduler is not None and raw == self.scheduler_raw_config:
            return
        had_override = self.override_started_at is not None
        self.scheduler_raw_config = deepcopy(raw)
        try:
            config = load_scheduler_config(raw, target_exists=self.state_provider.target_exists)
        except SchedulerConfigError as exc:
            logger.error("Invalid dynamic scheduler configuration; using playlist fallback: %s", exc)
            self.scheduler_config = None
            self.dynamic_scheduler = None
            self.scheduler_config_valid = False
            if was_enabled and self.override_started_at is not None:
                self.scheduler_shutdown_pending = True
            return
        self.scheduler_config = config
        self.dynamic_scheduler = DynamicScheduler(config)
        self.scheduler_config_valid = True
        if was_enabled and not config.enabled and self.override_started_at is not None:
            self.scheduler_shutdown_pending = True
        elif was_enabled and config.enabled and had_override:
            self.scheduler_reconfigure_pending = True

    def _get_loop_sleep_time(self):
        if self.scheduler_config and self.scheduler_config.enabled:
            return self.scheduler_config.evaluation_interval_seconds
        return self.device_config.get_config("plugin_cycle_interval_seconds", default=60 * 60)

    def _run_cycle(self, current_dt):
        """Determine and execute one manual, dynamic, or normal refresh action."""
        playlist_manager = self.device_config.get_playlist_manager()
        latest_refresh = self.device_config.get_refresh_info()
        is_manual = bool(self.manual_update_request)
        if is_manual:
            logger.info("Manual update requested")
            refresh_action = self.manual_update_request
            self.manual_update_request = ()
        else:
            if self.device_config.get_config("log_system_stats"):
                self.log_system_stats()
            logger.info(
                "Running interval refresh check. | current_time: %s",
                current_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            refresh_action = self._determine_refresh_action(
                playlist_manager, latest_refresh, current_dt
            )
        if not refresh_action:
            return None
        self._execute_refresh_action(refresh_action, latest_refresh, current_dt, is_manual)
        return refresh_action

    def _execute_refresh_action(self, refresh_action, latest_refresh, current_dt, is_manual=False):
        """Execute every action through the existing plugin/render/display pipeline."""
        plugin_config = self.device_config.get_plugin(refresh_action.get_plugin_id())
        if plugin_config is None:
            logger.error("Plugin config not found for '%s'.", refresh_action.get_plugin_id())
            return
        plugin = get_plugin_instance(plugin_config)
        image = refresh_action.execute(plugin, self.device_config, current_dt)
        image_hash = compute_image_hash(image)
        refresh_info = refresh_action.get_refresh_info()
        refresh_info.update({"refresh_time": current_dt.isoformat(), "image_hash": image_hash})
        if image_hash != latest_refresh.image_hash:
            logger.info("Updating display. | refresh_info: %s", refresh_info)
            self.display_manager.display_image(
                image, image_settings=plugin.config.get("image_settings", [])
            )
        else:
            logger.info("Image already displayed, skipping refresh. | refresh_info: %s", refresh_info)
        if is_manual and self.scheduler_config and self.scheduler_config.enabled:
            self.dynamic_scheduler.skip_occurrences(current_dt)
            interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
            self.manual_hold_until = current_dt + timedelta(seconds=interval)
            # Preserve the existing behavior in which a manual display restarts
            # the wait until the next ordinary playlist cycle.
            self._set_normal_refresh_time(current_dt)
        elif getattr(refresh_action, "affects_normal_cycle", False):
            self._set_normal_refresh_time(current_dt)
            self.last_normal_context = NormalDisplayContext(
                refresh_action.playlist.name, refresh_action.plugin_instance.instance_id
            )
        self.device_config.refresh_info = RefreshInfo(**refresh_info)
        self.device_config.write_config()

    def _determine_refresh_action(self, playlist_manager, latest_refresh, current_dt):
        """Apply the scheduler immediately before normal playlist selection."""
        if not self.scheduler_config or not self.scheduler_config.enabled:
            if self.scheduler_shutdown_pending:
                self.scheduler_shutdown_pending = False
                logger.info("Dynamic scheduler disabled; returning to playlist")
                return self._return_from_override(
                    "resume_playlist", playlist_manager, current_dt
                )
            if not self.scheduler_config_valid:
                logger.info("Dynamic scheduler configuration invalid; using playlist")
                latest_refresh = self._normal_refresh_clock(latest_refresh)
            else:
                logger.info("Dynamic scheduler disabled; using playlist")
            return self._normal_playlist_action(playlist_manager, latest_refresh, current_dt)
        if self.scheduler_reconfigure_pending:
            self.scheduler_reconfigure_pending = False
            logger.info("Dynamic scheduler configuration changed; returning to playlist before reevaluation")
            return self._return_from_override(
                "resume_playlist", playlist_manager, current_dt
            )
        if self.manual_hold_until and current_dt < self.manual_hold_until:
            self.dynamic_scheduler.skip_occurrences(current_dt)
            logger.info("Manual display hold active; skipping scheduler evaluation")
            return None
        if self.manual_hold_until:
            self.dynamic_scheduler.skip_occurrences(self.manual_hold_until - timedelta(microseconds=1))
        self.manual_hold_until = None
        try:
            self.last_scheduler_evaluation_at = current_dt
            self.state_provider.begin_evaluation(current_dt)
            outcome = self.dynamic_scheduler.evaluate(current_dt, self.state_provider.get_state)
            if outcome.decision:
                resolved = self.state_provider.resolve(outcome.decision.target)
                if resolved is None:
                    raise RuntimeError(
                        f"scheduler target unavailable for rule {outcome.decision.rule_id}"
                    )
                if outcome.decision.changed and self.override_started_at is None:
                    self._pause_normal_cycle(current_dt)
                logger.info(
                    "Selected dynamic override: %s rule=%s priority=%s",
                    resolved.instance.name,
                    outcome.decision.rule_id,
                    outcome.decision.priority,
                )
                return DynamicPlaylistRefresh(
                    resolved.playlist,
                    resolved.instance,
                    outcome.decision,
                    force=outcome.decision.changed,
                )
            if outcome.ended_rule_id:
                logger.info("Dynamic override ended: %s", outcome.ended_rule_id)
                return self._return_from_override(
                    outcome.return_behavior, playlist_manager, current_dt
                )
            logger.info("No scheduler rule matched; using playlist")
            return self._normal_playlist_action(
                playlist_manager, self._normal_refresh_clock(latest_refresh), current_dt
            )
        except Exception:
            logger.exception("Dynamic scheduler evaluation failed; using playlist fallback")
            if self.override_started_at is not None:
                return self._return_from_override(
                    "resume_playlist", playlist_manager, current_dt
                )
            return self._normal_playlist_action(
                playlist_manager, self._normal_refresh_clock(latest_refresh), current_dt
            )

    def _normal_playlist_action(self, playlist_manager, latest_refresh, current_dt):
        playlist, plugin_instance = self._determine_next_plugin(
            playlist_manager, latest_refresh, current_dt
        )
        return PlaylistRefresh(playlist, plugin_instance) if plugin_instance else None

    def _pause_normal_cycle(self, current_dt):
        interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
        if self.normal_refresh_time is None:
            remaining = 0
        else:
            elapsed = max(0, (current_dt - self.normal_refresh_time).total_seconds())
            remaining = max(0, interval - elapsed)
        self.normal_cycle_remaining_seconds = remaining
        self.override_started_at = current_dt
        self.interrupted_normal_context = self.last_normal_context

    def _resume_normal_cycle(self, current_dt):
        interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
        remaining = self.normal_cycle_remaining_seconds
        if remaining is not None:
            self._set_normal_refresh_time(
                current_dt - timedelta(seconds=interval - remaining)
            )
        self.override_started_at = None
        self.normal_cycle_remaining_seconds = None

    def _return_from_override(self, behavior, playlist_manager, current_dt):
        context = self.interrupted_normal_context
        self._resume_normal_cycle(current_dt)
        self.interrupted_normal_context = None
        if behavior == "stay_until_next_cycle":
            logger.info("Keeping dynamic image until the next normal playlist cycle")
            # No display action follows this transition, so persist the resumed
            # normal clock explicitly.
            self.device_config.write_config()
            return None
        if behavior == "resume_previous" and context:
            resolved = self._resolve_context(context)
            if resolved and resolved.playlist.is_active(current_dt.strftime("%H:%M")):
                logger.info("Returning to previous plugin: %s", resolved.instance.name)
                return SchedulerReturnRefresh(resolved.playlist, resolved.instance)
        # resume_playlist, or a previous instance which is no longer valid,
        # re-enters normal selection immediately without mutating state first.
        forced_clock = RefreshClock(None)
        return self._normal_playlist_action(playlist_manager, forced_clock, current_dt)

    def _resolve_context(self, context):
        return self.state_provider.resolve(PluginReference(
            instance_id=context.instance_id, playlist=context.playlist_name
        ))

    def _context_from_refresh_info(self, refresh_info):
        if refresh_info.refresh_type != "Playlist" or not refresh_info.playlist or not refresh_info.plugin_instance:
            return None
        playlist = self.device_config.get_playlist_manager().get_playlist(refresh_info.playlist)
        if not playlist:
            return None
        instance = playlist.find_plugin(refresh_info.plugin_id, refresh_info.plugin_instance)
        if not instance:
            return None
        return NormalDisplayContext(playlist.name, instance.instance_id)

    def _normal_refresh_clock(self, fallback):
        return RefreshClock(self.normal_refresh_time)

    def _set_normal_refresh_time(self, value):
        self.normal_refresh_time = value
        self.device_config.get_playlist_manager().normal_refresh_time = (
            value.isoformat() if value else None
        )

    def get_scheduler_status(self, current_dt=None):
        """Return a read-only, user-facing snapshot of scheduler runtime state."""
        current_dt = current_dt or self._get_current_datetime()
        enabled = bool(self.scheduler_config and self.scheduler_config.enabled)
        status = {
            "enabled": enabled,
            "valid": self.scheduler_config_valid,
            "currently_displaying": "Normal Playlist",
            "selected_by": None,
            "priority": None,
            "active_since": None,
            "return_behavior": None,
            "next_evaluation_seconds": None,
            "reason": "Dynamic Scheduler is off" if not enabled else "No dynamic rules currently match",
        }
        if enabled:
            interval = self.scheduler_config.evaluation_interval_seconds
            if self.last_scheduler_evaluation_at is None:
                status["next_evaluation_seconds"] = 0
            else:
                elapsed = max(0, (current_dt - self.last_scheduler_evaluation_at).total_seconds())
                status["next_evaluation_seconds"] = max(0, round(interval - elapsed))
            active_id = self.dynamic_scheduler.state.active_rule_id
            rule = next((item for item in self.scheduler_config.rules if item.id == active_id), None)
            activation = self.dynamic_scheduler.state.activations.get(active_id)
            if rule and activation:
                resolved = self.state_provider.resolve(rule.target)
                status.update({
                    "currently_displaying": resolved.instance.name if resolved else rule.target.plugin_instance,
                    "selected_by": rule.name or rule.id,
                    "rule_id": rule.id,
                    "priority": rule.priority,
                    "active_since": activation.activated_at.isoformat(),
                    "return_behavior": rule.return_behavior,
                    "reason": "Selected by the highest-priority active rule",
                })
        if not self.scheduler_config_valid:
            status["reason"] = "Scheduler configuration is invalid; using normal playlist"
        return status

    def _get_current_datetime(self):
        """Retrieves the current datetime based on the device's configured timezone."""
        tz_str = self.device_config.get_config("timezone", default="UTC")
        return datetime.now(pytz.timezone(tz_str))

    def _determine_next_plugin(self, playlist_manager, latest_refresh_info, current_dt):
        """Determines the next plugin to refresh based on the active playlist, plugin cycle interval, and current time."""
        playlist = playlist_manager.determine_active_playlist(current_dt)
        if not playlist:
            playlist_manager.active_playlist = None
            logger.info(f"No active playlist determined.")
            return None, None

        playlist_manager.active_playlist = playlist.name
        if not playlist.plugins:
            logger.info(f"Active playlist '{playlist.name}' has no plugins.")
            return None, None

        latest_refresh_dt = latest_refresh_info.get_refresh_datetime()
        plugin_cycle_interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
        should_refresh = PlaylistManager.should_refresh(latest_refresh_dt, plugin_cycle_interval, current_dt)

        if not should_refresh:
            latest_refresh_str = latest_refresh_dt.strftime('%Y-%m-%d %H:%M:%S') if latest_refresh_dt else "None"
            logger.info(f"Not time to update display. | latest_update: {latest_refresh_str} | plugin_cycle_interval: {plugin_cycle_interval}")
            return None, None

        plugin = playlist.get_next_plugin()
        logger.info(f"Determined next plugin. | active_playlist: {playlist.name} | plugin_instance: {plugin.name}")

        return playlist, plugin
    
    def log_system_stats(self):
        metrics = {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'load_avg_1_5_15': os.getloadavg(),
            'swap_percent': psutil.swap_memory().percent,
            'net_io': {
                'bytes_sent': psutil.net_io_counters().bytes_sent,
                'bytes_recv': psutil.net_io_counters().bytes_recv
            }
        }

        logger.info(f"System Stats: {metrics}")

class RefreshAction:
    """Base class for a refresh action. Subclasses should override the methods below."""
    
    def refresh(self, plugin, device_config, current_dt):
        """Perform a refresh operation and return the updated image."""
        raise NotImplementedError("Subclasses must implement the refresh method.")
    
    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        raise NotImplementedError("Subclasses must implement the get_refresh_info method.")
    
    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        raise NotImplementedError("Subclasses must implement the get_plugin_id method.")

class ManualRefresh(RefreshAction):
    """Performs a manual refresh based on a plugin's ID and its associated settings.
    
    Attributes:
        plugin_id (str): The ID of the plugin to refresh.
        plugin_settings (dict): The settings for the manual refresh.
    """

    def __init__(self, plugin_id: str, plugin_settings: dict):
        self.plugin_id = plugin_id
        self.plugin_settings = plugin_settings

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a manual refresh using the stored plugin ID and settings."""
        return plugin.generate_image(self.plugin_settings, device_config)

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {"refresh_type": "Manual Update", "plugin_id": self.plugin_id}

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_id

class PlaylistRefresh(RefreshAction):
    """Performs a refresh using a plugin instance within a playlist context.

    Attributes:
        playlist: The playlist object associated with the refresh.
        plugin_instance: The plugin instance to refresh.
    """

    def __init__(self, playlist, plugin_instance, force=False):
        self.playlist = playlist
        self.plugin_instance = plugin_instance
        self.force = force
        self.affects_normal_cycle = True

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {
            "refresh_type": "Playlist",
            "playlist": self.playlist.name,
            "plugin_id": self.plugin_instance.plugin_id,
            "plugin_instance": self.plugin_instance.name
        }

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_instance.plugin_id

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a refresh for the specified plugin instance within its playlist context."""
        # Determine the file path for the plugin's image
        plugin_image_path = os.path.join(device_config.plugin_image_dir, self.plugin_instance.get_image_path())

        # Check if a refresh is needed based on the plugin instance's criteria
        if self.plugin_instance.should_refresh(current_dt) or self.force:
            logger.info(f"Refreshing plugin instance. | plugin_instance: '{self.plugin_instance.name}'") 
            # Generate a new image
            image = plugin.generate_image(self.plugin_instance.settings, device_config)
            image.save(plugin_image_path)
            self.plugin_instance.latest_refresh_time = current_dt.isoformat()
        else:
            logger.info(f"Not time to refresh plugin instance, using latest image. | plugin_instance: {self.plugin_instance.name}.")
            # Load the existing image from disk
            with Image.open(plugin_image_path) as img:
                image = img.copy()

        return image


class DynamicPlaylistRefresh(PlaylistRefresh):
    """A scheduler selection using the ordinary playlist execution path."""

    def __init__(self, playlist, plugin_instance, decision, force=False):
        super().__init__(playlist, plugin_instance, force=force)
        self.decision = decision
        self.affects_normal_cycle = False

    def get_refresh_info(self):
        info = super().get_refresh_info()
        info["refresh_type"] = "Dynamic Scheduler"
        return info


class SchedulerReturnRefresh(PlaylistRefresh):
    """Restore an interrupted normal item without advancing its cycle clock."""

    def __init__(self, playlist, plugin_instance):
        super().__init__(playlist, plugin_instance)
        self.affects_normal_cycle = False

    def get_refresh_info(self):
        info = super().get_refresh_info()
        info["refresh_type"] = "Scheduler Return"
        return info
