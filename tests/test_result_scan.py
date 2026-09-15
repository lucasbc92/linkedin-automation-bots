"""Tests for scanning a page of search results end to end.

LinkedIn renders the people-search results as a virtualized list inside the
app shell's own scroll container: only the rows near the scroll position exist
in the DOM, and the document itself does not scroll at all — measured on a
live page, ``document.scrollingElement`` was 633px tall in a 633px viewport
while ``<main>`` held 1450px of content in a 524px one.

The bot used to scan whatever happened to be mounted, once, and then declare
the page finished. Pages whose first rows offer Follow or Message instead of
Connect were reported as having no Connect controls at all, and pages that
started with connectable people were abandoned as soon as a run of
non-connectable ones appeared — the tail was never scrolled into existence.
These tests pin down that the scan now walks the whole list.

Run with:  python -m unittest discover -s tests -t .
"""
import unittest

from selenium.webdriver.common.by import By

from connect import bot as connect_bot
from connect.bot import LinkedInConnectBot


CARD_HEIGHT = 120
VIEWPORT = 520
# How many rows LinkedIn appends each time scrolling reaches the bottom.
MOUNT_CHUNK = 3


class FakeAnchor:
    """The 'Invite <name> to connect' anchor on a mounted card."""

    def __init__(self, label):
        self.label = label

    def get_attribute(self, name):
        return self.label if name == "aria-label" else None


class VirtualizedResults:
    """A results list that only mounts rows as the inner scroller reaches them.

    ``actions`` is one entry per person on the page: "connect" for someone
    invitable, "follow" or "message" for someone whose card offers no Connect
    control at all.
    """

    def __init__(self, actions, mounted=4):
        self.actions = list(actions)
        self.mounted = min(mounted, len(self.actions))
        self.scroll_top = 0
        self.window_scrolls = 0

    # -- geometry -------------------------------------------------------
    @property
    def height(self):
        return self.mounted * CARD_HEIGHT

    @property
    def at_bottom(self):
        return self.scroll_top + VIEWPORT >= self.height - 8

    def state(self):
        return {"cards": self.mounted, "top": round(self.scroll_top),
                "height": self.height, "view": VIEWPORT, "atBottom": self.at_bottom}

    # -- scrolling ------------------------------------------------------
    def scroll_to_top(self):
        self.scroll_top = 0
        return self.state()

    def scroll_step(self, fraction):
        self.scroll_top = min(self.scroll_top + VIEWPORT * fraction,
                              max(self.height - VIEWPORT, 0))
        if self.at_bottom:
            self.mounted = min(self.mounted + MOUNT_CHUNK, len(self.actions))
        return self.state()

    # -- what the DOM exposes -------------------------------------------
    def connect_anchors(self):
        return [FakeAnchor(f"Invite Person {i} to connect")
                for i, action in enumerate(self.actions[:self.mounted])
                if action == "connect"]

    def tally(self):
        mounted = self.actions[:self.mounted]
        return {"cards": len(mounted),
                "connect": mounted.count("connect"),
                "follow": mounted.count("follow"),
                "message": mounted.count("message"),
                "other": 0}


class FakePageButton:
    def __init__(self, number):
        self.text = str(number)


class FakeDriver:
    """Answers only the queries the page scan issues."""

    def __init__(self, page, url="https://www.linkedin.com/search/results/people/?page=7",
                 page_number=7, on_refresh=None):
        self.page = page
        self.refreshed = 0
        self.current_url = url
        self.page_number = page_number
        self.on_refresh = on_refresh

    def execute_script(self, script, *args):
        if "linkedin-scroll:state" in script:
            return self.page.state()
        if "linkedin-scroll:top" in script:
            return self.page.scroll_to_top()
        if "linkedin-scroll:step" in script:
            return self.page.scroll_step(args[-1])
        if "linkedin-connect:tally" in script:
            return self.page.tally()
        if "window.scroll" in script:
            # The document does not scroll on the live page; a bot that only
            # ever drives the window moves nothing.
            self.page.window_scrolls += 1
            return None
        raise AssertionError(f"unexpected script: {script[:80]}")

    def find_elements(self, by, selector):
        if by == By.XPATH and selector == connect_bot.CONNECT_XPATH:
            return self.page.connect_anchors()
        raise AssertionError(f"unexpected find_elements: {selector}")

    def find_element(self, by, selector):
        if "aria-current" in selector:
            if self.page_number is None:
                raise LookupError("no current page button")
            return FakePageButton(self.page_number)
        raise AssertionError(f"unexpected find_element: {selector}")

    def refresh(self):
        self.refreshed += 1
        if self.on_refresh:
            self.on_refresh(self)


def make_bot(page, **driver_kwargs):
    bot = object.__new__(LinkedInConnectBot)
    bot.driver = FakeDriver(page, **driver_kwargs)
    bot.mount_settle = 0
    return bot


def drain(bot):
    """Every Connect label the scan yields for a page, in order.

    include_follow=False is the shape the scan takes on a results page
    that must not be left — see tests/test_profile_connect.py for the walk
    that also picks up the people who can only be invited from their profile.
    """
    processed = set()
    labels = []
    while True:
        target, label, _kind = bot.next_target(processed, include_follow=False)
        if target is None:
            return labels
        processed.add(label)
        labels.append(label)


