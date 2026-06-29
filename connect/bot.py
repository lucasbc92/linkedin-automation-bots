import json
import logging
import random
import re
import time

from selenium.common.exceptions import (ElementClickInterceptedException,
                                        NoSuchElementException, TimeoutException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common.browser import create_driver
from common.names import display_first_name
from common.sleep import allow_sleep, prevent_sleep

logger = logging.getLogger("linkedin_bot")

# URL fragments of LinkedIn's invitation endpoint.
# A 429 on these paths means the invite quota is exhausted.
INVITE_ENDPOINT_FRAGMENTS = (
    "voyagerRelationshipsDashMemberRelationships",
    "verifyQuotaAndCreate",
)


class LinkedInConnectBot:
    def __init__(self, auto_continue=False,
                 message_file="connect/templates/message.txt",
                 reverse=False, no_message=False):
        self.auto_continue = auto_continue
        self.reverse = reverse
        self.no_message = no_message

        self.driver, self.perf_logging = create_driver(attach_to_existing=True)

        self.wait = WebDriverWait(self.driver, 10)
        self.short_wait = WebDriverWait(self.driver, 3)

        self.message_templates = [] if no_message else self.load_message_templates(message_file)

        self.connections_sent = 0
        self.connections_failed = 0
        self.connections_skipped = 0

    MESSAGE_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
    DEFAULT_MESSAGE = "Hello {name}! I'd like to connect with you."

    def load_message_templates(self, file_path):
        """Load one or more message variations from a text file.

        Variations are separated by a line containing only dashes ("---").
        Each variation is trimmed and capped at LinkedIn's 300-char limit.
        Returns a list with at least one template, or [] in no-message mode.
        """
        try:
            if self.no_message or not file_path:
                return []

            import os
            if not os.path.exists(file_path):
                logger.warning(f"Message file '{file_path}' not found. Using default message.")
                return [self.DEFAULT_MESSAGE]

            with open(file_path, 'r', encoding='utf-8') as f:
                raw = f.read()

            variations = [v.strip() for v in self.MESSAGE_SEPARATOR.split(raw)]
            variations = [v for v in variations if v]

            if not variations:
                logger.warning("Message file is empty. Using default message.")
                return [self.DEFAULT_MESSAGE]

            cleaned = []
            for v in variations:
                if len(v) > 300:
                    v = v[:300]
                    logger.warning("A message variation exceeded 300 chars and was truncated.")
                cleaned.append(v)

            logger.info(f"Loaded {len(cleaned)} message variation(s) from '{file_path}'.")
            return cleaned

        except Exception as e:
            logger.error(f"Error loading message file: {e}. Using default message.")
            return [self.DEFAULT_MESSAGE]

    @staticmethod
    def _message_length(text):
        """Count chars the way LinkedIn does: UTF-16 code units (emoji = 2)."""
        return len(text.encode('utf-16-le')) // 2

    def _remove_name_placeholder(self, template):
        """Drop {name} along with an adjacent comma/space so greeting still reads cleanly."""
        return re.sub(r",?\s*\{name\}", "", template)

    def personalize_message(self, name=None):
        """Pick a random variation and fill in the name, falling back to no-name if too long."""
        if not self.message_templates:
            return ""

        template = random.choice(self.message_templates)

        if name:
            personalized = template.replace("{name}", name)
            if self._message_length(personalized) <= 300:
                return personalized
            logger.warning(
                f"Message would be {self._message_length(personalized)} chars with "
                f"'{name}' (limit 300). Omitting name for this invite.")

        return self._remove_name_placeholder(template)

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
            self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {**params, "type": "mousePressed"})
            time.sleep(0.05)
            self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {**params, "type": "mouseReleased"})
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

        logger.warning(f"All trusted clicks failed on {description}; falling back to JS click - LinkedIn may ignore it")
        try:
            self.driver.execute_script("arguments[0].click();", element)
            return True
        except Exception as e:
            logger.warning(f"All click methods failed on {description}: {e}")
            return False

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

    def extract_name_from_aria_label(self, aria_label):
        """Extract the display name from 'Invite <Full Name> to connect'."""
        if not aria_label:
            return None
        match = re.match(r"Invite\s+(.+?)\s+to connect", aria_label, re.IGNORECASE)
        if match:
            full_name = match.group(1).strip()
            if full_name:
                return display_first_name(full_name)
        return None

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

    def process_page(self):
        """Process all Connect controls on the current page. Returns False to stop."""
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.XPATH, "//section[@aria-label='Primary content'] | //div[@role='listitem']")))
        except Exception:
            logger.warning("Could not find search results, trying to continue anyway")

        try:
            for _ in range(3):
                self.driver.execute_script("window.scrollBy(0, document.body.scrollHeight/3);")
                time.sleep(0.5)
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
        except Exception:
            pass

        connect_xpath = ("//a[starts-with(@aria-label, 'Invite ') and "
                         "contains(@aria-label, 'to connect')]")
        processed_labels = set()

        while True:
            if not self.check_invitation_limit_warning():
                logger.info("Stopping due to invitation limit.")
                return False

            connect_links = self.driver.find_elements(By.XPATH, connect_xpath)

            target = None
            target_label = None
            for link in connect_links:
                try:
                    label = link.get_attribute("aria-label")
                except Exception:
                    continue
                if label and label not in processed_labels:
                    target = link
                    target_label = label
                    break

            if target is None:
                logger.info("No more Connect controls on this page")
                break

            processed_labels.add(target_label)

            name = self.extract_name_from_aria_label(target_label)
            if name:
                logger.info(f"Processing {target_label} → first name: {name}")

            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", target)
                time.sleep(random.uniform(1, 2))

                self._robust_click(target, f"Connect control ({target_label})")
                time.sleep(3)

                shadow = self.get_modal_shadow_root(timeout=10)
                if shadow is None:
                    if not self.check_invitation_limit_warning():
                        return False
                    logger.warning(f"No modal appeared for {target_label}. Skipping.")
                    self.connections_skipped += 1
                    continue

                if not self.check_invitation_limit_warning():
                    return False

                if self.modal_requires_email(shadow):
                    logger.warning(f"{target_label} requires email to connect. Skipping.")
                    self.dismiss_open_modal()
                    self.wait_modal_closed(shadow, timeout=3)
                    self.connections_skipped += 1
                    time.sleep(random.uniform(1, 2))
                    continue

                if not name:
                    name = self.extract_name_from_modal(shadow)

                if self.no_message:
                    send_btn = self.find_in_shadow(
                        shadow, "button[aria-label='Send without a note']", require_enabled=True)
                    if send_btn is None:
                        if not self.check_invitation_limit_warning():
                            return False
                        logger.warning(f"No 'Send without a note' button for {target_label}. Skipping.")
                        self.connections_skipped += 1
                        continue
                    self._robust_click(send_btn, "Send without a note button")
                    logger.info(f"Sending without note to {name or target_label}")
                else:
                    add_note_btn = self.find_in_shadow(
                        shadow, "button[aria-label='Add a note']", require_enabled=True)
                    if add_note_btn is None:
                        if not self.check_invitation_limit_warning():
                            return False
                        logger.warning(f"No 'Add a note' button for {target_label}. Skipping.")
                        self.connections_skipped += 1
                        continue
                    self._robust_click(add_note_btn, "Add a note button")

                    message_box = self.find_in_shadow(shadow, "#custom-message")
                    if message_box is None:
                        if not self.check_invitation_limit_warning():
                            return False
                        logger.warning(f"No message box appeared for {target_label}. Skipping.")
                        self.connections_skipped += 1
                        continue

                    personalized_message = self.personalize_message(name)
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
                            f"(expected {self._message_length(personalized_message)})")
                    except Exception:
                        pass

                    send_btn = self.find_in_shadow(
                        shadow, "button[aria-label='Send invitation']", require_enabled=True)
                    if send_btn is None:
                        if not self.check_invitation_limit_warning():
                            return False
                        logger.warning(
                            f"Send button never became clickable for {target_label}. Skipping.")
                        self.connections_skipped += 1
                        continue

                    try:
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", send_btn)
                    except Exception:
                        pass
                    logger.debug(f"Clicking Send (enabled={send_btn.is_enabled()}) for {target_label}")
                    self._robust_click(send_btn, "Send invitation button")

                if not self.wait_modal_closed(shadow, timeout=5):
                    if not self.check_invitation_limit_warning():
                        return False
                    logger.warning(f"Modal never closed for {target_label}. Skipping.")
                    continue

                if self.detect_rate_limit_429():
                    return False

                m = re.match(r"Invite\s+(.+?)\s+to connect", target_label)
                full_name = m.group(1) if m else None
                if self.verify_successful_invitation_sent(target_label, full_name):
                    self.connections_sent += 1
                    logger.info(
                        f"Invitation sent to {name or target_label} "
                        f"[sent={self.connections_sent}, "
                        f"failed={self.connections_failed}, "
                        f"skipped={self.connections_skipped}]")
                else:
                    if not self.check_invitation_limit_warning():
                        return False
                    self.connections_failed += 1
                    logger.warning(
                        f"Invite to {target_label} did not register "
                        f"[sent={self.connections_sent}, "
                        f"failed={self.connections_failed}, "
                        f"skipped={self.connections_skipped}]")

                time.sleep(random.uniform(3, 5))

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

            if self.reverse:
                try:
                    current = self.driver.find_element(By.XPATH, "//button[@aria-current='true']")
                    if current.text.strip() == "1":
                        logger.info("Reached first page")
                        return False
                except Exception:
                    pass
            else:
                try:
                    current = self.driver.find_element(By.XPATH, "//button[@aria-current='true']")
                    if current.text.strip() == "100":
                        logger.info("Reached LinkedIn's page limit (100)")
                        return False
                except Exception:
                    pass

            self.driver.execute_script("arguments[0].scrollIntoView(true);", nav_button)
            time.sleep(1)
            current_url = self.driver.current_url
            self._robust_click(nav_button)
            time.sleep(5)

            if self.driver.current_url != current_url:
                return True

            try:
                loading = self.driver.find_element(By.XPATH, "//div[contains(@class, 'loading')]")
                self.wait.until(EC.staleness_of(loading))
            except Exception:
                pass

            return True

        except (TimeoutException, NoSuchElementException):
            logger.warning(f"No {'previous' if self.reverse else 'next'} page button or it's disabled")
            return False
        except Exception as e:
            logger.error(f"Error navigating page: {e}")
            return False

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
                time.sleep(random.uniform(3, 5))
        finally:
            allow_sleep()

        direction = "reverse" if self.reverse else "forward"
        logger.info(f"Completed ({direction}) — {page_num} page(s) processed.")
        logger.info(
            f"Session summary — sent: {self.connections_sent} | "
            f"failed: {self.connections_failed} | "
            f"skipped: {self.connections_skipped}")
