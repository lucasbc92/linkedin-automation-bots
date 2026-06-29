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
import sys
from datetime import date
from pathlib import Path

import argcomplete
from argcomplete.completers import BaseCompleter

from common.logging_setup import setup_logging

_TEMPLATE_ROOT = "templates"


# ---------------------------------------------------------------------------
# Tab-completion helpers
# ---------------------------------------------------------------------------

class _TemplateCompleter(BaseCompleter):
    """Complete filenames from templates/<bot>/ for the -m flag."""

    def __init__(self, bot):
        self._bot = bot

    def __call__(self, prefix, **kwargs):
        folder = Path(_TEMPLATE_ROOT) / self._bot
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
    """Prepend templates/<bot>/ unless the user already gave a path."""
    if os.sep in filename or "/" in filename:
        return filename
    return os.path.join(_TEMPLATE_ROOT, bot, filename)


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

templates live in:  templates/connect/
"""

_MESSAGE_EPILOG = """
examples:
  python main.py message
  python main.py message --dry-run
  python main.py message --max 10
  python main.py message --date-limit 2025/12/31
  python main.py message -m message_v2.txt --max 5 --dry-run

templates live in:  templates/message/
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
        help="Template file in templates/connect/  (default: message.txt)")
    m_connect.completer = _TemplateCompleter("connect")
    cp.add_argument(
        "-r", "--reverse", action="store_true",
        help="Navigate in reverse order (click Previous instead of Next)")
    cp.add_argument(
        "-n", "--no-message", action="store_true",
        help="Send invitations without an accompanying note")
    cp.add_argument(
        "-l", "--log-level", default="DEBUG",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Log verbosity (default: DEBUG)")

    # ---- message ----
    mp = sub.add_parser(
        "message",
        help="Send follow-up messages to existing connections",
        description="Walk the LinkedIn Messaging inbox and send a personalized "
                    "follow-up to every non-sponsored conversation, newest first.",
        epilog=_MESSAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m_message = mp.add_argument(
        "-m", "--message", default="message.txt", metavar="FILE",
        help="Template file in templates/message/  (default: message.txt)")
    m_message.completer = _TemplateCompleter("message")
    mp.add_argument(
        "--date-limit", metavar="YYYY/MM/DD",
        help="Stop when a conversation is older than this date "
             "(the list is newest-first, so this halts the whole run)")
    mp.add_argument(
        "--dry-run", action="store_true",
        help="Preview who would be messaged and with what text — nothing is sent")
    mp.add_argument(
        "--max", dest="max_messages", type=int, metavar="N",
        help="Stop after sending N messages (blast-radius limit)")
    mp.add_argument(
        "-l", "--log-level", default="DEBUG",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Log verbosity (default: DEBUG)")

    argcomplete.autocomplete(parser)
    return parser


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_connect(args):
    level_name = "WARNING" if args.log_level == "WARN" else args.log_level
    logger = setup_logging(level=getattr(logging, level_name, logging.DEBUG))

    message_file = _resolve_template(args.message, "connect")

    logger.info("=" * 60)
    logger.info("LinkedIn Connect Bot")
    logger.info(f"  Mode       : {'no note (-n)' if args.no_message else 'personalized note'}")
    if not args.no_message:
        logger.info(f"  Message    : {message_file}")
    logger.info(f"  Navigation : {'reverse (Previous)' if args.reverse else 'forward (Next)'}")
    logger.info(f"  Auto-cont  : {'on (-y)' if args.yes else 'off'}")
    logger.info(f"  Log level  : {args.log_level}")
    logger.info("=" * 60)

    from connect.bot import LinkedInConnectBot
    bot = LinkedInConnectBot(
        auto_continue=args.yes,
        message_file=message_file,
        reverse=args.reverse,
        no_message=args.no_message,
    )
    try:
        bot.run_automation(max_pages=100)
    except KeyboardInterrupt:
        logger.warning("Stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Stopped due to error: {e}")


def run_message(args):
    level_name = "WARNING" if args.log_level == "WARN" else args.log_level
    logger = setup_logging(level=getattr(logging, level_name, logging.DEBUG))

    message_file = _resolve_template(args.message, "message")

    date_limit = None
    if args.date_limit:
        try:
            parts = args.date_limit.replace("-", "/").split("/")
            date_limit = date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            logger.error(f"Invalid --date-limit '{args.date_limit}'. Use YYYY/MM/DD.")
            sys.exit(1)

    logger.info("=" * 60)
    logger.info("LinkedIn Message Bot")
    logger.info(f"  Message    : {message_file}")
    logger.info(f"  Date limit : {date_limit or 'none (full list)'}")
    logger.info(f"  Dry run    : {'yes' if args.dry_run else 'no'}")
    logger.info(f"  Max msgs   : {args.max_messages or 'unlimited'}")
    logger.info(f"  Log level  : {args.log_level}")
    logger.info("=" * 60)

    from message.bot import LinkedInMessageBot
    bot = LinkedInMessageBot(
        message_file=message_file,
        date_limit=date_limit,
        dry_run=args.dry_run,
        max_messages=args.max_messages,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.warning("Stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Stopped due to error: {e}")


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
    else:
        parser.print_help()
        sys.exit(1)
