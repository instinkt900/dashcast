"""Single entrypoint, two roles. `dashcast serve` on the powerful box,
`dashcast display` on the weak one — same codebase, one config file."""

from __future__ import annotations

import argparse
import logging
import sys

from dashcast import __version__


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashcast", description=__doc__)
    parser.add_argument("--version", action="version", version=f"dashcast {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="render the dashboard and host the image (powerful box)")
    p_serve.add_argument("-c", "--config", required=True, help="path to config.toml")
    p_serve.add_argument("--once", action="store_true", help="capture a single screenshot and exit (no HTTP server)")

    p_display = sub.add_parser("display", help="fetch the image and show it on the framebuffer (weak box)")
    p_display.add_argument("-c", "--config", required=True, help="path to config.toml")
    p_display.add_argument("--once", action="store_true", help="fetch and show once, then exit")

    p_offline = sub.add_parser(
        "make-offline",
        help="pre-render the offline notice box to a raw .fb blob (needs Pillow; run on server/dev)",
    )
    p_offline.add_argument("-c", "--config", required=True, help="path to config.toml")
    p_offline.add_argument("-o", "--out", required=True, help="output .fb path")
    p_offline.add_argument("--text", default="Dashboard offline", help="notice title")
    p_offline.add_argument("--subtext", default="Can't reach the render server", help="notice subtitle")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "serve":
        from dashcast.serve import run_serve

        return run_serve(args.config, once=args.once)
    if args.command == "display":
        from dashcast.display import run_display

        return run_display(args.config, once=args.once)
    if args.command == "make-offline":
        from dashcast.offline import run_make_offline

        return run_make_offline(args.config, args.out, args.text, args.subtext)

    parser.print_help(sys.stderr)
    return 2
