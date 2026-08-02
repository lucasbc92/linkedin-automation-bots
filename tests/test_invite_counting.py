"""Regression tests for invitation counting.

Two production bugs motivated these (see connect/logs/week-2026-07-19.log):

1. A run stopped with Ctrl+C logged no tally at all — the summary sat after
   the page loop in run_automation(), so the interrupt jumped over it.
   Evidence: run 2026-07-13 01:24:46 sent 2 invitations and recorded nothing.

2. "Weekly total" was always 0: logging writes to connect/logs/ (main.py)
   while the count read logs/ (week_log_path's default).
   Evidence: run 2026-07-24 04:20:32 logged "sent: 53" then "Weekly total: 0"
   with 195 invitations actually in that week's file.

Run with:  python -m unittest discover -s tests -t .
"""

import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.logging_setup import BRT
from connect import history
from connect.bot import LinkedInConnectBot


class _CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def text(self):
        return "\n".join(self.messages)


def _make_bot(**overrides):
    """Build a bot with no browser — only the counting paths are exercised."""
    bot = object.__new__(LinkedInConnectBot)
    bot.reverse = False
    bot.connections_sent = 0
    bot.connections_failed = 0
    bot.connections_skipped = 0
    bot.non_tech_skipped = 0
    bot.select_search_tab = lambda: True
    bot.check_invitation_limit_warning = lambda: True
    bot.go_to_next_page = lambda: False
    bot.process_page = lambda: True
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "invites.jsonl"

    def test_counts_invites_recorded_in_a_week(self):
        for name in ("Ana", "Bruno", "Carla"):
            history.record_invite(
                name, path=self.tmp, now=datetime(2026, 7, 22, 10, tzinfo=BRT))
        self.assertEqual(history.count_invites_for_week("2026-07-19", self.tmp), 3)

    def test_week_boundary_is_sunday_2100_brt(self):
        history.record_invite(
            "Before", path=self.tmp, now=datetime(2026, 7, 19, 20, 59, tzinfo=BRT))
        history.record_invite(
            "After", path=self.tmp, now=datetime(2026, 7, 19, 21, 1, tzinfo=BRT))
        self.assertEqual(
            dict(history.weekly_counts(self.tmp)),
            {"2026-07-12": 1, "2026-07-19": 1})

    def test_truncated_final_line_does_not_break_counting(self):
        history.record_invite(
            "Ana", path=self.tmp, now=datetime(2026, 7, 22, 10, tzinfo=BRT))
        with open(self.tmp, "a", encoding="utf-8") as f:
            f.write('{"ts": "2026-07-2')          # killed mid-write
        self.assertEqual(history.count_invites_for_week("2026-07-19", self.tmp), 1)

    def test_missing_ledger_counts_zero_instead_of_raising(self):
        self.assertEqual(history.weekly_counts(self.tmp / "nope.jsonl"), {})

    def test_ledger_path_does_not_depend_on_cwd(self):
        """The original bug class: a relative path resolved against the CWD."""
        self.assertTrue(history.DEFAULT_INVITE_FILE.is_absolute())
        expected = Path(history.__file__).resolve().parent / ".invites.jsonl"
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())
            self.assertEqual(history.DEFAULT_INVITE_FILE.resolve(), expected)
        finally:
            os.chdir(cwd)

    def test_backfill_is_idempotent(self):
        log_dir = Path(tempfile.mkdtemp())
        (log_dir / "week-2026-07-19.log").write_text(
            "2026-07-20 03:37:31 [INFO ] Invitation sent to Anielle "
            "[sent=24, failed=0, skipped=0]\n"
            "2026-07-20 03:37:48 [INFO ] Invitation sent to Edson "
            "[sent=25, failed=0, skipped=0]\n"
            "2026-07-20 03:38:02 [WARN ] Invite to Invite X to connect "
            "did not register [sent=25, failed=1, skipped=0]\n",
            encoding="utf-8")

        self.assertEqual(history.backfill_from_logs(log_dir, self.tmp), 2)
        self.assertEqual(history.backfill_from_logs(log_dir, self.tmp), 0)
        self.assertEqual(history.count_invites_for_week("2026-07-19", self.tmp), 2)


class SummaryOnExitTest(unittest.TestCase):
    """The tally must be reported however the run ends."""

    def setUp(self):
        self.capture = _CaptureLogs()
        self.logger = logging.getLogger("linkedin_bot")
        self.logger.addHandler(self.capture)
        self.logger.setLevel(logging.INFO)

        import connect.bot as bot_module
        self._real_count = bot_module.count_invites_for_week
        bot_module.count_invites_for_week = lambda week=None: 195
        self.bot_module = bot_module

    def tearDown(self):
        self.logger.removeHandler(self.capture)
        self.bot_module.count_invites_for_week = self._real_count

    def test_ctrl_c_still_reports_the_tally(self):
        def interrupt():
            bot.connections_sent += 1
            raise KeyboardInterrupt

        bot = _make_bot()
        bot.process_page = interrupt

        bot.run_automation(max_pages=3)   # must not propagate

        text = self.capture.text()
        self.assertIn("Session summary — sent: 1", text)
        self.assertIn("Weekly total", text)
        self.assertIn("195 invitation(s) sent", text)

    def test_clean_run_reports_the_tally(self):
        bot = _make_bot()
        bot.process_page = lambda: setattr(bot, "connections_sent", 7) or True

        bot.run_automation(max_pages=1)

        text = self.capture.text()
        self.assertIn("Session summary — sent: 7", text)
        self.assertIn("195 invitation(s) sent", text)

    def test_unexpected_error_still_reports_the_tally(self):
        def boom():
            bot.connections_sent += 2
            raise RuntimeError("driver died")

        bot = _make_bot()
        bot.process_page = boom

        with self.assertRaises(RuntimeError):
            bot.run_automation(max_pages=2)   # propagates to main.py's handler

        self.assertIn("Session summary — sent: 2", self.capture.text())


if __name__ == "__main__":
    unittest.main()
