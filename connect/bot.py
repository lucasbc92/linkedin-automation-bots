import json
import logging
import random
import re
import time
import unicodedata

from selenium.common.exceptions import (ElementClickInterceptedException,
                                        NoSuchElementException, TimeoutException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common.browser import create_driver
from common.clicking import ClickMixin
from common.logging_setup import current_week_start
from common.messages import MessageTemplates
from common.names import display_first_name
from common.scrolling import list_state, scroll_list_step, scroll_list_to_top
from common.sleep import allow_sleep, prevent_sleep
from connect.history import count_invites_for_week, record_invite
from connect.tech_recruiter import DEFAULT_MIN_SCORE, score_title

logger = logging.getLogger("linkedin_bot")

# URL fragments of LinkedIn's invitation endpoint.
# A 429 on these paths means the invite quota is exhausted.
INVITE_ENDPOINT_FRAGMENTS = (
    "voyagerRelationshipsDashMemberRelationships",
    "verifyQuotaAndCreate",
)

# Fast mode (--fast) keeps the jitter of the humanizing pauses but shrinks the
# window to this fraction, never dropping below MIN_FAST_PAUSE seconds — some
# gap is still needed for LinkedIn's UI to settle between actions.
FAST_PAUSE_FACTOR = 0.1
MIN_FAST_PAUSE = 0.5

# The results are a virtualized LazyColumn: a page holds ten people but only
# the rows near the scroll position exist in the DOM, and they mount as the
# app shell's inner container scrolls (see common/scrolling.py). Scanning
# whatever is mounted and stopping there reads a page of Follow-only cards as
# an empty page, so the scan scrolls the list to its end before giving up.
RESULT_LIST = "[data-testid='lazy-column'], [data-component-type='LazyColumn']"
RESULT_CARD = "div[role='listitem']"
CONNECT_XPATH = ("//a[starts-with(@aria-label, 'Invite ') and "
                 "contains(@aria-label, 'to connect')]")

# Cards that offer only Follow. LinkedIn puts no Connect control on these in
# the results list, but the action still exists one level down, on the
# person's own profile, behind the three-dots More menu. Deliberately not
# scoped to the card markup — that changes shape between LinkedIn releases,
# while the label does not; the card is found by climbing afterwards.
FOLLOW_XPATH = ("//button[starts-with(@aria-label, 'Follow ')] | "
                "//a[starts-with(@aria-label, 'Follow ')]")
# Both kinds of person, as one list. An XPath union returns its matches in
# document order, so a single walk hands back the page's people in the order
# it shows them, each to be invited whichever way their own card allows.
TARGET_XPATH = CONNECT_XPATH + " | " + FOLLOW_XPATH
# The Connect control a card would have to carry for its Follow button to be
# the cheap way in; a card holding one is left to the normal route. Tag-free
# on purpose: it only has to notice the control, never click it.
CARD_INVITE_XPATH = ".//*[starts-with(@aria-label, 'Invite ')]"
# The Connect control as it appears on a profile page: in the top card, in the
# "Connect if you know each other" prompt, or inside the More menu once it is
# open. Shadow roots only take CSS, so this is CSS.
#
# It matches any tag, because the three places do not agree on which one
# carries the label. In the top card it is the <a> itself; in the More menu
# the item is an <a href="/preload/custom-invite/…"> whose *inner div* holds
# the label. Matching only a/button is why the menu route found nothing.
PROFILE_CONNECT_CSS = "[aria-label^='Invite '][aria-label*='to connect']"
# An open profile menu, and its items. Used to find Connect by where it is
# when the name on it cannot be matched.
MENU_CSS = "[role='menu']"
MENU_ITEM_CSS = "[role='menuitem']"
# From the labelled element to the thing that actually carries the action.
CLICKABLE_XPATH = "ancestor-or-self::*[self::a or self::button][1]"
# The three-dots button in a profile's top card. The sticky header renders a
# second one, so the search takes whichever is on screen and tries the next if
# the menu it opens has no Connect in it.
MORE_BUTTON_XPATH = ("//button[@aria-label='More' or "
                     "starts-with(@aria-label, 'More actions')]")
MORE_BUTTON_ATTEMPTS = 3
# How long a profile page gets to render, and its More menu to open.
PROFILE_TIMEOUT_SECONDS = 20
MENU_TIMEOUT_SECONDS = 5
# Ten results per page need three or four steps; the cap only exists so a
# list that somehow keeps growing cannot hold a run forever.
MOUNT_STEP_LIMIT = 30
# How long newly scrolled-to rows get to mount before they are counted.
MOUNT_SETTLE_SECONDS = 0.6
# How long a freshly navigated page gets to render its first card.
RESULTS_TIMEOUT_SECONDS = 20


class LinkedInConnectBot(ClickMixin):
    def __init__(self, auto_continue=False,
                 message_file="connect/msg_templates/message.txt",
                 reverse=False, no_message=False, max_invites=None,
                 tech_only=True, min_title_score=DEFAULT_MIN_SCORE,
                 fast=False):
        self.auto_continue = auto_continue
        self.reverse = reverse
        self.no_message = no_message
        self.max_invites = max_invites
        self.tech_only = tech_only
        self.min_title_score = min_title_score
        self.fast = fast

        self.driver, self.perf_logging = create_driver(attach_to_existing=True)

        self.wait = WebDriverWait(self.driver, 10)
        self.short_wait = WebDriverWait(self.driver, 3)

        self._msg = (None if no_message
                     else MessageTemplates(message_file, max_length=300))

        self.connections_sent = 0
        self.connections_failed = 0
        self.connections_skipped = 0
        self.non_tech_skipped = 0
        # Of the invitations sent, how many took the profile detour.
        self.profile_invites_sent = 0
        # Not shrunk by --fast: these are how long the page needs to render,
        # not pauses added to look human.
        self.mount_settle = MOUNT_SETTLE_SECONDS
        self.page_timeout = PROFILE_TIMEOUT_SECONDS
        self.menu_timeout = MENU_TIMEOUT_SECONDS

    def _pause_seconds(self, low, high):
        """How long the next humanizing pause should last, in seconds."""
        delay = random.uniform(low, high)
        if self.fast:
            delay = max(delay * FAST_PAUSE_FACTOR, MIN_FAST_PAUSE)
        return delay

    def _human_pause(self, low, high):
        """Sleep a randomized human-like interval, shortened under --fast."""
        delay = self._pause_seconds(low, high)
        logger.debug(f"Pausing {delay:.1f}s{' (fast)' if self.fast else ''}")
        time.sleep(delay)

    def fill_message_box(self, message_box, text):
        """Type the note into the modal textarea so LinkedIn registers it and enables Send.

        Non-BMP characters (emoji) break ChromeDriver's send_keys, so we type
        the BMP part first to activate the framework binding, then inject the
        full text via the native value setter.
        """
        try:
            message_box.click()
        except Exception:
            self.driver.execute_script("arguments[0].focus();", message_box)

        message_box.clear()
        bmp_text = ''.join(ch for ch in text if ord(ch) <= 0xFFFF)

        if bmp_text == text:
            message_box.send_keys(text)
            return

        if bmp_text:
            message_box.send_keys(bmp_text)
        self.driver.execute_script(
            "const el = arguments[0], val = arguments[1];"
            "const setter = Object.getOwnPropertyDescriptor("
            "window.HTMLTextAreaElement.prototype, 'value').set;"
            "setter.call(el, val);"
            "el.dispatchEvent(new Event('input', { bubbles: true }));"
            "el.dispatchEvent(new Event('change', { bubbles: true }));",
            message_box, text)

    def get_modal_shadow_root(self, timeout=10):
        """Return the #interop-outlet shadow root once the invite modal is inside it.

        LinkedIn renders the connect modal inside an open Shadow DOM host.
        Selenium can't reach shadow content with XPath, so every modal
        interaction goes through this shadow root (CSS selectors only).
        Returns the ShadowRoot, or None if the modal never appeared.
        """
        end = time.time() + timeout
        while time.time() < end:
            for host_sel in ("#interop-outlet", "[data-testid='interop-shadowdom']"):
                hosts = self.driver.find_elements(By.CSS_SELECTOR, host_sel)
                for host in hosts:
                    try:
                        sr = host.shadow_root
                    except Exception:
                        continue
                    try:
                        if sr.find_elements(
                                By.CSS_SELECTOR,
                                "[data-test-modal-id='send-invite-modal'], "
                                "[data-test-modal] [id='send-invite-modal']"):
                            return sr
                    except Exception:
                        continue
            time.sleep(0.3)
        return None

    def find_in_shadow(self, shadow_root, css, timeout=10, require_enabled=False):
        """Wait for and return a visible element matching css inside a shadow root."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                for el in shadow_root.find_elements(By.CSS_SELECTOR, css):
                    try:
                        if el.is_displayed() and (not require_enabled or el.is_enabled()):
                            return el
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def wait_modal_closed(self, shadow_root, timeout=5):
        """Return True once the invite modal is no longer present in the shadow root."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                if not shadow_root.find_elements(
                        By.CSS_SELECTOR, "[data-test-modal-id='send-invite-modal']"):
                    return True
            except Exception:
                return True
            time.sleep(0.3)
        return False

    def modal_requires_email(self, shadow_root):
        """Detect the 'enter their email to connect' screen; caller cancels and skips."""
        try:
            return bool(shadow_root.find_elements(
                By.CSS_SELECTOR,
                "input[type='email'], input[name='email'], "
                "[data-test-send-invite-modal-check-email-link]"))
        except Exception:
            return False

    def dismiss_open_modal(self):
        """Best-effort close of any open invite modal (shadow DOM first, then light DOM)."""
        try:
            for host_sel in ("#interop-outlet", "[data-testid='interop-shadowdom']"):
                for host in self.driver.find_elements(By.CSS_SELECTOR, host_sel):
                    try:
                        sr = host.shadow_root
                    except Exception:
                        continue
                    btns = sr.find_elements(By.CSS_SELECTOR, "button[aria-label='Dismiss']")
                    if btns:
                        self._robust_click(btns[0])
                        return
        except Exception:
            pass
        try:
            btns = self.driver.find_elements(By.XPATH, "//button[contains(@aria-label, 'Dismiss')]")
            if btns:
                self.driver.execute_script("arguments[0].click();", btns[0])
        except Exception:
            pass

    def check_invitation_limit_warning(self):
        """Check for invite limit dialogs. Returns False if automation must stop."""
        try:
            hard_limit_elements = self.driver.find_elements(
                By.XPATH,
                "//h2[contains(text(), 'reached the weekly invitation limit')] | "
                "//h2[@id='ip-fuse-limit-alert__header' and contains(text(), 'reached the weekly')] | "
                "//div[contains(@class, 'ip-fuse-limit-alert')]//h2[contains(text(), 'reached')]")

            if hard_limit_elements:
                logger.error("Weekly invitation limit reached. Stopping automation.")
                try:
                    got_it = self.driver.find_element(
                        By.XPATH,
                        "//button[.//span[text()='Got it']] | "
                        "//button[contains(@class, 'ip-fuse-limit-alert__primary-action')]")
                    self.driver.execute_script("arguments[0].click();", got_it)
                except Exception:
                    try:
                        self.driver.find_element(
                            By.XPATH, "//button[@aria-label='Dismiss']")
                    except Exception:
                        pass
                return False

            warning_elements = self.driver.find_elements(
                By.XPATH,
                "//h2[contains(text(), 'close to the weekly invitation limit')] | "
                "//div[contains(@class, 'ip-fuse-limit-alert')]//h2[contains(text(), 'close to')]")

            if warning_elements:
                logger.warning("Close to the weekly invitation limit!")

                if self.auto_continue:
                    logger.info("Auto-continue enabled (-y). Continuing past the warning.")
                    got_it = self.driver.find_element(
                        By.XPATH,
                        "//button[.//span[text()='Got it']] | "
                        "//button[contains(@class, 'ip-fuse-limit-alert__primary-action')]")
                    self.driver.execute_script("arguments[0].click();", got_it)
                    time.sleep(1)
                    return True
                else:
                    decision = input("\nUse remaining invites? (y/N): ").strip().lower()
                    if decision in ("yes", "y"):
                        logger.info("Continuing automation.")
                        got_it = self.driver.find_element(
                            By.XPATH,
                            "//button[.//span[text()='Got it']] | "
                            "//button[contains(@class, 'ip-fuse-limit-alert__primary-action')]")
                        self.driver.execute_script("arguments[0].click();", got_it)
                        time.sleep(1)
                        return True
                    else:
                        logger.info("Stopping to save remaining invites.")
                        return False

            return True
        except Exception as e:
            logger.error(f"Error checking invitation limit: {e}")
            return True

    def _drain_performance_logs(self):
        """Return newly buffered performance-log messages as parsed dicts."""
        if not self.perf_logging:
            return []
        try:
            raw = self.driver.get_log("performance")
        except Exception as e:
            logger.debug(f"Could not read performance log: {type(e).__name__}: {e}")
            return []

        messages = []
        for entry in raw:
            try:
                messages.append(json.loads(entry["message"])["message"])
            except Exception:
                continue
        return messages

    def _log_quota_from_invite_response(self, request_id, headers):
        """Log any rate-limit headers from a successful invite response (future-proofing)."""
        quota_headers = {k: v for k, v in headers.items()
                         if any(kw in k.lower() for kw in
                                ("ratelimit", "x-rate-limit", "quota", "remaining"))}
        if quota_headers:
            logger.info(f"[QUOTA] Invite endpoint quota headers: {quota_headers}")

    def detect_rate_limit_429(self, wait=3.0):
        """Watch network traffic for HTTP 429 on the invite endpoint.

        LinkedIn answers 429 when the quota is spent — sometimes without any UI
        dialog. Returns True (and stops) if such a 429 is detected.
        """
        if not self.perf_logging:
            return False

        end = time.time() + wait
        while True:
            for msg in self._drain_performance_logs():
                if msg.get("method") != "Network.responseReceived":
                    continue
                params = msg.get("params", {})
                response = params.get("response", {})
                url = response.get("url", "")
                if not any(frag in url for frag in INVITE_ENDPOINT_FRAGMENTS):
                    continue
                status = response.get("status")
                if status == 429:
                    logger.error(
                        "HTTP 429 from LinkedIn's invitation endpoint — "
                        "quota exhausted. Stopping.")
                    return True
                if status in (200, 201):
                    self._log_quota_from_invite_response(
                        params.get("requestId"), response.get("headers", {}))
            if time.time() >= end:
                return False
            time.sleep(0.5)

    def verify_successful_invitation_sent(self, target_label=None, full_name=None):
        """Confirm the invite registered by checking that the Connect control turned Pending."""
        try:
            time.sleep(2)

            if not self.check_invitation_limit_warning():
                return False

            if not target_label:
                return True

            if full_name:
                pending = self.driver.find_elements(
                    By.XPATH, "//a[contains(@aria-label, 'Pending')] | "
                              "//button[contains(@aria-label, 'Pending')]")
                for el in pending:
                    label = el.get_attribute("aria-label") or ""
                    if full_name in label:
                        logger.debug(f"Confirmed Pending state for {full_name}")
                        return True

            still_connect = self.driver.find_elements(
                By.XPATH, "//a[@aria-label=" + self._xpath_literal(target_label) + "]")
            if still_connect:
                logger.warning(
                    f"Connect control still present for {target_label} — "
                    "invite did NOT register (likely an ignored click)")
                return False

            logger.debug(f"Connect control for {target_label} is gone — assuming sent")
            return True

        except Exception as e:
            logger.debug(f"Error verifying invitation: {e}")
            return True

    @staticmethod
    def _xpath_literal(value):
        """Build a safe XPath string literal that handles embedded quotes."""
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        parts = value.split('"')
        return "concat(" + ", '\"', ".join(f'"{p}"' for p in parts) + ")"

    @staticmethod
    def full_name_from_label(aria_label):
        """The person's full name out of a card's action label.

        Both shapes a card can offer name the person: 'Invite <Full Name> to
        connect' and 'Follow <Full Name>'.
        """
        if not aria_label:
            return None
        match = re.match(r"Invite\s+(.+?)\s+to connect", aria_label, re.IGNORECASE)
        if match is None:
            match = re.match(r"Follow\s+(.+)", aria_label, re.IGNORECASE)
        if match is None:
            return None
        return match.group(1).strip() or None

    def extract_name_from_aria_label(self, aria_label):
        """Extract the display first name from a card's Connect or Follow label."""
        full_name = self.full_name_from_label(aria_label)
        return display_first_name(full_name) if full_name else None

    def extract_name_from_profile(self, connect_button):
        """Climb the DOM from the Connect button to find the person's name span."""
        try:
            parent_element = connect_button
            for _ in range(10):
                parent_element = parent_element.find_element(By.XPATH, "..")

                links = parent_element.find_elements(
                    By.XPATH, ".//a[contains(@href, 'linkedin.com/in/')]")
                if links:
                    for link in links:
                        try:
                            name_span = link.find_element(By.XPATH, ".//span[@aria-hidden='true']")
                            full_name = name_span.text.strip()
                            if full_name:
                                first_name = full_name.split()[0]
                                logger.debug(f"Found name: {full_name}, using: {first_name}")
                                return first_name
                        except Exception:
                            continue

                try:
                    spans = parent_element.find_elements(
                        By.XPATH,
                        ".//span[contains(@class, 'entity-result__title-text')]"
                        "//span[@aria-hidden='true']")
                    if spans:
                        for span in spans:
                            name_text = span.text.strip()
                            if name_text and " " in name_text:
                                return name_text.split()[0]
                except Exception:
                    pass

            all_name_links = self.driver.find_elements(
                By.XPATH,
                "//a[contains(@href, 'linkedin.com/in/')]//span[@aria-hidden='true']")
            if all_name_links:
                button_location = connect_button.location
                closest_distance = float('inf')
                closest_name = None
                for elem in all_name_links:
                    try:
                        loc = elem.location
                        distance = ((loc['x'] - button_location['x']) ** 2 +
                                    (loc['y'] - button_location['y']) ** 2) ** 0.5
                        if distance < closest_distance:
                            name_text = elem.text.strip()
                            if name_text:
                                closest_distance = distance
                                closest_name = name_text.split()[0]
                    except Exception:
                        continue
                if closest_name:
                    logger.debug(f"Found name by proximity: {closest_name}")
                    return closest_name

            logger.debug("Could not extract name from profile")
            return None

        except Exception as e:
            logger.debug(f"Error extracting name: {e}")
            return None

    #: Prefixes of the "Current: ... / Past: ..." snippet under a search result.
    _ROLE_SNIPPET_PREFIXES = ("current:", "past:", "atual:", "anterior:")

    def find_result_card(self, connect_button):
        """Climb from a Connect control to the ancestor holding one profile.

        Stops one level below the ancestor that contains a *second* profile
        link, which is the result list itself — going that far would read the
        neighbouring person's headline.
        """
        card = None
        element = connect_button
        for _ in range(12):
            try:
                element = element.find_element(By.XPATH, "..")
            except Exception:
                break
            try:
                links = element.find_elements(
                    By.XPATH, ".//a[contains(@href, 'linkedin.com/in/')]")
            except Exception:
                continue
            hrefs = {(link.get_attribute("href") or "").split("?")[0].rstrip("/")
                     for link in links}
            hrefs.discard("")
            if len(hrefs) > 1:
                break
            if hrefs:
                card = element
        return card

    def extract_title_texts(self, connect_button):
        """Return the headline (and role snippet) shown on this person's card.

        Both are returned because LinkedIn does not always render a headline;
        when it is missing the "Current: Tech recruiter at X" snippet carries
        the same information.
        """
        card = self.find_result_card(connect_button)
        if card is None:
            return []

        headline = None
        snippet = None
        try:
            paragraphs = card.find_elements(By.XPATH, ".//p")
        except Exception:
            paragraphs = []

        for paragraph in paragraphs:
            try:
                text = paragraph.text.strip()
            except Exception:
                continue
            if not text:
                continue
            lowered = text.lower()
            if lowered.startswith(self._ROLE_SNIPPET_PREFIXES):
                if snippet is None:
                    snippet = text
                continue
            # The name paragraph wraps the profile link; skip it.
            try:
                if paragraph.find_elements(
                        By.XPATH, ".//a[contains(@href, 'linkedin.com/in/')]"):
                    continue
            except Exception:
                pass
            if headline is None:
                headline = text

        if headline is None and snippet is None:
            # Older markup renders the headline outside a <p>; fall back to the
            # card's rendered text, whose second line is the headline.
            try:
                lines = [ln.strip() for ln in card.text.splitlines() if ln.strip()]
            except Exception:
                lines = []
            if len(lines) > 1:
                headline = lines[1]

        return [text for text in (headline, snippet) if text]

    def evaluate_title(self, connect_button, target_label):
        """Score this person's headline. Returns the best verdict, or None."""
        titles = self.extract_title_texts(connect_button)
        if not titles:
            logger.debug(f"No headline found on the card for {target_label}")
            return None

        best = max((score_title(text) for text in titles),
                   key=lambda verdict: verdict.score)
        logger.debug(f"Title for {target_label}: {best!r}")
        return best

    def extract_name_from_modal(self, shadow_root=None):
        """Extract name from the invite modal body (<strong>Full Name</strong>)."""
        try:
            if shadow_root is not None:
                try:
                    for el in shadow_root.find_elements(
                            By.CSS_SELECTOR, ".artdeco-modal__content strong"):
                        text = el.text.strip()
                        if text:
                            first_name = display_first_name(text)
                            logger.debug(f"Extracted name from modal body: {first_name}")
                            return first_name
                except Exception:
                    pass

            strong_elements = self.driver.find_elements(
                By.XPATH,
                "//div[@data-test-modal]//div[contains(@class, 'artdeco-modal__content')]//strong | "
                "//div[contains(@class, 'artdeco-modal')]//div[contains(@class, 'artdeco-modal__content')]//strong")
            for el in strong_elements:
                text = el.text.strip()
                if text:
                    first_name = display_first_name(text)
                    logger.debug(f"Extracted name from modal body: {first_name}")
                    return first_name

            modal_name_elements = self.driver.find_elements(
                By.XPATH, "//div[contains(@class, 'artdeco-modal')]//span[@aria-hidden='true']")
            for elem in modal_name_elements:
                name_text = elem.text.strip()
                if name_text and len(name_text.split()) >= 1:
                    if name_text.lower() in ("connect", "add a note", "send", "include", "add", "invite"):
                        continue
                    return name_text.split()[0]

            for selector in (
                "//div[contains(@class, 'artdeco-modal')]//h2",
                "//div[contains(@class, 'artdeco-modal')]//h3",
                "//div[contains(@class, 'send-invite')]//h2",
            ):
                try:
                    for element in self.driver.find_elements(By.XPATH, selector):
                        text = element.text.strip()
                        if not text:
                            continue
                        if "Connect with " in text:
                            return text.replace("Connect with ", "").split()[0]
                        if "Invite " in text and " to connect" in text:
                            return text.replace("Invite ", "").replace(" to connect", "").split()[0]
                except Exception:
                    continue

            logger.debug("Could not extract name from modal")
            return None
        except Exception as e:
            logger.debug(f"Error extracting name from modal: {e}")
            return None

    def results_state(self):
        """Card count and scroll position of the results list."""
        return list_state(self.driver, RESULT_LIST, RESULT_CARD)

    def wait_for_results(self, timeout=RESULTS_TIMEOUT_SECONDS):
        """Park the results list at the top and wait for its first card.

        Returns the number of cards mounted, or 0 if the page never rendered
        any — which is a page worth retrying, not a page worth skipping.
        """
        end = time.time() + timeout
        while True:
            state = scroll_list_to_top(self.driver, RESULT_LIST, RESULT_CARD)
            if state.get("cards"):
                return state["cards"]
            if time.time() >= end:
                return 0
            time.sleep(0.5)

    def mount_more_results(self):
        """Scroll one step further down the list so more rows mount.

        Returns True while there is more of the page left to scan: either new
        rows appeared, or the scroller had not yet reached the bottom.
        """
        before = self.results_state()
        after = scroll_list_step(self.driver, RESULT_LIST, RESULT_CARD)
        if self.mount_settle:
            time.sleep(self.mount_settle)
            after = self.results_state()

        if after.get("cards", 0) > before.get("cards", 0):
            logger.debug(
                f"Mounted {after['cards'] - before['cards']} more result(s) "
                f"({after['cards']} on the page)")
            return True

        # The scroller moved, so rows that were out of view are now in it.
        if after.get("top") != before.get("top"):
            return True

        # Bottom reached with nothing new: give a slow render one last chance
        # before calling the page finished. Reading the count a beat too
        # early is exactly how rows used to go unseen.
        if self.mount_settle:
            time.sleep(self.mount_settle)
            if self.results_state().get("cards", 0) > after.get("cards", 0):
                return True

        return not after.get("atBottom", True)

    def needs_profile_detour(self, control):
        """Whether this Follow button is the only way in to its card's person.

        A Follow button outside a result card has no profile behind it to
        open, and a card that carries a Connect control as well is reachable
        without leaving the page at all.
        """
        card = self.find_result_card(control)
        if card is None:
            return False
        try:
            return not card.find_elements(By.XPATH, CARD_INVITE_XPATH)
        except Exception:
            return False

    def next_target(self, processed_labels, include_follow=True):
        """Return the next (control, aria-label, kind) this page has not handled.

        One walk covers the whole page, in the order it shows people: each is
        handed back with the route their own card allows — "connect" for the
        Invite control on the card, "follow" for someone who has to be invited
        from their profile instead.

        Scrolling is part of the search: LinkedIn mounts result rows lazily,
        so a scan that finds nothing among the mounted ones has to pull the
        rest of the list into the DOM before it can conclude the page is
        done. Returns (None, None, None) once the whole list has been walked.
        """
        xpath = TARGET_XPATH if include_follow else CONNECT_XPATH
        for _ in range(MOUNT_STEP_LIMIT):
            for control in self.driver.find_elements(By.XPATH, xpath):
                try:
                    label = control.get_attribute("aria-label")
                except Exception:
                    continue
                if not label or label in processed_labels:
                    continue
                if label.startswith("Invite "):
                    return control, label, "connect"
                if self.needs_profile_detour(control):
                    return control, label, "follow"

            if not self.mount_more_results():
                return None, None, None

        logger.warning(
            f"Gave up mounting this page after {MOUNT_STEP_LIMIT} scroll steps")
        return None, None, None

    def page_action_tally(self):
        """How this page's cards break down by the action they offer.

        A page with ten cards and no Connect control is a normal LinkedIn
        result — Follow-only and Message-only profiles cannot be invited from
        search — but it used to be indistinguishable from a page the bot had
        simply failed to read. Logging the split tells the two apart, and
        says how much of the page had to go the long way round.
        """
        js = (
            "/*linkedin-connect:tally*/"
            "const cards = [...document.querySelectorAll(arguments[0])];"
            "let connect = 0, follow = 0, message = 0, other = 0;"
            "for (const c of cards) {"
            "  if (c.querySelector(\"a[aria-label^='Invite ']\")) connect++;"
            "  else if (c.querySelector(\"button[aria-label^='Follow'], "
            "                           a[aria-label^='Follow']\")) follow++;"
            "  else if (c.querySelector(\"a[aria-label^='Send a message'], "
            "                           a[href*='messaging/compose']\")) message++;"
            "  else other++;"
            "}"
            "return {cards: cards.length, connect, follow, message, other};")
        try:
            tally = self.driver.execute_script(js, RESULT_CARD)
        except Exception as e:
            logger.debug(f"Could not tally page actions: {type(e).__name__}: {e}")
            return None
        return tally if isinstance(tally, dict) else None

    def log_page_scanned(self, handled, tally=None):
        """Report what the finished page actually held."""
        if tally is None:
            tally = self.page_action_tally()
        if not tally:
            logger.info(f"Page scanned — {handled} person(s) handled")
            return

        breakdown = ", ".join(
            f"{tally[key]} {key}-only" for key in ("follow", "message")
            if tally.get(key))
        detail = f" ({breakdown})" if breakdown else ""
        logger.info(
            f"Page scanned — {tally['cards']} result(s), "
            f"{tally['connect']} connectable{detail}; "
            f"{handled} person(s) handled")

    def results_page_is_restorable(self):
        """True when this results page can be left and then come back to.

        LinkedIn's search URL carries ``page=N``, so reloading it — or
        navigating away and stepping back — lands on the same page. On page 1
        the parameter is absent and a reload lands there anyway. Anywhere
        else without it, leaving would silently restart the run from the top.
        """
        page = self.current_page_number()
        try:
            url = self.driver.current_url or ""
        except Exception:
            url = ""
        return "page=" in url or page in (None, 1)

    def reload_results(self):
        """Reload this results page in place, keeping our spot in the pagination."""
        before = self.current_page_number()
        if not self.results_page_is_restorable():
            logger.debug(
                f"Not reloading page {before}: this URL does not carry the "
                f"page number, so a reload would restart from page 1")
            return False

        try:
            self.driver.refresh()
        except Exception as e:
            logger.warning(f"Could not reload the page: {e}")
            return False

        if not self.wait_for_results():
            return False

        after = self.current_page_number()
        if before is not None and after is not None and after != before:
            logger.warning(f"Reloading moved from page {before} to page {after}")
        return True

    def await_results(self):
        """Make sure this page actually rendered its results before scanning.

        A page that renders nothing is a page the bot cannot read, and paging
        past it loses those people silently — the failure this whole scan
        path exists to stop. So an empty page is reloaded once, and if it is
        still empty the run stops loudly instead of walking on.
        """
        if self.wait_for_results():
            return True

        logger.warning("No results rendered on this page — reloading it once")
        if self.reload_results():
            logger.info("Results rendered after the reload")
            return True

        logger.error(
            "This page still shows no results after a reload. Stopping here "
            "rather than paging past people the bot never saw.")
        return False

    def profile_url_for(self, control):
        """The profile URL of the person whose result card holds this control."""
        card = self.find_result_card(control)
        links = []
        if card is not None:
            try:
                links = card.find_elements(By.XPATH, ".//a[contains(@href, '/in/')]")
            except Exception:
                links = []
        if not links:
            # Some card layouts wrap the whole row in the profile link itself,
            # so there is no descendant anchor to find — only an ancestor one.
            try:
                links = [control.find_element(
                    By.XPATH, "ancestor::a[contains(@href, '/in/')][1]")]
            except Exception:
                links = []

        for link in links:
            try:
                href = link.get_attribute("href") or ""
            except Exception:
                continue
            if "/in/" in href:
                # The person's own link always comes first in their card; the
                # ones after it belong to shared connections.
                return href.split("?")[0]
        return None

    @staticmethod
    def _same_person(one, other):
        """Compare two names ignoring case, whitespace and unicode form.

        "Nájera" can reach us composed from the card and decomposed from the
        profile; the two spell the same name and must compare equal.
        """
        def key(name):
            return unicodedata.normalize("NFC", " ".join((name or "").split())).casefold()

        return key(one) == key(other)

    def find_everywhere(self, css):
        """Every element matching this CSS, document and shadow roots alike.

        LinkedIn renders its overlays — the invite modal, and the popover the
        More menu lives in — into an open Shadow DOM host, where a plain
        document query cannot see them.
        """
        found = []
        try:
            found.extend(self.driver.find_elements(By.CSS_SELECTOR, css))
        except Exception:
            pass
        for host_sel in ("#interop-outlet", "[data-testid='interop-shadowdom']"):
            try:
                hosts = self.driver.find_elements(By.CSS_SELECTOR, host_sel)
            except Exception:
                continue
            for host in hosts:
                try:
                    found.extend(host.shadow_root.find_elements(By.CSS_SELECTOR, css))
                except Exception:
                    continue
        return found

    def clickable_for(self, element):
        """The anchor or button that actually carries this control's action.

        A menu item labels its inner <div>, not the <a href="/preload/custom-
        invite/…"> around it. A real click on the div bubbles up to the
        anchor, but the JavaScript click at the bottom of the click ladder
        would not follow the link, so the anchor is what gets handed back.
        """
        try:
            found = element.find_elements(By.XPATH, CLICKABLE_XPATH)
        except Exception:
            return element
        return found[0] if found else element

    def find_profile_connect(self, full_name, timeout=None):
        """The 'Invite <full_name> to connect' control on the open profile.

        Matched by name on purpose: a profile page carries Connect buttons for
        *other* people too — the "More profiles for you" rail — and clicking
        one of those would send the invitation to the wrong person.
        """
        end = time.time() + (self.menu_timeout if timeout is None else timeout)
        while True:
            for control in self.find_everywhere(PROFILE_CONNECT_CSS):
                try:
                    if not control.is_displayed():
                        continue
                    label = control.get_attribute("aria-label") or ""
                except Exception:
                    continue
                if full_name and not self._same_person(
                        self.full_name_from_label(label), full_name):
                    continue
                return self.clickable_for(control)
            if time.time() >= end:
                return None
            time.sleep(0.3)

    def connect_in_open_menu(self):
        """The Connect item of whatever profile menu is open, found by place.

        Matching on the name is the safe search, but it leans on the card and
        the profile spelling that name the same way. Inside the person's own
        More menu there is only one person Connect can mean, so a Connect item
        there is taken even when the names did not line up — or when LinkedIn
        labelled the item with nothing at all and only its text says what it
        does.
        """
        for menu in self.find_everywhere(MENU_CSS):
            try:
                if not menu.is_displayed():
                    continue
            except Exception:
                continue

            try:
                labelled = menu.find_elements(By.CSS_SELECTOR, PROFILE_CONNECT_CSS)
            except Exception:
                labelled = []
            for control in labelled:
                try:
                    if control.is_displayed():
                        return self.clickable_for(control)
                except Exception:
                    continue

            try:
                items = menu.find_elements(By.CSS_SELECTOR, MENU_ITEM_CSS)
            except Exception:
                items = []
            for item in items:
                try:
                    if item.is_displayed() and (item.text or "").strip().casefold() == "connect":
                        return self.clickable_for(item)
                except Exception:
                    continue
        return None

    def close_open_menu(self):
        """Press Escape to close whatever dropdown is open."""
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
        except Exception as e:
            logger.debug(f"Could not close the open menu: {type(e).__name__}: {e}")

    def connect_from_more_menu(self, full_name):
        """Open the profile's three-dots menu and return the Connect item in it.

        For someone you can only Follow, Connect is not in the top card at
        all — it lives one level down, behind More. The sticky header renders
        a second More button, so each one is tried in turn until a menu with
        Connect in it opens.
        """
        try:
            buttons = self.driver.find_elements(By.XPATH, MORE_BUTTON_XPATH)
        except Exception as e:
            logger.debug(f"Could not look for a More button: {type(e).__name__}: {e}")
            return None

        tried = 0
        for button in buttons:
            if tried >= MORE_BUTTON_ATTEMPTS:
                break
            try:
                if not button.is_displayed():
                    continue
            except Exception:
                continue
            tried += 1
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button)
            except Exception:
                pass
            self._human_pause(2, 5)
            self._robust_click(button, f"More button on {full_name}'s profile")
            control = self.find_profile_connect(full_name)
            if control is None:
                control = self.connect_in_open_menu()
                if control is not None:
                    logger.info(
                        f"Taking Connect from the open menu on {full_name}'s "
                        f"profile — nothing on the page carries that name")
            if control is not None:
                return control
            logger.debug("That More menu holds no Connect; closing it")
            self.close_open_menu()

        return None

    def wait_for_profile(self, full_name, timeout=None):
        """Wait until the profile page has rendered its action buttons."""
        timeout = self.page_timeout if timeout is None else timeout
        end = time.time() + timeout
        while True:
            try:
                if (self.driver.find_elements(By.XPATH, MORE_BUTTON_XPATH)
                        or self.driver.find_elements(
                            By.CSS_SELECTOR, PROFILE_CONNECT_CSS)):
                    return True
            except Exception:
                pass
            if time.time() >= end:
                logger.warning(
                    f"{full_name}'s profile did not render its actions "
                    f"within {timeout}s")
                return False
            time.sleep(0.5)

    def connect_on_profile(self, full_name, name):
        """Send the invitation from the person's own profile page.

        Returns the same verdicts as ``complete_invite_modal``.
        """
        if not self.wait_for_profile(full_name):
            self.connections_skipped += 1
            return "skipped"

        # The actions have rendered by now, so a Connect in the top card is
        # already there to be found; anything else is behind the menu.
        control = self.find_profile_connect(full_name, timeout=0)
        if control is None:
            control = self.connect_from_more_menu(full_name)
        if control is None:
            logger.warning(
                f"No Connect action on {full_name}'s profile — not even "
                f"behind More. Skipping.")
            self.connections_skipped += 1
            return "skipped"

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", control)
        except Exception:
            pass
        self._human_pause(2, 4)
        self._robust_click(control, f"Connect control on {full_name}'s profile")
        time.sleep(3)

        status = self.complete_invite_modal(f"Invite {full_name} to connect", name)
        if status == "sent":
            self.profile_invites_sent += 1
        return status

    def on_results_page(self):
        """Whether the browser is showing a search-results page right now."""
        try:
            return "/search/results" in (self.driver.current_url or "")
        except Exception:
            return False

    def return_to_results(self, results_url):
        """Step back from a profile onto the results page the detour left.

        Returns False when the results could not be restored, which ends the
        run — paging on from a page the bot cannot see loses everyone on it.
        """
        if self.on_results_page():
            # The detour never left: a profile that failed to open leaves the
            # results on screen, and stepping back from *there* would land on
            # the previous page of results and rescan the wrong people.
            return bool(self.await_results())

        try:
            self.driver.back()
        except Exception as e:
            logger.warning(f"Could not go back to the results: {e}")

        end = time.time() + self.page_timeout
        while True:
            if self.on_results_page():
                break
            if time.time() >= end:
                logger.warning(
                    "Going back did not land on the search results; opening "
                    "them again directly")
                try:
                    self.driver.get(results_url)
                except Exception as e:
                    logger.error(f"Could not reopen the results page: {e}")
                    return False
                break
            time.sleep(0.5)

        return bool(self.await_results())

    def invite_via_profile(self, control, label, name):
        """Invite someone whose result card offers nothing but Follow.

        The detour: open their profile, take Connect out of the More menu,
        run the usual note-and-Send ritual there, then step back into the
        results, where the scan resumes from the person just handled.
        """
        full_name = self.full_name_from_label(label) or label
        profile_url = self.profile_url_for(control)
        if not profile_url:
            logger.warning(f"No profile link on the card for {label}. Skipping.")
            self.connections_skipped += 1
            return "skipped"

        try:
            results_url = self.driver.current_url
        except Exception:
            results_url = None

        logger.info(
            f"{full_name} can only be followed from the results — opening "
            f"{profile_url} to connect there instead")

        try:
            self.driver.get(profile_url)
        except Exception as e:
            logger.error(f"Could not open {profile_url}: {e}")
            self.connections_skipped += 1
            status = "skipped"
        else:
            try:
                status = self.connect_on_profile(full_name, name)
            except Exception as e:
                logger.error(f"Error connecting from {profile_url}: {e}")
                try:
                    self.dismiss_open_modal()
                except Exception:
                    pass
                self.connections_skipped += 1
                status = "skipped"

        if not self.return_to_results(results_url):
            logger.error(
                "Lost the results page after the profile detour. Stopping "
                "rather than paging on from somewhere else.")
            return "stop"
        return status

    def complete_invite_modal(self, target_label, name):
        """Drive the open invite modal through to a confirmed invitation.

        Shared by both routes into it — the Connect control on a search card
        and the one behind a profile's More menu — because the ritual (note,
        Send, confirmation, ledger) is the same either way.

        Returns "sent", "failed", "skipped", or "stop" when the run must end.
        """
        shadow = self.get_modal_shadow_root(timeout=10)
        if shadow is None:
            if not self.check_invitation_limit_warning():
                return "stop"
            logger.warning(f"No modal appeared for {target_label}. Skipping.")
            self.connections_skipped += 1
            return "skipped"

        if not self.check_invitation_limit_warning():
            return "stop"

        if self.modal_requires_email(shadow):
            logger.warning(f"{target_label} requires email to connect. Skipping.")
            self.dismiss_open_modal()
            self.wait_modal_closed(shadow, timeout=3)
            self.connections_skipped += 1
            self._human_pause(2, 4)
            return "skipped"

        if not name:
            name = self.extract_name_from_modal(shadow)

        if self.no_message:
            send_btn = self.find_in_shadow(
                shadow, "button[aria-label='Send without a note']", require_enabled=True)
            if send_btn is None:
                if not self.check_invitation_limit_warning():
                    return "stop"
                logger.warning(f"No 'Send without a note' button for {target_label}. Skipping.")
                self.connections_skipped += 1
                return "skipped"
            self._robust_click(send_btn, "Send without a note button")
            logger.info(f"Sending without note to {name or target_label}")
        else:
            add_note_btn = self.find_in_shadow(
                shadow, "button[aria-label='Add a note']", require_enabled=True)
            if add_note_btn is None:
                if not self.check_invitation_limit_warning():
                    return "stop"
                logger.warning(f"No 'Add a note' button for {target_label}. Skipping.")
                self.connections_skipped += 1
                return "skipped"
            self._robust_click(add_note_btn, "Add a note button")

            message_box = self.find_in_shadow(shadow, "#custom-message")
            if message_box is None:
                if not self.check_invitation_limit_warning():
                    return "stop"
                logger.warning(f"No message box appeared for {target_label}. Skipping.")
                self.connections_skipped += 1
                return "skipped"

            personalized_message = self._msg.personalize(name)
            logger.info(
                f"Sending to {name or target_label}: "
                f"{personalized_message.splitlines()[0] if personalized_message else ''}")

            self.fill_message_box(message_box, personalized_message)
            time.sleep(1)

            try:
                textarea_len = self.driver.execute_script(
                    "return (arguments[0].value || '').length;", message_box)
                logger.debug(
                    f"Textarea length as seen by LinkedIn: {textarea_len} "
                    f"(expected {len(personalized_message.encode('utf-16-le')) // 2})")
            except Exception:
                pass

            send_btn = self.find_in_shadow(
                shadow, "button[aria-label='Send invitation']", require_enabled=True)
            if send_btn is None:
                if not self.check_invitation_limit_warning():
                    return "stop"
                logger.warning(
                    f"Send button never became clickable for {target_label}. Skipping.")
                self.connections_skipped += 1
                return "skipped"

            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", send_btn)
            except Exception:
                pass
            logger.debug(f"Clicking Send (enabled={send_btn.is_enabled()}) for {target_label}")
            self._robust_click(send_btn, "Send invitation button")

        if not self.wait_modal_closed(shadow, timeout=5):
            if not self.check_invitation_limit_warning():
                return "stop"
            logger.warning(f"Modal never closed for {target_label}. Skipping.")
            return "skipped"

        if self.detect_rate_limit_429():
            return "stop"

        full_name = self.full_name_from_label(target_label)
        if self.verify_successful_invitation_sent(target_label, full_name):
            self.connections_sent += 1
            # Recorded before anything else can fail, so an invite is never
            # counted in the log but missing from the ledger.
            record_invite(full_name or name or target_label)
            logger.info(
                f"Invitation sent to {name or target_label} "
                f"[sent={self.connections_sent}, "
                f"failed={self.connections_failed}, "
                f"skipped={self.connections_skipped}]")

            if self.max_invites and self.connections_sent >= self.max_invites:
                logger.info(f"Reached --max {self.max_invites}. Stopping.")
                return "stop"
            return "sent"

        if not self.check_invitation_limit_warning():
            return "stop"
        self.connections_failed += 1
        logger.warning(
            f"Invite to {target_label} did not register "
            f"[sent={self.connections_sent}, "
            f"failed={self.connections_failed}, "
            f"skipped={self.connections_skipped}]")
        return "failed"

    def detours_allowed(self):
        """Whether Follow-only people on this page may be invited via profile.

        The detour leaves the results page, so it is only on the table when
        coming back lands on the same page of results.
        """
        if self.results_page_is_restorable():
            return True
        logger.warning(
            "This results URL does not carry its page number, so leaving it "
            "for a profile would restart the run from page 1 — the "
            "Follow-only people on this page are left alone.")
        return False

    def process_page(self):
        """Process every reachable person on the current page. Returns False to stop."""
        if not self.await_results():
            return False

        include_follow = self.detours_allowed()
        try:
            results_url = self.driver.current_url
        except Exception:
            results_url = None
        processed_labels = set()
        reloaded = False
        probed = False

        while True:
            if not self.check_invitation_limit_warning():
                logger.info("Stopping due to invitation limit.")
                return False

            target, target_label, kind = self.next_target(
                processed_labels, include_follow=include_follow)

            if target is None:
                tally = self.page_action_tally()
                # A full page of results where not one card offers Connect is
                # what LinkedIn starts serving after a burst of invitations:
                # the run's last two sessions each went dead this way, nine
                # pages in a row, and came back the moment the page was
                # loaded afresh. So the verdict is only accepted once it
                # survives a reload.
                if (not processed_labels and not reloaded and tally
                        and tally.get("cards") and not tally.get("connect")):
                    logger.info(
                        f"{tally['cards']} result(s) but none connectable — "
                        f"reloading the page before accepting that")
                    reloaded = True
                    if self.reload_results():
                        continue

                self.log_page_scanned(len(processed_labels), tally)
                break

            # The first person on this page can only be reached through
            # their profile. Before paying for that — and for the nine behind
            # them — find out whether the page is one LinkedIn has stripped
            # of its Connect controls, which is what it starts serving after
            # a burst of invitations, and which a reload usually undoes. The
            # connect-only scan walks the whole list, so the answer is about
            # the page rather than about the handful of rows mounted so far.
            if kind == "follow" and not processed_labels and not probed:
                probed = True
                connectable, _, _ = self.next_target(set(), include_follow=False)
                if connectable is None and not reloaded:
                    tally = self.page_action_tally()
                    if tally and tally.get("cards"):
                        logger.info(
                            f"{tally['cards']} result(s) and not one Connect "
                            f"control — reloading before spending a profile "
                            f"detour on each of them")
                        reloaded = True
                        if self.reload_results():
                            continue
                # The scan above moved the list, so the walk starts over to
                # pick the same person up by a handle that is still attached.
                continue

            processed_labels.add(target_label)

            name = self.extract_name_from_aria_label(target_label)
            if name:
                logger.info(f"Processing {target_label} → first name: {name}")

            # Judge the headline before clicking: a skipped card must cost
            # nothing against the weekly invitation quota — and, for a
            # Follow-only card, nothing in page loads either.
            if self.tech_only:
                verdict = self.evaluate_title(target, target_label)
                if verdict is None:
                    logger.warning(
                        f"Could not read a headline for {target_label}. "
                        f"Skipping (tech-recruiter filter is on).")
                    self.connections_skipped += 1
                    self.non_tech_skipped += 1
                    continue
                if verdict.score < self.min_title_score:
                    logger.info(
                        f"Skipping {name or target_label} — not a tech "
                        f"recruiter: {verdict.title!r} "
                        f"(score {verdict.score:.2f} < {self.min_title_score:.2f}; "
                        f"{verdict.reason})")
                    self.connections_skipped += 1
                    self.non_tech_skipped += 1
                    continue
                logger.info(
                    f"Tech recruiter confirmed for {name or target_label}: "
                    f"{verdict.title!r} (score {verdict.score:.2f}; {verdict.reason})")

            if kind == "follow":
                # The page is about to be left and restored, so every element
                # found on it goes stale; the scan picks up from the labels
                # already processed. Errors are caught here rather than in the
                # handler below, which assumes the results are still on screen.
                try:
                    status = self.invite_via_profile(target, target_label, name)
                except Exception as e:
                    logger.error(f"Error on the profile detour for {target_label}: {e}")
                    self.connections_skipped += 1
                    status = ("skipped" if self.return_to_results(results_url)
                              else "stop")
                if status == "stop":
                    return False
                self._human_pause(8, 18)
                continue

            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target)
                self._human_pause(2, 5)

                self._robust_click(target, f"Connect control ({target_label})")
                time.sleep(3)

                if self.complete_invite_modal(target_label, name) == "stop":
                    return False

                self._human_pause(8, 18)

            except ElementClickInterceptedException:
                logger.warning(f"Connect control for {target_label} was intercepted")
                if not self.check_invitation_limit_warning():
                    return False
                self.dismiss_open_modal()

            except Exception as e:
                logger.error(f"Error processing {target_label}: {e}")
                if not self.check_invitation_limit_warning():
                    return False
                try:
                    self.dismiss_open_modal()
                except Exception:
                    pass

        return True

    def select_search_tab(self):
        """Switch to the LinkedIn people-search tab among all open tabs."""
        try:
            handles = self.driver.window_handles
        except Exception as e:
            logger.error(f"Could not enumerate browser tabs: {e}")
            return False

        people_search = None
        any_search = None
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                url = (self.driver.current_url or "").lower()
            except Exception:
                continue
            if "linkedin.com/search/results/people" in url:
                people_search = h
                break
            if any_search is None and "linkedin.com/search/results" in url:
                any_search = h

        chosen = people_search or any_search
        if chosen is not None:
            self.driver.switch_to.window(chosen)
            logger.info(f"Using tab: {self.driver.current_url}")
            return True

        logger.warning("No LinkedIn people-search tab found. Using the current tab.")
        if handles:
            self.driver.switch_to.window(handles[0])
        return False

    def current_page_number(self):
        """The results page LinkedIn is showing, or None while it is swapping."""
        try:
            current = self.driver.find_element(
                By.XPATH, "//button[@aria-current='true']")
            return int(current.text.strip())
        except Exception:
            return None

    def go_to_next_page(self):
        """Navigate to the next or previous results page. Returns False when none available."""
        try:
            if not self.check_invitation_limit_warning():
                return False

            direction = "prev" if self.reverse else "next"
            nav_xpath = f"//button[starts-with(@data-testid, 'pagination-controls-{direction}-button')]"
            nav_css = f"button[data-testid^='pagination-controls-{direction}-button']"

            nav_button = None
            try:
                nav_button = self.short_wait.until(
                    EC.presence_of_element_located((By.XPATH, nav_xpath)))
            except (TimeoutException, NoSuchElementException):
                nav_button = None

            if nav_button is None:
                for host in self.driver.find_elements(
                        By.CSS_SELECTOR, "#interop-outlet, [data-testid='interop-shadowdom']"):
                    try:
                        sr = host.shadow_root
                    except Exception:
                        continue
                    found = sr.find_elements(By.CSS_SELECTOR, nav_css)
                    if found:
                        nav_button = found[0]
                        break

            if nav_button is None:
                logger.info(f"No {'previous' if self.reverse else 'next'} page button found")
                return False

            testid = nav_button.get_attribute("data-testid") or ""
            if "hidden" in testid or nav_button.get_attribute("disabled"):
                logger.info(f"No more pages ({'previous' if self.reverse else 'next'} button disabled)")
                return False

            current = self.current_page_number()
            if self.reverse and current == 1:
                logger.info("Reached first page")
                return False
            if not self.reverse and current == 100:
                logger.info("Reached LinkedIn's page limit (100)")
                return False

            self.driver.execute_script("arguments[0].scrollIntoView(true);", nav_button)
            time.sleep(1)
            current_url = self.driver.current_url
            before = self.current_page_number()
            self._robust_click(nav_button)

            # Wait for the swap rather than guessing at it: LinkedIn empties
            # the list for a moment and then remounts it, so a fixed sleep
            # either wastes time or scans the outgoing page.
            end = time.time() + 15
            while time.time() < end:
                time.sleep(0.5)
                if self.driver.current_url != current_url:
                    return True
                now = self.current_page_number()
                if now is not None and now != before:
                    return True

            logger.warning(
                f"The {'previous' if self.reverse else 'next'} page button did "
                f"not move off page {before}. Stopping rather than rescanning "
                f"the same page.")
            return False

        except (TimeoutException, NoSuchElementException):
            logger.warning(f"No {'previous' if self.reverse else 'next'} page button or it's disabled")
            return False
        except Exception as e:
            logger.error(f"Error navigating page: {e}")
            return False

    def log_summary(self, pages_processed):
        """Log the session and weekly totals. Safe to call from a finally block."""
        direction = "reverse" if self.reverse else "forward"
        logger.info(f"Completed ({direction}) — {pages_processed} page(s) processed.")
        logger.info(
            f"Session summary — sent: {self.connections_sent}"
            + (f" (of which {self.profile_invites_sent} via the profile detour)"
               if self.profile_invites_sent else "")
            + f" | failed: {self.connections_failed}"
            + f" | skipped: {self.connections_skipped}"
            + (f" (of which {self.non_tech_skipped} not tech recruiters)"
               if self.non_tech_skipped else ""))

        week = current_week_start()
        logger.info(
            f"Weekly total (week of {week}): "
            f"{count_invites_for_week(week)} invitation(s) sent")

    def run_automation(self, max_pages=100):
        """Run the full automation across all result pages."""
        page_num = 1
        prevent_sleep()
        self.select_search_tab()

        try:
            while page_num <= max_pages:
                logger.info(f"--- Processing page {page_num} ---")

                if not self.check_invitation_limit_warning():
                    logger.info("Stopping due to invitation limit.")
                    break

                if not self.process_page():
                    logger.info("Stopped.")
                    break

                if not self.go_to_next_page():
                    direction = "first" if self.reverse else "last"
                    logger.info(f"Reached the {direction} page.")
                    break

                page_num += 1
                self._human_pause(12, 25)
        except KeyboardInterrupt:
            # Swallowed here so the summary below still runs; main.py's
            # handler only covers interrupts outside this method.
            logger.warning("Stopped by user (Ctrl+C)")
        finally:
            allow_sleep()
            self.log_summary(page_num)
