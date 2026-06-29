import ctypes
import logging
import sys

logger = logging.getLogger("linkedin_bot")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep():
    """Tell Windows not to sleep or turn off the display during automation."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        logger.info("Sleep prevention enabled — PC will stay awake during automation.")
    except Exception as e:
        logger.warning(f"Could not enable sleep prevention: {e}")


def allow_sleep():
    """Restore normal Windows sleep behaviour."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        logger.debug("Sleep prevention disabled — normal power settings restored.")
    except Exception as e:
        logger.debug(f"Could not restore sleep settings: {e}")
