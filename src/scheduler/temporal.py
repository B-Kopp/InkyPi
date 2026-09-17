"""Pure local-calendar restrictions and minute-resolution occurrences.

No clocks, plugin data, lifecycle mutations, or display operations live here.
"""

from datetime import date, datetime, time, timedelta, timezone

import pytz


SCHEDULE_TYPES = {"minute_of_hour", "interval", "daily", "once"}


def restrictions_match(config, current_dt):
    start, end = config.get("start"), config.get("end")
    wall_time = current_dt.strftime("%H:%M")
    day = current_dt.date()
    if start and end and start > end and wall_time < end:
        day -= timedelta(days=1)
    if config.get("days") and day.strftime("%a").lower() not in {d.lower() for d in config["days"]}:
        return False
    if config.get("months") and day.month not in config["months"]:
        return False
    if config.get("days_of_month") and day.day not in config["days_of_month"]:
        return False
    if config.get("hours") and current_dt.hour not in config["hours"]:
        return False
    if config.get("date_start") and day < date.fromisoformat(config["date_start"]):
        return False
    if config.get("date_end") and day > date.fromisoformat(config["date_end"]):
        return False
    if start is None:
        return True
    return start <= wall_time < end if start <= end else wall_time >= start or wall_time < end


def localize(naive, tz):
    """Skip nonexistent times; use the first occurrence of ambiguous times."""
    if hasattr(tz, "localize"):
        try:
            return tz.localize(naive, is_dst=None)
        except pytz.AmbiguousTimeError:
            return tz.localize(naive, is_dst=True)
        except pytz.NonExistentTimeError:
            return None
    aware = naive.replace(tzinfo=tz, fold=0)
    if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return aware


def instant(value):
    if value.tzinfo is None:
        raise ValueError("Temporal scheduling requires timezone-aware current_dt")
    return value.astimezone(timezone.utc)


def _minutes(schedule):
    kind = schedule["type"]
    if kind == "minute_of_hour":
        return sorted(hour * 60 + minute for hour in range(24) for minute in set(schedule["minutes"]))
    if kind == "interval":
        return range(0, 1440, schedule["every_minutes"])
    return sorted({int(value[:2]) * 60 + int(value[3:]) for value in schedule["times"]})


def _on_day(schedule, day, tz):
    # Reject impossible calendar dates before expanding up to 1440 wall minutes.
    # Overnight windows may anchor their early hours to the preceding date.
    calendar = {key: schedule[key] for key in ("days", "months", "days_of_month", "date_start", "date_end") if key in schedule}
    overnight = schedule.get("start", "") > schedule.get("end", "")
    anchors = (day, day - timedelta(days=1)) if overnight else (day,)
    if not any(restrictions_match(calendar, datetime.combine(anchor, time())) for anchor in anchors):
        return
    for minute in _minutes(schedule):
        wall = datetime.combine(day, time(minute // 60, minute % 60))
        if not restrictions_match(schedule, wall):
            continue
        occurrence = localize(wall, tz)
        if occurrence is not None:
            yield occurrence


def _once(schedule, tz):
    value = datetime.fromisoformat(schedule["at"])
    return value.astimezone(tz) if value.tzinfo else localize(value, tz)


def occurrence_key(occurrence):
    # Local minute identity deliberately ignores the repeated DST fold.
    return occurrence.strftime("%Y-%m-%dT%H:%M")


def latest_occurrence(schedule, current_dt, previous_dt=None):
    """Latest occurrence in (previous, now], or current minute on cold start."""
    now = instant(current_dt)
    lower = instant(previous_dt) if previous_dt is not None else now.replace(second=0, microsecond=0) - timedelta(microseconds=1)
    if lower >= now:
        return None
    if schedule["type"] == "once":
        occurrence = _once(schedule, current_dt.tzinfo)
        return occurrence if occurrence and lower < instant(occurrence) <= now and restrictions_match(schedule, occurrence) else None
    first_day = lower.astimezone(current_dt.tzinfo).date()
    day = current_dt.date()
    while day >= first_day:
        candidates = [value for value in _on_day(schedule, day, current_dt.tzinfo) if lower < instant(value) <= now]
        if candidates:
            return max(candidates, key=instant)
        day -= timedelta(days=1)
    return None


def next_occurrence(schedule, current_dt):
    """Next strictly future occurrence, searching at most four calendar years."""
    now = instant(current_dt)
    if schedule["type"] == "once":
        value = _once(schedule, current_dt.tzinfo)
        return value if value and instant(value) > now and restrictions_match(schedule, value) else None
    day = current_dt.date()
    for _ in range(1462):
        for value in _on_day(schedule, day, current_dt.tzinfo):
            if instant(value) > now:
                return value
        if schedule.get("date_end") and day > date.fromisoformat(schedule["date_end"]) + timedelta(days=1):
            break
        day += timedelta(days=1)
    return None


def schedule_summary(schedule):
    kind = schedule["type"]
    if kind == "minute_of_hour":
        base = "at " + ", ".join(f":{minute:02d}" for minute in sorted(set(schedule["minutes"]))) + " past every hour"
    elif kind == "interval":
        base = f"every {schedule['every_minutes']} minutes"
    elif kind == "daily":
        base = "at " + ", ".join(schedule["times"])
    else:
        base = "once on " + schedule["at"].replace("T", " at ")
    restrictions = []
    for key, label in (("days", "days"), ("hours", "hours"), ("days_of_month", "days of month"), ("months", "months")):
        if schedule.get(key):
            restrictions.append(label + " " + ", ".join(map(str, schedule[key])))
    if schedule.get("start"):
        restrictions.append(f"from {schedule['start']} to {schedule['end']}")
    if schedule.get("date_start") or schedule.get("date_end"):
        restrictions.append(f"dates {schedule.get('date_start', 'any')} through {schedule.get('date_end', 'any')}")
    return base + (", " + ", ".join(restrictions) if restrictions else "")
