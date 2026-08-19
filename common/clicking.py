"""Trusted-click ladder shared by both bots.

LinkedIn ignores synthetic clicks on some controls, so a click is retried
through four increasingly forceful strategies. Both bots had their own copy of
this; they had already drifted (different press-hold time, different log
wording), which is exactly the divergence a shared mixin prevents.

Mix into any class that exposes a Selenium ``self.driver``.
"""

import logging
import time

from selenium.webdriver.common.action_chains import ActionChains

logger = logging.getLogger("linkedin_bot")

# How long the CDP mouse button stays down. Long enough to read as a real
# press, short enough not to register as a long-press.
CLICK_HOLD_SECONDS = 0.1


class ClickMixin:
    """Native → ActionChains → CDP → JavaScript click fallbacks."""

    def _cdp_click(self, element, description="element"):
        """Dispatch a trusted click via Chrome DevTools Protocol.

        Produces isTrusted=true events and works inside shadow DOM where
        ActionChains can fail to compute correct coordinates.
        """
        try:
            loc = element.location
            sz = element.size
            x = loc['x'] + sz['width'] / 2
            y = loc['y'] + sz['height'] / 2
            params = {"button": "left", "clickCount": 1, "modifiers": 0, "x": x, "y": y}
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {**params, "type": "mousePressed"})
            time.sleep(CLICK_HOLD_SECONDS)
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {**params, "type": "mouseReleased"})
            logger.debug(f"CDP click OK on {description} at ({x:.0f},{y:.0f})")
            return True
        except Exception as e:
            logger.debug(f"CDP click failed on {description}: {type(e).__name__}: {e}")
            return False

    def _robust_click(self, element, description="element"):
        """Click with a trusted event: native → ActionChains → CDP → JS fallback."""
        try:
            element.click()
            logger.debug(f"Native click OK on {description}")
            return True
        except Exception as e:
            logger.debug(f"Native click failed on {description} ({type(e).__name__}: {e}); trying ActionChains")

        try:
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", element)
            except Exception:
                pass
            ActionChains(self.driver).move_to_element(element).pause(0.1).click().perform()
            logger.debug(f"ActionChains click OK on {description}")
            return True
        except Exception as e:
            logger.debug(f"ActionChains failed on {description} ({type(e).__name__}: {e}); trying CDP click")

        if self._cdp_click(element, description):
            return True

        logger.warning(
            f"All trusted clicks failed on {description}; falling back to JS "
            f"click - LinkedIn may ignore it")
        try:
            self.driver.execute_script("arguments[0].click();", element)
            return True
        except Exception as e:
            logger.warning(f"All click methods failed on {description}: {e}")
            return False
