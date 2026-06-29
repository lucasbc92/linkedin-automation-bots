import argparse
import logging
import sys

from common.logging_setup import setup_logging


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="LinkedIn automation bots")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # connect
    cp = sub.add_parser("connect", help="Send connection requests with personalized notes")
    cp.add_argument("-y", "--yes", action="store_true",
                    help="Auto-continue past close-to-limit warnings")
    cp.add_argument("-m", "--message", default="connect/templates/message.txt",
                    help="Path to message template file (default: connect/templates/message.txt)")
    cp.add_argument("-r", "--reverse", action="store_true",
                    help="Navigate in reverse (Previous instead of Next)")
    cp.add_argument("-n", "--no-message", action="store_true",
                    help="Send invitations without a note")
    cp.add_argument("-l", "--log-level", default="DEBUG",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="Log verbosity (default: DEBUG)")

    # message
    mp = sub.add_parser("message", help="Send messages to existing connections")
    mp.add_argument("-l", "--log-level", default="DEBUG",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="Log verbosity (default: DEBUG)")

    return parser


def run_connect(args):
    level_name = "WARNING" if args.log_level == "WARN" else args.log_level
    logger = setup_logging(level=getattr(logging, level_name, logging.DEBUG))

    logger.info("=" * 60)
    logger.info("LinkedIn Connect Bot")
    logger.info(f"  Mode       : {'no note (-n)' if args.no_message else 'personalized note'}")
    if not args.no_message:
        logger.info(f"  Message    : {args.message}")
    logger.info(f"  Navigation : {'reverse (Previous)' if args.reverse else 'forward (Next)'}")
    logger.info(f"  Auto-cont  : {'on (-y)' if args.yes else 'off'}")
    logger.info(f"  Log level  : {args.log_level}")
    logger.info("=" * 60)

    from connect.bot import LinkedInConnectBot
    bot = LinkedInConnectBot(
        auto_continue=args.yes,
        message_file=args.message,
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
    logger.info("LinkedIn Message Bot — not yet implemented.")


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
