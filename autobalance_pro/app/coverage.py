"""AB-020: measurement gaps and data coverage. Pure functions, no I/O.

Design decisions (ChatGPT review 2026-11-26, thresholds are proposals):
  * A "tick" is one acquisition round that stored samples (one distinct `samples.at`).
  * A gap is a period of more than WARN_SECONDS without a stored tick, between two
    ticks or between the last tick and now. Gaps are derived from stored data only,
    so they survive restarts and are never filled by interpolation.
  * Coverage per entity counts only its own active time (first sample in the window
    until now), so a sensor added later does not create a retroactive gap.
"""
from datetime import datetime, timedelta

INTERVAL_SECONDS = 300          # acquisition cycle of the app
DELAYED_SECONDS = 600           # > 10 min: delayed
WARN_SECONDS = 900              # > 15 min: interrupted, gap is recorded
CRITICAL_SECONDS = 3600         # > 60 min: critical
LOOP_STALL_SECONDS = 3600       # own loop made no attempt at all for this long


def parse(ts):
    """ISO timestamp with time zone -> aware datetime, else None."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def status_for_age(age_seconds):
    if age_seconds is None:
        return "no_data"
    if age_seconds > CRITICAL_SECONDS:
        return "critical"
    if age_seconds > WARN_SECONDS:
        return "warning"
    if age_seconds > DELAYED_SECONDS:
        return "delayed"
    return "ok"


def find_gaps(ticks, now, window_start):
    """Gaps longer than WARN_SECONDS inside [window_start, now].

    ``ticks``: ascending aware datetimes. Time before the first tick is not a gap
    (nothing was configured or measured yet).
    """
    gaps = []
    ticks = [t for t in ticks if t is not None]
    for previous, current in zip(ticks, ticks[1:]):
        if (current - previous).total_seconds() > WARN_SECONDS and current > window_start:
            start = max(previous, window_start)
            gaps.append({"start": start, "end": current, "ongoing": False})
    if ticks and (now - ticks[-1]).total_seconds() > WARN_SECONDS:
        gaps.append({"start": max(ticks[-1], window_start), "end": now, "ongoing": True})
    for gap in gaps:
        gap["minutes"] = round((gap["end"] - gap["start"]).total_seconds() / 60)
    return gaps


def expected_ticks(start, end):
    seconds = max(0.0, (end - start).total_seconds())
    return int(seconds // INTERVAL_SECONDS) + 1


def coverage(count, start, end):
    """Share of expected ticks that are present, 0..1 (rounded to 3 places)."""
    if start is None:
        return None
    return round(min(1.0, count / expected_ticks(start, end)), 3)


def analyse(tick_rows, entity_rows, last_attempt, now, hours):
    """Build the data-quality report.

    ``tick_rows``: iterable of `at` strings (distinct ticks in the window, ascending).
    ``entity_rows``: iterable of (entity_id, first_at, samples, valid_samples) in the window.
    """
    window_start = now - timedelta(hours=hours)
    ticks = sorted(t for t in (parse(x) for x in tick_rows) if t is not None)
    last_tick = ticks[-1] if ticks else None
    age = (now - last_tick).total_seconds() if last_tick else None
    gaps = find_gaps(ticks, now, window_start)
    in_window = [t for t in ticks if t >= window_start]   # caller may add the last tick before the window
    # Measurement already existed before the window -> the whole window is expected.
    first = window_start if ticks and ticks[0] < window_start else (in_window[0] if in_window else None)
    entities = []
    for entity_id, first_at, samples, valid in entity_rows:
        start = parse(first_at)
        entities.append({
            "entity_id": entity_id, "first_sample": first_at,
            "samples": samples, "valid_samples": valid,
            "expected_samples": expected_ticks(start, now) if start else 0,
            "coverage": coverage(samples, start, now),
            "valid_coverage": coverage(valid, start, now),
        })
    attempt = parse(last_attempt)
    attempt_age = (now - attempt).total_seconds() if attempt else None
    return {
        "window_hours": hours,
        "window_start": window_start.isoformat(),
        "now": now.isoformat(),
        "status": status_for_age(age),
        "last_tick": last_tick.isoformat() if last_tick else None,
        "last_tick_age_seconds": round(age) if age is not None else None,
        "loop_stalled": attempt_age is None or attempt_age > LOOP_STALL_SECONDS,
        "coverage": coverage(len(in_window), first, now),
        "gap_minutes_total": sum(g["minutes"] for g in gaps),
        "gaps": [dict(g, start=g["start"].isoformat(), end=g["end"].isoformat()) for g in gaps],
        "entities": entities,
        "thresholds": {"interval": INTERVAL_SECONDS, "delayed": DELAYED_SECONDS,
                       "warning": WARN_SECONDS, "critical": CRITICAL_SECONDS},
    }
