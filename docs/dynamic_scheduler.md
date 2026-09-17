# Dynamic Scheduler

The Dynamic Scheduler can temporarily select an existing plugin instance using
state, time, priority, duration, and cooldown rules. Ordinary playlists remain
the default and fallback. Scheduling is disabled by default.

The scheduler chooses **what** to show. Existing plugin loading, image
generation, instance-image caching, image hashing, `DisplayManager`, and display
drivers still determine **how** it is shown. It is not a display plugin.

## Enable and configure

Open **Settings**, expand **Dynamic Scheduler**, and use **Add Rule**. The visual
editor offers configured display instances, plugin state fields, type-aware
operators and values, time windows, priority, duration, cooldown, and return
behavior. Friendly names are shown while stable instance IDs are persisted.

Saved rules appear as readable cards. **Test** evaluates one rule without
changing the display, timers, cooldowns, or playlist state. **Test All** shows
every match and the deterministic winner. **View Current State** shows only the
small normalized state contract exposed by each plugin. The Current Status
panel explains whether a rule or the normal playlist selected the display.

The original JSON representation remains under **Advanced JSON**. Both editors
operate on that same persisted object; there is no second configuration model.
Invalid JSON or rules are rejected without replacing the last valid settings.

Configuration is stored under `dynamic_scheduler` in `src/config/device.json`
(or `src/config/device_dev.json` in development mode):

```json
{
  "dynamic_scheduler": {
    "enabled": true,
    "version": 1,
    "evaluation_interval_seconds": 60,
    "defaults": {
      "fallback": "playlist",
      "return_behavior": "resume_previous",
      "minimum_display_seconds": 60
    },
    "rules": []
  }
}
```

Scheduler evaluation cadence is independent from
`plugin_cycle_interval_seconds`. Dynamic displays neither advance nor reset the
normal playlist clock or `current_plugin_index`. While an override is active,
the remaining normal cycle is paused.

## Rule schema

```json
{
  "id": "live_baseball",
  "name": "Late Close Braves Game",
  "enabled": true,
  "priority": 80,
  "target": {
    "instance_id": "STABLE-INSTANCE-UUID",
    "plugin_id": "mlb_live_score",
    "plugin_instance": "Braves Scoreboard",
    "playlist": "Default"
  },
  "conditions": {
    "all": [
      {
        "source_instance_id": "STABLE-INSTANCE-UUID",
        "source": "Braves Scoreboard",
        "path": "game.status",
        "operator": "eq",
        "value": "live"
      }
    ]
  },
  "duration": {
    "mode": "while_true",
    "min_seconds": 120,
    "max_seconds": 14400
  },
  "cooldown_seconds": 0,
  "return_behavior": "resume_previous"
}
```

`instance_id` is the preferred target and source identifier. The descriptive
fields are optional and useful when reading configuration. Legacy references
can use `plugin_instance`, optionally narrowed by `plugin_id` and `playlist`,
but ambiguous references are rejected.

Conditions compose with `all`, `any`, and `not`. State leaves use dot paths and
support:

- `eq`, `ne`
- `gt`, `gte`, `lt`, `lte`
- `in`, `not_in`, `contains`
- `exists`, `truthy`, `falsy`

Missing paths, incompatible values, unavailable providers, and provider
exceptions do not match. They do not stop playlist operation.

Scheduler-native time conditions need no plugin state:

```json
{
  "time": {
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "start": "07:00",
    "end": "09:00"
  }
}
```

Times use the configured device timezone. An end earlier than the start is an
overnight window, such as `22:00` through `02:00`. The end is exclusive.

## Recurring schedules and one-time displays

A time **condition** stays true throughout a window. A rule-level `schedule`
instead represents an **occurrence**: a moment that can temporarily display any
plugin, without that plugin exposing scheduler state. In the visual editor,
choose a Temporal Schedule type, then set Display for in minutes or seconds.
Occurrence schedules automatically select fixed duration (default two minutes).
Additional plugin-state conditions may still restrict an occurrence.

