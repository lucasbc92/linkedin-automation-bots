"""Tests for reaching people whose result card offers only Follow.

LinkedIn has been putting fewer and fewer Connect controls on search results:
a card that once offered Connect now offers Follow, and the people behind
those cards cannot be invited from the results list at all. The action still
exists on the person's own profile, behind the three-dots More menu, so the
scan takes the detour as part of its normal walk — open the profile, Connect
from the More menu, run the usual note ritual, step back into the results and
carry on from the person just handled. It is one walk, not two passes: nobody
is reached by a separate "Follow-only" mode, and nothing has to be asked for.

Two things about that detour can go badly wrong, and both are pinned down
here: clicking the Connect button of the *wrong person* (a profile page also
shows Connect buttons for the "More profiles for you" rail), and losing the
run's place in the pagination by navigating away from a results URL that does
not carry its page number.

Run with:  python -m unittest discover -s tests -t .
"""
import inspect
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selenium.webdriver.common.by import By

from connect import bot as connect_bot
from connect.bot import LinkedInConnectBot

VIEWPORT = 520
CARD_HEIGHT = 120
MOUNT_CHUNK = 3


# --------------------------------------------------------------------------
# A DOM small enough to read, real enough to climb
# --------------------------------------------------------------------------

_STEP = re.compile(r"^(?P<axis>\.//|//|ancestor-or-self::|ancestor::)?"
                   r"(?P<tag>[a-z*]+)(?P<preds>(\[[^]]*\])*)$")
_PRED = re.compile(r"\[([^]]*)\]")
_CALL = re.compile(r"(contains|starts-with)\(@([\w-]+), '([^']*)'\)")
_SELF = re.compile(r"self::(\w+)")


class Node:
    """A DOM node that answers the handful of queries the bot actually uses."""

    def __init__(self, tag, children=(), text="", **attrs):
        self.tag = tag
        self.attrs = {k.replace("_", "-"): v for k, v in attrs.items()}
        self.parent = None
        self.own_text = text
        self.displayed = self.attrs.pop("displayed", True)
        self.children = []
        for child in children:
            self.append(child)

    def append(self, child):
        child.parent = self
        self.children.append(child)
        return self

    # -- what Selenium exposes -----------------------------------------
    @property
    def text(self):
        """Selenium's .text is the whole subtree's rendered text."""
        parts = [self.own_text] + [n.own_text for n in self._descendants()]
        return " ".join(part for part in parts if part).strip()

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return True

    def find_elements(self, by, selector):
        if by == By.CSS_SELECTOR:
            return [n for n in self._descendants() if css_matches(n, selector)]
        return self._select(selector)

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise LookupError(selector)
        return found[0]

    # -- the mini XPath engine -----------------------------------------
    def _descendants(self):
        for child in self.children:
            yield child
            yield from child._descendants()

    def _ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def _pool(self, axis):
        if axis == "ancestor::":
            return list(self._ancestors())
        if axis == "ancestor-or-self::":
            return [self] + list(self._ancestors())
        return list(self._descendants())

    def _select(self, xpath):
        found = []
        for step in xpath.split(" | "):
            step = step.strip()
            if step == "..":
                if self.parent is not None:
                    found.append(self.parent)
                continue
            match = _STEP.match(step)
            assert match, f"unsupported xpath: {step}"
            tag = match.group("tag")
            preds = _PRED.findall(match.group("preds") or "")
            hits = [n for n in self._pool(match.group("axis"))
                    if (tag == "*" or n.tag == tag) and n._matches(preds)]
            found.extend(hits[:1] if any(p.isdigit() for p in preds) else hits)
        return found

    def _matches(self, preds):
        for pred in preds:
            if pred.isdigit():
                continue
            if "self::" in pred:
                if self.tag not in _SELF.findall(pred):
                    return False
                continue
            for term in pred.split(" and "):
                call = _CALL.match(term.strip())
                assert call, f"unsupported predicate: {term}"
                fn, attr, value = call.groups()
                actual = self.attrs.get(attr) or ""
                if fn == "contains" and value not in actual:
                    return False
                if fn == "starts-with" and not actual.startswith(value):
                    return False
        return True


_CSS_GROUP = re.compile(r"^(?P<tag>[a-z*]*)(?P<attrs>(\[[^]]*\])*)$")
_CSS_ATTR = re.compile(r"^([\w-]+)(?:([~^*$]?=)'([^']*)')?$")


