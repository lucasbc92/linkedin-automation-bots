"""Tests for parsing invitation ages and picking withdrawal targets.

The sent-invitation manager shows relative ages ("Sent 3 months ago"), never
dates, so every withdrawal decision runs through parse_sent_age. LinkedIn
rounds ages down, so the parsed value is the *newest* date a card can have —
these tests pin that direction, because getting it backwards would withdraw
invitations that are still inside the cutoff.

Run with:  python -m unittest discover -s tests -t .
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from withdraw.bot import (LinkedInWithdrawBot, parse_sent_age, parse_until,
                          reached_cutoff, targets_older_than)

TODAY = date(2026, 8, 20)


class ParseSentAgeEnglishTest(unittest.TestCase):
    def test_minutes_and_hours_are_today(self):
        self.assertEqual(parse_sent_age("Sent 40 minutes ago", TODAY), TODAY)
        self.assertEqual(parse_sent_age("Sent 1 hour ago", TODAY), TODAY)
        self.assertEqual(parse_sent_age("Sent 9 hours ago", TODAY), TODAY)

    def test_hours_past_a_full_day_roll_back(self):
        self.assertEqual(parse_sent_age("Sent 30 hours ago", TODAY),
                         date(2026, 8, 19))

    def test_days(self):
        self.assertEqual(parse_sent_age("Sent 1 day ago", TODAY),
                         date(2026, 8, 19))
        self.assertEqual(parse_sent_age("Sent 6 days ago", TODAY),
                         date(2026, 8, 14))

    def test_weeks(self):
        self.assertEqual(parse_sent_age("Sent 2 weeks ago", TODAY),
                         date(2026, 8, 6))

    def test_months_use_calendar_arithmetic(self):
        self.assertEqual(parse_sent_age("Sent 3 months ago", TODAY),
                         date(2026, 5, 20))
        self.assertEqual(parse_sent_age("Sent 1 month ago", TODAY),
                         date(2026, 7, 20))

    def test_months_clamp_to_short_months(self):
        # 31 March minus one month has no 31 February to land on.
        self.assertEqual(parse_sent_age("Sent 1 month ago", date(2026, 3, 31)),
                         date(2026, 2, 28))

    def test_years(self):
        self.assertEqual(parse_sent_age("Sent 1 year ago", TODAY),
                         date(2025, 8, 20))
        self.assertEqual(parse_sent_age("Sent 2 years ago", TODAY),
                         date(2024, 8, 20))

    def test_implicit_one(self):
        self.assertEqual(parse_sent_age("Sent a month ago", TODAY),
                         date(2026, 7, 20))

    def test_today_and_yesterday(self):
        self.assertEqual(parse_sent_age("Sent today", TODAY), TODAY)
        self.assertEqual(parse_sent_age("Sent yesterday", TODAY),
                         date(2026, 8, 19))


class ParseSentAgePortugueseTest(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(parse_sent_age("Enviado há 9 horas", TODAY), TODAY)

    def test_days(self):
        self.assertEqual(parse_sent_age("Enviado há 3 dias", TODAY),
                         date(2026, 8, 17))

    def test_weeks(self):
        self.assertEqual(parse_sent_age("Enviado há 2 semanas", TODAY),
                         date(2026, 8, 6))

    def test_months_singular_and_plural(self):
        self.assertEqual(parse_sent_age("Enviado há 1 mês", TODAY),
                         date(2026, 7, 20))
        self.assertEqual(parse_sent_age("Enviado há 3 meses", TODAY),
                         date(2026, 5, 20))

    def test_years(self):
        self.assertEqual(parse_sent_age("Enviado há 1 ano", TODAY),
                         date(2025, 8, 20))

    def test_yesterday(self):
        self.assertEqual(parse_sent_age("Enviado ontem", TODAY),
                         date(2026, 8, 19))


class ParseSentAgeUnknownTest(unittest.TestCase):
    def test_unparsable_returns_none(self):
        for raw in ("", None, "Pending", "Sent", "Sent a while back"):
            self.assertIsNone(parse_sent_age(raw, TODAY), raw)


class ReachedCutoffTest(unittest.TestCase):
    def test_false_while_everything_is_newer(self):
        labels = ["Sent 1 hour ago", "Sent 3 days ago", "Sent 2 weeks ago"]
        self.assertFalse(reached_cutoff(labels, date(2026, 6, 1), TODAY))

    def test_true_once_the_oldest_passes_the_cutoff(self):
        labels = ["Sent 1 hour ago", "Sent 3 days ago", "Sent 3 months ago"]
        self.assertTrue(reached_cutoff(labels, date(2026, 6, 1), TODAY))

    def test_boundary_is_inclusive(self):
        self.assertTrue(reached_cutoff(["Sent 1 month ago"],
                                       date(2026, 7, 20), TODAY))

    def test_unparsable_tail_falls_back_to_the_last_readable_label(self):
        labels = ["Sent 1 hour ago", "Sent 4 months ago", "Pending"]
        self.assertTrue(reached_cutoff(labels, date(2026, 6, 1), TODAY))

    def test_empty_list(self):
        self.assertFalse(reached_cutoff([], date(2026, 6, 1), TODAY))


def _card(name, age):
    return {"name": name, "age": age, "key": f"key-{name}"}


class TargetsOlderThanTest(unittest.TestCase):
    def setUp(self):
        # List order: newest first, the way LinkedIn renders it.
        self.cards = [
            _card("Camilla", "Sent 1 hour ago"),
            _card("Ana", "Sent 3 days ago"),
            _card("Caroline", "Sent 2 months ago"),
            _card("Anderson", "Sent 5 months ago"),
        ]

    def test_only_cards_past_the_cutoff_are_selected(self):
        picked = targets_older_than(self.cards, date(2026, 7, 1), TODAY)
        self.assertEqual([c["name"] for c in picked], ["Anderson", "Caroline"])

    def test_result_is_oldest_first(self):
        picked = targets_older_than(self.cards, date(2026, 8, 19), TODAY)
        self.assertEqual([c["name"] for c in picked],
                         ["Anderson", "Caroline", "Ana"])

    def test_unparsable_cards_are_never_withdrawn(self):
        cards = self.cards + [_card("Mystery", "Pending")]
        picked = targets_older_than(cards, date(2026, 7, 1), TODAY)
        self.assertNotIn("Mystery", [c["name"] for c in picked])

    def test_nothing_selected_when_all_are_newer(self):
        self.assertEqual(targets_older_than(self.cards, date(2020, 1, 1), TODAY), [])

    def test_rounding_direction_keeps_ambiguous_cards(self):
        # "2 months ago" spans 20 May–20 June; a 1 June cutoff can't prove the
        # card is older, so it stays.
        cards = [_card("Caroline", "Sent 2 months ago")]
        self.assertEqual(targets_older_than(cards, date(2026, 6, 1), TODAY), [])

    def test_a_recent_card_at_the_bottom_does_not_hide_older_ones(self):
        # LinkedIn doesn't always sort the list: here the last card is a week
        # old while month-old ones sit above it. Walking up from the bottom
        # stopped at that card and withdrew nothing.
        cards = [
            _card("Camilla", "Sent 1 hour ago"),
            _card("Caroline", "Sent 2 months ago"),
            _card("Anderson", "Sent 5 months ago"),
            _card("Ana", "Sent 3 days ago"),
            _card("Bruno", "Sent 1 week ago"),
        ]
        picked = targets_older_than(cards, date(2026, 7, 1), TODAY)
        self.assertEqual([c["name"] for c in picked], ["Anderson", "Caroline"])

    def test_out_of_order_cards_come_back_oldest_first(self):
        cards = [
            _card("Caroline", "Sent 2 months ago"),
            _card("Camilla", "Sent 1 hour ago"),
            _card("Anderson", "Sent 5 months ago"),
            _card("Beatriz", "Sent 1 year ago"),
        ]
        picked = targets_older_than(cards, date(2026, 7, 1), TODAY)
        self.assertEqual([c["name"] for c in picked],
                         ["Beatriz", "Anderson", "Caroline"])

    def test_same_age_cards_keep_bottom_up_order(self):
        cards = [
            _card("Caroline", "Sent 5 months ago"),
            _card("Camilla", "Sent 1 hour ago"),
            _card("Anderson", "Sent 5 months ago"),
        ]
        picked = targets_older_than(cards, date(2026, 7, 1), TODAY)
        self.assertEqual([c["name"] for c in picked], ["Anderson", "Caroline"])


class ParseUntilTest(unittest.TestCase):
    """--until accepts an absolute date or a relative age."""

    def test_absolute_slash_and_dash(self):
        self.assertEqual(parse_until("2026/06/20", TODAY), date(2026, 6, 20))
        self.assertEqual(parse_until("2026-06-20", TODAY), date(2026, 6, 20))

    def test_relative_shorthand(self):
        self.assertEqual(parse_until("2m", TODAY), date(2026, 6, 20))
        self.assertEqual(parse_until("3w", TODAY), date(2026, 7, 30))
        self.assertEqual(parse_until("10d", TODAY), date(2026, 8, 10))
        self.assertEqual(parse_until("1y", TODAY), date(2025, 8, 20))

    def test_relative_spelled_out(self):
        self.assertEqual(parse_until("2 months", TODAY), date(2026, 6, 20))
        self.assertEqual(parse_until("2 meses", TODAY), date(2026, 6, 20))

    def test_case_and_spacing_are_forgiven(self):
        self.assertEqual(parse_until("  2M  ", TODAY), date(2026, 6, 20))

    def test_relative_matches_the_card_labels_it_targets(self):
        # The whole point of the shorthand: "--until 2m" must select exactly
        # the cards reading "Sent 2 months ago" and older, with no drift
        # between how a cutoff and a card age are computed.
        cutoff = parse_until("2m", TODAY)
        self.assertEqual(parse_sent_age("Sent 2 months ago", TODAY), cutoff)
        cards = [
            _card("newer", "Sent 1 month ago"),
            _card("boundary", "Sent 2 months ago"),
            _card("older", "Sent 5 months ago"),
        ]
        self.assertEqual([c["name"] for c in targets_older_than(cards, cutoff, TODAY)],
                         ["older", "boundary"])

    def test_rejects_nonsense(self):
        for value in ("", None, "soon", "2026", "20/06/2026", "5x", "tomorrow"):
            with self.assertRaises(ValueError, msg=value):
                parse_until(value, TODAY)

    def test_rejects_impossible_dates(self):
        with self.assertRaises(ValueError):
            parse_until("2026/02/31", TODAY)


class LoadStopConditionTest(unittest.TestCase):
    """When the loading phase is allowed to stop short of the list's end.

    Uses real "today", since the condition parses labels against the current
    date the way a live run does.
    """

    def _bot(self, stop_early, months_back=2):
        bot = object.__new__(LinkedInWithdrawBot)
        bot.stop_early = stop_early
        bot.until = parse_until(f"{months_back}m")
        return bot

    def test_full_expansion_has_no_stop_condition(self):
        self.assertIsNone(self._bot(stop_early=False)._load_stop_condition())

    def test_stops_once_the_cutoff_is_in_view(self):
        stop = self._bot(stop_early=True)._load_stop_condition()
        self.assertFalse(stop(["Sent 1 hour ago", "Sent 3 weeks ago"]))
        self.assertTrue(stop(["Sent 1 hour ago", "Sent 5 months ago"]))

    def test_unreadable_labels_never_reach_the_cutoff(self):
        stop = self._bot(stop_early=True)._load_stop_condition()
        self.assertFalse(stop(["Pending", "Pending"]))


class LoadCapTest(unittest.TestCase):
    """--max caps the *loading* phase, in invitation cards.

    The point of the flag is a light page: load N cards, then withdraw
    whatever among them is old enough. A run must therefore never page past
    N, and must not page at all when N cards are already there.
    """

    PAGE = 50

    def _bot(self, loaded=50, total=500):
        """A bot whose list grows one page per click, up to ``total`` cards."""
        bot = object.__new__(LinkedInWithdrawBot)
        bot.stop_early = False
        bot.until = None
        bot.chunks = []
        state = {"count": loaded}

        def expand_in_page(max_pages=None, max_cards=None, **kwargs):
            bot.chunks.append({"max_pages": max_pages, "max_cards": max_cards})
            pages = 0
            for _ in range(max_pages):
                # The real in-page loop checks the cap before each page load.
                if max_cards and state["count"] >= max_cards:
                    return {"cards": state["count"], "pages": pages,
                            "reason": "cap"}
                if state["count"] >= total:
                    return {"cards": state["count"], "pages": pages,
                            "reason": "end"}
                state["count"] += self.PAGE
                pages += 1
            return {"cards": state["count"], "pages": pages,
                    "reason": "chunk-done"}

        bot.open_sent_page = lambda: None
        bot.card_count = lambda: state["count"]
        bot.sent_labels = lambda: []
        bot.oldest_label = lambda: None
        bot.expand_in_page = expand_in_page
        return bot

    def test_stops_at_the_cap_instead_of_the_end_of_the_list(self):
        bot = self._bot()
        self.assertEqual(bot.scroll_to_end(max_cards=200), 200)

    def test_the_cap_reaches_the_in_page_loop(self):
        bot = self._bot()
        bot.scroll_to_end(max_cards=200)
        self.assertTrue(bot.chunks)
        self.assertTrue(all(c["max_cards"] == 200 for c in bot.chunks))

    def test_a_cap_already_covered_loads_nothing(self):
        bot = self._bot(loaded=300)
        self.assertEqual(bot.scroll_to_end(max_cards=200), 300)
        self.assertEqual(bot.chunks, [])

    def test_no_cap_still_loads_to_the_end(self):
        bot = self._bot()
        self.assertEqual(bot.scroll_to_end(), 500)


class ScrollOnlyPageBudgetTest(unittest.TestCase):
    """A page budget has to bound a list that loads without a button.

    Some LinkedIn builds have no "Load more" at all: scrolling to the bottom
    mounts the next page. Counting only clicks left those runs unbounded — a
    two-page top-up walked a 969-invitation list to its end and took the
    browser's memory with it.
    """

    PAGE = 10

    def _bot(self, loaded=50, total=1000):
        bot = object.__new__(LinkedInWithdrawBot)
        bot.list_exhausted = False
        state = {"count": loaded, "scrolls": 0}
        bot.state = state

        def wait_for_growth(previous, timeout=None):
            # Scrolling alone mounts the next page — no button involved.
            state["count"] = min(state["count"] + self.PAGE, total)
            return state["count"]

        bot.open_sent_page = lambda: None
        bot.card_count = lambda: state["count"]
        bot.oldest_label = lambda: None
        bot.sent_labels = lambda: []
        bot._scroll_list_bottom = lambda: state.update(
            scrolls=state["scrolls"] + 1)
        bot._find_load_more = lambda: None
        bot._page_pause = lambda: None
        bot._wait_for_growth = wait_for_growth
        return bot

    def test_scroll_loaded_pages_count_against_the_budget(self):
        bot = self._bot()
        self.assertEqual(bot.scroll_to_end(max_pages=2, use_js=False),
                         50 + 2 * self.PAGE)

    def test_the_card_cap_still_holds_without_a_button(self):
        bot = self._bot()
        self.assertLessEqual(bot.scroll_to_end(max_cards=80, use_js=False), 90)


class RollingRunTest(unittest.TestCase):
    """--rolling: withdraw what is loaded, load two more pages, repeat.

    Drives the real loop against a stand-in for the page: a list of cards
    that only reveals a window of itself, grows that window when the bot
    loads more, and drops cards as they are withdrawn — which is what makes
    the page-stays-light claim true, and what could just as easily make the
    loop spin forever.
    """

    OLD = "Sent 2 months ago"
    RECENT = "Sent 3 days ago"

    def _bot(self, ages, loaded=3, page=2, fails=()):
        bot = object.__new__(LinkedInWithdrawBot)
        bot.until = parse_until("1m")
        bot.dry_run = False
        bot.auto_continue = True
        bot.rolling = True
        bot.max_cards = None
        bot.max_pages = None
        bot.stop_early = False
        bot.use_js = True
        bot.withdrawn = bot.previewed = bot.failed = 0
        bot._processed = set()
        bot.list_exhausted = False

        cards = [{"name": f"P{i}", "age": age, "key": f"k{i}"}
                 for i, age in enumerate(ages)]
        state = {"cards": cards, "window": loaded, "loads": 0}
        bot.state = state
        bot.attempts = []

        def visible():
            return state["cards"][:state["window"]]

        def withdraw_card(card):
            bot.attempts.append(card["key"])
            if card["key"] in fails:
                return False
            state["cards"] = [c for c in state["cards"]
                              if c["key"] != card["key"]]
            state["window"] = max(0, state["window"] - 1)
            return True

        def top_up():
            state["loads"] += 1
            # A stuck page would otherwise spin here forever; fail loudly.
            assert state["loads"] < 50, "rolling run kept loading forever"
            if state["window"] >= len(state["cards"]):
                bot.list_exhausted = True
            else:
                state["window"] = min(state["window"] + page,
                                      len(state["cards"]))
                bot.list_exhausted = state["window"] >= len(state["cards"])
            return len(visible())

        bot.open_sent_page = lambda: None
        bot.card_count = lambda: len(visible())
        bot.oldest_label = lambda: None
        bot._loaded_cards = visible
        bot.withdraw_card = withdraw_card
        bot._top_up = top_up
        return bot

    def _run(self, bot):
        with mock.patch("time.sleep"):
            bot._run_rolling()

    def test_drains_the_list_down_to_the_oldest(self):
        bot = self._bot([self.OLD] * 10)
        self._run(bot)
        self.assertEqual(bot.withdrawn, 10)
        self.assertEqual(bot.state["cards"], [])

    def test_loads_more_than_one_batch(self):
        bot = self._bot([self.OLD] * 10, loaded=3, page=2)
        self._run(bot)
        self.assertGreater(bot.state["loads"], 1)

    def test_invitations_inside_the_cutoff_are_left_alone(self):
        bot = self._bot([self.RECENT] * 3 + [self.OLD] * 4)
        self._run(bot)
        self.assertEqual(bot.withdrawn, 4)
        self.assertEqual([c["age"] for c in bot.state["cards"]],
                         [self.RECENT] * 3)

    def test_unreadable_ages_are_never_withdrawn(self):
        bot = self._bot([self.OLD, "Pending", self.OLD])
        self._run(bot)
        self.assertEqual(bot.withdrawn, 2)
        self.assertEqual([c["age"] for c in bot.state["cards"]], ["Pending"])

    def test_a_failed_withdrawal_is_tried_once_and_then_skipped(self):
        bot = self._bot([self.OLD] * 4, fails={"k1"})
        self._run(bot)
        self.assertEqual(bot.attempts.count("k1"), 1)
        self.assertEqual(bot.failed, 1)
        self.assertEqual(bot.withdrawn, 3)

    def test_a_list_that_stops_loading_ends_the_run(self):
        bot = self._bot([self.RECENT] * 3 + [self.OLD] * 5)

        def stuck():
            bot.state["loads"] += 1
            assert bot.state["loads"] < 50, "rolling run kept loading forever"
            return bot.card_count()

        bot._top_up = stuck
        self._run(bot)
        # Nothing past the cutoff is loaded and the page will not grow, so the
        # run has to give up rather than page for the rest of the day.
        self.assertEqual(bot.withdrawn, 0)


class DefaultRunTest(unittest.TestCase):
    """The default run: expand the whole list, then withdraw what is old.

    LinkedIn doesn't keep the list strictly newest-first — hours-old
    invitations turn up below month-old ones. A run that walked up from the
    bottom stopped at the first recent card and reported "Nothing to
    withdraw" on a list full of old invitations.
    """

    OLD = "Sent 2 months ago"
    RECENT = "Sent 1 week ago"

    def _run(self, ages):
        bot = object.__new__(LinkedInWithdrawBot)
        bot.until = parse_until("1m")
        bot.dry_run = False
        bot.auto_continue = True
        bot.rolling = False
        bot.max_cards = None
        bot.max_pages = None
        bot.stop_early = False
        bot.use_js = True
        bot.withdrawn = bot.previewed = bot.failed = 0
        bot._processed = set()

        cards = [{"name": f"P{i}", "age": age, "key": f"k{i}"}
                 for i, age in enumerate(ages)]
        bot.withdrawn_keys = []
        bot.scroll_to_end = lambda **kwargs: len(cards)
        bot.oldest_label = lambda: None
        bot._loaded_cards = lambda: cards

        def withdraw_card(card):
            bot.withdrawn_keys.append(card["key"])
            return True

        bot.withdraw_card = withdraw_card
        with mock.patch("time.sleep"):
            bot.run()
        return bot

    def test_a_recent_card_at_the_bottom_does_not_stop_the_run(self):
        bot = self._run([self.RECENT, self.OLD, self.OLD, self.RECENT])
        self.assertEqual(sorted(bot.withdrawn_keys), ["k1", "k2"])
        self.assertEqual(bot.withdrawn, 2)

    def test_old_cards_between_recent_ones_are_all_withdrawn(self):
        bot = self._run([self.OLD, self.RECENT, "Sent 5 months ago",
                         "Sent 3 hours ago", self.OLD, self.RECENT])
        self.assertEqual(bot.withdrawn_keys, ["k2", "k4", "k0"])

    def test_nothing_past_the_cutoff_withdraws_nothing(self):
        bot = self._run([self.RECENT, "Sent 3 hours ago"])
        self.assertEqual(bot.withdrawn_keys, [])


if __name__ == "__main__":
    unittest.main()
