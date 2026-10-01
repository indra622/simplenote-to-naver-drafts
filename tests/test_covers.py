from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from threads_to_naver.covers import CoverStore, cover_queue_entry, write_private_queue
from threads_to_naver.models import ThreadPost
from threads_to_naver.naver import (
    NaverDraftWriter,
    _editor_text_body_text,
    _verify_cover,
)
from threads_to_naver.service import _create_drafts
from threads_to_naver.state import StateStore


def _post(text: str = "body") -> ThreadPost:
    return ThreadPost(
        "simplenote:item-1", text, datetime(2026, 9, 29, tzinfo=UTC),
        "simplenote://note/item-1", "TEXT_POST", title_override="직접 쓰는 AI교양",
    )


def _jpeg(path: Path) -> None:
    path.write_bytes(b"\xff\xd8\xff\xe0test\xff\xd9")


def test_cover_cache_binds_image_to_exact_source_and_detects_damage(tmp_path):
    post = _post()
    image = tmp_path / "generated.jpg"
    _jpeg(image)
    store = CoverStore(tmp_path / "covers")
    entry = cover_queue_entry(post)
    cached = store.install(post.id, entry["fingerprint"], image)

    assert store.resolve(post) == cached
    with pytest.raises(RuntimeError, match="Missing generated cover"):
        store.resolve(replace(post, text="revised body"))

    cached.write_bytes(b"not an image")
    with pytest.raises(RuntimeError, match="Invalid generated cover"):
        store.resolve(post)


def test_cover_queue_file_is_private_and_contains_source_for_agent(tmp_path):
    path = tmp_path / "queue.json"
    entry = cover_queue_entry(_post("private note body"))
    write_private_queue(path, [entry])

    assert path.stat().st_mode & 0o777 == 0o600
    assert "private note body" in path.read_text(encoding="utf-8")
    assert "private note body" not in entry["fingerprint"]


def test_missing_cover_never_opens_naver_or_marks_source_complete(
    config, monkeypatch
):
    config = replace(config, require_generated_cover=True)

    class ForbiddenWriter:
        def __init__(self, _config):
            raise AssertionError("Naver must not open before cover preflight")

    monkeypatch.setattr("threads_to_naver.service.NaverDraftWriter", ForbiddenWriter)
    with pytest.raises(RuntimeError, match="Missing generated cover"):
        _create_drafts(config, [_post()], dry_run=False, dry_run_name="test")
    with StateStore(config.state_db) as state:
        assert not state.contains(_post().id)
    with pytest.raises(RuntimeError, match="Missing generated cover"):
        _create_drafts(config, [_post()], dry_run=True, dry_run_name="test")


def test_draft_log_does_not_echo_source_content(config, monkeypatch, capsys):
    class Writer:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def create_draft(self, _post, _media, *, cover_path):
            assert cover_path is None

    monkeypatch.setattr("threads_to_naver.service.NaverDraftWriter", Writer)
    _create_drafts(
        config, [_post("private source body")], dry_run=False, dry_run_name="test"
    )

    output = capsys.readouterr().out
    assert "private source body" not in output
    assert "직접 쓰는 AI교양" not in output


def test_representative_cover_must_be_first_and_selected(tmp_path):
    image = tmp_path / "cover.jpg"
    _jpeg(image)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-documentTitle"></div>'
            '<div class="se-component se-image">'
            '<img class="se-image-resource" alt="cover.jpg" '
            'src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' '
            'width=\'2\' height=\'2\'%3E%3C/svg%3E">'
            '<button class="se-set-rep-image-button" '
            'onclick="this.classList.add(\'se-is-selected\')">대표</button></div>'
            '<div class="se-component se-text">body</div>'
        )
        _verify_cover(page, image, select_representative=True)
        assert page.locator(".se-is-selected").count() == 1
        page.locator("img.se-image-resource").first.evaluate(
            "image => { image.alt = ''; image.src += '#cover.jpg'; }"
        )
        _verify_cover(page, image)
        page.locator(".se-component.se-image").first.evaluate(
            "e => e.insertAdjacentHTML('beforebegin', "
            "'<div class=\"se-component se-text\">earlier body</div>')"
        )
        with pytest.raises(RuntimeError, match="representative cover"):
            _verify_cover(page, image)
        browser.close()


def test_new_draft_uploads_cover_before_body_and_verifies_before_save(
    config, tmp_path, monkeypatch
):
    image = tmp_path / "cover.jpg"
    _jpeg(image)
    post = _post("source body")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle" contenteditable="true"></div>'
            '<div class="se-component se-text"><div class="se-section-text" '
            'contenteditable="true"><p class="se-text-paragraph"><br></p></div></div>'
            '<button style="position:fixed;top:0" '
            'onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda _ms: None)
        monkeypatch.setattr(
            "threads_to_naver.naver._select_draft_category", lambda page: None
        )

        def upload(_page, paths, _title):
            assert paths == [image]
            page.locator(".se-component.se-text").evaluate(
                """e => {
                  e.insertAdjacentHTML('beforebegin',
                    `<div class="se-component se-image">
                       <img class="se-image-resource" alt="cover.jpg"
                         src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='2' height='2'%3E%3C/svg%3E">
                       <button class="se-set-rep-image-button se-is-selected">대표</button>
                     </div>`);
                  e.querySelector('.se-section-text').innerHTML =
                    '<p class="se-text-paragraph"><br></p>';
                }"""
            )

        monkeypatch.setattr(writer, "_upload_media", upload)
        writer.create_draft(post, [], cover_path=image)

        assert page.evaluate("Boolean(window.saved)")
        assert _editor_text_body_text(page) == post.text
        _verify_cover(page, image)
        browser.close()
