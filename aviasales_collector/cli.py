from __future__ import annotations

import argparse
import json
import sys

from .collector import collect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect current Aviasales Hot Tickets through the page DOM.")
    parser.add_argument("--origin", required=True, help="three-letter origin route code, for example MOW")
    parser.add_argument("--trip-type", required=True, choices=("OW", "RT"))
    parser.add_argument("--duration-min", type=int, default=1, help="RT minimum duration in days (1..30)")
    parser.add_argument("--duration-max", type=int, default=30, help="RT maximum duration in days (1..30)")
    parser.add_argument("--headed", action="store_true", help="show the Chromium window")
    parser.add_argument(
        "--no-refresh-check",
        action="store_true",
        help="skip the default refresh-and-reapply verification",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    result = collect(
        origin=args.origin,
        trip_type=args.trip_type,
        duration_min=args.duration_min,
        duration_max=args.duration_max,
        headless=not args.headed,
        refresh_before_scan=not args.no_refresh_check,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