The optional `schedule` field supports these shapes:

```json
{"type": "minute_of_hour", "minutes": [5]}
{"type": "minute_of_hour", "minutes": [0, 20, 40]}
{"type": "interval", "every_minutes": 30}
{"type": "daily", "times": ["08:00", "12:00", "18:00"]}
{"type": "once", "at": "2026-12-31T23:55"}
```

All schedules can include optional restrictions: `days` (`mon` through `sun`),
`hours` (0–23), `months` (1–12), `days_of_month` (1–31), `start`/`end` (HH:MM),
and inclusive ISO `date_start`/`date_end`. These restrictions are also supported
in existing `time` conditions. Arrays must be nonempty if supplied; omission
means unrestricted. Invalid dates are never invented: day 31 simply has no
occurrence in a month without that day. For first-of-month scheduling, combine
daily times with `days_of_month: [1]`.

Intervals are aligned to local midnight, not service startup. Every 15 minutes
means :00, :15, :30, :45; 60 means the top of each hour. Values 1–1440 are
accepted. Intervals not dividing 1440 restart their alignment each day.

Windows are start-inclusive/end-exclusive: 08:00–17:00 allows 08:05 but not
17:05. For overnight 22:00–02:00, the after-midnight portion belongs to the
starting day for weekday/month/date restrictions. Explicit `hours` always refer
to the actual hour. Start equal to end is an empty window; omit both for all day.

Example B, suitable for Advanced JSON after substituting an instance ID:

```json
{
  "id": "weekday_countdown",
  "name": "Weekday Countdown",
  "enabled": true,
  "priority": 30,
  "target": {"instance_id": "COUNTDOWN-INSTANCE-ID"},
  "schedule": {
    "type": "minute_of_hour", "minutes": [0, 30],
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "start": "08:00", "end": "17:00"
  },
  "conditions": {"time": {}},
  "duration": {"mode": "fixed", "seconds": 180},
  "cooldown_seconds": 0,
  "return_behavior": "resume_previous"
}
```

Other acceptance examples use the same fields:

- A: Year Progress, `minute_of_hour` with `[5]`, fixed 120 seconds, priority 20.
- C: Year Progress, `daily` with `["08:00", "12:00", "18:00"]`, fixed 120 seconds.
- D: Countdown, `once` at `2026-12-31T23:55`, fixed 600 seconds.

The visual editor writes configuration version 2 when saving a scheduled rule.
Versions 1 and 2 both load, including unchanged legacy window-only rules.
`conditions` may be omitted for a schedule-only rule; it defaults to true.
An omitted duration on a scheduled rule defaults to fixed 120 seconds. Explicit
`while_true` remains accepted, but an occurrence is a pulse, not a persistent
condition; fixed duration is recommended.

### Occurrences, priority, restart, and DST

The evaluator catches occurrences between the previous evaluation and now, so
10:04:48 → 10:05:43 catches :05 without evaluating at the exact second. A local
scheduled-minute key (YYYY-MM-DDTHH:MM) prevents repeated activation. If several
occurrences passed during a long gap, only the latest is considered. There is
no backlog or grace-period queue.

Occurrences are consumed even if blocked by priority, cooldown, manual Display
Now, or additional false conditions. A missed low-priority display never appears
later after the higher-priority display ends. An occurrence while the same rule
is already active does not extend its existing fixed timer. Existing return
behaviors and image-hash protection remain unchanged. Scheduler polling does
not change the normal playlist index or cycle clock.

History is transient. On startup only occurrences in the current local minute
are eligible; earlier occurrences are not replayed. Restarting in the same
scheduled minute can therefore activate that occurrence again. Fixed timers
remain transient too. One-time rules stay configured after firing but do not
fire again during the same runtime.

