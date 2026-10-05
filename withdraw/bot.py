"""Sent-invitation manager bot — bulk withdrawal of old invitations.

LinkedIn's "Sent" invitation manager renders a LazyColumn: only the first
page of invitations exists in the DOM, and every further page is appended by
a "Load more" button at the bottom of the list. Nothing loads from scrolling
alone, so reaching the *oldest* invitations means scrolling that button into
view and clicking it once per page until it disappears.

Cards carry a relative age ("Sent 3 months ago"), never a date. LinkedIn
rounds those down, so "3 months ago" means *at least* three months — the
date derived from a label is therefore the newest the invitation can
possibly be. Withdrawal only ever fires when that newest-possible date is
already past the cutoff, which means an invitation is never withdrawn on a
guess: ambiguous cards are left alone.
"""

import logging
import random
import re
import time
from datetime import date, timedelta

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from common.browser import create_driver
from common.clicking import ClickMixin

logger = logging.getLogger("linkedin_bot")

SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"

# Selectors. LinkedIn's SDUI markup has hashed, per-build class names, so
# everything here anchors on roles, test ids and aria labels instead.
_LIST_CONTAINER = "div[data-testid='lazy-column'], [data-component-type='LazyColumn']"
# Card selectors from most to least specific. LinkedIn ships several SDUI
# builds; whichever one is live decides which of these matches, so the bot
# probes them at runtime instead of betting on one.
_CARD_SELECTORS = (
    "div[data-testid='lazy-column'] > div[role='listitem']",
    "[data-testid='lazy-column'] [role='listitem']",
    "[data-component-type='LazyColumn'] [role='listitem']",
    "div[role='listitem']",
)
_CARD_ITEM = _CARD_SELECTORS[0]
_WITHDRAW_LINK = "a[aria-label^='Withdraw invitation sent to'], a[aria-label^='Retirar convite']"
# Shadow hosts LinkedIn renders modals into — same ones the connect bot uses.
_SHADOW_HOSTS = ("#interop-outlet", "[data-testid='interop-shadowdom']")

# "Load more" carries no id and no stable class — it is found by its label.
# Both UI languages are accepted: the account language decides which shows.
_LOAD_MORE_LABELS = ("load more", "carregar mais", "ver mais", "mostrar mais")
# Anything LinkedIn might render "Load more" as — SDUI swaps between a real
# button and a div carrying role="button".
_CLICKABLE = "button, [role='button'], a[role='button']"
# The withdrawal confirmation dialog ("If you withdraw now, you won't be able
# to resend to this person for up to 3 weeks."). Its confirm button repeats
# the invitee's name in aria-label, which is what lets the bot check the
# dialog belongs to the card it just clicked. Cancel sits right beside it, so
# text matching stays exact — never a substring.
_DIALOG = "dialog[open], [role='dialog'][open], [data-testid='dialog'][open]"
_CONFIRM_BUTTON = (
    "dialog[open] button[aria-label^='Withdraw invitation sent to'], "
    "dialog[open] button[aria-label^='Retirar convite'], "
    "[role='dialog'] button[aria-label^='Withdraw invitation sent to'], "
    "[role='dialog'] button[aria-label^='Retirar convite']"
)
_CONFIRM_LABELS = ("withdraw", "retirar", "remover")
_CANCEL_LABELS = ("cancel", "cancelar", "dismiss", "fechar")

# The "Sent 9 hours ago" line inside a card, in either UI language.
_SENT_LABEL_RE = re.compile(r"^(sent|enviad)", re.IGNORECASE)

# Relative-age units, English and Portuguese, mapped to a common key.
_AGE_UNITS = (
    ("minute", "minutes"), ("minutes", "minutes"),
    ("minuto", "minutes"), ("minutos", "minutes"),
    ("hour", "hours"), ("hours", "hours"),
    ("hora", "hours"), ("horas", "hours"),
    ("day", "days"), ("days", "days"), ("dia", "days"), ("dias", "days"),
    ("week", "weeks"), ("weeks", "weeks"),
    ("semana", "weeks"), ("semanas", "weeks"),
    ("month", "months"), ("months", "months"),
    ("mes", "months"), ("meses", "months"),
    ("year", "years"), ("years", "years"),
    ("ano", "years"), ("anos", "years"),
)
_AGE_UNIT_BY_WORD = dict(_AGE_UNITS)
# Shorthand accepted by --until: "2m", "3w", "10d", "1y". Minutes and hours
# are deliberately absent — a cutoff measured in minutes is never meant.
_SHORT_UNITS = {"d": "days", "w": "weeks", "m": "months", "y": "years"}
# "Sent today" / "Enviado ontem" — no number to read.
_TODAY_WORDS = ("today", "hoje", "now", "agora")
_YESTERDAY_WORDS = ("yesterday", "ontem")

# Pull a card's age line out by wording. Cards carry the age *and* the
# invitation note, both as <p><span>, with the note last — so position is not
# a usable signal and the text itself has to be matched.
_AGE_FROM_CARD_JS = (
    "const AGE_RE = /^(sent|enviad)/i;"
    "const ageOf = (card) => {"
    "  const spans = [...card.querySelectorAll('p span, p, time')];"
    "  for (const el of spans) {"
    "    const t = (el.textContent || '').trim();"
    "    if (t && t.length < 60 && AGE_RE.test(t)) return t;"
    "  }"
    "  return null;"
    "};"
)

# The list lives inside LinkedIn's app shell, which scrolls an inner
# container rather than the document — window.scrollTo() moves nothing and the
# list never lazy-loads. This walks up from the list to whichever ancestor
# actually scrolls, then drives that one.
_SCROLLER_JS = (
    "const findScroller = (listSel) => {"
    "  const list = document.querySelector(listSel);"
    "  let el = list;"
    "  while (el && el !== document.body) {"
    "    const oy = getComputedStyle(el).overflowY;"
    "    if (/(auto|scroll|overlay)/.test(oy) && el.scrollHeight > el.clientHeight + 4)"
    "      return el;"
    "    el = el.parentElement;"
    "  }"
    "  return document.scrollingElement || document.documentElement;"
    "};"
    "const scrollBottom = (listSel, cardSel) => {"
    "  const s = findScroller(listSel);"
    "  if (s) s.scrollTop = s.scrollHeight;"
    "  window.scrollTo(0, document.body.scrollHeight);"
    "  const cards = document.querySelectorAll(cardSel);"
    "  const last = cards[cards.length - 1];"
    "  if (last) last.scrollIntoView({block: 'end'});"
    "  return s ? {tag: s.tagName, id: s.id || null,"
    "              cls: (s.className || '').toString().slice(0, 40),"
    "              scrollTop: Math.round(s.scrollTop),"
    "              scrollHeight: s.scrollHeight, clientHeight: s.clientHeight} : null;"
    "};"
)

