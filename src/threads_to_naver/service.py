from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path

from .config import Config
from .media import download_media
from .models import DATED_SERIES_TITLE, ThreadPost
from .naver import NaverDraftWriter
from .secrets import (
    get_threads_token,
    get_threads_token_saved_at,
    save_threads_token,
)
from .simplenote import SimplenoteMCPClient
from .state import StateStore
from .threads_api import ThreadsAPI

THREADS_EARLIEST_TIMESTAMP = 1_688_540_400


def run(
    config: Config,
    target_date: date,
    dry_run: bool = False,
    latest_only: bool = False,
) -> int:
    config.ensure_directories()
    if latest_only:
        end = datetime.now(config.timezone) - timedelta(seconds=5)
        start = end - timedelta(days=30)
    else:
        start = datetime.combine(target_date, time.min, config.timezone)
        end = start + timedelta(days=1)
    token = get_threads_token()
    api = ThreadsAPI(token)
    posts = api.get_posts(start, end)
    _refresh_token_if_due(api, token)
    posts = _filter_posts(config, posts)
    posts = posts[-1:] if latest_only else posts[: config.max_posts_per_run]

    return _create_drafts(
        config,
        posts,
        dry_run=dry_run,
        dry_run_name=target_date.isoformat(),
    )


def backfill(config: Config, dry_run: bool = False, limit: int | None = None) -> int:
    """Copy every historical original post, oldest first, with resumable state."""
    config.ensure_directories()
    start = datetime.fromtimestamp(THREADS_EARLIEST_TIMESTAMP, config.timezone)
    end = datetime.now(config.timezone) - timedelta(seconds=5)
    token = get_threads_token()
    api = ThreadsAPI(token)
    posts = _filter_posts(config, api.get_posts(start, end))
    _refresh_token_if_due(api, token)

    with StateStore(config.state_db) as state:
        pending = [post for post in posts if not state.contains(post.id)]
    if limit is not None:
        pending = pending[:limit]

    return _create_drafts(
        config,
        pending,
        dry_run=dry_run,
        dry_run_name="backfill",
    )