Device timezone is used throughout. Nonexistent spring-forward times are
skipped; ambiguous fall-back times use the first occurrence only. The duplicate
local minute is not fired twice. An offset-qualified `once.at` is accepted in
Advanced JSON and converted to device timezone; editing that form remains
Advanced-only to avoid silently changing its meaning.

Test Rule/Test All are observational: they report current time, due/consumed
occurrence and next occurrence without consuming anything or starting timers.
Rule cards and Current Status show upcoming triggers. Test All reports current
trigger/condition eligibility, not a prediction that bypasses active fixed
timers or cooldowns. Future occurrence searches are bounded to four years.

Frequent schedules (especially every minute) are discouraged for e-ink; the
editor warns for intervals below five minutes. Every N hours can be expressed
with selected hours; first-of-month and seasonal/date ranges are supported.
Week-of-month, last day, last weekday, and nth weekday schedules are deferred
to avoid creating a cron replacement.

Optional repeatable browser acceptance check (requires Chromium plus configured
Countdown and Year Progress instances): start `.venv/bin/python src/inkypi.py
--dev`, then run `.venv/bin/python scripts/verify_temporal_builder.py` in another
terminal. It exercises examples A–D and editor/JSON/diagnostic round-trips using
server validation, but intercepts saves so no acceptance rules are persisted.
The dev server itself can update ordinary dev display/refresh metadata.

## Priority, duration, and return behavior

Higher integer priority wins. At equal priority, the active rule remains active
when still valid; otherwise configuration order wins. Selection is never random.

- `while_true` remains active while conditions match. `min_seconds` prevents a
  short false transition from flapping the display. `max_seconds` caps one
  activation and requires the condition to become false before rearming.
- `fixed` requires `seconds`. Once selected it remains eligible for that wall
  clock duration even when its trigger disappears, unless preempted by a higher
  priority rule.
- `cooldown_seconds` prevents a completed activation from immediately starting
  again.

Preempted rules remain tracked. If a priority-80 rule interrupts a valid
priority-60 rule, the lower rule resumes when the higher rule ends.

Return modes are:

- `resume_previous`: restore the interrupted normal instance, if it still
  exists and its playlist is active, without advancing the playlist.
- `resume_playlist`: immediately select the ordinary playlist's next item.
- `stay_until_next_cycle`: leave the override image displayed until the paused
  normal cycle becomes due.

Manual **Display Now** has first priority and is held until the next ordinary
cycle. Scheduler rules follow, then normal playlist selection.

## Plugin state contract

A plugin does not need scheduler-specific code unless rules need its domain
state. Any existing plugin can be targeted by a time-only rule.

Plugins may override the value and metadata methods:

```python
def get_scheduler_state(self, settings, device_config, current_dt):
    return {
        "domain": {
            "active": True
        }
    }

def get_scheduler_state_schema(self):
    return {
        "domain.active": {
            "label": "Domain is active",
            "type": "boolean",
            "description": "Optional help shown by the visual editor."
        }
    }
```

Both default `BasePlugin` implementations return `{}`. Metadata supports
`boolean`, `number`, `probability` (stored from 0 through 1 but displayed as a
percentage), `enum` with `allowed_values`, `string`, and `datetime`. State
should be small, normalized, and independent of rendering. Reuse cached domain
data when possible; do not generate an image just to obtain state. The
scheduler queries only sources referenced by enabled rules and deduplicates a
source within one evaluation. Plugins without metadata remain valid targets,
including for time-only rules; advanced JSON can still reference a known state
path.

This checkout exposes:

- MLB: lifecycle flags, selected team, inning/half, selected/opponent scores,
  absolute score difference, lead/trail/tie flags, close-game flag, start time,
  and whether the selected team has a game today. `game.is_close` means a live
  game whose absolute run difference is two or fewer.
- Sleeper: displayed/live/close matchup counts, lead/trail counts, minimum and
  maximum win probability, distance from 50%, and live starter count. A matchup
  is live only when at least one starter on the user's or opponent's lineup is
  mapped to an NFL game whose actual schedule state is live. Bench players,
  future/final games, fantasy points, and an active NFL week do not qualify.

