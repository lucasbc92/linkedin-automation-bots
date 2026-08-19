"""Tests for choosing a template from the last-message preview.

Covers --last-message-regex (default template) together with repeatable
--last-message-regex-custom REGEX FILE rules.

Run with:  python -m unittest discover -s tests -t .
"""

import logging
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.messages import MessageTemplates
from message import bot as bot_module
from message.bot import LinkedInMessageBot

TEMPLATES = Path(__file__).resolve().parents[1] / "message" / "msg_templates"


def _make_bot(default_regex=None, rules=()):
    """A bot with no browser — only the routing helpers are exercised.

    ``rules`` is an iterable of ``(pattern, template name)``; the templates
    themselves are stand-ins that report which file they came from.
    """
    bot = object.__new__(LinkedInMessageBot)
    bot._msg = "default-templates"
    bot._msg_source = "message.txt"
    bot.last_message_regex = re.compile(default_regex) if default_regex else None
    bot._rules = [(re.compile(p), f"{name}-templates", name) for p, name in rules]
    return bot


class TemplateRoutingTest(unittest.TestCase):
    def test_no_rules_means_every_card_gets_the_default(self):
        bot = _make_bot()
        for preview in ("anything at all", "", None):
            templates, source, matched = bot._templates_for(preview)
            self.assertEqual((templates, source, matched),
                             ("default-templates", "message.txt", None))

    def test_default_regex_alone_still_filters(self):
        bot = _make_bot(default_regex=r".*há 7.*")
        self.assertEqual(bot._templates_for("Você: há 7 anos")[1], "message.txt")
        self.assertEqual(bot._templates_for("Você: ontem")[0], None)

    def test_custom_rule_picks_its_own_template(self):
        """The flag combination from the feature request."""
        bot = _make_bot(default_regex=r".*há 7.*",
                        rules=[(r".*for 7.*", "message_english.txt")])

        self.assertEqual(bot._templates_for("You: for 7 years")[1],
                         "message_english.txt")
        self.assertEqual(bot._templates_for("Você: há 7 anos")[1],
                         "message.txt")
        self.assertIsNone(bot._templates_for("You: thanks!")[0])

    def test_custom_rules_are_checked_in_order(self):
        bot = _make_bot(rules=[(r"7", "first.txt"), (r"7 years", "second.txt")])
        self.assertEqual(bot._templates_for("for 7 years")[1], "first.txt")

    def test_custom_rules_win_over_the_default_regex(self):
        bot = _make_bot(default_regex=r"7", rules=[(r"7", "custom.txt")])
        self.assertEqual(bot._templates_for("for 7 years")[1], "custom.txt")

    def test_custom_rules_alone_skip_everything_else(self):
        """No --last-message-regex: only the custom patterns get messaged."""
        bot = _make_bot(rules=[(r".*for 7.*", "message_english.txt")])
        self.assertEqual(bot._templates_for("You: for 7 years")[1],
                         "message_english.txt")
        self.assertIsNone(bot._templates_for("Você: há 7 anos")[0])

    def test_unreadable_preview_is_skipped_when_rules_exist(self):
        bot = _make_bot(rules=[(r".", "any.txt")])
        self.assertIsNone(bot._templates_for(None)[0])

    def test_matched_pattern_is_reported_for_logging(self):
        bot = _make_bot(default_regex=r".*há 7.*",
                        rules=[(r".*for 7.*", "message_english.txt")])
        self.assertEqual(bot._templates_for("You: for 7 years")[2], ".*for 7.*")
        self.assertEqual(bot._templates_for("Você: há 7 anos")[2], ".*há 7.*")

    def test_rule_patterns_are_listed_in_evaluation_order(self):
        bot = _make_bot(default_regex="default",
                        rules=[("a", "a.txt"), ("b", "b.txt")])
        self.assertEqual(bot._rule_patterns(), ["a", "b", "default"])


class _CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def text(self):
        return "\n".join(self.messages)