def css_matches(node, selector):
    """Does this node match the selector? Enough CSS for what the bot runs:
    comma-separated groups of an optional tag name and attribute tests.

    Parsed rather than looked up, so that changing a selector in the bot
    changes what these tests see — a selector that stops matching LinkedIn's
    markup has to fail a test, not quietly match nothing.
    """
    for group in selector.split(","):
        match = _CSS_GROUP.match(group.strip())
        assert match, f"unsupported css: {group}"
        tag = match.group("tag")
        if tag and tag != "*" and node.tag != tag:
            continue
        if all(_css_attr_matches(node, test)
               for test in _PRED.findall(match.group("attrs") or "")):
            return True
    return False


def _css_attr_matches(node, test):
    match = _CSS_ATTR.match(test)
    assert match, f"unsupported css attribute test: {test}"
    name, operator, value = match.groups()
    actual = node.attrs.get(name)
    if actual is None:
        return False
    if operator is None:
        return True
    if operator == "=":
        return actual == value
    if operator == "^=":
        return actual.startswith(value)
    if operator == "*=":
        return value in actual
    raise AssertionError(f"unsupported css operator: {operator}")


def menu_item(text, **attrs):
    """A plain item of a profile's More menu."""
    return Node("a", [Node("div", [Node("p", text=text)])],
                role="menuitem", href="https://www.linkedin.com/in/someone/",
                **attrs)


def connect_menu_item(name):
    """The Connect item as LinkedIn actually renders it.

    The <a> carries the link and the menuitem role; the label naming the
    person sits on a *div inside it*, which is why looking for an <a> or a
    <button> with that label came up empty.
    """
    label = Node("div", [Node("div", [Node("p", text="Connect")])],
                 **({"aria_label": f"Invite {name} to connect"} if name else {}))
    return Node("a", [label], role="menuitem",
                href="/preload/custom-invite/?vanityName=x")


def top_card_connect(name):
    """The Connect control in a profile's top card, which is labelled itself."""
    return Node("a", [Node("span", text="Connect")],
                aria_label=f"Invite {name} to connect",
                href="/preload/custom-invite/?vanityName=x")


def result_card(name, action, headline="Tech Recruiter", mutuals=()):
    """One search result, shaped like LinkedIn's: the row is itself a link to
    the profile, the person's own name link comes first, and any shared
    connections are linked further down."""
    url = f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}/"
    if action == "connect":
        control = Node("a", aria_label=f"Invite {name} to connect",
                       href="/preload/custom-invite/")
    else:
        control = Node("button", aria_label=f"Follow {name}")

    card = Node("div", [
        Node("p", [Node("a", href=url)]),
        Node("p", [Node("span")]),
        Node("div", [control]),
    ])
    row = Node("a", [card], href=url)
    for mutual in mutuals:
        row.append(Node("p", [Node("a", href=f"https://www.linkedin.com/in/{mutual}/")]))
    return Node("div", [row], role="listitem"), control, url


class FakeResults:
    """A virtualized results list: rows mount as the scroller reaches them."""

    def __init__(self, cards, mounted=None):
        self.cards = list(cards)
        self.mounted = len(self.cards) if mounted is None else mounted
        self.scroll_top = 0

    @property
    def height(self):
        return self.mounted * CARD_HEIGHT

    @property
    def at_bottom(self):
        return self.scroll_top + VIEWPORT >= self.height - 8

    def state(self):
        return {"cards": self.mounted, "top": round(self.scroll_top),
                "height": self.height, "view": VIEWPORT,
                "atBottom": self.at_bottom}

    def scroll_to_top(self):
        self.scroll_top = 0
        return self.state()

    def scroll_step(self, fraction):
        self.scroll_top = min(self.scroll_top + VIEWPORT * fraction,
                              max(self.height - VIEWPORT, 0))
        if self.at_bottom:
            self.mounted = min(self.mounted + MOUNT_CHUNK, len(self.cards))
        return self.state()

    def tally(self):
        """What page_action_tally() reports for the mounted rows."""
        counts = {"cards": 0, "connect": 0, "follow": 0, "message": 0, "other": 0}
        for row, _control, _url in self.cards[:self.mounted]:
            counts["cards"] += 1
            if row._select(".//a[starts-with(@aria-label, 'Invite ')]"):
                counts["connect"] += 1
            elif row._select(".//button[starts-with(@aria-label, 'Follow ')]"):
                counts["follow"] += 1
            else:
                counts["other"] += 1
        return counts

    def controls(self, xpath):
        found = []
        for row, _control, _url in self.cards[:self.mounted]:
            found.extend(row._select(xpath.replace("//", ".//")))
        return found