Sleeper reuses its existing projection mapping (player ID to NFL game ID) and
NFL schedule adapter. Projection rows are cached for 20 minutes; actual game
status is refreshed at most once per 60 seconds. Plugin-level normalized MLB
and Sleeper models are retained for 30 seconds so evaluation followed by a
render does not immediately repeat domain requests. Unknown player/game state
is treated as unavailable, never assumed live.

## Examples

Use the stable IDs shown on the Settings page in place of the placeholders.

```json
[
  {
    "id": "live_baseball",
    "priority": 80,
    "target": {"instance_id": "MLB_ID"},
    "conditions": {"source_instance_id": "MLB_ID", "path": "game.is_live", "operator": "truthy"},
    "duration": {"mode": "while_true", "min_seconds": 120},
    "return_behavior": "resume_previous"
  },
  {
    "id": "fantasy_live",
    "priority": 60,
    "target": {"instance_id": "SLEEPER_ID"},
    "conditions": {"source_instance_id": "SLEEPER_ID", "path": "matchups.live_count", "operator": "gte", "value": 1},
    "duration": {"mode": "while_true", "min_seconds": 120},
    "return_behavior": "resume_previous"
  },
  {
    "id": "weekday_weather",
    "priority": 30,
    "target": {"instance_id": "WEATHER_ID"},
    "conditions": {"time": {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "07:00", "end": "09:00"}},
    "duration": {"mode": "while_true"},
    "return_behavior": "resume_playlist"
  },
  {
    "id": "future_emergency",
    "enabled": false,
    "priority": 100,
    "target": {"instance_id": "EMERGENCY_ID"},
    "conditions": {"source_instance_id": "EMERGENCY_ID", "path": "emergency.active", "operator": "truthy"},
    "duration": {"mode": "fixed", "seconds": 300},
    "cooldown_seconds": 300
  }
]
```

These are examples only and are not installed as default rules.

## Product scope

Global quiet hours and rotation among equal-priority matching rules are not in
version 1. Quiet hours would add suppression and per-rule bypass semantics to
the scheduler engine, so it was deliberately deferred instead of changing the
already-tested lifecycle model. Equal-priority rules continue to use the
stable active-rule/configuration-order policy. A future rotation mode can be
implemented as an explicit tie-resolution policy without changing condition
or plugin-state contracts.

## Failure and restart behavior

Disabled, unmatched, or invalid scheduling falls back to the existing playlist.
State errors and unexpected recoverable scheduler errors are logged and also
fall back safely. Normal INFO logs show matches, selections, preemption, ending,
return behavior, and fallback without dumping state payloads.

Configuration and the independent normal playlist clock persist. Active rule
stacks, cooldowns, and fixed-duration timers do not. On service restart the
scheduler starts with empty transient state and immediately reevaluates current conditions;
a fixed-duration condition that is still true starts a new timer.

## Development scenarios

Run deterministic, API-free decision scenarios:

```bash
.venv/bin/python scripts/scheduler_scenarios.py none
.venv/bin/python scripts/scheduler_scenarios.py mlb_live
.venv/bin/python scripts/scheduler_scenarios.py sleeper_live
.venv/bin/python scripts/scheduler_scenarios.py both_live
.venv/bin/python scripts/scheduler_scenarios.py higher_priority_interrupt
.venv/bin/python scripts/scheduler_scenarios.py condition_expires
.venv/bin/python scripts/scheduler_scenarios.py fixed_duration
.venv/bin/python scripts/scheduler_scenarios.py provider_failure
```

For an end-to-end mock display, start `.venv/bin/python src/inkypi.py --dev`,
open `http://127.0.0.1:8080/settings`, expand **Dynamic Scheduler**, and create
a rule through the visual editor. A time-only rule whose window includes the
current device time is deterministic and needs no external sports event. Watch
the Current Status/Test panels, terminal decision logs, and
`mock_display_output/latest.png`.
