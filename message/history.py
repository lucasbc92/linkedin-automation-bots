import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("linkedin_bot")

# Local, gitignored record of every contact a message has been confirmed
# sent to, across all runs. The conversation list re-sorts by recent
# activity and isn't append-only, so "everything above the card I clicked"
# is not a reliable way to know who's already been messaged once a run is
# stopped and resumed later. This file is the durable source of truth.
DEFAULT_HISTORY_FILE = Path(__file__).parent / ".sent_history.json"


def load_sent_names(path=DEFAULT_HISTORY_FILE):
    """Return the set of participant names already messaged in past runs."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.keys())
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            f"Could not read send history at {path}: {e}. "
            f"Starting with empty history.")
        return set()


def record_sent(name_full, path=DEFAULT_HISTORY_FILE):
    """Record that a message was just sent to name_full, for future runs."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    data[name_full] = datetime.now().isoformat(timespec="seconds")

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as e:
        logger.warning(f"Could not write send history at {path}: {e}")
