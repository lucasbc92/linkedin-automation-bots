import argparse
import logging
import os
import sys
from datetime import date

from common.logging_setup import setup_logging

_TEMPLATE_ROOT = "templates"


def _resolve_template(filename, bot):
    """Turn a bare filename into templates/<bot>/<filename>.

    If the user already passed a path that contains a separator (e.g.
    "templates/connect/message.txt" or "./msg.txt") it is used as-is,
    so power users can always point at an arbitrary file.
    """
    if os.sep in filename or "/" in filename:
        return filename
    return os.path.join(_TEMPLATE_ROOT, bot, filename)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="LinkedIn automation bots")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # connect
    cp = sub.add_parser("connect", help="Send connection requests with personalized notes")
    cp.add_argument("-y", "--yes", action="store_true",
                    help="Auto-continue past close-to-limit warnings")
    cp.add_argument("-m", "--message", default="message.txt", metavar="FILE",
                    help="Template filename in templates/connect/ (default: message.txt)")
    cp.add_argument("-r", "--reverse", action="store_true",
                    help="Navigate in reverse (Previous instead of Next)")
    cp.add_argument("-n", "--no-message", action="store_true",
                    help="Send invitations without a note")
    cp.add_argument("-l", "--log-level", default="DEBUG",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="Log verbosity (default: DEBUG)")

    # message
    mp = sub.add_parser("message", help="Send follow-up messages to existing connections")
    mp.add_argument("-m", "--message", default="message.txt", metavar="FILE",
                    help="Template filename in templates/message/ (default: message.txt)")
    mp.add_argument("--date-limit", metavar="YYYY/MM/DD",
                    help="Stop when reaching conversations older than this date")
    mp.add_argument("--dry-run", action="store_true",
                    help="Log who would be messaged without sending anything")
    mp.add_argument("--max", dest="max_messages", type=int, metavar="N",
                    help="Stop after sending N messages")
    mp.add_argument("-l", "--log-level", default="DEBUG",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="Log verbosity (default: DEBUG)")

    return parser


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
