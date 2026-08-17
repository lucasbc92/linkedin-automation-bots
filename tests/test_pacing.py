"""Tests for the connect bot's humanizing pauses and --fast mode.

Run with:  python -m unittest discover -s tests -t .
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connect.bot import (FAST_PAUSE_FACTOR, MIN_FAST_PAUSE,
                         LinkedInConnectBot)

# The windows _human_pause() is called with in connect/bot.py.
PAUSE_WINDOWS = [(2, 5), (2, 4), (8, 18), (12, 25)]


def _make_bot(fast):
    """A bot with no browser — only the pacing helpers are exercised."""
    bot = object.__new__(LinkedInConnectBot)
    bot.fast = fast
    return bot


class PauseLengthTest(unittest.TestCase):
    def test_default_pauses_stay_inside_their_window(self):
        bot = _make_bot(fast=False)
        for low, high in PAUSE_WINDOWS:
            for _ in range(200):
                self.assertTrue(low <= bot._pause_seconds(low, high) <= high)

    def test_fast_pauses_are_scaled_down_but_never_below_the_floor(self):
        bot = _make_bot(fast=True)
        for low, high in PAUSE_WINDOWS:
            for _ in range(200):
                delay = bot._pause_seconds(low, high)
                self.assertGreaterEqual(delay, MIN_FAST_PAUSE)
                self.assertLessEqual(
                    delay, max(high * FAST_PAUSE_FACTOR, MIN_FAST_PAUSE))

    def test_fast_is_never_slower_than_the_default_window(self):
        """The whole point of the flag: every window shrinks, none grows."""
        fast, normal = _make_bot(fast=True), _make_bot(fast=False)
        for low, high in PAUSE_WINDOWS:
            worst_fast = max(fast._pause_seconds(low, high) for _ in range(200))
            self.assertLess(worst_fast, low)

    def test_fast_defaults_to_off(self):
        """A bot built without the flag keeps the humanized pacing."""
        default = inspect.signature(
            LinkedInConnectBot.__init__).parameters["fast"].default
        self.assertFalse(default)


class CliWiringTest(unittest.TestCase):
    def test_flag_reaches_the_parser(self):
        from main import build_parser

        parser = build_parser()
        self.assertFalse(parser.parse_args(["connect"]).fast)
        self.assertTrue(parser.parse_args(["connect", "-f"]).fast)
        self.assertTrue(parser.parse_args(["connect", "--fast"]).fast)


if __name__ == "__main__":
    unittest.main()