# How long to wait for a clicked "Load more" to append its page.
GROWTH_TIMEOUT_SECONDS = 12
# How long a withdrawn card gets to disappear from the list.
REMOVAL_TIMEOUT_SECONDS = 10
# Gap between pages. Fast enough to walk ~1000 invitations, slow enough that
# the page settles and the request rate stays unremarkable.
PAGE_PAUSE_RANGE = (1.2, 2.4)
# Every loading budget below is counted in *pages*, not in clicks: some
# LinkedIn builds page on scroll alone, with no "Load more" button to click,
# and a budget that only counted clicks would never be spent on those — which
# is how a two-page top-up once expanded a 969-invitation list to its end.
#
# The in-page loader runs in chunks so the cutoff check stays in Python
# between them, instead of the date logic being duplicated in JavaScript.
JS_CHUNK_PAGES = 10
# …and a much smaller chunk when a stop condition is armed, so the loader
# overshoots the cutoff by at most a page or two rather than a full chunk.
JS_STOP_CHECK_PAGES = 2
JS_PAUSE_MS = 700
# Pages a rolling run loads per top-up. Small on purpose: withdrawal resumes
# as soon as new invitations arrive, so the page never has to hold the list.
ROLLING_CHUNK_PAGES = 2
# Gap between withdrawals — these hit LinkedIn's API, so they pace slower.
WITHDRAW_PAUSE_RANGE = (1.5, 3.0)


# ---------------------------------------------------------------------------
# Age parsing
# ---------------------------------------------------------------------------

def _subtract_months(anchor, months):
    """``anchor`` shifted back whole calendar months, clamped to month length."""
    total = anchor.year * 12 + (anchor.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = anchor.day
    while day > 1:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, 1)


