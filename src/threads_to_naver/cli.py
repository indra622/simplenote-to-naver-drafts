from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .covers import CoverStore
from .naver import NaverDraftWriter
from .secrets import setup_threads_token
from .service import (
    append_footer_to_temp_drafts,
    backfill,
    prepare_cover_queue,
    retitle_dated_series_temp_drafts,
    run,
    run_daily,
    run_simplenote,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threads-to-naver",
        description="Copy Threads posts verbatim into private Naver Blog drafts.",
    )
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("setup-token", help="Save a Threads token in macOS Keychain")
    subparsers.add_parser("login", help="Open the persistent browser for Naver login")

    daily_parser = subparsers.add_parser(
        "daily", help="Run the configured daily source"
    )
    daily_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the configured source without opening Naver",
    )

    queue_parser = subparsers.add_parser(
        "cover-queue", help="Write pending cover inputs to a private local JSON file"
    )
    queue_parser.add_argument("--output", type=Path, required=True)
    install_parser = subparsers.add_parser(
        "cover-install", help="Validate and cache a generated cover for one queue item"
    )
    install_parser.add_argument("--queue", type=Path, required=True)
    install_parser.add_argument("--item-id", required=True)
    install_parser.add_argument("--image", type=Path, required=True)

    simplenote_parser = subparsers.add_parser(
        "simplenote-run",
        help="Create drafts from Simplenote notes with the configured tag",
    )
    simplenote_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum pending notes to process; defaults to the configured daily limit",
    )
    simplenote_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Simplenote and render JSON without opening Naver",
    )

    run_parser = subparsers.add_parser("run", help="Create drafts for one day")
    date_group = run_parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date",
        help="Source date in YYYY-MM-DD; defaults to yesterday",
    )
    date_group.add_argument(
        "--latest",
        action="store_true",
        help="Use only the newest post from the last 30 days",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and render JSON without opening Naver",
    )

    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Create drafts for every historical original post",
    )
    backfill_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum drafts to create in this resumable batch",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and render JSON without opening Naver",
    )

    footer_parser = subparsers.add_parser(
        "append-footer",
        help="Append the configured URL and image to existing temporary drafts",
    )
    footer_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum temporary drafts to update in this resumable batch",
    )
    footer_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the pending count without changing Naver drafts",
    )

    retitle_parser = subparsers.add_parser(
        "retitle-series",
        help="Append original dates to fixed-series temporary-draft titles",
    )
    retitle_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum temporary drafts to retitle in this resumable batch",
    )
    retitle_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the pending count without changing Naver drafts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup-token":
            setup_threads_token()
            return 0

        config = Config.load(args.config)
        config.ensure_directories()
        if args.command == "login":
            with NaverDraftWriter(config) as writer:
                writer.login()
            return 0

        if args.command == "daily":
            count = run_daily(config, dry_run=args.dry_run)
            print(f"Completed: {count} item(s).")
            return 0

        if args.command == "cover-queue":
            prepare_cover_queue(config, args.output)
            return 0

        if args.command == "cover-install":
            queue = json.loads(args.queue.read_text(encoding="utf-8"))
            matches = [
                item for item in queue["items"] if item["id"] == args.item_id
            ]
            if len(matches) != 1:
                raise ValueError("Item ID must match one cover queue entry.")
            item = matches[0]
            destination = CoverStore(config.data_dir / "covers").install(
                item["id"], item["fingerprint"], args.image
            )
            print(f"Installed generated cover in private cache: {destination}")
            return 0

        if args.command == "simplenote-run":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be at least 1")
            count = run_simplenote(
                config,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(f"Completed: {count} item(s).")
            return 0

        if args.command == "backfill":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be at least 1")
            count = backfill(config, dry_run=args.dry_run, limit=args.limit)
            print(f"Completed: {count} item(s).")
            return 0


        if args.command == "append-footer":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be at least 1")
            count = append_footer_to_temp_drafts(
                config,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(f"Completed: {count} item(s).")
            return 0

        if args.command == "retitle-series":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be at least 1")
            count = retitle_dated_series_temp_drafts(
                config,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(f"Completed: {count} item(s).")
            return 0

        target_date = (
            date.fromisoformat(args.date)
            if args.date
            else datetime_today(config) - timedelta(days=1)
        )
        count = run(
            config,
            target_date,
            dry_run=args.dry_run,
            latest_only=args.latest,
        )
        print(f"Completed: {count} item(s).")
        return 0
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def datetime_today(config: Config) -> date:
    from datetime import datetime

    return datetime.now(config.timezone).date()


if __name__ == "__main__":
    raise SystemExit(main())
