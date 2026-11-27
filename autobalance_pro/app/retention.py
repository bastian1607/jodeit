"""AB-026: retention of customer data on the mobile measuring device. No database access here.

Decision Bastian/Jodeit, 2026-11-27: customer-related data of a measuring job are deleted from the
device automatically 30 calendar days after the job ended. This is an internal rule, not a legal
deadline. The deadline starts at the persisted ``ended_at`` and is never reset (export, report,
restart and configuration changes do not extend it). A running job has no deadline.

Deletion register: ids (random, non-personal) of deleted jobs are also written to a small JSON file
outside the app's data folder (``/share/autobalance``). After restoring an app-only backup the app
deletes those jobs again on start. Jobs whose deadline has passed are deleted on start anyway.
"""
import json
import os
from datetime import timedelta
from pathlib import Path

RETENTION_DAYS = 30          # after the end of a job
WARN_DAYS = (7, 1)           # notices before the deadline
LOOSE_DATA_DAYS = 30         # passive samples, configuration history and app events outside jobs
PURGE_INTERVAL_SECONDS = 3600

OPTIONS_FILE = Path(os.environ.get("AUTO_BALANCE_OPTIONS", "/data/options.json"))


def mobile_kit(path=None):
    """App option ``messkoffer``: the device is the mobile Jodeit measuring kit that moves between customers.

    Only then are jobs deleted automatically 30 days after their end and the customer configuration neutralised
    (decision Bastian, 27.11.2026). Default off: a stationary installation (e.g. a private Home Assistant) keeps its
    jobs and configuration; manual deletion and "prepare for next customer" work everywhere."""
    try:
        return json.loads(Path(path or OPTIONS_FILE).read_text()).get("messkoffer") is True
    except (OSError, ValueError, AttributeError):
        return False


def config_expired(last_change, jobs_left, expired_job_ends, now):
    """Mobile kit: does the stored customer configuration belong only to expired jobs?

    True if no job (running or stopped) is left and the configuration was last changed no later than the end
    of a job whose deadline has passed. A configuration without any expired job is never removed (no idle
    deadline, review 9). ``now`` is kept for callers and future rules."""
    if jobs_left or last_change is None:
        return False
    return any(last_change <= end for end in expired_job_ends)


REGISTER_DIR = Path(os.environ.get("AUTO_BALANCE_REGISTER_DIR", "/share/autobalance"))
REGISTER_FILE = "geloeschte-messungen.json"


def due_at(ended_at):
    """Deletion deadline for a stopped job (aware datetime) or None for a running one."""
    return None if ended_at is None else ended_at + timedelta(days=RETENTION_DAYS)


def notice(due, archived, now):
    """Notice level before the deadline: None, "week" (≤ 7 days) or "day" (≤ 1 day)."""
    if due is None:
        return None
    left = (due - now).total_seconds()
    if left <= WARN_DAYS[1] * 86400:
        return "day"
    if left <= WARN_DAYS[0] * 86400 and not archived:
        return "week"
    return None


REGISTER_STATUS_TEXT = {
    "ok": "Löschregister in /share/autobalance geführt.",
    "missing": "Löschregister noch nicht angelegt (noch keine Löschung).",
    "unavailable": "Löschregister in /share/autobalance nicht beschreibbar. Nach einer reinen App-Wiederherstellung "
                   "werden bereits gelöschte Messungen erst zu ihrer Frist erneut gelöscht.",
    "corrupt": "Löschregister war beschädigt. Es wurde beiseitegelegt und aus dem Löschprotokoll neu aufgebaut.",
}


def _path(directory=None):
    return Path(directory or REGISTER_DIR) / REGISTER_FILE


def read_register_status(directory=None):
    """(entries, status) with status "ok", "missing", "unavailable" or "corrupt"."""
    path = _path(directory)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return [], "missing"
    except OSError:
        return [], "unavailable"
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("no list")
    except ValueError:
        return [], "corrupt"
    return [x for x in data if isinstance(x, dict) and isinstance(x.get("id"), str)], "ok"


def read_register(directory=None):
    return read_register_status(directory)[0]


def write_register(entries, directory=None, stamp=""):
    """Merge ``entries`` ({id, deleted_at, reason}) into the register. Returns the resulting status.

    A corrupt file is never silently overwritten: it is moved aside first and the register is rebuilt
    from the existing entries plus ``entries`` (callers pass the full local deletion log)."""
    folder = Path(directory or REGISTER_DIR)
    if not folder.parent.exists():
        return "unavailable"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        current, status = read_register_status(folder)
        if status == "unavailable":
            return status
        if status == "corrupt":
            _path(folder).replace(folder / f"{REGISTER_FILE}.defekt-{stamp or 'x'}")
        known = {x["id"] for x in current}
        merged = list(current)
        for entry in entries:
            if entry["id"] not in known:
                merged.append({"id": entry["id"], "deleted_at": entry["deleted_at"], "reason": entry["reason"]})
                known.add(entry["id"])
        tmp = folder / (REGISTER_FILE + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
        tmp.replace(_path(folder))
        return "corrupt" if status == "corrupt" else "ok"
    except OSError:
        return "unavailable"


def append_register(entry, directory=None):
    """Backward-compatible single append. True if the register holds the entry afterwards."""
    return write_register([entry], directory) in ("ok", "corrupt")