class PageScanTests(unittest.TestCase):
    def test_finds_people_below_the_mounted_rows(self):
        """The bug: rows 7-9 are connectable but only rows 0-3 start mounted."""
        page = VirtualizedResults(
            ["follow"] * 7 + ["connect"] * 3, mounted=4)
        labels = drain(make_bot(page))
        self.assertEqual(labels, ["Invite Person 7 to connect",
                                  "Invite Person 8 to connect",
                                  "Invite Person 9 to connect"])

    def test_keeps_scanning_past_a_run_of_non_connectable_cards(self):
        """A gap of Follow-only cards must not end the page."""
        page = VirtualizedResults(
            ["connect"] + ["follow"] * 6 + ["connect"] + ["message"] * 2,
            mounted=4)
        labels = drain(make_bot(page))
        self.assertEqual(labels, ["Invite Person 0 to connect",
                                  "Invite Person 7 to connect"])

    def test_mounts_the_whole_list_before_giving_up(self):
        page = VirtualizedResults(["follow"] * 10, mounted=4)
        self.assertEqual(drain(make_bot(page)), [])
        self.assertEqual(page.mounted, 10,
                         "the scan must scroll the list to its end")

    def test_a_genuinely_unconnectable_page_is_reported_with_its_tally(self):
        page = VirtualizedResults(["follow"] * 7 + ["message"] * 3, mounted=4)
        bot = make_bot(page)
        drain(bot)
        self.assertEqual(bot.page_action_tally(),
                         {"cards": 10, "connect": 0, "follow": 7,
                          "message": 3, "other": 0})

    def test_does_not_rely_on_scrolling_the_window(self):
        page = VirtualizedResults(["follow"] * 8 + ["connect"] * 2, mounted=4)
        bot = make_bot(page)
        drain(bot)
        self.assertEqual(page.window_scrolls, 0,
                         "the document does not scroll; only the inner "
                         "container moves the list")

    def test_stops_after_the_step_limit_on_a_list_that_never_ends(self):
        page = VirtualizedResults(["follow"] * 5000, mounted=4)
        bot = make_bot(page)
        target, label, _kind = bot.next_target(set(), include_follow=False)
        self.assertIsNone(target)
        self.assertLess(page.mounted, 5000)


class WaitForResultsTests(unittest.TestCase):
    def test_returns_the_card_count_once_rows_mount(self):
        page = VirtualizedResults(["connect"] * 10, mounted=4)
        self.assertEqual(make_bot(page).wait_for_results(timeout=1), 4)

    def test_parks_the_scroller_at_the_top(self):
        page = VirtualizedResults(["connect"] * 10, mounted=10)
        page.scroll_top = 600
        bot = make_bot(page)
        bot.wait_for_results(timeout=1)
        self.assertEqual(page.scroll_top, 0)

    def test_reports_an_empty_page(self):
        page = VirtualizedResults([], mounted=0)
        self.assertEqual(make_bot(page).wait_for_results(timeout=1), 0)


class ReloadInPlaceTests(unittest.TestCase):
    """After a burst of invitations LinkedIn serves pages where no card offers
    Connect at all; loading the page afresh brings the controls back. The
    reload is only safe where it lands back on the same page."""

    def make(self, **kwargs):
        page = VirtualizedResults(["follow"] * 10, mounted=10)
        return page, make_bot(page, **kwargs)

    def test_reloads_a_page_whose_url_carries_the_page_number(self):
        page, bot = self.make(url="https://www.linkedin.com/search/"
                                  "results/people/?keywords=latam&page=7",
                              page_number=7)
        self.assertTrue(bot.reload_results())
        self.assertEqual(bot.driver.refreshed, 1)

    def test_reloads_on_page_one_where_the_parameter_is_absent(self):
        page, bot = self.make(url="https://www.linkedin.com/search/"
                                  "results/people/?keywords=latam",
                              page_number=1)
        self.assertTrue(bot.reload_results())
        self.assertEqual(bot.driver.refreshed, 1)

    def test_refuses_a_reload_that_would_restart_the_run(self):
        """No page number in the URL and not on page 1: reloading would land
        back on page 1 and silently re-walk the whole search."""
        page, bot = self.make(url="https://www.linkedin.com/search/"
                                  "results/people/?keywords=latam",
                              page_number=7)
        self.assertFalse(bot.reload_results())
        self.assertEqual(bot.driver.refreshed, 0)

    def test_a_reload_that_brings_the_controls_back_is_scanned(self):
        page = VirtualizedResults(["follow"] * 10, mounted=10)

        def restore_connect(driver):
            driver.page.actions = ["follow"] * 8 + ["connect"] * 2

        bot = make_bot(page, on_refresh=restore_connect)
        self.assertTrue(bot.reload_results())
        self.assertEqual(drain(bot), ["Invite Person 8 to connect",
                                      "Invite Person 9 to connect"])


class TallyTests(unittest.TestCase):
    def test_counts_only_the_cards_that_are_mounted(self):
        page = VirtualizedResults(["connect"] * 2 + ["follow"] * 8, mounted=4)
        self.assertEqual(make_bot(page).page_action_tally(),
                         {"cards": 4, "connect": 2, "follow": 2,
                          "message": 0, "other": 0})


if __name__ == "__main__":
    unittest.main()
