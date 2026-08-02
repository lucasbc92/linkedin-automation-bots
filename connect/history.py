"""Durable ledger of every invitation confirmed sent.

Counting invitations by grepping the weekly log is fragile: the count breaks
when the log level hides INFO lines, when log files are moved between
directories, or when the run is killed before the summary is written. This
file is append-only and written the instant an invite is confirmed, so a
Ctrl+C never loses a count.

One JSON object per line (JSONL) — appending can't corrupt earlier entries:

    {"ts": "2026-07-25T04:12:33-03:00", "name": "Ana Silva", "week": "2026-07-19"}
"""

import json
import logging
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from common.logging_setup import BRT, current_week_start

logger = logging.getLogger("linkedin_bot")

# Local, gitignored record of every invitation across all runs.
DEFAULT_INVITE_FILE = Path(__file__).parent / ".invites.jsonl"

# Matches the log line emitted on a confirmed invite, used to backfill the
# ledger from logs written before it existed:
#   2026-07-20 03:37:31 [INFO ] Invitation sent to Anielle [sent=24, ...]
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[\w+\s*\] "
    r"Invitation sent to (?P<name>.+?) \[sent=")


def record_invite(name, path=DEFAULT_INVITE_FILE, now=None):
    """Append one confirmed invitation to the ledger. Never raises."""
    ts = now or datetime.now(BRT)
    entry = {
        "ts": ts.isoformat(timespec="seconds"),
        "name": name or "",
        "week": current_week_start(ts).isoformat(),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"Could not record invitation in {path}: {e}")
    return entry


def load_invites(path=DEFAULT_INVITE_FILE):
    """Return every ledger entry, skipping any line that got truncated."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning(f"Could not read invitation ledger at {path}: {e}")
        return []

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"Skipping malformed ledger line: {line[:80]}")
            continue
        if isinstance(entry, dict) and entry.get("ts"):
            entries.append(entry)
    return entries


def weekly_counts(path=DEFAULT_INVITE_FILE):
    """Return {week-start ISO date: invitations sent}, oldest week first."""
    counts = {}
    for entry in load_invites(path):
        week = entry.get("week")
        if not week:
            try:
                week = current_week_start(
                    datetime.fromisoformat(entry["ts"])).isoformat()
            except ValueError:
                continue
        counts[week] = counts.get(week, 0) + 1
    return OrderedDict(sorted(counts.items()))


def count_invites_for_week(week=None, path=DEFAULT_INVITE_FILE):
    """Count invitations sent during a logging week (default: the current one)."""
    week = week or current_week_start().isoformat()
    return weekly_counts(path).get(str(week), 0)


def backfill_from_logs(log_dir="connect/logs", path=DEFAULT_INVITE_FILE):
    """Import invitations from existing weekly logs. Returns how many were added.

    Entries already in the ledger are matched on (timestamp, name), so running
    this repeatedly is safe.
    """
    known = {(e.get("ts"), e.get("name")) for e in load_invites(path)}
    added = 0

    for log_file in sorted(Path(log_dir).glob("week-*.log")):
        try:
            with log_file.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning(f"Could not read {log_file}: {e}")
            continue

        for line in lines:
            match = _LOG_LINE.match(line.strip())
            if not match:
                continue
            ts = datetime.strptime(
                match.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRT)
            key = (ts.isoformat(timespec="seconds"), match.group("name"))
            if key in known:
                continue
            record_invite(match.group("name"), path=path, now=ts)
            known.add(key)
            added += 1

    return added