def run_simplenote(
    config: Config,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    """Create drafts from pending Simplenote notes carrying a queue tag."""
    config.ensure_directories()
    effective_limit = limit or config.simplenote_max_notes_per_run
    with SimplenoteMCPClient(
        config.simplenote_mcp_command,
        config.simplenote_store_path,
    ) as client:
        notes_by_id = {}
        for tag in config.simplenote_queue_tags:
            for note in client.list_tagged_notes(
                tag,
                limit=config.simplenote_scan_limit,
            ):
                notes_by_id[note.id] = note
        notes = list(notes_by_id.values())
    if config.simplenote_start_date is not None:
        notes = [
            note
            for note in notes
            if note.created.astimezone(config.timezone).date()
            >= config.simplenote_start_date
        ]
    items = sorted(
        (note.to_draft_item() for note in notes),
        key=lambda item: (item.timestamp, item.id),
    )
    with StateStore(config.state_db) as state:
        pending = [item for item in items if not state.contains(item.id)]
    return _create_drafts(
        config,
        pending[:effective_limit],
        dry_run=dry_run,
        dry_run_name="simplenote",
    )


def run_daily(config: Config, *, dry_run: bool = False) -> int:
    if config.source == "simplenote":
        return run_simplenote(config, dry_run=dry_run)
    target_date = datetime.now(config.timezone).date() - timedelta(days=1)
    return run(config, target_date, dry_run=dry_run)


def append_footer_to_temp_drafts(
    config: Config,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    config.ensure_directories()
    if not config.footer_url or config.footer_image_path is None:
        raise RuntimeError(
            "Configure both footer_url and footer_image_path before appending footers."
        )
    if not config.footer_image_path.is_file():
        raise FileNotFoundError(f"Missing footer image: {config.footer_image_path}")

    signature = _footer_signature(config.footer_url, config.footer_image_path)
    with StateStore(config.state_db) as state, NaverDraftWriter(config) as writer:
        drafts = writer.list_temp_drafts()
        pending = [
            draft
            for draft in drafts
            if not state.contains_footer(draft.log_no, signature)
        ]
        if limit is not None:
            pending = pending[:limit]
        if dry_run:
            print(
                f"Footer dry run: {len(pending)} pending of {len(drafts)} "
                "temporary draft(s); nothing changed."
            )
            return len(pending)

        handled = 0
        for draft in pending:
            changed = writer.append_footer_to_temp_draft(
                draft.log_no,
                config.footer_url,
                config.footer_image_path,
                save_artifact=handled == 0,
            )
            state.mark_footer_updated(draft.log_no, signature)
            handled += 1
            action = "Updated" if changed else "Already contained footer"
            print(f"{action}: Naver temporary draft {draft.log_no}")
        return handled


def retitle_dated_series_temp_drafts(
    config: Config,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    config.ensure_directories()
    with NaverDraftWriter(config) as writer:
        targets = [
            draft
            for draft in writer.list_temp_drafts()
            if draft.title == DATED_SERIES_TITLE
        ]
        if limit is not None:
            targets = targets[:limit]
        if dry_run:
            print(
                f"Series-title dry run: {len(targets)} temporary draft(s) "
                "need an original-date suffix; nothing changed."
            )
            return len(targets)
        if not targets:
            print("No temporary drafts need a dated series title.")
            return 0

        start = datetime.fromtimestamp(THREADS_EARLIEST_TIMESTAMP, config.timezone)
        end = datetime.now(config.timezone) - timedelta(seconds=5)
        token = get_threads_token()
        api = ThreadsAPI(token)
        posts = [
            post
            for post in _filter_posts(config, api.get_posts(start, end))
            if post.source_title == DATED_SERIES_TITLE
        ]
        _refresh_token_if_due(api, token)

        handled = 0
        for draft in targets:
            new_title = writer.retitle_temp_draft(
                draft.log_no,
                DATED_SERIES_TITLE,
                lambda body, candidates=posts: _dated_title_for_body(
                    body,
                    candidates,
                    config,
                ),
                save_artifact=handled == 0,
            )
            handled += 1
            print(f"Retitled Naver temporary draft {draft.log_no}: {new_title}")
        return handled


def _dated_title_for_body(body: str, posts: list[ThreadPost], config: Config) -> str:
    normalized_body = _normalize_text(_without_footer_url(body, config.footer_url))
    matches = [
        post
        for post in posts
        if (
            normalized_source := _normalize_text(
                _without_footer_url(post.text, config.footer_url)
            )
        )
        and normalized_source in normalized_body
    ]
    if not matches:
        raise RuntimeError(
            "Could not match a Naver series draft to its original Threads post."
        )
    normalized_matches = [
        (
            post,
            _normalize_text(_without_footer_url(post.text, config.footer_url)),
        )
        for post in matches
    ]
    longest = max(len(source) for _, source in normalized_matches)
    best = [post for post, source in normalized_matches if len(source) == longest]
    if len(best) != 1:
        raise RuntimeError(
            "A Naver series draft matched multiple Threads posts; no title was changed."
        )
    return best[0].title_for_timezone(config.timezone)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _without_footer_url(text: str, footer_url: str) -> str:
    return text.replace(footer_url, "") if footer_url else text


def _footer_signature(url: str, image_path: Path) -> str:
    digest = sha256()
    digest.update(url.encode("utf-8"))
    digest.update(b"\0")
    with image_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filter_posts(config: Config, posts: list) -> list:
    return [
        post
        for post in posts
        if (config.include_replies or not post.is_reply)
        and (config.include_reposts or not post.is_repost)
    ]


def _create_drafts(
    config: Config,
    posts: list,
    *,
    dry_run: bool,
    dry_run_name: str,
) -> int:

    with StateStore(config.state_db) as state:
        pending = [post for post in posts if not state.contains(post.id)]
        if dry_run:
            _write_dry_run(config, dry_run_name, pending)
            print(f"Dry run: {len(pending)} pending post(s); no Naver browser opened.")
            return len(pending)

        if not pending:
            print("No new source items to draft.")
            return 0

        created = 0
        with NaverDraftWriter(config) as writer:
            for post in pending:
                title = post.title_for_timezone(config.timezone)
                media_dir = config.artifacts_dir / "media" / post.id
                media_paths = download_media(post, media_dir) if post.media else []
                writer.create_draft(post, media_paths)
                state.mark_drafted(post.id, post.permalink, post.timestamp, title)
                created += 1
                print(f"Drafted source item {post.id}: {title}")
        return created


def _write_dry_run(config: Config, name: str, posts: list) -> Path:
    path = config.artifacts_dir / f"dry-run-{name}.json"
    payload = [
        {
            "id": post.id,
            "title": post.title_for_timezone(config.timezone),
            "text": post.text,
            "timestamp": post.timestamp.isoformat(),
            "permalink": post.permalink,
            "media": [media.url for media in post.media],
        }
        for post in posts
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _refresh_token_if_due(api: ThreadsAPI, token: str) -> None:
    saved_at = get_threads_token_saved_at()
    now = datetime.now(UTC)
    if saved_at is None:
        save_threads_token(token, now)
        return
    if now - saved_at < timedelta(days=1):
        return
    save_threads_token(api.refresh_long_lived_token(), now)