class FakeProfile:
    """A profile page: its top card, its rail, and the menu More opens.

    ``menu_connect`` says how the open menu offers Connect:
      ``"labelled"``   — the item LinkedIn ships, whose inner div carries
                         "Invite <name> to connect";
      ``"unlabelled"`` — the same item with no aria-label anywhere, so only
                         the word Connect in its text says what it does;
      ``None``         — the menu has no Connect in it at all.
    """

    def __init__(self, name, connect_in_top_card=False,
                 menu_connect="labelled", rail=("Someone Else",),
                 more_buttons=2):
        self.name = name
        self.menu_connect = menu_connect
        self.more_clicks = 0
        self.menu = None
        self.more_buttons = [Node("button", aria_label="More")
                             for _ in range(more_buttons)]
        self.own_connect = (top_card_connect(name) if connect_in_top_card
                            else None)
        # Every profile page also carries Connect buttons for other people.
        self.rail = [top_card_connect(other) for other in rail]

    def click_more(self):
        self.more_clicks += 1
        items = [menu_item("Send profile in a message"), menu_item("Save to PDF")]
        if self.menu_connect:
            items.append(connect_menu_item(
                self.name if self.menu_connect == "labelled" else None))
        items.append(menu_item(f"Report {self.name.split()[0]}"))
        self.menu = Node("div", items, role="menu")

    def menus(self):
        return [self.menu] if self.menu is not None else []

    def connect_controls(self):
        """Everything on the page that PROFILE_CONNECT_CSS matches."""
        found = list(self.rail)
        if self.own_connect is not None:
            found.insert(0, self.own_connect)
        if self.menu is not None:
            found.extend(self.menu.find_elements(
                By.CSS_SELECTOR, connect_bot.PROFILE_CONNECT_CSS))
        return found


class FakeBrowser:
    """Answers only the queries the detour issues."""

    def __init__(self, results, profiles=None,
                 url="https://www.linkedin.com/search/results/people/?page=7",
                 page_number=7, back_lands_on_results=True, on_refresh=None):
        self.results = results
        self.profiles = profiles or {}
        self.results_url = url
        self.current_url = url
        self.page_number = page_number
        self.back_lands_on_results = back_lands_on_results
        self.on_refresh = on_refresh
        self.refreshed = 0
        self.visited = []
        self.backs = 0
        self.clicked = []

    # -- navigation -----------------------------------------------------
    @property
    def profile(self):
        return self.profiles.get(self.current_url)

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def click(self, element):
        """Clicking More on a profile is what opens its menu."""
        self.clicked.append(element.get_attribute("aria-label"))
        if self.profile is not None and element in self.profile.more_buttons:
            self.profile.click_more()
        return True

    def back(self):
        self.backs += 1
        if self.back_lands_on_results:
            self.current_url = self.results_url

    def refresh(self):
        self.refreshed += 1
        self.results.mounted = min(4, len(self.results.cards))
        self.results.scroll_top = 0
        if self.on_refresh:
            self.on_refresh(self)

    # -- queries --------------------------------------------------------
    def execute_script(self, script, *args):
        if "linkedin-scroll:state" in script:
            return self.results.state()
        if "linkedin-scroll:top" in script:
            return self.results.scroll_to_top()
        if "linkedin-scroll:step" in script:
            return self.results.scroll_step(args[-1])
        if "linkedin-connect:tally" in script:
            return self.results.tally()
        if "scrollIntoView" in script:
            return None
        raise AssertionError(f"unexpected script: {script[:60]}")

    def find_elements(self, by, selector):
        if by == By.XPATH and selector in (connect_bot.TARGET_XPATH,
                                           connect_bot.CONNECT_XPATH,
                                           connect_bot.FOLLOW_XPATH):
            return self.results.controls(selector)
        if by == By.XPATH and selector == connect_bot.MORE_BUTTON_XPATH:
            return self.profile.more_buttons if self.profile else []
        if by == By.CSS_SELECTOR and selector == connect_bot.PROFILE_CONNECT_CSS:
            return self.profile.connect_controls() if self.profile else []
        if by == By.CSS_SELECTOR and selector == connect_bot.MENU_CSS:
            return self.profile.menus() if self.profile else []
        if by == By.CSS_SELECTOR and "interop" in selector:
            return []
        raise AssertionError(f"unexpected find_elements: {selector}")

    def find_element(self, by, selector):
        if "aria-current" in selector:
            if self.page_number is None:
                raise LookupError("no current page button")
            return Node("button", text=str(self.page_number))
        raise AssertionError(f"unexpected find_element: {selector}")


