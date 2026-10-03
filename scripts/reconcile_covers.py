"""Reconcile completed image artifacts independently of model continuation events."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from pathlib import Path

from threads_to_naver.config import Config
from threads_to_naver.covers import (
    CoverStore,
    _image_suffix,
    source_fingerprint,
    write_private_queue,
)
from threads_to_naver.models import ThreadPost
from threads_to_naver.service import _create_drafts, _pending_simplenote_items

UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


class ReconciliationError(RuntimeError):
    """A diagnostic that contains no source-provider output or note content."""


def reconcile(
    store: CoverStore,
    posts: list[ThreadPost],
    queue_path: Path,
    image_dir: Path,
    *,
    apply: bool = False,
    fingerprint_chars: int = 32,
) -> int:
    if fingerprint_chars not in (12, 32):
        raise ValueError("Fingerprint length must be 12 or 32.")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    entries = queue["items"]
    requests = {}
    prefixes = {}
    for entry in entries:
        item_id, fingerprint = entry["id"], entry["fingerprint"]
        if not isinstance(item_id, str) or not isinstance(fingerprint, str):
            raise TypeError("Invalid cover queue identity.")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Invalid cover queue fingerprint.")
        prefix = fingerprint[:fingerprint_chars]
        if item_id in requests or prefix in prefixes:
            raise ReconciliationError("Ambiguous cover queue; duplicate identity or fingerprint prefix.")
        requests[item_id] = fingerprint
        prefixes[prefix] = fingerprint

    pending_prefixes = [source_fingerprint(post)[:fingerprint_chars] for post in posts]
    if len(set(pending_prefixes)) != len(pending_prefixes):
        raise ReconciliationError("Ambiguous pending-source fingerprint prefixes.")
    files = list(image_dir.iterdir())
    installs = []
    for post in posts:
        fingerprint = source_fingerprint(post)
        if post.id in requests and requests[post.id] != fingerprint:
            raise ReconciliationError("Queued source changed; generate a new cover request before retrying.")
        try:
            store.resolve(post)
            continue
        except RuntimeError:
            pass
        if requests.get(post.id) != fingerprint:
            raise ReconciliationError("A pending source has no matching cover request.")
        pattern = re.compile(
            rf"naver-{fingerprint[:fingerprint_chars]}---{UUID_PATTERN}\.(?:jpg|png)"
        )
        matches = [path for path in files if pattern.fullmatch(path.name)]
        if len(matches) != 1:
            raise ReconciliationError(
                f"Expected one completed image for a queued source; found {len(matches)}."
            )
        image = matches[0]
        if image.is_symlink() or not image.is_file():
            raise ReconciliationError("Generated image must be a regular file in the image directory.")
        if _image_suffix(image) != image.suffix:
            raise ReconciliationError("Generated image format does not match its filename.")
        installs.append((post, fingerprint, image))

    # Validate the entire batch before changing any cache entry.
    if apply:
        for post, fingerprint, image in installs:
            store.install(post.id, fingerprint, image)
            store.resolve(post)
    return len(installs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--fingerprint-chars", type=int, choices=(12, 32), default=32)
    parser.add_argument("--apply", action="store_true", help="Install validated missing covers")
    parser.add_argument(
        "--save-drafts", action="store_true",
        help="Save the verified pending batch; requires --apply and exclusive scheduler ownership",
    )
    args = parser.parse_args(argv)
    if args.save_drafts and not args.apply:
        parser.error("--save-drafts requires --apply")
    try:
        config = Config.load(args.config)
        if config.source != "simplenote" or not config.require_generated_cover:
            raise ReconciliationError("Reconciliation requires Simplenote and generated-cover preflight.")
        config.ensure_directories()
        lock_fd = os.open(config.data_dir / "reconciliation.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReconciliationError("Another reconciliation is active.") from None
            attempt = config.data_dir / "reconciliation-attempt.json"
            if args.save_drafts and attempt.exists():
                raise ReconciliationError(
                    "An earlier draft attempt needs review; refusing automatic retry. "
                    "Check Naver and local state before clearing the private attempt marker."
                )
            posts = _pending_simplenote_items(config)[:config.simplenote_max_notes_per_run]
            count = reconcile(
                CoverStore(config.data_dir / "covers"), posts, args.queue, args.image_dir,
                apply=args.apply, fingerprint_chars=args.fingerprint_chars,
            )
            print(f"Verified {len(posts)} pending source(s); {count} cover installation(s) "
                  f"{'applied' if args.apply else 'planned'}.")
            if args.save_drafts and posts:
                records = [{"id": post.id, "fingerprint": source_fingerprint(post)} for post in posts]
                # A crash between Naver save and local state commit must require human review.
                write_private_queue(attempt, records)
                _create_drafts(config, posts, dry_run=True, dry_run_name="reconciliation")
                created = _create_drafts(config, posts, dry_run=False, dry_run_name="reconciliation")
                attempt.unlink()
                print(f"Completed: {created} new private draft(s).")
        return 0
    except ReconciliationError as error:
        print(f"Reconciliation failed: {error}", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        # Exception messages from source providers can contain private source content.
        print("Reconciliation failed: missing, stale, ambiguous, or invalid input; "
              "or an unresolved attempt/active reconciliation. "
              "No automatic retry of an uncertain draft save is allowed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
