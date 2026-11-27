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


def read_register(directory=None):
    path = Path(directory or REGISTER_DIR) / REGISTER_FILE
    try:
        data = json.loads(path.read_text())
        return [x for x in data if isinstance(x, dict) and isinstance(x.get("id"), str)]
    except (OSError, ValueError):
        return []


def append_register(entry, directory=None):
    """Add {id, deleted_at, reason} (no personal data). Returns False if the folder is not available."""
    folder = Path(directory or REGISTER_DIR)
    if not folder.parent.exists():
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        entries = read_register(folder)
        if not any(x["id"] == entry["id"] for x in entries):
            entries.append(entry)
        tmp = folder / (REGISTER_FILE + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
        tmp.replace(folder / REGISTER_FILE)
        return True
    except OSError:
        return False