def make_bot(browser, **overrides):
    bot = object.__new__(LinkedInConnectBot)
    bot.driver = browser
    bot.mount_settle = 0
    bot.fast = True
    bot.connections_sent = 0
    bot.connections_failed = 0
    bot.connections_skipped = 0
    bot.non_tech_skipped = 0
    bot.profile_invites_sent = 0
    bot._human_pause = lambda *_: None
    bot._robust_click = lambda element, description="element": browser.click(element)
    bot.page_timeout = 0
    bot.menu_timeout = 0
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class ScanTests(unittest.TestCase):
    """One walk over the page, whichever way each person has to be invited."""

    def page(self, *actions, **kwargs):
        return FakeResults([result_card(f"Person {i}", action)
                            for i, action in enumerate(actions)], **kwargs)

    def walk(self, bot, **kwargs):
        """Every (label, kind) the scan hands back for a page, in order."""
        processed, seen = set(), []
        while True:
            control, label, kind = bot.next_target(processed, **kwargs)
            if control is None:
                return seen
            processed.add(label)
            seen.append((label, kind))

    def test_people_come_back_in_the_order_the_page_shows_them(self):
        """Not Connect controls first and detours afterwards — one list, in
        card order, each person with the route their own card allows."""
        bot = make_bot(FakeBrowser(self.page("follow", "connect", "follow")))
        self.assertEqual(self.walk(bot), [
            ("Follow Person 0", "follow"),
            ("Invite Person 1 to connect", "connect"),
            ("Follow Person 2", "follow"),
        ])

    def test_a_page_of_follow_only_cards_is_no_longer_a_dead_end(self):
        bot = make_bot(FakeBrowser(self.page("follow", "follow")))
        self.assertEqual([kind for _label, kind in self.walk(bot)],
                         ["follow", "follow"])

    def test_rows_that_mount_late_are_still_reached(self):
        page = self.page("follow", "connect", "follow", "connect", mounted=1)
        bot = make_bot(FakeBrowser(page))
        self.assertEqual(len(self.walk(bot)), 4)
        self.assertEqual(page.mounted, 4, "the scan must reach the list's end")

    def test_a_card_offering_both_is_taken_the_cheap_way(self):
        """Some cards carry Follow *and* Connect; opening a profile for one of
        those would spend a page load on someone already reachable."""
        row, control, url = result_card("Dual Card", "connect")
        row._select(".//div")[-1].append(Node("button", aria_label="Follow Dual Card"))
        bot = make_bot(FakeBrowser(FakeResults([(row, control, url)])))
        self.assertEqual(self.walk(bot),
                         [("Invite Dual Card to connect", "connect")])

    def test_follow_cards_are_left_alone_where_the_page_must_not_be_left(self):
        """The one case that still skips them: a results URL that would not
        survive the round trip (see RestorableResultsTests)."""
        bot = make_bot(FakeBrowser(self.page("follow", "connect", "follow")))
        self.assertEqual(self.walk(bot, include_follow=False),
                         [("Invite Person 1 to connect", "connect")])

    def test_the_walk_takes_the_detour_without_being_asked(self):
        default = inspect.signature(
            LinkedInConnectBot.next_target).parameters["include_follow"].default
        self.assertTrue(default)


