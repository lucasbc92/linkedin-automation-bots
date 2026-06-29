import logging
import random
import re
import time
from datetime import date, timedelta

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common.browser import create_driver
from common.messages import MessageTemplates
from common.names import display_first_name
from common.sleep import allow_sleep, prevent_sleep

logger = logging.getLogger("linkedin_bot")

# LinkedIn timestamp weekday abbreviations
_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Selectors
_LIST_CONTAINER = "div.msg-conversations-container--inbox-shortcuts"
_CONVERSATION_LIST = "ul.msg-conversations-container__conversations-list"
_CARD_ITEM = "li.msg-conversation-listitem"
_NAME_SELECTOR = "h3.msg-conversation-listitem__participant-names span.truncate"
_TIME_SELECTOR = "time.msg-conversation-listitem__time-stamp"
_SPONSORED_SELECTOR = "span.msg-conversation-card__pill"
_COMPOSE_BOX = "div.msg-form__contenteditable[contenteditable='true'][role='textbox']"
_THREAD_HEADER_NAME = (
    "h2.msg-entity-lockup__entity-title,"
    "span.msg-thread__link-to-profile-name,"
    "span.msg-entity-lockup__entity-title"
)


def parse_card_timestamp(raw, today=None):
    """Parse a LinkedIn conversation-card timestamp into a ``date``.

    LinkedIn uses several relative and absolute formats:
    - ``"10:01 PM"`` / ``"9:12 AM"``         → today
    - ``"Mon"`` / ``"Tue"`` etc.              → most recent past occurrence
    - ``"Jun 27"``                            → that day, current or previous year
    - ``"Jun 27, 2024"`` / ``"Mar 2024"``    → explicit year
    - Anything else                           → ``None`` (within range, don't stop)
    """
    if today is None:
        today = date.today()
    text = raw.strip()

    # "10:01 PM" / "9:12 AM" — time-only → today
    if re.match(r"^\d{1,2}:\d{2}\s*(AM|PM)$", text, re.IGNORECASE):
        return today

    # Weekday abbreviation → most recent past occurrence within last 7 days
    key = text[:3].lower()
    if key in _WEEKDAYS and len(text) <= 4:
        target_dow = _WEEKDAYS[key]
        days_back = (today.weekday() - target_dow) % 7
        if days_back == 0:
            days_back = 7  # same weekday → last week
        return today - timedelta(days=days_back)

    # "Jun 27, 2024" — month + day + explicit year
    m = re.match(
        r"^([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})$", text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass

    # "Mar 2024" — month + year only (use the 1st of the month)
    m = re.match(r"^([A-Za-z]{3})\s+(\d{4})$", text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return date(int(m.group(2)), month, 1)
            except ValueError:
                pass

    # "Jun 27" — month + day, no year
    m = re.match(r"^([A-Za-z]{3})\s+(\d{1,2})$", text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                d = date(today.year, month, int(m.group(2)))
                if d > today:
                    d = date(today.year - 1, month, int(m.group(2)))
                return d
            except ValueError:
                pass

    logger.warning(f"Could not parse timestamp '{text}' — treating as within range.")
    return None


class LinkedInMessageBot:
    def __init__(self, message_file="message/templates/message.txt",
                 date_limit=None, dry_run=False, max_messages=None):
        """
        Args:
            message_file: Path to the template file.
            date_limit: ``date`` object; stop when a card is older than this.
                        ``None`` → process the whole list.
            dry_run: If True, log what would be sent without typing or sending.
            max_messages: Stop after this many messages sent. ``None`` = unlimited.
        """
        self.date_limit = date_limit
        self.dry_run = dry_run
        self.max_messages = max_messages

        self.driver, _ = create_driver(attach_to_existing=True)
        self.wait = WebDriverWait(self.driver, 10)
        self.short_wait = WebDriverWait(self.driver, 3)

        self._msg = MessageTemplates(message_file)

        self.sent = 0
        self.failed = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    # Click helpers (mirrors connect/bot.py pattern)
    # ------------------------------------------------------------------

    def _cdp_click(self, element, description="element"):
        try:
            loc = element.location
            sz = element.size
            x = loc['x'] + sz['width'] / 2
            y = loc['y'] + sz['height'] / 2
            params = {"button": "left", "clickCount": 1, "modifiers": 0,
                      "x": x, "y": y}
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {**params, "type": "mousePressed"})
            time.sleep(0.05)
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {**params, "type": "mouseReleased"})
            logger.debug(f"CDP click OK on {description} at ({x:.0f},{y:.0f})")
            return True
        except Exception as e:
            logger.debug(f"CDP click failed on {description}: {type(e).__name__}: {e}")
            return False

    def _robust_click(self, element, description="element"):
        try:
            element.click()
            logger.debug(f"Native click OK on {description}")
            return True
        except Exception as e:
            logger.debug(f"Native click failed on {description} ({type(e).__name__}); trying ActionChains")

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
            logger.debug(f"ActionChains failed ({type(e).__name__}); trying CDP")

        if self._cdp_click(element, description):
            return True

        logger.warning(f"All trusted clicks failed on {description}; falling back to JS")
        try:
            self.driver.execute_script("arguments[0].click();", element)
            return True
        except Exception as e:
            logger.warning(f"All click methods failed on {description}: {e}")
            return False

    # ------------------------------------------------------------------
    # Compose-box helpers
    # ------------------------------------------------------------------

    def _insert_text(self, compose_box, text):
        """Insert text into the contenteditable compose box without triggering send.

        LinkedIn's DM box sends on Enter, so we cannot use send_keys for
        multi-line text.  ``execCommand('insertText')`` injects the full body
        (including newlines as soft-breaks) in one atomic operation.
        """
        try:
            compose_box.click()
        except Exception:
            self.driver.execute_script("arguments[0].focus();", compose_box)

        inserted = self.driver.execute_script(
            "arguments[0].focus();"
            "return document.execCommand('insertText', false, arguments[1]);",
            compose_box, text)

        if not inserted:
            # Fallback: set innerText and fire input event
            logger.debug("execCommand returned false; falling back to innerText setter")
            self.driver.execute_script(
                "const el = arguments[0];"
                "el.innerText = arguments[1];"
                "el.dispatchEvent(new Event('input', {bubbles: true}));"
                "el.dispatchEvent(new Event('change', {bubbles: true}));",
                compose_box, text)

    def _box_is_empty(self, compose_box):
        try:
            content = self.driver.execute_script(
                "return (arguments[0].textContent || '').trim();", compose_box)
            return not content
        except Exception:
            return False

    def _send_message(self, compose_box, text, contact_label):
        """Type text into the compose box and send it. Returns True on success."""
        try:
            self._insert_text(compose_box, text)
            time.sleep(0.5)

            # Verify the box has content
            if self._box_is_empty(compose_box):
                logger.warning(f"Compose box appears empty after insert for {contact_label}. Skipping.")
                return False

            # Primary send: trusted Enter key
            compose_box.send_keys(Keys.ENTER)
            time.sleep(2)

            # Verify sent: box should be empty / placeholder restored
            if self._box_is_empty(compose_box):
                return True

            # Fallback: click the Send button
            logger.debug("Box still has content after Enter; trying Send button fallback")
            try:
                send_btn = self.driver.find_element(
                    By.CSS_SELECTOR,
                    "button.msg-form__send-button, "
                    "button[data-test-msg-send-btn]")
                self._robust_click(send_btn, "Send button")
                time.sleep(2)
                return self._box_is_empty(compose_box)
            except NoSuchElementException:
                logger.warning(f"No Send button found for {contact_label}.")
                return False

        except Exception as e:
            logger.error(f"Error sending message to {contact_label}: {e}")
            return False

    # ------------------------------------------------------------------
    # Conversation-list helpers
    # ------------------------------------------------------------------

    def _select_messaging_tab(self):
        """Switch to the LinkedIn messaging tab; fall back to the current tab."""
        handles = self.driver.window_handles
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                url = (self.driver.current_url or "").lower()
                if "linkedin.com/messaging" in url or "linkedin.com/msg" in url:
                    logger.info(f"Using messaging tab: {self.driver.current_url}")
                    return True
            except Exception:
                continue
        logger.warning("No LinkedIn messaging tab found. Using the current tab.")
        return False

    def _get_cards(self):
        """Return all conversation-list <li> elements currently in the DOM."""
        try:
            return self.driver.find_elements(By.CSS_SELECTOR, _CARD_ITEM)
        except Exception:
            return []

    def _card_name(self, card):
        try:
            el = card.find_element(By.CSS_SELECTOR, _NAME_SELECTOR)
            return el.text.strip()
        except Exception:
            return None

    def _card_timestamp(self, card):
        try:
            el = card.find_element(By.CSS_SELECTOR, _TIME_SELECTOR)
            return el.text.strip()
        except Exception:
            return None

    def _card_is_sponsored(self, card):
        try:
            pills = card.find_elements(By.CSS_SELECTOR, _SPONSORED_SELECTOR)
            return any("sponsored" in p.text.lower() for p in pills)
        except Exception:
            return False

    def _scroll_list_bottom(self):
        """Scroll the conversation list container to trigger lazy-loading."""
        try:
            container = self.driver.find_element(By.CSS_SELECTOR, _CONVERSATION_LIST)
            self.driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;", container)
            time.sleep(1.5)
        except Exception:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)

    def _wait_thread_open(self, name_full, timeout=8):
        """Wait until the active thread panel shows the contact's name."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                for sel in _THREAD_HEADER_NAME.split(","):
                    els = self.driver.find_elements(By.CSS_SELECTOR, sel.strip())
                    for el in els:
                        if name_full and name_full.split()[0].lower() in el.text.lower():
                            return True
                        if el.is_displayed() and el.text.strip():
                            return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _get_compose_box(self, timeout=8):
        """Return the compose contenteditable div once it's available."""
        end = time.time() + timeout
        while time.time() < end:
            els = self.driver.find_elements(By.CSS_SELECTOR, _COMPOSE_BOX)
            for el in els:
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
            time.sleep(0.4)
        return None

    def _build_skip_set(self):
        """Return names to skip based on the currently active (clicked) card.

        If a conversation is already open when the bot starts, all cards from
        the top of the list down to and including that card are added to the
        skip set, so the bot begins processing from the next card below.

        If no card is active, returns an empty set (process from the top).
        """
        skipped = set()
        try:
            cards = self._get_cards()
            for card in cards:
                name = self._card_name(card)
                if name:
                    skipped.add(name)
                # Stop as soon as we hit the active card
                try:
                    active = card.find_elements(
                        By.CSS_SELECTOR,
                        ".msg-conversations-container__convo-item-link--active")
                    if active:
                        break
                except Exception:
                    pass
            else:
                # Loop completed without finding an active card → start from top
                return set()
        except Exception as e:
            logger.debug(f"Could not determine starting position: {e}")
            return set()

        if skipped:
            logger.info(
                f"Starting from contact below the active card — "
                f"skipping {len(skipped)} conversation(s) above.")
        return skipped

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        """Walk the conversation list and send personalized messages."""
        prevent_sleep()
        self._select_messaging_tab()

        processed = self._build_skip_set()   # names already handled / to skip

        try:
            while True:
                cards = self._get_cards()

                # Find the topmost unprocessed, non-Sponsored card
                target = None
                for card in cards:
                    name_full = self._card_name(card)
                    if not name_full or name_full in processed:
                        continue
                    if self._card_is_sponsored(card):
                        processed.add(name_full)
                        logger.debug(f"Skipping Sponsored: {name_full}")
                        continue
                    target = card
                    break

                if target is None:
                    # Try to lazy-load more
                    count_before = len(cards)
                    self._scroll_list_bottom()
                    count_after = len(self._get_cards())
                    if count_after <= count_before:
                        logger.info("Reached the bottom of the conversation list.")
                        break
                    continue

                name_full = self._card_name(target)
                first = display_first_name(name_full) if name_full else None

                # --- date-limit check ---
                ts_raw = self._card_timestamp(target)
                if ts_raw:
                    card_date = parse_card_timestamp(ts_raw)
                    if self.date_limit and card_date and card_date < self.date_limit:
                        logger.info(
                            f"Card for {name_full} dated {card_date} is before "
                            f"date limit {self.date_limit}. Stopping.")
                        break

                processed.add(name_full)

                message = self._msg.personalize(first)

                if self.dry_run:
                    first_line = message.splitlines()[0] if message else ""
                    logger.info(
                        f"[DRY-RUN] Would send to {first or name_full}: "
                        f"{first_line}")
                    continue

                logger.info(f"Opening thread with {name_full} …")
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", target)
                    time.sleep(random.uniform(0.8, 1.5))
                    self._robust_click(target, f"conversation card ({name_full})")
                    time.sleep(2)

                    if not self._wait_thread_open(name_full):
                        logger.warning(f"Thread for {name_full} did not open. Skipping.")
                        self.skipped += 1
                        continue

                    compose = self._get_compose_box()
                    if compose is None:
                        logger.warning(f"No compose box found for {name_full}. Skipping.")
                        self.skipped += 1
                        continue

                    logger.info(
                        f"Sending to {first or name_full}: "
                        f"{message.splitlines()[0] if message else ''}")

                    if self._send_message(compose, message, name_full):
                        self.sent += 1
                        logger.info(
                            f"Message sent to {first or name_full} "
                            f"[sent={self.sent}, failed={self.failed}, "
                            f"skipped={self.skipped}]")
                    else:
                        self.failed += 1
                        logger.warning(
                            f"Message to {name_full} did not register "
                            f"[sent={self.sent}, failed={self.failed}, "
                            f"skipped={self.skipped}]")

                    time.sleep(random.uniform(3, 5))

                    if self.max_messages and self.sent >= self.max_messages:
                        logger.info(f"Reached --max {self.max_messages}. Stopping.")
                        break

                except Exception as e:
                    logger.error(f"Error processing {name_full}: {e}")
                    self.failed += 1

        finally:
            allow_sleep()

        logger.info(
            f"Run complete — "
            f"sent: {self.sent} | failed: {self.failed} | skipped: {self.skipped}")