class _Card:
    """Stand-in for a conversation <li>."""

    def __init__(self, name, preview):
        self.name = name
        self.preview = preview


class RunLoopRoutingTest(unittest.TestCase):
    """The routing decision has to reach the send path, not just the helper."""

    def _run(self, cards, **kwargs):
        bot = _make_bot(**kwargs)
        # Real templates so the logged first line proves which file was used.
        bot._msg = MessageTemplates(
            str(TEMPLATES / "message.txt"))
        bot._rules = [
            (pattern, MessageTemplates(str(TEMPLATES / name)), name)
            for pattern, _, name in bot._rules
        ]

        bot.date_limit = None
        bot.start_date = None
        bot.dry_run = True
        bot.max_messages = None
        bot.inv = False
        bot.sent = bot.failed = bot.skipped = 0
        bot.wait = types.SimpleNamespace(until=lambda cond: True)

        bot._select_messaging_tab = lambda: True
        bot._get_cards = lambda: cards
        bot._active_card = lambda: None
        bot._ensure_rendered = lambda card: card.name if card else None
        bot._card_timestamp = lambda card: None
        bot._card_skip_pill = lambda card: None
        bot._card_last_message = lambda card: card.preview
        bot._advance = lambda card: (
            cards[cards.index(card) + 1] if cards.index(card) + 1 < len(cards)
            else None)

        handler = _CaptureLogs()
        log = logging.getLogger("linkedin_bot")
        log.addHandler(handler)
        try:
            with mock.patch.object(bot_module, "load_sent_names", lambda: set()), \
                 mock.patch.object(bot_module, "prevent_sleep", lambda: None), \
                 mock.patch.object(bot_module, "allow_sleep", lambda: None):
                bot.run()
        finally:
            log.removeHandler(handler)
        return handler.text()

    def test_each_contact_gets_the_template_its_preview_selects(self):
        output = self._run(
            [_Card("Ana Silva", "Você: há 7 anos"),
             _Card("John Doe", "You: for 7 years"),
             _Card("No Match", "You: thanks!")],
            default_regex=r".*há 7.*",
            rules=[(r".*for 7.*", "message_english.txt")])

        self.assertIn("Would send to Ana Silva (message.txt)", output)
        self.assertIn("Would send to John Doe (message_english.txt)", output)
        self.assertIn("Skipping No Match — last message matches no regex", output)

    def test_english_contact_gets_english_body(self):
        output = self._run(
            [_Card("John Doe", "You: for 7 years")],
            rules=[(r".*for 7.*", "message_english.txt")])
        self.assertIn("Hi, John, how are you?", output)
        self.assertNotIn("Oi, John", output)


class CliWiringTest(unittest.TestCase):
    def test_pairs_are_collected_repeatably(self):
        from main import build_parser

        parser = build_parser()
        self.assertEqual(parser.parse_args(["message"]).last_message_regex_custom, [])

        args = parser.parse_args([
            "message",
            "--last-message-regex", ".*há 7.*",
            "--last-message-regex-custom", ".*for 7.*", "message_english.txt",
            "--last-message-regex-custom", ".*hace 7.*", "message_es.txt",
        ])
        self.assertEqual(args.last_message_regex, ".*há 7.*")
        self.assertEqual(args.last_message_regex_custom,
                         [[".*for 7.*", "message_english.txt"],
                          [".*hace 7.*", "message_es.txt"]])

    def test_a_missing_template_stops_the_run(self):
        """A filename typo must not fall through to the generic default text."""
        from main import _require_template

        logger = logging.getLogger("linkedin_bot")
        with self.assertLogs(logger, logging.ERROR) as logs:
            with self.assertRaises(SystemExit) as cm:
                _require_template(str(TEMPLATES / "nope.txt"), "-m/--message", logger)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("nope.txt", logs.output[0])

        # An existing one passes through silently.
        _require_template(str(TEMPLATES / "message_english.txt"),
                          "-m/--message", logger)


if __name__ == "__main__":
    unittest.main()