class PageLoopTests(unittest.TestCase):
    """What a whole page costs, walked end to end."""

    def setUp(self):
        patched = mock.patch.object(connect_bot.time, "sleep", lambda *_: None)
        patched.start()
        self.addCleanup(patched.stop)

    def run_page(self, *actions, mounted=None, on_refresh=None):
        """Walk one page; returns the bot and what each person cost."""
        cards = [result_card(f"Person {i}", action)
                 for i, action in enumerate(actions)]
        page = FakeResults(cards, mounted=mounted)
        browser = FakeBrowser(page, on_refresh=on_refresh)
        bot = make_bot(browser, tech_only=False)
        bot.detoured = []
        bot.clicked_connect = []
        bot.check_invitation_limit_warning = lambda: True
        bot.complete_invite_modal = lambda label, name: (
            bot.clicked_connect.append(label) or "sent")

        def detour(control, label, name):
            bot.detoured.append(label)
            return "sent"

        bot.invite_via_profile = detour
        self.assertTrue(bot.process_page())
        return bot, browser

    def test_both_routes_are_taken_on_one_page(self):
        bot, browser = self.run_page("connect", "follow", "connect")
        self.assertEqual(bot.clicked_connect, ["Invite Person 0 to connect",
                                               "Invite Person 2 to connect"])
        self.assertEqual(bot.detoured, ["Follow Person 1"])
        self.assertEqual(browser.refreshed, 0)

    def test_a_stripped_page_is_reloaded_before_ten_detours_are_paid_for(self):
        """LinkedIn serves pages with every Connect control removed after a
        burst of invitations; they come back on a reload."""
        def restore(browser):
            browser.results.cards = [result_card(f"Person {i}", "connect")
                                     for i in range(3)]

        bot, browser = self.run_page("follow", "follow", "follow",
                                     on_refresh=restore)
        self.assertEqual(browser.refreshed, 1)
        self.assertEqual(bot.detoured, [], "nobody had to go the long way")
        self.assertEqual(len(bot.clicked_connect), 3)

    def test_a_page_that_merely_starts_with_follow_cards_is_not_reloaded(self):
        """The page has connectable people further down, below the rows that
        happened to be mounted — that is not a stripped page."""
        bot, browser = self.run_page("follow", "follow", "follow", "connect",
                                     mounted=2)
        self.assertEqual(browser.refreshed, 0)
        self.assertEqual(bot.detoured, ["Follow Person 0", "Follow Person 1",
                                        "Follow Person 2"])
        self.assertEqual(bot.clicked_connect, ["Invite Person 3 to connect"])

    def test_a_page_still_stripped_after_its_reload_is_walked_by_profile(self):
        bot, browser = self.run_page("follow", "follow")
        self.assertEqual(browser.refreshed, 1)
        self.assertEqual(bot.detoured, ["Follow Person 0", "Follow Person 1"])


class ProfileUrlTests(unittest.TestCase):
    def test_reads_the_persons_own_link_not_a_shared_connections(self):
        row, control, url = result_card(
            "Silvana Ferreira Alcides", "follow",
            mutuals=("flavioceci", "jefersonalberto"))
        bot = make_bot(FakeBrowser(FakeResults([(row, control, url)])))
        self.assertEqual(bot.profile_url_for(control), url)

    def test_drops_the_query_string(self):
        row, control, url = result_card("Someone", "follow")
        link = row._select(".//a")[1]
        link.attrs["href"] = url + "?miniProfileUrn=urn%3Ali%3A123"
        bot = make_bot(FakeBrowser(FakeResults([(row, control, url)])))
        self.assertEqual(bot.profile_url_for(control), url)


