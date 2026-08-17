"""LinkedIn automation bots — entry point.

Usage
-----
  python main.py connect [options]   Send connection requests
  python main.py message [options]   Send follow-up messages

Shell tab-completion (one-time setup)
--------------------------------------
  Bash / Git Bash:
      eval "$(register-python-argcomplete main.py)"
      # Add that line to ~/.bashrc to make it permanent.

  Zsh:
      autoload -U bashcompinit && bashcompinit
      eval "$(register-python-argcomplete main.py)"

  PowerShell:
      pip install argcomplete
      Register-ArgumentCompleter -Native -CommandName python -ScriptBlock {
          param($wordToComplete, $commandAst, $cursorPosition)
          $env:_ARGCOMPLETE=1
          $env:COMP_LINE = $commandAst.ToString()
          $env:COMP_POINT = $cursorPosition
          python main.py 2>&1 | ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
      }
"""

import argparse
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import argcomplete
from argcomplete.completers import BaseCompleter

from common.logging_setup import setup_logging
# stdlib-only, unlike connect.bot — safe to import before the browser exists.
from connect.tech_recruiter import DEFAULT_MIN_SCORE

def _template_root(bot):
    return os.path.join(bot, "msg_templates")


# ---------------------------------------------------------------------------
# Tab-completion helpers
# ---------------------------------------------------------------------------

class _TemplateCompleter(BaseCompleter):
    """Complete filenames from <bot>/msg_templates/ for the -m flag."""

    def __init__(self, bot):
        self._bot = bot

    def __call__(self, prefix, **kwargs):
        folder = Path(_template_root(self._bot))
        try:
            return [
                p.name for p in folder.iterdir()
                if p.suffix == ".txt" and p.name.startswith(prefix)
            ]
        except OSError:
            return []


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_template(filename, bot):
    """Prepend <bot>/msg_templates/ unless the user already gave a path."""
    if os.sep in filename or "/" in filename:
        return filename
    return os.path.join(_template_root(bot), filename)