def parse_sent_age(raw, today=None):
    """Parse a card's "Sent …" line into the newest date it can represent.

    LinkedIn rounds ages down — "Sent 3 months ago" covers everything from
    three to four months old — so the returned date is the *upper bound* of
    when the invitation was sent. Callers can therefore treat
    ``parsed <= cutoff`` as proof the invitation predates the cutoff.

    Handles both UI languages:
    - ``"Sent 40 minutes ago"`` / ``"Enviado há 40 minutos"``  → today
    - ``"Sent 9 hours ago"`` / ``"Enviado há 9 horas"``        → today (or
      the previous day once the hours pass a full day)
    - ``"Sent 3 days ago"`` / ``"Enviado há 3 dias"``          → today − 3d
    - ``"Sent 2 weeks ago"`` / ``"Enviado há 2 semanas"``      → today − 14d
    - ``"Sent 3 months ago"`` / ``"Enviado há 3 meses"``       → today − 3 months
    - ``"Sent 1 year ago"`` / ``"Enviado há 1 ano"``           → today − 12 months
    - Anything unrecognized                                    → ``None``,
      and the card is left untouched.
    """
    if today is None:
        today = date.today()
    if not raw:
        return None

    text = raw.strip().lower()
    # Strip accents on the few words that carry them ("há", "mês").
    text = text.replace("á", "a").replace("ê", "e").replace("ó", "o")

    words = re.findall(r"[a-z]+", text)
    if any(w in _TODAY_WORDS for w in words):
        return today
    if any(w in _YESTERDAY_WORDS for w in words):
        return today - timedelta(days=1)

    match = re.search(r"(\d+)\s*([a-z]+)", text)
    if match:
        amount, word = int(match.group(1)), match.group(2)
    else:
        # "Sent a month ago" / "Enviado há um mes" — implicit 1.
        word = next((w for w in words if w in _AGE_UNIT_BY_WORD), None)
        if word is None:
            logger.debug(f"Could not parse age '{raw}' — leaving the card alone.")
            return None
        amount = 1

    unit = _AGE_UNIT_BY_WORD.get(word)
    if unit is None:
        logger.debug(f"Could not parse age '{raw}' — leaving the card alone.")
        return None

    if unit == "minutes":
        return today
    if unit == "hours":
        return today - timedelta(days=amount // 24)
    if unit == "days":
        return today - timedelta(days=amount)
    if unit == "weeks":
        return today - timedelta(weeks=amount)
    if unit == "months":
        return _subtract_months(today, amount)
    if unit == "years":
        return _subtract_months(today, amount * 12)
    return None


def parse_until(value, today=None):
    """Resolve a ``--until`` argument into a cutoff ``date``.

    Two forms:
    - absolute — ``"2026/06/20"`` or ``"2026-06-20"``
    - relative — ``"2m"``, ``"3w"``, ``"10d"``, ``"1y"`` (also spelled out:
      ``"2 months"``, ``"2 meses"``), meaning "that far back from today"

    The relative form runs the same arithmetic as ``parse_sent_age``, so
    ``--until 2m`` lines up exactly with the cards labelled "Sent 2 months
    ago" — the boundary can't drift between how a card is read and how the
    cutoff is computed.

    Raises ``ValueError`` on anything unrecognized; the CLI turns that into
    an error message rather than a stack trace.
    """
    if today is None:
        today = date.today()
    text = (value or "").strip().lower()
    if not text:
        raise ValueError("no date given")

    absolute = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if absolute:
        try:
            return date(*(int(part) for part in absolute.groups()))
        except ValueError as e:
            raise ValueError(f"'{value}' is not a real date ({e})") from None

    relative = re.match(r"^(\d+)\s*([a-z]+)$", text.replace("á", "a").replace("ê", "e"))
    if relative:
        amount = int(relative.group(1))
        word = relative.group(2)
        # Full words first ("months", "meses"), then the single-letter form.
        unit = _AGE_UNIT_BY_WORD.get(word) or _SHORT_UNITS.get(word[0])
        if unit == "days":
            return today - timedelta(days=amount)
        if unit == "weeks":
            return today - timedelta(weeks=amount)
        if unit == "months":
            return _subtract_months(today, amount)
        if unit == "years":
            return _subtract_months(today, amount * 12)

    raise ValueError(
        f"'{value}' is neither YYYY/MM/DD nor a relative age like 2m, 3w, "
        f"10d or 1y")


def reached_cutoff(labels, cutoff, today=None):
    """True once the loaded list stretches back to ``cutoff``.

    ``labels`` are the raw "Sent …" lines in list order (newest first), so
    the last parsable one is the oldest invitation loaded so far. Used as
    ``scroll_to_end``'s stop condition: there is no reason to keep loading
    pages past the point where withdrawal stops.
    """
    for raw in reversed(list(labels)):
        sent = parse_sent_age(raw, today=today)
        if sent is not None:
            return sent <= cutoff
    return False


def targets_older_than(cards, cutoff, today=None):
    """Every card provably sent on or before ``cutoff``, oldest first.

    ``cards`` are dicts as returned by ``LinkedInWithdrawBot._loaded_cards``
    (``age``, ``name``, ``key`` and, in the browser, ``link``), in list order.
    That order is only *roughly* newest-first: LinkedIn sometimes lists an
    hours-old invitation below month-old ones, so the whole list is filtered
    — no position is trusted as a boundary — and the result is sorted by age.
    Cards of the same age keep bottom-up order. Cards whose age can't be
    parsed are skipped, never withdrawn.
    """
    selected = []
    for card in reversed(list(cards)):
        sent = parse_sent_age(card.get("age"), today=today)
        if sent is not None and sent <= cutoff:
            selected.append((sent, card))
    selected.sort(key=lambda pair: pair[0])
    return [card for _, card in selected]


class LinkedInWithdrawBot(ClickMixin):
    def __init__(self, until=None, dry_run=False, max_cards=None,
                 auto_continue=False, use_js=True, stop_early=False,
                 max_pages=None, rolling=False):
        """
        Args:
            until: ``date``; withdraw invitations sent on or before it.
                   Required by ``run`` — there is no "withdraw everything"
                   mode, since that is rarely what anyone means and always
                   irreversible.
            dry_run: Log what would be withdrawn without clicking anything.
            max_cards: Stop loading once this many invitation cards are in
                       the DOM, then withdraw whatever among them is older
                       than the cutoff. ``None`` = load the whole list.
                       Bounds how heavy the page gets: a fully expanded list
                       of ~1000 cards is slow to scroll and slow to click.
            auto_continue: Skip the confirmation prompt before the first
                           withdrawal.
            use_js: Expand the list with the in-page loop first, falling back
                    to trusted clicks when it stalls. False = ladder only.
            stop_early: Stop loading as soon as the cutoff is in view instead
                        of expanding to the true end of the list. Cheaper on
                        a long list, but the oldest invitations never load.
            max_pages: Hard cap on pages loaded. Bounds the same phase as
                       max_cards, counted in page loads rather than in
                       cards; whichever is reached first stops it. A page
                       counts whether it came from clicking "Load more" or
                       from the list paging on scroll alone.
            rolling: Withdraw from what is already on the page, then load a
                     couple more pages and withdraw again, down to the oldest
                     invitation — instead of expanding the whole list first.
                     Withdrawn cards leave the DOM as new ones arrive, so the
                     page stays roughly the size it started at.
        """
        self.until = until
        self.dry_run = dry_run
        self.max_cards = max_cards
        self.auto_continue = auto_continue
        self.use_js = use_js
        self.stop_early = stop_early
        self.max_pages = max_pages
        self.rolling = rolling

        self.driver, _ = create_driver(attach_to_existing=True)
        # Resolved against the live page on first use.
        self._card_sel = None
        self._tab_logged = False

        self.withdrawn = 0
        self.previewed = 0
        self.failed = 0
        # Cards already acted on, by componentkey. A failed withdrawal leaves
        # its card in the list, so without this a rolling run would find it
        # again on the next pass and retry it forever.
        self._processed = set()
        # Set by the loading phase: True once "Load more" is gone (or dead),
        # which is what tells a rolling run the list has no more pages.
        self.list_exhausted = False

    # ------------------------------------------------------------------
    # Page state
    # ------------------------------------------------------------------

    def select_sent_tab(self):
        """Switch to the sent-invitation tab among all open tabs.

        Attaching to a running Chrome lands on whichever tab Selenium reports
        first, which is rarely the one the user is looking at. Both other bots
        pick their tab explicitly; without it this bot reads an unrelated
        page, counts zero cards and gives up.
        """
        try:
            handles = self.driver.window_handles
        except Exception as e:
            logger.error(f"Could not enumerate browser tabs: {e}")
            return False

        invitation_manager = None
        any_linkedin = None
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                url = (self.driver.current_url or "").lower()
            except Exception:
                continue
            if "invitation-manager/sent" in url:
                invitation_manager = h
                break
            if any_linkedin is None and "linkedin.com" in url:
                any_linkedin = h

        chosen = invitation_manager or any_linkedin
        if chosen is not None:
            self.driver.switch_to.window(chosen)
            # A rolling run comes back here on every top-up; saying it once is
            # informative, saying it forty times buries the withdrawals.
            log = logger.debug if self._tab_logged else logger.info
            log(f"Using tab: {self.driver.current_url}")
            self._tab_logged = True
            return invitation_manager is not None

        logger.warning("No LinkedIn tab found. Using the current tab.")
        if handles:
            self.driver.switch_to.window(handles[0])
        return False

    def open_sent_page(self):
        """Focus the sent-invitation tab, navigating there if none is open."""
        self.select_sent_tab()
        if "invitation-manager/sent" not in (self.driver.current_url or ""):
            logger.info(f"Opening {SENT_URL}")
            self.driver.get(SENT_URL)
            time.sleep(3)
        # The list mounts after the shell; give it a beat before counting.
        if not self.card_count():
            time.sleep(2)
            self._resolve_card_selector(force=True)

    def _resolve_card_selector(self, force=False):
        """Pick the card selector that actually matches the live page.

        Tries the candidates most-specific first and keeps the first one that
        matches anything, so a markup change costs a fallback rather than a
        silent zero-card run.
        """
        if self._card_sel and not force:
            return self._card_sel
        try:
            counts = self.driver.execute_script(
                "return arguments[0].map(s => document.querySelectorAll(s).length);",
                list(_CARD_SELECTORS)) or []
        except Exception as e:
            logger.debug(f"Selector probe failed: {type(e).__name__}: {e}")
            counts = []
        for sel, found in zip(_CARD_SELECTORS, counts):
            if found:
                if sel != self._card_sel:
                    logger.debug(f"Card selector: {sel} ({found} matched)")
                self._card_sel = sel
                return sel
        self._card_sel = _CARD_SELECTORS[0]
        return self._card_sel

    def cards(self):
        """Every invitation card currently in the DOM, newest first."""
        return self.driver.find_elements(By.CSS_SELECTOR, self._resolve_card_selector())

    def card_count(self):
        """How many invitation cards are loaded, counted in the browser.

        Counting in JS rather than through ``find_elements`` keeps the growth
        check to one round-trip; with ~1000 cards loaded, materializing every
        WebElement on each poll costs whole seconds.
        """
        try:
            return self.driver.execute_script(
                "return document.querySelectorAll(arguments[0]).length;",
                self._resolve_card_selector()) or 0
        except Exception as e:
            logger.debug(f"Could not count cards: {type(e).__name__}: {e}")
            return 0

    def sent_labels(self):
        """Return the raw "Sent …" line of every loaded card, in list order.

        The age is matched by its wording, not by position. Taking the card's
        last text span instead picks up the invitation note ("Saudações,
        …!"), which every card with a note carries *below* the age — that
        misread every age as unparseable and made the whole list look
        ageless.
        """
        try:
            return self.driver.execute_script(
                _AGE_FROM_CARD_JS +
                "const out = [];"
                "document.querySelectorAll(arguments[0]).forEach(card => {"
                "  const age = ageOf(card);"
                "  if (age) out.push(age);"
                "});"
                "return out;",
                self._resolve_card_selector()) or []
        except Exception as e:
            logger.debug(f"Could not read sent labels: {type(e).__name__}: {e}")
            return []

    def oldest_label(self):
        """The "Sent …" line of the oldest loaded card, or None.

        Picked by age, not position: the last card can be hours old while
        month-old invitations sit above it.
        """
        labels = [t for t in self.sent_labels() if _SENT_LABEL_RE.match(t)]
        dated = [(parse_sent_age(t), t) for t in labels]
        dated = [pair for pair in dated if pair[0] is not None]
        if dated:
            return min(dated, key=lambda pair: pair[0])[1]
        return labels[-1] if labels else None

    def _loaded_cards(self):
        """Every loaded invitation as ``{link, name, age, key}``, newest first.

        ``key`` is the withdraw link's ``componentkey`` — unique per
        invitation, unlike the display name, so two people with the same name
        stay distinguishable across rescans.
        """
        try:
            return self.driver.execute_script(
                _AGE_FROM_CARD_JS +
                "const out = [];"
                "document.querySelectorAll(arguments[0]).forEach(card => {"
                "  const link = card.querySelector(arguments[1]);"
                "  if (!link) return;"
                "  const label = link.getAttribute('aria-label') || '';"
                "  out.push({"
                "    link: link,"
                "    name: label.replace(/^.*?(sent to|convite para)\\s*/i, '').trim(),"
                "    age: ageOf(card) || '',"
                "    key: link.getAttribute('componentkey') || label,"
                "  });"
                "});"
                "return out;",
                self._resolve_card_selector(), _WITHDRAW_LINK) or []
        except Exception as e:
            logger.warning(f"Could not read the invitation list: "
                           f"{type(e).__name__}: {e}")
            return []

    def probe(self):
        """Report what the live page exposes, selector by selector.

        A run that finds nothing looks identical whether the tab is wrong,
        the markup changed, or the list simply hasn't mounted. This prints
        the evidence for each so the next fix isn't a guess.
        """
        report = {}
        try:
            tabs = []
            for h in self.driver.window_handles:
                self.driver.switch_to.window(h)
                tabs.append(self.driver.current_url)
            report["tabs"] = tabs
        except Exception as e:
            report["tabs"] = f"unavailable: {type(e).__name__}: {e}"

        self.select_sent_tab()
        report["active tab"] = self.driver.current_url

        try:
            report["selector counts"] = dict(zip(
                _CARD_SELECTORS,
                self.driver.execute_script(
                    "return arguments[0].map(s => document.querySelectorAll(s).length);",
                    list(_CARD_SELECTORS)) or []))
            report["list containers"] = self.driver.execute_script(
                "return document.querySelectorAll(arguments[0]).length;",
                _LIST_CONTAINER)
            report["withdraw links"] = self.driver.execute_script(
                "return document.querySelectorAll(arguments[0]).length;",
                _WITHDRAW_LINK)
            report["buttons on page"] = self.driver.execute_script(
                "return [...document.querySelectorAll(\"button, [role='button']\")]"
                "  .map(b => ((b.innerText || b.textContent || '').trim() ||"
                "             (b.getAttribute('aria-label') || '').trim()))"
                "  .filter(Boolean).slice(-12);")
            report["page scrolls"] = self.driver.execute_script(
                "const e = document.scrollingElement || document.body;"
                "return {scrollHeight: e.scrollHeight, innerHeight: window.innerHeight};")
        except Exception as e:
            report["dom probe"] = f"failed: {type(e).__name__}: {e}"

        report["scroller (before)"] = self._scroll_list_bottom()
        time.sleep(2)
        report["scroller (after scroll)"] = self._scroll_list_bottom()
        report["cards after scrolling"] = self.card_count()
        report["load more found"] = self._find_load_more() is not None
        labels = self.sent_labels()
        report["sent labels"] = (labels[:3] + ["…"] + labels[-3:]
                                 if len(labels) > 6 else labels)

        print("\n--- withdraw bot probe ---")
        for key, value in report.items():
            if isinstance(value, (list, dict)) and value:
                print(f"{key}:")
                items = value.items() if isinstance(value, dict) else enumerate(value)
                for k, v in items:
                    print(f"    {k}: {v}" if isinstance(value, dict) else f"    {v}")
            else:
                print(f"{key}: {value}")
        print("--- end probe ---\n")
        return report

    # ------------------------------------------------------------------
    # Loading the full list
    # ------------------------------------------------------------------

    def _scroll_list_bottom(self):
        """Scroll the real scroll container to the bottom of the list.

        Drives whichever ancestor actually scrolls, nudges the window, and
        pulls the last card into view — the list only mounts more rows once
        the bottom is genuinely reached.
        """
        try:
            info = self.driver.execute_script(
                _SCROLLER_JS + "return scrollBottom(arguments[0], arguments[1]);",
                _LIST_CONTAINER, self._resolve_card_selector())
            if info:
                logger.debug(
                    f"Scrolled {info.get('tag')}#{info.get('id') or '-'} to "
                    f"{info.get('scrollTop')}/{info.get('scrollHeight')} "
                    f"(viewport {info.get('clientHeight')})")
            return info
        except Exception as e:
            logger.debug(f"Scroll failed: {type(e).__name__}: {e}")
            return None

    def _find_load_more(self):
        """Return the "Load more" button element, or None when the list ends.

        Matched by visible label: the button has no id, no test id and only
        hashed class names, all of which change between LinkedIn builds.
        """
        try:
            return self.driver.execute_script(
                "const labels = arguments[0];"
                "const cands = [...document.querySelectorAll("
                "  \"button, [role='button'], a[role='button']\")];"
                "const text = el => ((el.innerText || el.textContent || '') + ' ' +"
                "  (el.getAttribute('aria-label') || '')).trim().toLowerCase();"
                "return cands.find(b => labels.includes(text(b)))"
                "  || cands.find(b => labels.some(l => text(b).includes(l)))"
                "  || null;",
                list(_LOAD_MORE_LABELS))
        except Exception as e:
            logger.debug(f"Could not locate 'Load more': {type(e).__name__}: {e}")
            return None

    def _wait_for_growth(self, previous, timeout=GROWTH_TIMEOUT_SECONDS):
        """Wait for the card count to exceed ``previous``; return the count."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.card_count()
            if current > previous:
                return current
            time.sleep(0.5)
        return self.card_count()

    def _page_pause(self):
        time.sleep(random.uniform(*PAGE_PAUSE_RANGE))

    def expand_in_page(self, max_pages=JS_CHUNK_PAGES, max_cards=None,
                       pause_ms=JS_PAUSE_MS,
                       growth_timeout_ms=GROWTH_TIMEOUT_SECONDS * 1000):
        """Run the load-more loop inside the page, in one round-trip.

        Each Selenium-driven page costs a scroll, an element lookup, a click
        and a polling loop across the wire; in the browser the same work is a
        ``setTimeout`` chain, which walks a thousand invitations in a fraction
        of the time. It runs in bounded chunks so the cutoff check stays on
        the Python side, where the date logic lives.

        The catch is that ``btn.click()`` fires an untrusted event, which
        LinkedIn ignores on some controls — hence ``scroll_to_end``'s fallback
        to the trusted-click ladder the moment a chunk stops making progress.

        Scrolling comes first and clicking second: the "Load more" button is
        mounted lazily at the bottom of the list, so on a freshly opened page
        it does not exist until the scroller actually reaches the end. A pass
        that only looked for the button therefore saw an empty page-one list
        and reported the end of a 969-invitation list after one second.
        Growth from scrolling alone also counts as progress, since some
        builds page on scroll with no button at all.

        ``max_cards`` is checked in the browser, before each page load, so
        a card cap stops mid-chunk instead of overshooting by the rest of it.

        ``max_pages`` counts pages that actually arrived, from a click or
        from scrolling — the loop below spends its budget on growth, not on
        button presses, because a build that pages on scroll alone never
        presses one.

        Returns ``{"cards": n, "pages": n, "scrolls": n, "reason": …}``, or
        None when the script itself failed. ``reason`` is one of
        ``chunk-done`` (budget spent), ``cap`` (max_cards reached), ``end``
        (nothing left to load) or ``stalled`` (clicked, nothing loaded).
        """
        script = (
            _SCROLLER_JS +
            "const [cardSel, listSel, btnSel, labels, maxPages, maxCards,"
            "       pauseMs, growthTimeout, done] = arguments;"
            "const count = () => document.querySelectorAll(cardSel).length;"
            "const text = el => ((el.innerText || el.textContent || '') + ' ' +"
            "  (el.getAttribute('aria-label') || '')).trim().toLowerCase();"
            "const findBtn = () => {"
            "  const cands = [...document.querySelectorAll(btnSel)]"
            "    .filter(b => b.offsetParent !== null);"
            "  return cands.find(b => labels.includes(text(b)))"
            "    || cands.find(b => labels.some(l => text(b).includes(l)))"
            "    || null;"
            "};"
            "let pages = 0, scrolls = 0, idle = 0;"
            "const finish = (reason) => done({cards: count(), pages, scrolls, reason});"
            "const step = () => {"
            "  if (maxCards && count() >= maxCards) return finish('cap');"
            "  if (pages >= maxPages) return finish('chunk-done');"
            "  const before = count();"
            "  scrollBottom(listSel, cardSel);"
            "  scrolls++;"
            "  const btn = findBtn();"
            "  if (btn) {"
            "    idle = 0;"
            "    btn.scrollIntoView({block: 'end'});"
            "    btn.click();"
            "  }"
            "  const deadline = Date.now() + (btn ? growthTimeout : 2500);"
            "  const waitGrow = () => {"
            # A page is a page: however it arrived, it costs one budget.
            "    if (count() > before) {"
            "      idle = 0; pages++;"
            "      return setTimeout(step, pauseMs);"
            "    }"
            "    if (Date.now() > deadline) {"
            "      if (btn) return finish('stalled');"
            "      idle++;"
            # Three scrolls with no button and no new rows: the list is out.
            "      return idle >= 3 ? finish('end') : setTimeout(step, 400);"
            "    }"
            "    setTimeout(waitGrow, 250);"
            "  };"
            "  setTimeout(waitGrow, 250);"
            "};"
            "step();"
        )
        budget = max_pages * (growth_timeout_ms + pause_ms) / 1000 + 30
        try:
            self.driver.set_script_timeout(budget)
            return self.driver.execute_async_script(
                script, self._resolve_card_selector(), _LIST_CONTAINER,
                _CLICKABLE, list(_LOAD_MORE_LABELS), max_pages,
                max_cards or 0, pause_ms, growth_timeout_ms)
        except Exception as e:
            logger.warning(f"In-page expansion failed ({type(e).__name__}: {e}); "
                           f"falling back to trusted clicks.")
            return None

    def scroll_to_end(self, max_pages=None, max_cards=None, stop_when=None,
                      use_js=True):
        """Load invitation pages until the oldest one is in the DOM.

        Clicks "Load more" repeatedly, scrolling it into view each time, and
        stops at the first of: the button disappearing (true end of the
        list), the list refusing to grow, ``max_pages`` or ``max_cards``
        reached, or ``stop_when`` returning True.

        Args:
            max_pages: Cap on pages loaded. ``None`` = unlimited. With ~1000
                       invitations the fully expanded DOM gets heavy, so cap
                       it when only recent pages are needed. A page counts
                       however it arrived: some builds have no "Load more"
                       button at all and page on scroll instead.
            max_cards: Cap on loaded invitation cards — the same bound as
                       max_pages, expressed in the unit the caller actually
                       cares about. ``None`` = unlimited.
            stop_when: Callable taking the list of raw "Sent …" labels (list
                       order, newest first) and returning True to stop early
                       — e.g. once a card older than the cutoff date has
                       appeared, there is no reason to keep loading.
            use_js: Load pages with the in-page loop (fast), dropping to the
                    trusted-click ladder the first time a chunk stalls.
                    False forces the slow path from the start.

        Sets ``list_exhausted`` on the way out: True when it stopped
        because there is nothing left to load, False when a cap or a stop
        condition ended it. A rolling run reads that to know it is done.

        Returns:
            The number of invitation cards loaded.
        """
        self.open_sent_page()
        self.list_exhausted = False

        count = self.card_count()
        if not count:
            logger.warning(
                "No invitation cards found — is the Sent tab open and loaded?")
            self.list_exhausted = True
            return 0

        logger.info(f"{count} invitation(s) loaded; expanding the list …")
        pages = 0

        while True:
            if max_cards is not None and count >= max_cards:
                logger.info(
                    f"Reached the {max_cards}-invitation load limit with "
                    f"{count} loaded "
                    f"(oldest: {self.oldest_label() or 'unknown'}).")
                return count

            if stop_when is not None and stop_when(self.sent_labels()):
                logger.info(
                    f"Stop condition met with {count} invitation(s) loaded "
                    f"(oldest: {self.oldest_label() or 'unknown'}).")
                return count

            if max_pages is not None and pages >= max_pages:
                logger.info(
                    f"Reached the {max_pages}-page limit with {count} "
                    f"invitation(s) loaded.")
                return count

            if use_js:
                # The in-page loop can't evaluate the cutoff (the date logic
                # lives in Python), so a stop condition is only seen between
                # chunks. Ten pages per chunk means overshooting the cutoff by
                # up to ten page loads — on a slow list that reads as "it
                # never stops". Shrink the chunk whenever something is
                # waiting to stop it.
                budget = JS_STOP_CHECK_PAGES if stop_when else JS_CHUNK_PAGES
                if max_pages is not None:
                    budget = min(budget, max_pages - pages)
                result = self.expand_in_page(max_pages=budget,
                                             max_cards=max_cards)
                if result is None:
                    use_js = False
                    continue

                pages += result.get("pages", 0)
                grown = result.get("cards", count)
                if grown > count:
                    count = grown
                    logger.info(
                        f"{count} invitation(s) loaded — {pages} page(s), "
                        f"{result.get('scrolls', 0)} scroll(s) this chunk "
                        f"(oldest: {self.oldest_label() or 'unknown'}).")

                reason = result.get("reason")
                if reason == "cap":
                    logger.info(
                        f"Reached the {max_cards}-invitation load limit with "
                        f"{count} loaded "
                        f"(oldest: {self.oldest_label() or 'unknown'}).")
                    return count
                if reason == "end":
                    logger.info(
                        f"End of the list: {count} invitation(s) loaded "
                        f"(oldest: {self.oldest_label() or 'unknown'}).")
                    self.list_exhausted = True
                    return count
                if reason == "stalled":
                    # An untrusted click LinkedIn ignored, or a genuinely
                    # exhausted list — the ladder below tells them apart.
                    logger.info(
                        "In-page loop stopped making progress; switching to "
                        "trusted clicks.")
                    use_js = False
                continue

            self._scroll_list_bottom()
            button = self._find_load_more()
            if button is None:
                # "Load more" is mounted lazily at the bottom of the list, so
                # on a page that has only ever shown its first rows it does
                # not exist yet. Scroll, settle, look again — and if the list
                # grew from the scrolling alone, that was the page load.
                self._page_pause()
                self._scroll_list_bottom()
                grown = self._wait_for_growth(count, timeout=4)
                if grown > count:
                    # No button was pressed, but a page arrived all the same;
                    # not counting it here is what let a bounded run walk the
                    # whole list.
                    pages += 1
                    count = grown
                    logger.info(
                        f"{count} invitation(s) loaded by scrolling — "
                        f"{pages} page(s) "
                        f"(oldest: {self.oldest_label() or 'unknown'}).")
                    continue
                button = self._find_load_more()
            if button is None:
                logger.info(
                    f"End of the list: {count} invitation(s) loaded "
                    f"(oldest: {self.oldest_label() or 'unknown'}).")
                self.list_exhausted = True
                return count

            try:
                clicked = self._robust_click(button, "'Load more'")
            except StaleElementReferenceException:
                # The list re-rendered under us; the next pass re-finds it.
                logger.debug("'Load more' went stale; retrying.")
                continue
            if not clicked:
                logger.warning(
                    f"Could not click 'Load more'; stopping with {count} "
                    f"invitation(s) loaded.")
                self.list_exhausted = True
                return count

            pages += 1
            grown = self._wait_for_growth(count)
            if grown <= count:
                logger.warning(
                    f"'Load more' clicked but the list stayed at {count} "
                    f"invitation(s); stopping.")
                self.list_exhausted = True
                return count

            logger.info(
                f"Page {pages}: {grown} invitation(s) loaded "
                f"(oldest: {self.oldest_label() or 'unknown'}).")
            count = grown
            self._page_pause()

    # ------------------------------------------------------------------
    # Withdrawing
    # ------------------------------------------------------------------

    def _find_confirm_button(self):
        """The confirm button of LinkedIn's withdrawal dialog, or None.

        The dialog is a light-DOM ``<dialog open>`` whose confirm button is
        ``button[aria-label^='Withdraw invitation sent to …']`` — matched
        first, because that label is also how the button is checked against
        the right person. Falling back to button text keeps a build that
        renames the label working, and the ``#interop-outlet`` shadow host is
        still searched since the connect bot's modals live there.

        Note the sibling ``Cancel`` button: matching on text alone has to stay
        exact, or a substring rule would happily confirm the wrong control.
        """
        try:
            precise = self.driver.execute_script(
                "return document.querySelector(arguments[0]);", _CONFIRM_BUTTON)
            if precise is not None:
                return precise
        except Exception as e:
            logger.debug(f"Precise confirm lookup failed: {type(e).__name__}: {e}")

        try:
            return self.driver.execute_script(
                "const [labels, hostSel] = arguments;"
                "const roots = [document];"
                "hostSel.forEach(s => document.querySelectorAll(s).forEach(h => {"
                "  if (h.shadowRoot) roots.push(h.shadowRoot);"
                "}));"
                "for (const root of roots) {"
                "  const dialogs = root.querySelectorAll("
                "    \"dialog[open], [role='dialog'], [data-test-modal]\");"
                "  for (const d of dialogs) {"
                "    const hit = [...d.querySelectorAll(\"button, a[role='button']\")]"
                "      .find(b => labels.includes((b.innerText || '').trim().toLowerCase())"
                "                 && b.offsetParent !== null);"
                "    if (hit) return hit;"
                "  }"
                "}"
                "return null;",
                list(_CONFIRM_LABELS), list(_SHADOW_HOSTS))
        except Exception as e:
            logger.debug(f"Could not look for a confirm button: "
                         f"{type(e).__name__}: {e}")
            return None

    def _dismiss_dialog(self):
        """Close an open dialog via its Cancel button, then its own close().

        A modal ``<dialog>`` blocks the page underneath it, so a confirmation
        left standing after a failed withdrawal would break every click that
        follows. Clearing it is what keeps one bad card from ending the run.
        """
        try:
            cancel = self.driver.execute_script(
                "const d = document.querySelector(arguments[0]);"
                "if (!d) return null;"
                "const labels = arguments[1];"
                "return [...d.querySelectorAll('button')].find(b => {"
                "  const t = ((b.innerText || '') + ' ' +"
                "    (b.getAttribute('aria-label') || '')).trim().toLowerCase();"
                "  return labels.some(l => t === l);"
                "}) || null;",
                _DIALOG, list(_CANCEL_LABELS))
        except Exception:
            cancel = None

        if cancel is not None:
            self._robust_click(cancel, "dialog cancel")
        else:
            try:
                self.driver.execute_script(
                    "const d = document.querySelector(arguments[0]);"
                    "if (d && d.close) d.close();", _DIALOG)
            except Exception:
                pass
        time.sleep(0.5)

    def _confirm_withdrawal(self, name, timeout=6):
        """Click Withdraw in the confirmation dialog.

        The dialog's confirm button carries the invitee's name in its
        ``aria-label`` ("Withdraw invitation sent to Camilla Souza"), so the
        dialog is checked against the card that opened it. A mismatch — a
        dialog left over from a previous card, or one opened by a mis-aimed
        click — is dismissed rather than confirmed: withdrawing the wrong
        person is not recoverable, and a skipped card is.

        Returns True when the dialog was confirmed.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            button = self._find_confirm_button()
            if button is not None:
                try:
                    label = (button.get_attribute("aria-label") or "").strip()
                except Exception:
                    label = ""
                if name and label and name.lower() not in label.lower():
                    logger.warning(
                        f"Confirmation dialog reads '{label}' but the card was "
                        f"{name} — dismissing it instead of withdrawing.")
                    self._dismiss_dialog()
                    return False
                self._robust_click(button, f"withdrawal confirm ({name})")
                return True
            time.sleep(0.3)
        logger.debug(f"No confirmation dialog appeared for {name}.")
        return False

    def _wait_card_gone(self, key, timeout=REMOVAL_TIMEOUT_SECONDS):
        """True once the card with this ``componentkey`` has left the DOM."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                still_there = self.driver.execute_script(
                    "const key = arguments[0];"
                    "return [...document.querySelectorAll(arguments[1])].some("
                    "  a => (a.getAttribute('componentkey') || "
                    "        a.getAttribute('aria-label')) === key);",
                    key, _WITHDRAW_LINK)
            except Exception:
                return True
            if not still_there:
                return True
            time.sleep(0.4)
        return False

    def withdraw_card(self, card):
        """Withdraw one invitation. Returns True when the card is gone."""
        name = card.get("name") or "(unknown)"
        age = card.get("age") or "unknown age"

        if self.dry_run:
            logger.info(f"[DRY RUN] Would withdraw: {name} — {age}")
            return True

        if not self._robust_click(card["link"], f"withdraw ({name})"):
            logger.warning(f"Could not click withdraw for {name}.")
            return False

        if not self._confirm_withdrawal(name):
            # Either no dialog appeared or it was for someone else. Both leave
            # the click unfinished; clear anything still open so the next card
            # isn't blocked by a modal.
            self._dismiss_dialog()
            logger.warning(f"Withdrawal of {name} was not confirmed. Skipping.")
            return False

        if not self._wait_card_gone(card.get("key")):
            logger.warning(
                f"Withdrawal of {name} did not take — the card is still "
                f"listed. Moving on.")
            self._dismiss_dialog()
            return False

        logger.info(f"Withdrew: {name} — {age}")
        return True

    def _load_stop_condition(self):
        """When to stop loading pages, or None to load to the end of the list.

        The list is only roughly sorted — old invitations can sit anywhere,
        even below hours-old ones — so by default every page is loaded and
        every card past the cutoff is withdrawn, wherever it sits.

        ``--stop-early`` gives that up deliberately: stop as soon as the last
        loaded card is past the cutoff. Cheaper on a long list, at the price
        of never reaching the invitations below it.

        ``--max`` bounds the same phase, by loaded card count rather than by
        age, and is enforced inside ``scroll_to_end`` — no label parsing
        needed, so it holds for cards whose age is unreadable too.
        """
        if not self.stop_early:
            return None

        def stop(labels):
            return reached_cutoff(labels, self.until)

        return stop

    def run(self):
        """Load the list, then withdraw every card older than the cutoff."""
        if self.until is None:
            raise ValueError(
                "until is required — refusing to withdraw the whole list.")

        if self.rolling:
            return self._run_rolling()

        loaded = self.scroll_to_end(max_pages=self.max_pages,
                                    max_cards=self.max_cards,
                                    stop_when=self._load_stop_condition(),
                                    use_js=self.use_js)
        if not loaded:
            return

        targets = targets_older_than(self._loaded_cards(), self.until)
        if not targets:
            logger.info(
                f"Nothing to withdraw: none of the {loaded} loaded "
                f"invitation(s) is provably older than {self.until} "
                f"(oldest: {self.oldest_label() or 'unknown'}).")
            if self.max_cards is not None and loaded >= self.max_cards:
                logger.info(
                    f"Only the {loaded} newest invitation(s) were loaded "
                    f"(--max {self.max_cards}) — raise it to reach further "
                    f"back.")
            return

        logger.info(
            f"{len(targets)} invitation(s) to withdraw, oldest first: "
            f"{targets[0].get('name')} ({targets[0].get('age')}) "
            f"→ {targets[-1].get('name')} ({targets[-1].get('age')}).")

        if not self.dry_run and not self.auto_continue:
            answer = input(
                f"\nWithdraw {len(targets)} invitation(s)? "
                f"This cannot be undone. [y/N] ").strip().lower()
            if answer not in ("y", "yes", "s", "sim"):
                logger.info("Cancelled by user — nothing was withdrawn.")
                return

        self._withdraw_all(
            targets,
            rescan=lambda: targets_older_than(self._loaded_cards(), self.until))
        self._report()

    def _withdraw_all(self, targets, rescan):
        """Withdraw every card in ``targets``, oldest first.

        Args:
            targets: Cards to withdraw, as ``_loaded_cards`` returns them.
                     Ones already acted on in this run are skipped, so a
                     failed withdrawal is never retried.
            rescan: Callable returning a fresh target list. Called when a
                    card goes stale mid-run: the list re-rendered under us
                    and every remaining element reference died with it.
        """
        while True:
            pending = [c for c in targets
                       if c.get("key") not in self._processed]
            if not pending:
                return

            stale = False
            for card in pending:
                try:
                    ok = self.withdraw_card(card)
                except StaleElementReferenceException:
                    logger.debug("Card went stale mid-withdrawal; rescanning.")
                    stale = True
                    break

                self._processed.add(card.get("key"))
                if not ok:
                    self.failed += 1
                elif self.dry_run:
                    self.previewed += 1
                else:
                    self.withdrawn += 1
                if not self.dry_run:
                    time.sleep(random.uniform(*WITHDRAW_PAUSE_RANGE))

            if stale:
                targets = rescan()

    def _top_up(self):
        """Load another couple of pages; return how many cards are loaded now.

        The batch is ``ROLLING_CHUNK_PAGES`` pages, or ``--max-pages``
        when given: in a rolling run that flag reads as pages per top-up,
        since there is no single expansion for it to cap.

        The ``--max`` ceiling applies here too, with one exception: when the
        page already sits at it, one page is loaded anyway. Withdrawal only
        reaches invitations that exist in the DOM, so a run that refuses to
        load anything could never work its way down to the oldest.
        """
        before = self.card_count()
        if not before:
            # The whole loaded window was just withdrawn. The list refills
            # itself, but not instantly, and a count taken now would read as
            # an empty page and end the run one batch early.
            self._page_pause()
            before = self.card_count()
        cap = self.max_cards
        pages = self.max_pages or ROLLING_CHUNK_PAGES
        if cap is not None and before >= cap:
            logger.debug(
                f"{before} card(s) loaded, already at the --max {cap} "
                f"ceiling; loading one page anyway to keep moving down.")
            cap, pages = None, 1
        return self.scroll_to_end(max_pages=pages, max_cards=cap,
                                  use_js=self.use_js)

    def _run_rolling(self):
        """Withdraw down the list, loading more invitations as it goes.

        The default run expands the whole list before withdrawing anything.
        This one goes the other way: withdraw every loaded invitation already
        past the cutoff, load a couple more pages, withdraw again, down to the
        end of the list. Withdrawn cards
        leave the DOM as new ones arrive, so the page stays about the size it
        started at instead of growing to a thousand rows.

        It starts from whatever is on screen, so scrolling by hand to the
        first invitation past the cutoff and then starting the bot does
        exactly what it looks like. Nothing is withdrawn on a guess here
        either: every card still has to prove its own age.
        """
        self.open_sent_page()
        if not self.card_count():
            logger.warning(
                "No invitation cards found — is the Sent tab open and loaded?")
            return

        confirmed = self.dry_run or self.auto_continue
        while True:
            targets = self._rolling_targets()
            if targets:
                logger.info(
                    f"{len(targets)} loaded invitation(s) past {self.until}, "
                    f"oldest first: "
                    f"{targets[0].get('name')} ({targets[0].get('age')}) "
                    f"→ {targets[-1].get('name')} ({targets[-1].get('age')}).")
                if not confirmed:
                    answer = input(
                        f"\nWithdraw these {len(targets)} and keep going "
                        f"down to the oldest? This cannot be undone. [y/N] "
                    ).strip().lower()
                    if answer not in ("y", "yes", "s", "sim"):
                        logger.info(
                            "Cancelled by user — nothing was withdrawn.")
                        return
                    confirmed = True
                self._withdraw_all(targets, rescan=self._rolling_targets)
            elif self.list_exhausted:
                break

            before = self.card_count()
            if self._top_up() > before:
                continue
            if self.list_exhausted:
                logger.info("End of the list — nothing older left to load.")
            else:
                logger.warning(
                    "The list stopped loading before the end; stopping here. "
                    "Re-run to pick up where this left off.")
            break

        self._report()

    def _rolling_targets(self):
        """Loaded cards past the cutoff that this run has not acted on yet."""
        return [c for c in targets_older_than(self._loaded_cards(), self.until)
                if c.get("key") not in self._processed]

    def _report(self):
        logger.info("=" * 60)
        if self.dry_run:
            logger.info(
                f"Dry run — {self.previewed} invitation(s) would be withdrawn.")
        else:
            logger.info(f"Withdrawn: {self.withdrawn}")
        if self.failed:
            logger.info(f"Failed   : {self.failed}")
        logger.info("=" * 60)


if __name__ == "__main__":
    import argparse
    import logging as _logging

    from common.logging_setup import setup_logging
    from common.sleep import allow_sleep, prevent_sleep

    parser = argparse.ArgumentParser(
        description="Withdraw sent invitations older than a given date.")
    parser.add_argument(
        "--until", metavar="DATE|AGE",
        help="Withdraw invitations sent on or before this date (2026/06/20) "
             "or this far back from today (2m, 3w, 10d, 1y). "
             "Omit to only load the list and report how far back it goes.")
    parser.add_argument(
        "--max", dest="max_cards", type=int, metavar="N",
        help="Load at most N invitations, then withdraw the ones among them "
             "that are older than --until (keeps the page light)")
    parser.add_argument(
        "--rolling", action="store_true",
        help="Withdraw what is loaded and past --until, load two more pages, "
             "repeat down to the oldest, instead of expanding the list first")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be withdrawn without clicking anything")
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt")
    parser.add_argument(
        "--max-pages", "--max-clicks", dest="max_pages", type=int,
        default=None,
        help="Cap the pages loaded, with or without --until "
             "(default: unlimited). With --rolling: pages per top-up")
    parser.add_argument(
        "--no-js", action="store_true",
        help="Skip the in-page load loop; drive every page with trusted clicks")
    parser.add_argument(
        "--stop-early", action="store_true",
        help="Stop loading once --until is in view instead of reaching the "
             "true end of the list")
    parser.add_argument(
        "--probe", action="store_true",
        help="Report what the page exposes (tabs, selectors, buttons) and exit")
    args = parser.parse_args()

    setup_logging(level=_logging.INFO, log_dir="withdraw/logs")
    prevent_sleep()
    try:
        cutoff = None
        if args.until:
            try:
                cutoff = parse_until(args.until)
            except ValueError as e:
                parser.error(f"--until: {e}")
            logger.info(f"Cutoff: {cutoff} (from --until {args.until})")

        bot = LinkedInWithdrawBot(
            until=cutoff,
            dry_run=args.dry_run,
            max_cards=args.max_cards,
            auto_continue=args.yes,
            use_js=not args.no_js,
            stop_early=args.stop_early,
            max_pages=args.max_pages,
            rolling=args.rolling,
        )
        if args.probe:
            bot.probe()
        elif cutoff is None:
            total = bot.scroll_to_end(max_pages=args.max_pages,
                                      max_cards=args.max_cards,
                                      use_js=not args.no_js)
            print(f"\n{total} invitation(s) loaded. "
                  f"Oldest: {bot.oldest_label() or 'unknown'}")
        else:
            bot.run()
    except KeyboardInterrupt:
        logger.warning("Stopped by user (Ctrl+C)")
    finally:
        allow_sleep()