class ProfileConnectTests(unittest.TestCase):
    """Finding the right Connect control on a profile page."""

    def setUp(self):
        patched = mock.patch.object(connect_bot.time, "sleep", lambda *_: None)
        patched.start()
        self.addCleanup(patched.stop)

    def browser_on(self, profile):
        url = "https://www.linkedin.com/in/renata/"
        browser = FakeBrowser(FakeResults([]), {url: profile})
        browser.get(url)
        return browser

    def test_never_invites_someone_from_the_profiles_rail(self):
        """A profile page shows Connect buttons for other people too; the one
        that gets clicked is matched by name."""
        profile = FakeProfile("Renata Santos", connect_in_top_card=True,
                              rail=("Someone Else", "Another Person"))
        bot = make_bot(self.browser_on(profile))
        control = bot.find_profile_connect("Renata Santos", timeout=0)
        self.assertEqual(control.get_attribute("aria-label"),
                         "Invite Renata Santos to connect")

    def test_a_page_with_only_other_peoples_connects_yields_nothing(self):
        profile = FakeProfile("Renata Santos", menu_connect=None)
        bot = make_bot(self.browser_on(profile))
        self.assertIsNone(bot.find_profile_connect("Renata Santos", timeout=0))

    def test_the_menus_connect_is_labelled_on_a_div_inside_its_link(self):
        """The regression this class exists for. LinkedIn does not put the
        label on the menu item's <a> — it puts it on a div inside it, so a
        search for a labelled <a> or <button> found nothing and every
        Follow-only person was skipped."""
        profile = FakeProfile("Renata Santos")
        browser = self.browser_on(profile)
        bot = make_bot(browser)

        control = bot.connect_from_more_menu("Renata Santos")

        self.assertEqual(profile.more_clicks, 1)
        self.assertIsNotNone(control, "the Connect in the More menu was missed")
        self.assertEqual(control.tag, "a", "the link is what carries the action")
        self.assertIn("/preload/custom-invite/", control.get_attribute("href"))
        self.assertIsNone(control.get_attribute("aria-label"),
                          "the label is on the div inside it, not on the link")

    def test_the_named_search_reaches_into_the_open_menu(self):
        """The search that keeps the rail safe has to see the menu item too,
        and the label it carries is on a div, not on the link."""
        profile = FakeProfile("Renata Santos")
        bot = make_bot(self.browser_on(profile))
        profile.click_more()

        control = bot.find_profile_connect("Renata Santos", timeout=0)

        self.assertIsNotNone(
            control, "the menu's Connect is labelled on an inner div")
        self.assertIn("/preload/custom-invite/", control.get_attribute("href"))

    def test_an_unlabelled_menu_item_is_found_by_what_it_reads(self):
        """If LinkedIn drops the aria-label entirely, the item in the person's
        own menu that reads Connect is still unambiguous."""
        profile = FakeProfile("Renata Santos", menu_connect="unlabelled")
        bot = make_bot(self.browser_on(profile))
        control = bot.connect_from_more_menu("Renata Santos")
        self.assertIsNotNone(control)
        self.assertIn("/preload/custom-invite/", control.get_attribute("href"))

    def test_a_menu_item_that_is_not_connect_is_never_taken(self):
        profile = FakeProfile("Renata Santos", menu_connect=None)
        bot = make_bot(self.browser_on(profile))
        self.assertIsNone(bot.connect_in_open_menu() if profile.menu
                          else bot.connect_from_more_menu("Renata Santos"))

    def test_a_name_in_a_different_unicode_form_still_matches(self):
        """'Nájera' can arrive composed from the card and decomposed from the
        profile; they spell the same name."""
        profile = FakeProfile("Alex Nájera Arcia", connect_in_top_card=True)
        bot = make_bot(self.browser_on(profile))
        decomposed = "Alex Nájera Arcia"
        self.assertNotEqual(decomposed, "Alex Nájera Arcia")
        self.assertIsNotNone(bot.find_profile_connect(decomposed, timeout=0))

    def test_gives_up_after_a_few_more_buttons(self):
        """The sticky header renders its own More button, and posts further
        down the page render others; only the first few are worth trying."""
        profile = FakeProfile("Renata Santos", menu_connect=None, more_buttons=9)
        bot = make_bot(self.browser_on(profile))
        self.assertIsNone(bot.connect_from_more_menu("Renata Santos"))
        self.assertEqual(profile.more_clicks, connect_bot.MORE_BUTTON_ATTEMPTS)


