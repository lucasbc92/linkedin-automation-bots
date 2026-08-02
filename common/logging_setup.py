import logging
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")

_LOG_DIR = "logs"
# A logging week runs Sunday to Sunday, starting at Sunday 21:00 BRT; runs
# are appended to that week's file.
_WEEK_START_HOUR = 21
_WEEK_START_WEEKDAY = 6  # Sunday (Monday=0 … Sunday=6, per date.weekday())


def current_week_start(now=None):
    """Return the Sunday date that identifies the current logging week."""
    now = (now or datetime.now(BRT)).astimezone(BRT)
    days_since_start = (now.weekday() - _WEEK_START_WEEKDAY) % 7
    sunday = (now - timedelta(days=days_since_start)).date()
    boundary = datetime.combine(sunday, dtime(_WEEK_START_HOUR, 0), tzinfo=BRT)
    if now < boundary:
        sunday -= timedelta(days=7)
    return sunday


def week_log_path(now=None, log_dir=_LOG_DIR):
    return Path(log_dir) / f"week-{current_week_start(now).isoformat()}.log"


def setup_logging(level=logging.DEBUG, log_file=None, log_dir=None):
    logging.addLevelName(logging.WARNING, "WARN")

    logger = logging.getLogger("linkedin_bot")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    path = Path(log_file) if log_file else week_log_path(log_dir=log_dir or _LOG_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)

    return logger
