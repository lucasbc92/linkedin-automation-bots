import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

logger = logging.getLogger("linkedin_bot")


def create_driver(attach_to_existing=False):
    """Return a (driver, perf_logging_enabled) tuple.

    attach_to_existing=True connects to a Chrome instance already running with
    --remote-debugging-port=9222, so the user stays logged in to LinkedIn.
    Performance logging enables network-level HTTP 429 detection; it degrades
    gracefully when unavailable.
    """
    options = Options()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    if attach_to_existing:
        options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get_log("performance")
        perf_logging = True
        logger.debug("Performance logging enabled; HTTP 429 detection active.")
    except Exception as e:
        perf_logging = False
        logger.warning(
            f"Performance logging unavailable ({type(e).__name__}): "
            "network-level HTTP 429 detection disabled, "
            "relying on UI limit checks only.")

    return driver, perf_logging