class DetourTests(unittest.TestCase):
    """The round trip: results → profile → invite → results."""

    def setUp(self):
        patched = mock.patch.object(connect_bot.time, "sleep", lambda *_: None)
        patched.start()
        self.addCleanup(patched.stop)

    def make(self, **browser_kwargs):
        row, control, url = result_card("Renata Santos", "follow")
        profile = FakeProfile("Renata Santos")
        browser = FakeBrowser(FakeResults([(row, control, url)]),
                              {url: profile}, **browser_kwargs)
        bot = make_bot(browser, complete_invite_modal=lambda *_: "sent")
        return bot, browser, control, url, profile

    def test_opens_the_profile_invites_and_comes_back(self):
        bot, browser, control, url, profile = self.make()
        status = bot.invite_via_profile(control, "Follow Renata Santos", "Renata")
        self.assertEqual(status, "sent")
        self.assertEqual(browser.visited, [url])
        self.assertEqual(profile.more_clicks, 1)
        self.assertEqual(browser.backs, 1)
        self.assertEqual(browser.current_url, browser.results_url)
        self.assertEqual(bot.profile_invites_sent, 1)

    def test_counts_the_detour_only_when_the_invite_went_out(self):
        bot, browser, control, _url, _profile = self.make()
        bot.complete_invite_modal = lambda *_: "skipped"
        self.assertEqual(
            bot.invite_via_profile(control, "Follow Renata Santos", "Renata"),
            "skipped")
        self.assertEqual(bot.profile_invites_sent, 0)

    def test_a_profile_with_no_connect_anywhere_is_skipped(self):
        bot, browser, control, url, _profile = self.make()
        browser.profiles[url] = FakeProfile("Renata Santos", menu_connect=None)
        status = bot.invite_via_profile(control, "Follow Renata Santos", "Renata")
        self.assertEqual(status, "skipped")
        self.assertEqual(bot.connections_skipped, 1)
        self.assertEqual(browser.backs, 1, "the results are restored anyway")

    def test_a_profile_that_never_opened_does_not_step_off_the_results(self):
        """Going back from a page the detour never left would land on the
        previous page of results and rescan the wrong people."""
        bot, browser, control, _url, _profile = self.make()

        def refuse(url):
            raise RuntimeError("net::ERR_CONNECTION_RESET")

        browser.get = refuse
        status = bot.invite_via_profile(control, "Follow Renata Santos", "Renata")
        self.assertEqual(status, "skipped")
        self.assertEqual(browser.backs, 0)
        self.assertEqual(browser.current_url, browser.results_url)

    def test_losing_the_results_stops_the_run(self):
        """Coming back somewhere that is not the results page would mean
        paging on from a page the bot never scanned."""
        bot, browser, control, _url, _profile = self.make(
            back_lands_on_results=False)
        bot.await_results = lambda: False
        self.assertEqual(
            bot.invite_via_profile(control, "Follow Renata Santos", "Renata"),
            "stop")


class RestorableResultsTests(unittest.TestCase):
    """Whether the results page may be left at all."""

    def bot_on(self, url, page_number):
        browser = FakeBrowser(FakeResults([]), url=url, page_number=page_number)
        return make_bot(browser)

    def test_a_page_that_carries_its_number_may_be_left(self):
        bot = self.bot_on("https://www.linkedin.com/search/results/people/"
                          "?keywords=recruiter&page=7", 7)
        self.assertTrue(bot.detours_allowed())

    def test_page_one_may_be_left_without_the_parameter(self):
        bot = self.bot_on("https://www.linkedin.com/search/results/people/"
                          "?keywords=recruiter", 1)
        self.assertTrue(bot.detours_allowed())

    def test_a_page_that_would_be_lost_is_not_left(self):
        bot = self.bot_on("https://www.linkedin.com/search/results/people/"
                          "?keywords=recruiter", 7)
        self.assertFalse(bot.detours_allowed())

class LabelTests(unittest.TestCase):
    def test_reads_the_name_out_of_both_kinds_of_label(self):
        bot = object.__new__(LinkedInConnectBot)
        self.assertEqual(bot.full_name_from_label("Invite Ana Lima to connect"),
                         "Ana Lima")
        self.assertEqual(bot.full_name_from_label("Follow Ana Lima"), "Ana Lima")
        self.assertIsNone(bot.full_name_from_label("Send a message to Ana"))
        self.assertIsNone(bot.full_name_from_label(None))

    def test_the_first_name_comes_off_a_follow_label_too(self):
        bot = object.__new__(LinkedInConnectBot)
        self.assertEqual(
            bot.extract_name_from_aria_label("Follow Silvana Ferreira Alcides"),
            "Silvana")