def _require_template(path, flag, logger):
    """Exit unless ``path`` exists.

    MessageTemplates falls back to a generic "Hello {name}!" when a file is
    missing, so without this a typo in a filename sends that text to real
    contacts instead of stopping the run.
    """
    if not os.path.exists(path):
        logger.error(f"Template '{path}' ({flag}) not found.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_CONNECT_EPILOG = """
examples:
  python main.py connect
  python main.py connect -m message_formal.txt
  python main.py connect -n                         # no note
  python main.py connect -y -l INFO                 # auto-continue, less verbose
  python main.py connect -r                         # navigate in reverse (Previous)
  python main.py connect --max 80                   # stop after 80 invitations sent
  python main.py connect --any-title                # invite every recruiter, not just tech
  python main.py connect --title-score 0.9          # stricter tech-recruiter filter
  python main.py connect -f                         # fast: minimal pauses between invites

by default only headlines that read as a *tech* recruiter are invited
(scored by connect/tech_recruiter.py); templates live in:  connect/msg_templates/
"""

_MESSAGE_EPILOG = """
examples:
  python main.py message
  python main.py message --dry-run
  python main.py message --max 10
  python main.py message --date-limit 2025/12/31
  python main.py message --start-date 2025/07/30
  python main.py message -m message_v2.txt --max 5 --dry-run
  python main.py message -i -m reconnect_older.txt --date-limit 2024/10/20
      # click an old conversation first, then walk upward (older→newer),
      # stopping once a conversation is newer than the date limit
  python main.py message --last-message-regex "You:.*"
      # only message conversations where your last message matches the regex
  python main.py message --last-message-regex ".*há 7.*" --last-message-regex-custom ".*for 7.*" message_ingles.txt
      # Portuguese previews get the -m template, English ones message_ingles.txt;
      # everything matching neither regex is skipped

templates live in:  message/msg_templates/
"""

_STATS_EPILOG = """
examples:
  python main.py stats                # invitations sent per week
  python main.py stats --weeks 4      # only the last 4 weeks
  python main.py stats --backfill     # import past invites from connect/logs/

a logging week starts Sunday 21:00 (America/Sao_Paulo)
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="LinkedIn automation bots. Attach Chrome with "
                    "--remote-debugging-port=9222 before running.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ---- connect ----
    cp = sub.add_parser(
        "connect",
        help="Send connection requests with personalized notes",
        description="Walk LinkedIn people-search results and send "
                    "personalised connection invitations.",
        epilog=_CONNECT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cp.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-continue past the weekly close-to-limit warning")
    m_connect = cp.add_argument(
        "-m", "--message", default="message.txt", metavar="FILE",
        help="Template file in connect/msg_templates/  (default: message.txt)")
    m_connect.completer = _TemplateCompleter("connect")
    cp.add_argument(
        "-r", "--reverse", action="store_true",
        help="Navigate in reverse order (click Previous instead of Next)")
    cp.add_argument(
        "-n", "--no-message", action="store_true",
        help="Send invitations without an accompanying note")
    cp.add_argument(
        "--max", dest="max_invites", type=int, metavar="N",
        help="Stop after sending N invitations (blast-radius limit)")
    cp.add_argument(
        "--any-title", action="store_true",
        help="Invite every recruiter, skipping the tech-recruiter headline filter")
    cp.add_argument(
        "--title-score", type=float, default=DEFAULT_MIN_SCORE, metavar="S",
        help=f"Minimum tech-recruiter similarity score, 0.0-1.0 "
             f"(default: {DEFAULT_MIN_SCORE})")
    cp.add_argument(
        "-f", "--fast", action="store_true",
        help="Shrink the randomized pauses between invitations and pages to a "
             "minimum (faster, but a more obviously automated pattern)")
    cp.add_argument(
        "-l", "--log-level", default="DEBUG",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Log verbosity (default: DEBUG)")

    # ---- message ----
    mp = sub.add_parser(
        "message",
        help="Send follow-up messages to existing connections",
        description="Walk the LinkedIn Messaging inbox and send a personalized "
                    "follow-up to every conversation (skipping Sponsored, "
                    "InMail and LinkedIn Offer), newest first.",
        epilog=_MESSAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m_message = mp.add_argument(
        "-m", "--message", default="message.txt", metavar="FILE",
        help="Template file in message/msg_templates/  (default: message.txt)")
    m_message.completer = _TemplateCompleter("message")
    mp.add_argument(
        "--date-limit", metavar="YYYY/MM/DD",
        help="Stop when a conversation is older than this date "
             "(the list is newest-first, so this halts the whole run)")
    mp.add_argument(
        "--start-date", metavar="YYYY/MM/DD",
        help="Scroll down the list, click the first conversation dated on or "
             "before this date, and start sending from there (inclusive)")
    mp.add_argument(
        "-i", "--inv", action="store_true",
        help="Walk the list upward (older→newer) instead of downward. "
             "Flips --date-limit to mean 'stop once newer than this date'")
    mp.add_argument(
        "--dry-run", action="store_true",
        help="Preview who would be messaged and with what text — nothing is sent")
    mp.add_argument(
        "--max", dest="max_messages", type=int, metavar="N",
        help="Stop after sending N messages (blast-radius limit)")
    mp.add_argument(
        "--last-message-regex", metavar="REGEX",
        help="Only message conversations whose last message preview matches "
             "this regular expression (they get the -m template)")
    mp.add_argument(
        "--last-message-regex-custom", action="append", nargs=2, default=[],
        metavar=("REGEX", "FILE"),
        help="Send FILE instead of the -m template to conversations whose "
             "last message matches REGEX. Repeatable; checked in order, "
             "before --last-message-regex")
    mp.add_argument(
        "-l", "--log-level", default="DEBUG",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Log verbosity (default: DEBUG)")

    # ---- stats ----
    sp = sub.add_parser(
        "stats",
        help="Show how many invitations were sent per week",
        description="Report invitations sent per week from the Connect bot's "
                    "ledger (connect/.invites.jsonl).",
        epilog=_STATS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument(
        "--weeks", type=int, metavar="N",
        help="Show only the last N weeks (default: all)")
    sp.add_argument(
        "--backfill", action="store_true",
        help="Import invitations from connect/logs/ into the ledger first "
             "(safe to repeat — already-known entries are skipped)")

    argcomplete.autocomplete(parser)
    return parser


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_connect(args):
    level_name = "WARNING" if args.log_level == "WARN" else args.log_level
    logger = setup_logging(level=getattr(logging, level_name, logging.DEBUG), log_dir="connect/logs")

    message_file = _resolve_template(args.message, "connect")
    if not args.no_message:
        _require_template(message_file, "-m/--message", logger)

    logger.info("=" * 60)
    logger.info("LinkedIn Connect Bot")
    logger.info(f"  Mode       : {'no note (-n)' if args.no_message else 'personalized note'}")
    if not args.no_message:
        logger.info(f"  Message    : {message_file}")
    logger.info(f"  Navigation : {'reverse (Previous)' if args.reverse else 'forward (Next)'}")
    logger.info(f"  Auto-cont  : {'on (-y)' if args.yes else 'off'}")
    logger.info(f"  Max invites: {args.max_invites or 'unlimited'}")
    logger.info(f"  Titles     : {'any recruiter (--any-title)' if args.any_title else f'tech recruiters only (score >= {args.title_score:.2f})'}")
    logger.info(f"  Pacing     : {'fast (-f)' if args.fast else 'humanized'}")
    logger.info(f"  Log level  : {args.log_level}")
    logger.info("=" * 60)

    from connect.bot import LinkedInConnectBot
    bot = LinkedInConnectBot(
        auto_continue=args.yes,
        message_file=message_file,
        reverse=args.reverse,
        no_message=args.no_message,
        max_invites=args.max_invites,
        tech_only=not args.any_title,
        min_title_score=args.title_score,
        fast=args.fast,
    )
    try:
        bot.run_automation(max_pages=100)
    except KeyboardInterrupt:
        logger.warning("Stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Stopped due to error: {e}")


def run_message(args):
    level_name = "WARNING" if args.log_level == "WARN" else args.log_level
    logger = setup_logging(level=getattr(logging, level_name, logging.DEBUG), log_dir="message/logs")

    message_file = _resolve_template(args.message, "message")
    _require_template(message_file, "-m/--message", logger)

    def parse_cli_date(value, flag):
        if not value:
            return None
        try:
            parts = value.replace("-", "/").split("/")
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            logger.error(f"Invalid {flag} '{value}'. Use YYYY/MM/DD.")
            sys.exit(1)

    date_limit = parse_cli_date(args.date_limit, "--date-limit")
    start_date = parse_cli_date(args.start_date, "--start-date")

    def check_regex(pattern, flag):
        if pattern is None:
            return
        try:
            re.compile(pattern)
        except re.error as e:
            logger.error(f"Invalid regex for {flag}: {pattern!r} ({e})")
            sys.exit(1)

    check_regex(args.last_message_regex, "--last-message-regex")

    # (regex, template path) pairs — resolved and validated here so a typo in
    # a filename stops the run instead of silently sending the fallback text.
    regex_templates = []
    for pattern, filename in args.last_message_regex_custom:
        check_regex(pattern, "--last-message-regex-custom")
        path = _resolve_template(filename, "message")
        _require_template(path, "--last-message-regex-custom", logger)
        regex_templates.append((pattern, path))

    logger.info("=" * 60)
    logger.info("LinkedIn Message Bot")
    logger.info(f"  Message    : {message_file}")
    logger.info(f"  Direction  : {'up (older→newer)' if args.inv else 'down (newer→older)'}")
    logger.info(f"  Date limit : {date_limit or 'none (full list)'}")
    logger.info(f"  Start date : {start_date or 'none (top or clicked card)'}")
    logger.info(f"  Dry run    : {'yes' if args.dry_run else 'no'}")
    logger.info(f"  Max msgs   : {args.max_messages or 'unlimited'}")
    logger.info(f"  Last msg regex: {args.last_message_regex or 'none'}")
    for pattern, path in regex_templates:
        logger.info(f"  Custom rule: {pattern!r} -> {path}")
    logger.info(f"  Log level  : {args.log_level}")
    logger.info("=" * 60)

    from message.bot import LinkedInMessageBot
    bot = LinkedInMessageBot(
        message_file=message_file,
        date_limit=date_limit,
        start_date=start_date,
        dry_run=args.dry_run,
        max_messages=args.max_messages,
        inv=args.inv,
        last_message_regex=args.last_message_regex,
        regex_templates=regex_templates,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.warning("Stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Stopped due to error: {e}")


def run_stats(args):
    from common.logging_setup import current_week_start
    from connect.history import DEFAULT_INVITE_FILE, backfill_from_logs, weekly_counts

    if args.backfill:
        added = backfill_from_logs()
        print(f"Backfilled {added} invitation(s) from connect/logs/\n")

    counts = weekly_counts()
    if not counts:
        print(f"No invitations recorded yet in {DEFAULT_INVITE_FILE}.")
        print("Run 'python main.py stats --backfill' to import past runs "
              "from connect/logs/.")
        return

    weeks = list(counts.items())
    if args.weeks:
        weeks = weeks[-args.weeks:]

    this_week = current_week_start().isoformat()

    print("Invitations sent per week (week starts Sunday 21:00 BRT)\n")
    for week, count in weeks:
        marker = "  ← current" if week == this_week else ""
        print(f"  week of {week}   {count:>4}{marker}")
    print(f"  {'-' * 26}")
    print(f"  total{' ' * 15}{sum(c for _, c in weeks):>4}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "connect":
        run_connect(args)
    elif args.command == "message":
        run_message(args)
    elif args.command == "stats":
        run_stats(args)
    else:
        parser.print_help()
        sys.exit(1)
