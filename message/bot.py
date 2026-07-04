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
from message.history import load_sent_names, record_sent

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
_CARD_PILL_SELECTOR = "span.msg-conversation-card__pill"
# Pill labels that mean the conversation should be left untouched.
_SKIP_PILL_LABELS = ("sponsored", "inmail", "linkedin offer")
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


def first_card_on_or_before(timestamps, target, today=None):
    """Index of the first card whose date is on or before ``target``.

    ``timestamps`` is an iterable of ``(card_index, raw_text)`` pairs in list
    order (newest first). Returns ``None`` when every parsable date is still
    newer than ``target``.
    """
    for idx, raw in timestamps:
        card_date = parse_card_timestamp(raw, today=today)
        if card_date and card_date <= target:
            return idx
    return None


class LinkedInMessageBot:
    def __init__(self, message_file="templates/message/message.txt",
                 date_limit=None, start_date=None, dry_run=False,
                 max_messages=None):
        """
        Args:
            message_file: Path to the template file.
            date_limit: ``date`` object; stop when a card is older than this.
                        ``None`` → process the whole list.
            start_date: ``date`` object; scroll down the list until the first
                        conversation dated on or before this, click it, and
                        start sending from there. ``None`` → start from the
                        top or from the card the user clicked manually.
            dry_run: If True, log what would be sent without typing or sending.
            max_messages: Stop after this many messages sent. ``None`` = unlimited.
        """
        self.date_limit = date_limit
        self.start_date = start_date
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
            time.sleep(0.1)
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
            time.sleep(0.8)

            # Verify the box has content
            if self._box_is_empty(compose_box):
                logger.warning(f"Compose box appears empty after insert for {contact_label}. Skipping.")
                return False

            # Primary send: trusted Enter key
            compose_box.send_keys(Keys.ENTER)
            time.sleep(3)

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
                time.sleep(3)
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

    def _card_skip_pill(self, card):
        """Return the pill label ("Sponsored"/"InMail") if the card carries a
        pill that means we should not message it, else ``None``.

        Sponsored ads and InMails share the same pill component; we match on
        the pill text so both are skipped without messaging.
        """
        try:
            pills = card.find_elements(By.CSS_SELECTOR, _CARD_PILL_SELECTOR)
        except Exception:
            return None
        for p in pills:
            try:
                text = p.text.lower()
            except Exception:
                continue
            for label in _SKIP_PILL_LABELS:
                if label in text:
                    return p.text.strip() or label.capitalize()
        return None

    def _scroll_list_bottom(self):
        """Scroll the conversation list container to trigger lazy-loading."""
        try:
            container = self.driver.find_element(By.CSS_SELECTOR, _CONVERSATION_LIST)
            self.driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;", container)
            time.sleep(2)
        except Exception:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

    def _visible_card_timestamps(self):
        """Return ``[(index, raw timestamp), …]`` for every card that has a
        rendered ``<time>`` element, in list order.

        Done in one JS round-trip: with hundreds of cards loaded, probing each
        ``<li>`` through Selenium takes seconds per pass. Occluded
        (virtualized) cards have their content emptied by LinkedIn and simply
        don't appear in the result.
        """
        try:
            return self.driver.execute_script(
                "const out = [];"
                "document.querySelectorAll(arguments[0]).forEach((li, i) => {"
                "  const t = li.querySelector(arguments[1]);"
                "  if (t && t.textContent.trim()) out.push([i, t.textContent.trim()]);"
                "});"
                "return out;",
                _CARD_ITEM, _TIME_SELECTOR) or []
        except Exception as e:
            logger.debug(f"Could not collect card timestamps: {e}")
            return []

    def _focus_last_card(self):
        """Focus the second-to-last card to make the list lazy-load more.

        Same technique as focusLastElementLinkedinMessage.js: setting
        ``scrollTop`` alone doesn't reliably make the virtualized list fetch
        the next page, but moving focus to the last card does.
        """
        try:
            self.driver.execute_script(
                "const items = document.querySelectorAll(arguments[0]);"
                "const el = items[items.length - 2] || items[items.length - 1];"
                "if (el) el.focus();",
                _CARD_ITEM)
        except Exception:
            self._scroll_list_bottom()

    def _scroll_to_start_date(self, target):
        """Scroll until a conversation dated on or before ``target`` appears,
        then click it so the run starts from that card (inclusive).

        Returns True once the start card was clicked, False if the bottom of
        the list was reached without finding one.
        """
        logger.info(
            f"Looking for the first conversation dated on or before {target} …")
        while True:
            idx = first_card_on_or_before(self._visible_card_timestamps(), target)
            if idx is not None:
                cards = self._get_cards()
                if idx >= len(cards):
                    # List changed between the JS scan and now; rescan.
                    time.sleep(1)
                    continue
                card = cards[idx]
                name = self._card_name(card)
                ts = self._card_timestamp(card)
                logger.info(
                    f"Start card found at index {idx}: "
                    f"{name or '(unknown)'} — dated '{ts}'. Clicking it.")
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", card)
                except Exception:
                    pass
                time.sleep(1)
                self._robust_click(card, f"start card ({name or ts})")
                time.sleep(3)
                return True

            count_before = len(self._get_cards())
            logger.debug(
                f"No card on or before {target} among {count_before} loaded; "
                f"focusing the last card to load more …")
            self._focus_last_card()
            time.sleep(3)
            if len(self._get_cards()) <= count_before:
                logger.warning(
                    f"Reached the bottom of the list without finding a "
                    f"conversation dated on or before {target}.")
                return False

    def _wait_thread_open(self, name_full, timeout=8):
        """Wait until the active thread panel shows the contact's name.

        A header is only a match when it actually mentions the expected
        first name. Treating "some header is visible" as a match (regardless
        of whose name it shows) let the bot proceed while a *different*,
        already-messaged contact's thread was still open — sending the next
        message to the wrong person.
        """
        expected = name_full.split()[0].lower() if name_full else None
        end = time.time() + timeout
        while time.time() < end:
            try:
                for sel in _THREAD_HEADER_NAME.split(","):
                    for el in self.driver.find_elements(By.CSS_SELECTOR, sel.strip()):
                        text = el.text.strip()
                        if not text or not el.is_displayed():
                            continue
                        if expected is None or expected in text.lower():
                            return True
            except Exception:
                pass
            time.sleep(0.8)
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
            time.sleep(0.8)
        return None

    def _active_card(self):
        """Return the <li> of the currently active conversation, or None."""
        try:
            return self.driver.execute_script(
                "const a = document.querySelector("
                "  '.msg-conversations-container__convo-item-link--active');"
                "return a ? a.closest(arguments[0]) : null;",
                _CARD_ITEM)
        except Exception as e:
            logger.debug(f"Could not find the active card: {e}")
            return None

    def _next_card(self, card):
        """Return the conversation <li> right after ``card`` in the list,
        lazy-loading one more page if ``card`` is currently the last one.
        Returns None only when the true bottom of the list was reached.
        """
        for attempt in (1, 2):
            try:
                nxt = self.driver.execute_script(
                    "let el = arguments[0].nextElementSibling;"
                    "while (el && !el.matches(arguments[1]))"
                    "  el = el.nextElementSibling;"
                    "return el;",
                    card, _CARD_ITEM)
            except Exception as e:
                logger.debug(f"Could not get the next card: {e}")
                return None
            if nxt is not None:
                return nxt
            if attempt == 1:
                logger.debug("At the last loaded card; focusing it to load more …")
                self._focus_last_card()
                time.sleep(3)
        return None

    def _ensure_rendered(self, card, tries=3):
        """Return the card's name, scrolling it into view first if LinkedIn's
        virtualization has emptied it (occluded cards have no content until
        they get near the viewport)."""
        for _ in range(tries):
            name = self._card_name(card)
            if name:
                return name
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", card)
            except Exception:
                return None
            time.sleep(1)
        return self._card_name(card)

    def _card_by_name(self, name):
        """Find a conversation card by its participant name, or None."""
        for card in self._get_cards():
            if self._card_name(card) == name:
                return card
        return None

    def _refresh_card(self, card, name):
        """Return a usable element for a previously captured card.

        The list re-sorts after every send; if LinkedIn re-created the DOM
        node in the process, the captured reference goes stale and the card
        is re-found by name instead.
        """
        if card is None:
            return None
        try:
            card.is_displayed()   # probe: raises if the node went stale
            return card
        except Exception:
            logger.debug(f"Captured card for {name!r} went stale; re-finding by name.")
        if name:
            return self._card_by_name(name)
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _send_to_card(self, card, name_full, message):
        """Open the thread for ``card`` and send ``message``. Updates the
        sent/failed/skipped counters."""
        first = display_first_name(name_full)
        logger.info(f"Opening thread with {name_full} …")
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", card)
            time.sleep(random.uniform(1, 2))
            self._robust_click(card, f"conversation card ({name_full})")
            time.sleep(3)

            if not self._wait_thread_open(name_full):
                logger.warning(f"Thread for {name_full} did not open. Skipping.")
                self.skipped += 1
                return

            compose = self._get_compose_box()
            if compose is None:
                logger.warning(f"No compose box found for {name_full}. Skipping.")
                self.skipped += 1
                return

            # Re-confirm identity right before typing: the panel can switch
            # to a different (already-messaged) contact between the click and
            # now (list re-sort, LinkedIn's own focus changes). Sending here
            # would resend to that earlier contact instead of name_full.
            if not self._wait_thread_open(name_full, timeout=1):
                logger.warning(
                    f"Active thread switched away from {name_full} "
                    f"just before sending. Skipping to avoid "
                    f"messaging the wrong contact.")
                self.skipped += 1
                return

            logger.info(
                f"Sending to {first or name_full}: "
                f"{message.splitlines()[0] if message else ''}")

            if self._send_message(compose, message, name_full):
                self.sent += 1
                record_sent(name_full)
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

            time.sleep(random.uniform(4, 6))

        except Exception as e:
            logger.error(f"Error processing {name_full}: {e}")
            self.failed += 1

    def run(self):
        """Walk the conversation list strictly downward, one card at a time.

        The next contact is ALWAYS the card right after the current one —
        never a rescan of the list from the top. The list re-sorts the moment
        a message is sent (the messaged contact jumps to the top), so the
        next card is captured *before* sending; anything computed after the
        send would be relative to the top of the list and restart the walk
        among already-messaged contacts.
        """
        prevent_sleep()
        self._select_messaging_tab()

        try:
            if self.start_date:
                if not self._scroll_to_start_date(self.start_date):
                    logger.error(
                        f"No conversation dated on or before "
                        f"{self.start_date} was found. Nothing to do.")
                    return

            # Durable record of everyone already messaged in past runs; used
            # only to decide whether to skip a card, never to navigate.
            history = load_sent_names()
            handled = set()   # names walked past in this run

            current = self._active_card()
            if current is not None:
                logger.info(
                    "Starting from the active (clicked) conversation, "
                    "walking down.")
            else:
                cards = self._get_cards()
                current = cards[0] if cards else None
                logger.info("No active conversation — starting from the top.")

            while current is not None:
                name_full = self._ensure_rendered(current)

                # --- date-limit check ---
                ts_raw = self._card_timestamp(current)
                card_date = parse_card_timestamp(ts_raw) if ts_raw else None
                if self.date_limit and card_date and card_date < self.date_limit:
                    logger.info(
                        f"Card for {name_full or '(unknown)'} dated {card_date} "
                        f"is before date limit {self.date_limit}. Stopping.")
                    break

                # --- decide whether this card gets a message ---
                skip_reason = None
                if not name_full:
                    skip_reason = "card has no readable name"
                else:
                    pill = self._card_skip_pill(current)
                    if pill:
                        skip_reason = pill
                    elif name_full in handled:
                        skip_reason = "already handled in this run"
                    elif name_full in history:
                        skip_reason = "already messaged in a previous run"

                if skip_reason:
                    logger.info(
                        f"Skipping {name_full or '(unnamed card)'} — {skip_reason}.")
                    if name_full:
                        handled.add(name_full)
                    current = self._next_card(current)
                    continue

                handled.add(name_full)
                message = self._msg.personalize(display_first_name(name_full))

                if self.dry_run:
                    first_line = message.splitlines()[0] if message else ""
                    logger.info(f"[DRY-RUN] Would send to {name_full}: {first_line}")
                    current = self._next_card(current)
                    continue

                # Capture the next card BEFORE sending — sending moves the
                # current card to the top of the list.
                next_card = self._next_card(current)
                next_name = (self._ensure_rendered(next_card)
                             if next_card is not None else None)

                self._send_to_card(current, name_full, message)

                if self.max_messages and self.sent >= self.max_messages:
                    logger.info(f"Reached --max {self.max_messages}. Stopping.")
                    break

                current = self._refresh_card(next_card, next_name)
                if current is None and next_card is not None:
                    logger.warning(
                        f"Lost track of the next card "
                        f"({next_name or 'unnamed'}) after the list "
                        f"re-sorted. Stopping instead of re-walking from "
                        f"the top.")
                    break
            else:
                logger.info("Reached the bottom of the conversation list.")

        finally:
            allow_sleep()

        logger.info(
            f"Run complete — "
            f"sent: {self.sent} | failed: {self.failed} | skipped: {self.skipped}")