class SavedMarkupTests(unittest.TestCase):
    """The selectors, run against the LinkedIn HTML saved in connect/examples/.

    Every other test here uses a hand-built DOM, which proves the logic but
    not that the selectors match what LinkedIn actually ships. These cases
    close that gap. They need lxml for its XPath engine — it is not needed to
    *run* the bots, so they skip rather than fail when it is absent.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from lxml import html
        except ImportError:  # pragma: no cover - depends on the environment
            raise unittest.SkipTest("lxml is not installed")
        cls.html = html
        cls.examples = (Path(__file__).resolve().parent.parent
                        / "connect" / "examples")

    def load(self, name):
        return self.html.fromstring(
            self.examples.joinpath(name).read_text(encoding="utf-8"))

    def card_for(self, control):
        """What find_result_card() climbs to, in lxml terms."""
        node, card = control, None
        for _ in range(12):
            node = node.getparent()
            if node is None:
                break
            hrefs = {a.get("href", "").split("?")[0].rstrip("/")
                     for a in node.xpath(".//a[contains(@href, 'linkedin.com/in/')]")}
            hrefs.discard("")
            if len(hrefs) > 1:
                break
            if hrefs:
                card = node
        return card

    def test_the_follow_only_card_offers_follow_and_nothing_else(self):
        card_html = self.load("follow-person.html")
        follows = card_html.xpath(connect_bot.FOLLOW_XPATH)
        self.assertEqual([f.get("aria-label") for f in follows],
                         ["Follow Silvana Ferreira Alcides"])
        self.assertEqual(card_html.xpath(connect_bot.CONNECT_XPATH), [],
                         "this is the card the detour exists for")

    def test_the_card_climb_lands_on_the_right_person(self):
        """The row also links the shared connections; picking one of those
        would open a stranger's profile."""
        card_html = self.load("follow-person.html")
        control = card_html.xpath(connect_bot.FOLLOW_XPATH)[0]
        card = self.card_for(control)
        self.assertIsNotNone(card)
        self.assertEqual(card.xpath(connect_bot.CARD_INVITE_XPATH), [])
        links = card.xpath(".//a[contains(@href, '/in/')]")
        self.assertEqual(
            links[0].get("href").split("?")[0],
            "https://www.linkedin.com/in/silvana-ferreira-alcides-09831623/")

    def test_a_connectable_results_page_offers_no_follow_detours(self):
        page = self.load("search-results-section-new.html")
        self.assertTrue(page.xpath(connect_bot.CONNECT_XPATH))
        self.assertEqual(page.xpath(connect_bot.FOLLOW_XPATH), [])

    def test_the_more_menu_labels_its_connect_on_an_inner_div(self):
        """Saved from a live Follow-only profile with the menu open. The
        first cut of this feature looked for a labelled <a> or <button> and
        found nothing here, so every one of those people was skipped."""
        menu = self.load("profile-more-menu.html")

        self.assertEqual(
            menu.xpath("//a[starts-with(@aria-label, 'Invite ')] | "
                       "//button[starts-with(@aria-label, 'Invite ')]"), [],
            "the link does not carry the label")

        labelled = menu.xpath("//*[starts-with(@aria-label, 'Invite ') and "
                              "contains(@aria-label, 'to connect')]")
        self.assertEqual([e.tag for e in labelled], ["div"])
        self.assertEqual(labelled[0].get("aria-label"),
                         "Invite Alex Nájera Arcia to connect")

        clickable = labelled[0].xpath(connect_bot.CLICKABLE_XPATH)
        self.assertEqual(clickable[0].tag, "a",
                         "the <a> around it is what carries the action")
        self.assertIn("/preload/custom-invite/", clickable[0].get("href"))

    def test_the_more_menu_item_reads_connect(self):
        """The last-resort match, for a menu that carries no label at all."""
        menu = self.load("profile-more-menu.html")
        items = menu.xpath("//*[@role='menu']//*[@role='menuitem']")
        texts = [" ".join(item.text_content().split()) for item in items]
        self.assertIn("Connect", texts)

    def test_the_profile_carries_a_more_button_and_a_named_connect(self):
        profile = self.load("profile.html")
        self.assertTrue(profile.xpath(connect_bot.MORE_BUTTON_XPATH))
        # PROFILE_CONNECT_CSS as XPath — lxml's CSS support is optional.
        connects = profile.xpath(
            "//a[starts-with(@aria-label, 'Invite ') and "
            "contains(@aria-label, 'to connect')] | "
            "//button[starts-with(@aria-label, 'Invite ') and "
            "contains(@aria-label, 'to connect')]")
        self.assertEqual([c.get("aria-label") for c in connects],
                         ["Invite Renata Santos to connect"])

if __name__ == "__main__":
    unittest.main()
