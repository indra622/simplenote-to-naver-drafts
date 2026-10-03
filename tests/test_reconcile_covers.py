import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from threads_to_naver.covers import (
    CoverStore,
    cover_queue_entry,
    source_fingerprint,
    write_private_queue,
)
from threads_to_naver.models import ThreadPost

spec = importlib.util.spec_from_file_location(
    "reconcile_covers", Path(__file__).resolve().parents[1] / "scripts/reconcile_covers.py"
)
reconciler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconciler)


def post(item_id="item-1"):
    return ThreadPost(item_id, "private body", datetime(2026, 10, 2, tzinfo=UTC), "", "TEXT_POST")


def inputs(tmp_path, posts):
    queue = tmp_path / "queue.json"
    write_private_queue(queue, [cover_queue_entry(item) for item in posts])
    images = tmp_path / "images"
    images.mkdir()
    for index, item in enumerate(posts):
        name = f"naver-{source_fingerprint(item)[:32]}---00000000-0000-0000-0000-{index:012x}.jpg"
        (images / name).write_bytes(b"\xff\xd8\xff\xe0test\xff\xd9")
    return queue, images, CoverStore(tmp_path / "covers")


def test_reconcile_is_read_only_by_default_and_idempotent_when_applied(tmp_path):
    item = post()
    queue, images, store = inputs(tmp_path, [item])
    assert reconciler.reconcile(store, [item], queue, images) == 1
    assert not store.directory.exists()
    assert reconciler.reconcile(store, [item], queue, images, apply=True) == 1
    cached = store.resolve(item)
    before = cached.stat().st_mtime_ns
    assert reconciler.reconcile(store, [item], queue, images, apply=True) == 0
    assert cached.stat().st_mtime_ns == before
    assert cached.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("problem", ["duplicate", "missing", "corrupt", "symlink", "stale", "collision"])
def test_entire_batch_is_validated_before_install(tmp_path, problem):
    first, second = post(), post("item-2")
    queue, images, store = inputs(tmp_path, [first, second])
    image = max(images.iterdir())
    if problem == "duplicate":
        (images / image.name.replace("00000000-0000", "11111111-0000")).write_bytes(image.read_bytes())
    elif problem == "missing":
        image.unlink()
    elif problem == "corrupt":
        image.write_bytes(b"incomplete JPEG")
    elif problem == "symlink":
        target = tmp_path / "external.jpg"
        image.rename(target)
        image.symlink_to(target)
    elif problem == "stale":
        second = replace(second, text="revised private body")
    else:
        entry = cover_queue_entry(first)
        write_private_queue(queue, [entry, {**entry, "id": "another-id"}])
    with pytest.raises((RuntimeError, ValueError)):
        reconciler.reconcile(store, [first, second], queue, images, apply=True)
    assert not store.directory.exists()


def test_legacy_prefix_requires_explicit_opt_in_and_unrelated_names_are_ignored(tmp_path):
    item = post()
    queue, images, store = inputs(tmp_path, [item])
    image = next(images.iterdir())
    legacy = image.with_name(image.name.replace(source_fingerprint(item)[:32], source_fingerprint(item)[:12]))
    image.rename(legacy)
    (images / ("unrelated-" + legacy.name)).write_bytes(legacy.read_bytes())
    with pytest.raises(RuntimeError, match="found 0"):
        reconciler.reconcile(store, [item], queue, images)
    assert reconciler.reconcile(store, [item], queue, images, fingerprint_chars=12) == 1


def setup_main(config, tmp_path, monkeypatch):
    config = replace(config, source="simplenote", require_generated_cover=True)
    item = post()
    queue, images, _store = inputs(tmp_path, [item])
    monkeypatch.setattr(reconciler.Config, "load", lambda _path: config)
    monkeypatch.setattr(reconciler, "_pending_simplenote_items", lambda _config: [item])
    return config, ["--queue", str(queue), "--image-dir", str(images)]


def test_uncertain_save_leaves_private_marker_and_blocks_automatic_retry(config, tmp_path, monkeypatch, capsys):
    config, args = setup_main(config, tmp_path, monkeypatch)
    calls = []

    def create(_config, _posts, *, dry_run, dry_run_name):
        calls.append(dry_run)
        if not dry_run:
            raise RuntimeError("private source content must not appear in logs")
        return 1

    monkeypatch.setattr(reconciler, "_create_drafts", create)
    assert reconciler.main(args + ["--apply", "--save-drafts"]) == 1
    marker = config.data_dir / "reconciliation-attempt.json"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text())["items"][0]["id"] == post().id
    assert reconciler.main(args + ["--apply", "--save-drafts"]) == 1
    assert calls == [True, False]
    assert "private source content" not in capsys.readouterr().err


def test_read_only_main_never_saves_and_success_clears_marker(config, tmp_path, monkeypatch):
    config, args = setup_main(config, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(reconciler, "_create_drafts", lambda *a, **kw: calls.append(kw["dry_run"]) or 1)
    assert reconciler.main(args) == 0
    assert calls == []
    assert reconciler.main(args + ["--apply", "--save-drafts"]) == 0
    assert calls == [True, False]
    assert not (config.data_dir / "reconciliation-attempt.json").exists()
