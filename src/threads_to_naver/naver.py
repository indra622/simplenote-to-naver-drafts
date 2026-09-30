from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .config import Config
from .models import ThreadPost

TITLE_SELECTORS = (
    ".se-documentTitle",
    ".se-title-text",
    '.se-title-text [contenteditable="true"]',
    '[contenteditable="true"][data-placeholder*="제목"]',
    'textarea[placeholder*="제목"]',
)
BODY_SELECTORS = (
    ".se-section-text .se-text-paragraph",
    '.se-component-content .se-text-paragraph[contenteditable="true"]',
    '.se-text-paragraph[contenteditable="true"]',
    '[contenteditable="true"][data-placeholder*="본문"]',
)
DRAFT_SELECTORS = (
    'button:has-text("임시저장")',
    '[role="button"]:has-text("임시저장")',
    'button:has-text("저장")',
    '[role="button"]:has-text("저장")',
)
IMAGE_BUTTON_SELECTORS = (
    'button[data-name="image"]',
    'button[aria-label*="사진"]',
    'button:has-text("사진")',
)
VIDEO_BUTTON_SELECTORS = (
    'button[data-name="video"]',
    'button[aria-label*="동영상"]',
    'button:has-text("동영상")',
)
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
SAFE_DRAFT_COUNT_LIMIT = 98


@dataclass(frozen=True)
class TempDraft:
    log_no: str
    title: str = ""


class NaverDraftWriter:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._config.profile_dir),
            headless=self._config.headless,
            viewport={"width": 1440, "height": 1000},
            locale="ko-KR",
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()

    def login(self) -> None:
        page = self._new_page()
        page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
        print("Log in to Naver in the opened browser. Credentials stay in the browser.")
        print("Waiting up to 10 minutes for the Naver login session...")
        for _ in range(600):
            cookie_names = {
                cookie["name"] for cookie in self._context.cookies("https://naver.com")
            }
            if "NID_AUT" in cookie_names or "NID_SES" in cookie_names:
                print("Naver login session detected and saved.")
                return
            page.wait_for_timeout(1_000)
        raise RuntimeError("Timed out waiting for Naver login.")

    def create_draft(
        self,
        post: ThreadPost,
        media_paths: Iterable[Path],
        *,
        cover_path: Path | None = None,
    ) -> None:
        page = self._prepare_editor()
        draft_count = _current_draft_count(page)
        if draft_count is not None and draft_count >= SAFE_DRAFT_COUNT_LIMIT:
            raise RuntimeError(
                f"Naver has {draft_count} temporary drafts. "
                "Review or publish some drafts before resuming; no new draft was created."
            )
        post_title = post.title_for_timezone(self._config.timezone)
        title = _find_visible(page, TITLE_SELECTORS, "title editor")
        title.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(post_title)

        body = _find_visible(page, BODY_SELECTORS, "body editor")
        body.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        if cover_path is not None:
            if not cover_path.is_file():
                raise FileNotFoundError(f"Missing generated cover: {cover_path}")
            self._upload_media(page, [cover_path], post_title)
            _verify_cover(page, cover_path, select_representative=True)
            _find_visible(page, BODY_SELECTORS, "body editor after cover").click()
        _insert_verbatim(page, post.text)

        paths = list(media_paths)
        if paths:
            self._upload_media(page, paths, post_title)
        body_text = _editor_text_body_text(page)
        if body_text.strip("\n") != post.text.strip("\n"):
            raise RuntimeError(
                "The Naver body differs from the source text; no draft was saved."
            )
        self._append_configured_footer(page, body_focused=not paths)
        if cover_path is not None:
            _verify_cover(page, cover_path, select_representative=True)

        draft_button = _find_safe_draft_button(page)
        draft_button.click()
        page.wait_for_timeout(5_000)
        if cover_path is not None:
            _verify_cover(page, cover_path)
        self._save_artifact(page, post.id, "saved")

    def list_temp_drafts(self) -> list[TempDraft]:
        page = self._prepare_editor()
        drafts, _ = self._open_temp_draft_list(page)
        return drafts

    def retitle_temp_draft(
        self,
        log_no: str,
        expected_title: str,
        title_for_body: Callable[[str], str],
        *,
        save_artifact: bool = False,
    ) -> str:
        page = self._prepare_editor()
        self._load_temp_draft(page, log_no)
        title = _find_visible(page, TITLE_SELECTORS, "loaded temporary-draft title")
        current_title = " ".join((title.inner_text() or "").split())
        if current_title != expected_title:
            raise RuntimeError(
                f"Naver draft {log_no} title changed before update; no draft was saved."
            )
        new_title = title_for_body(_editor_text_body_text(page))
        if not new_title or new_title == current_title:
            raise RuntimeError(
                f"Naver draft {log_no} did not resolve to a distinct dated title."
            )

        title.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(new_title)
        draft_button = _find_safe_draft_button(page)
        draft_button.click()
        page.wait_for_timeout(5_000)

        saved_title = " ".join((title.inner_text() or "").split())
        if saved_title != new_title:
            raise RuntimeError(f"Naver draft {log_no} did not retain its dated title.")
        if save_artifact:
            self._save_artifact(page, log_no, "retitled")
        return new_title

    def append_footer_to_temp_draft(
        self,
        log_no: str,
        footer_url: str,
        footer_image_path: Path,
        *,
        save_artifact: bool = False,
    ) -> bool:
        page = self._prepare_editor()
        self._load_temp_draft(page, log_no)
        # Reopened editor cards expose preview metadata, but no target URL. The
        # draft read response has no verified target field in this integration.
        # Treat any existing card as ambiguous rather than claim idempotency.
        if _visible_og_cards(page):
            raise RuntimeError(
                "An existing Naver OG card has an unverifiable target; "
                "no draft was saved."
            )
        original_text = _editor_text_body_text(page)
        before_images = _visible_image_count(page)
        if footer_url in original_text:
            existing_guide_count = _visible_footer_guide_image_count(
                page, footer_image_path
            )
            if existing_guide_count > 1:
                raise RuntimeError(
                    "The legacy Naver draft has duplicate guide images; no draft was saved."
                )
            legacy_images_after = _visible_images_after_footer(page, footer_url)
            if legacy_images_after > 1:
                raise RuntimeError(
                    "The legacy Naver footer has ambiguous guide images; no draft was saved."
                )
            original_text = _remove_standalone_legacy_footer(page, footer_url)
            card, metadata = _insert_footer_og_card(page, footer_url)
            _verify_footer_card(page, original_text, card, metadata, footer_url)
            if existing_guide_count:
                if (
                    _visible_footer_guide_images_after_card(card, footer_image_path)
                    != 1
                ):
                    raise RuntimeError(
                        "The legacy Naver guide image is not after the OG card; "
                        "no draft was saved."
                    )
            else:
                if not footer_image_path.is_file():
                    raise FileNotFoundError(f"Missing footer image: {footer_image_path}")
                self._upload_media(page, [footer_image_path], "footer")
                if _visible_image_count(page) != before_images + 1:
                    raise RuntimeError(
                        "Could not verify the Naver footer guide image; no draft was saved."
                    )
                if (
                    _visible_footer_guide_images_after_card(card, footer_image_path)
                    != 1
                ):
                    raise RuntimeError(
                        "The Naver footer guide image is not after its OG card; "
                        "no draft was saved."
                    )
            _verify_footer_card(
                page,
                original_text,
                card,
                metadata,
                footer_url,
                require_image=True,
                footer_image_path=footer_image_path,
            )
        else:
            card, metadata = self._append_footer(page, footer_url, footer_image_path)
        draft_button = _find_safe_draft_button(page)
        draft_button.click()
        page.wait_for_timeout(5_000)
        if len(_visible_og_cards(page)) != 1:
            raise RuntimeError(f"Naver draft {log_no} did not retain its footer OG card.")
        if _visible_image_count(page) < before_images:
            raise RuntimeError(
                f"Naver draft {log_no} did not retain the configured footer image."
            )
        _verify_footer_card(
            page,
            original_text,
            card,
            metadata,
            footer_url,
            require_image=True,
            footer_image_path=footer_image_path,
        )
        if save_artifact:
            self._save_artifact(page, log_no, "footer-saved")
        return True

    def _upload_media(self, page: Page, paths: list[Path], post_title: str) -> None:
        for path in paths:
            is_video = path.suffix.lower() in VIDEO_SUFFIXES
            if is_video:
                _upload_video(page, path, post_title)
                continue
            upload_button = _find_visible(
                page,
                IMAGE_BUTTON_SELECTORS,
                "image upload button",
            )
            with page.expect_file_chooser(timeout=10_000) as chooser_info:
                upload_button.click()
            chooser_info.value.set_files(str(path))
            page.wait_for_timeout(2_000)

    def _append_configured_footer(self, page: Page, *, body_focused: bool = False) -> None:
        if not self._config.footer_url and self._config.footer_image_path is None:
            return
        if not self._config.footer_url or self._config.footer_image_path is None:
            raise RuntimeError(
                "Configure both footer_url and footer_image_path, or leave both empty."
            )
        self._append_footer(
            page,
            self._config.footer_url,
            self._config.footer_image_path,
            body_focused=body_focused,
        )

    def _append_footer(
        self,
        page: Page,
        footer_url: str,
        footer_image_path: Path,
        *,
        body_focused: bool = False,
    ) -> tuple[Locator, tuple[str, str, str]]:
        if not footer_image_path.is_file():
            raise FileNotFoundError(f"Missing footer image: {footer_image_path}")
        original_text = _editor_text_body_text(page)
        if footer_url in original_text:
            raise RuntimeError("The Naver body already contains the footer URL.")
        if _visible_og_cards(page):
            raise RuntimeError(
                "An existing Naver OG card has an unverifiable target; "
                "no draft was saved."
            )
        existing_guide_count = _visible_footer_guide_image_count(
            page, footer_image_path
        )
        if existing_guide_count > 1:
            raise RuntimeError(
                "The Naver draft has duplicate guide images; no draft was saved."
            )
        before_images = _visible_image_count(page)
        paragraph = _find_or_create_footer_paragraph(page)
        if _editor_text_body_text(page).strip():
            if _paragraph_is_in_list(paragraph):
                _exit_list_at_end(page, paragraph)
            else:
                _place_caret_at_end(paragraph, body_focused=body_focused)
        else:
            _place_caret_at_end(paragraph, body_focused=body_focused)
            page.keyboard.insert_text(".")
            page.keyboard.press("Backspace")
        card, metadata = _insert_footer_og_card(page, footer_url)
        _verify_footer_card(page, original_text, card, metadata, footer_url)
        if existing_guide_count:
            if _visible_footer_guide_images_after_card(card, footer_image_path) != 1:
                raise RuntimeError(
                    "The existing Naver footer guide image is not after its OG card; "
                    "no draft was saved."
                )
        else:
            self._upload_media(page, [footer_image_path], "footer")
            if _visible_image_count(page) != before_images + 1:
                raise RuntimeError(
                    "Could not verify the Naver footer guide image; "
                    "no draft was saved."
                )
            if _visible_footer_guide_images_after_card(card, footer_image_path) != 1:
                raise RuntimeError(
                    "The Naver footer guide image is not after its OG card; "
                    "no draft was saved."
                )
        _verify_footer_card(
            page,
            original_text,
            card,
            metadata,
            footer_url,
            require_image=True,
            footer_image_path=footer_image_path,
        )
        return card, metadata

    def _prepare_editor(self) -> Page:
        page = self._open_editor()
        page.wait_for_timeout(3_000)
        if "nidlogin" in page.url or "nid.naver.com" in page.url:
            raise RuntimeError("Naver login expired. Run `threads-to-naver login`.")
        _dismiss_restore_popup(page)
        return page

    def _open_temp_draft_list(
        self, page: Page
    ) -> tuple[list[TempDraft], Locator]:
        count_button = _find_visible(
            page,
            ('button[aria-label^="임시저장된 글 보기"]',),
            "temporary-draft list button",
        )
        with page.expect_response(
            lambda response: urlsplit(response.url).path.endswith(
                "/TempPostList.naver"
            ),
            timeout=15_000,
        ) as response_info:
            count_button.click()
        result = response_info.value.json().get("result", {})
        drafts = [
            TempDraft(
                log_no=str(item["logNo"]),
                title=" ".join(str(item.get("title") or "").split()),
            )
            for item in result.get("tempPostList", [])
        ]
        buttons = _find_temp_draft_buttons(page, len(drafts))
        if buttons is None:
            raise RuntimeError(
                "Naver temporary-draft list did not match its visible controls."
            )
        return drafts, buttons

    def _load_temp_draft(self, page: Page, log_no: str) -> None:
        drafts, buttons = self._open_temp_draft_list(page)
        try:
            index = next(
                index for index, draft in enumerate(drafts) if draft.log_no == log_no
            )
        except StopIteration as error:
            raise RuntimeError(f"Naver temporary draft {log_no} no longer exists.") from error

        with page.expect_response(
            lambda response: _is_temp_draft_read_response(response.url, log_no),
            timeout=15_000,
        ):
            buttons.nth(index).click()
        page.wait_for_timeout(2_000)
        _find_visible(page, TITLE_SELECTORS, "loaded temporary-draft title")

    def _open_editor(self) -> Page:
        page = self._new_page()
        if self._config.naver_blog_id:
            write_url = self._config.naver_write_url.format(
                blog_id=self._config.naver_blog_id
            )
            page.goto(write_url, wait_until="domcontentloaded", timeout=60_000)
            return page

        page.goto(
            "https://www.naver.com/", wait_until="domcontentloaded", timeout=60_000
        )
        cookie_names = {
            cookie["name"] for cookie in self._context.cookies("https://naver.com")
        }
        if not ({"NID_AUT", "NID_SES"} & cookie_names):
            raise RuntimeError("Naver login expired. Run `threads-to-naver login`.")

        blog_tab = page.get_by_text("블로그", exact=True).filter(visible=True).first
        try:
            blog_tab.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as error:
            raise RuntimeError("Could not find the Naver Blog account menu.") from error
        blog_page = self._click_maybe_new_page(page, blog_tab)
        blog_page.wait_for_timeout(1_500)
        write_link = (
            blog_page.get_by_text("글쓰기", exact=True).filter(visible=True).first
        )
        try:
            write_link.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as error:
            raise RuntimeError("Could not find the Naver Blog write link.") from error
        return self._click_maybe_new_page(blog_page, write_link)

    def _click_maybe_new_page(self, source_page: Page, control: Locator) -> Page:
        try:
            with self._context.expect_page(timeout=10_000) as page_info:
                control.click()
            destination = page_info.value
            destination.wait_for_load_state("domcontentloaded")
            return destination
        except PlaywrightTimeoutError:
            source_page.wait_for_load_state("domcontentloaded")
            return source_page

    def _new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("NaverDraftWriter must be used as a context manager.")
        return (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )

    def _save_artifact(self, page: Page, post_id: str, suffix: str) -> None:
        timestamp = datetime.now(self._config.timezone).strftime("%Y%m%d-%H%M%S")
        base = self._config.artifacts_dir / f"{timestamp}-{post_id}-{suffix}"
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        metadata = {"url": page.url, "title": page.title(), "post_id": post_id}
        base.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _candidate_frames(page: Page) -> list[Frame]:
    return list(page.frames)


def _find_visible(page: Page, selectors: tuple[str, ...], description: str) -> Locator:
    for frame in _candidate_frames(page):
        for selector in selectors:
            candidates = frame.locator(selector)
            for index in range(min(candidates.count(), 10)):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    return candidate
    raise RuntimeError(
        f"Could not find the Naver {description}. "
        "The editor may have changed; no publish control was clicked."
    )


def _wait_for_visible(
    page: Page,
    selectors: tuple[str, ...],
    description: str,
    *,
    timeout_ms: int = 5_000,
) -> Locator:
    attempts = max(1, timeout_ms // 100)
    for _ in range(attempts):
        try:
            return _find_visible(page, selectors, description)
        except RuntimeError:
            page.wait_for_timeout(100)
    return _find_visible(page, selectors, description)


def _insert_verbatim(page: Page, text: str) -> None:
    lines = text.split("\n")
    inserted = ""
    for index, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
            inserted += line
            _wait_for_editor_text(page, inserted)
        if index < len(lines) - 1:
            page.keyboard.press("Shift+Enter")
            inserted += "\n"
            _wait_for_editor_text(page, inserted, line_break=True)


def _wait_for_editor_text(page: Page, expected: str, *, line_break: bool = False) -> None:
    for _ in range(50):
        actual = _editor_text_body_text(page)
        if actual == expected or (
            line_break
            and actual.startswith(expected)
            and not actual[len(expected):].strip("\n")
        ):
            return
        page.wait_for_timeout(100)
    raise RuntimeError("The Naver body differs from the source text; no draft was saved.")


def _is_safe_draft_label(label: str) -> bool:
    compact = " ".join(label.split())
    if "발행" in compact:
        return False
    return compact == "임시저장" or compact == "저장" or compact.startswith("저장 |")


def _find_safe_draft_button(page: Page) -> Locator:
    for frame in _candidate_frames(page):
        for selector in DRAFT_SELECTORS:
            candidates = frame.locator(selector)
            for index in range(min(candidates.count(), 20)):
                candidate = candidates.nth(index)
                if not candidate.is_visible():
                    continue
                label = (
                    candidate.inner_text()
                    or candidate.get_attribute("aria-label")
                    or ""
                ).strip()
                box = candidate.bounding_box()
                if _is_safe_draft_label(label) and box and box["y"] < 180:
                    return candidate
    raise RuntimeError(
        "Could not identify a safe Naver temporary-save control. "
        "No publish control was clicked."
    )


def _dismiss_restore_popup(page: Page) -> bool:
    """Discard only Naver's known autosave-restore prompt, never other dialogs."""
    for frame in _candidate_frames(page):
        popups = frame.locator('[data-group="popupLayer"]')
        for index in range(min(popups.count(), 20)):
            popup = popups.nth(index)
            if not popup.is_visible():
                continue
            label = " ".join((popup.inner_text() or "").split())
            if not (
                label.startswith("작성 중인 글이 있습니다.")
                and "이어서 작성하시겠습니까?" in label
            ):
                continue
            cancel = popup.get_by_role("button", name="취소", exact=True)
            if cancel.count() == 1 and cancel.is_visible():
                cancel.click()
                popup.wait_for(state="hidden", timeout=10_000)
                return True
    return False


def _current_draft_count(page: Page) -> int | None:
    for frame in _candidate_frames(page):
        controls = frame.locator('button[aria-label^="임시저장된 글 보기"]')
        for index in range(min(controls.count(), 10)):
            control = controls.nth(index)
            if not control.is_visible():
                continue
            label = control.get_attribute("aria-label") or ""
            match = re.search(r"(\d+)개", label)
            if match:
                return int(match.group(1))
    return None


def _find_temp_draft_buttons(page: Page, expected_count: int) -> Locator | None:
    for frame in _candidate_frames(page):
        buttons = frame.locator('button[data-click-area="tpb*s.tlist"]')
        if buttons.count() == expected_count:
            return buttons
    return None


def _is_temp_draft_read_response(url: str, log_no: str) -> bool:
    parsed = urlsplit(url)
    return parsed.path.endswith("/RabbitTempPostRead.naver") and (
        parse_qs(parsed.query).get("logNo") == [log_no]
    )


def _editor_body_text(page: Page) -> str:
    parts: list[str] = []
    for frame in _candidate_frames(page):
        components = frame.locator(".se-component-content")
        for index in range(min(components.count(), 100)):
            component = components.nth(index)
            if component.is_visible():
                parts.append(component.inner_text() or "")
    return "\n".join(parts)


def _editor_text_body_text(page: Page) -> str:
    parts: list[str] = []
    for frame in _candidate_frames(page):
        sections = frame.locator(".se-section-text")
        for index in range(sections.count()):
            section = sections.nth(index)
            if section.is_visible():
                paragraphs = section.locator(".se-text-paragraph")
                if not paragraphs.count():
                    parts.append(section.inner_text() or "")
                    continue
                lines = []
                for paragraph_index in range(paragraphs.count()):
                    paragraph = paragraphs.nth(paragraph_index)
                    only_placeholder = paragraph.evaluate(
                        """element => {
                          const placeholder = element.querySelector('.se-placeholder');
                          if (!placeholder) return false;
                          const copy = element.cloneNode(true);
                          copy.querySelector('.se-placeholder').remove();
                          return !copy.textContent && !copy.querySelector('br');
                        }"""
                    )
                    lines.append("" if only_placeholder else paragraph.inner_text() or "")
                parts.append("\n".join(lines))
    return "\n".join(parts)


def _visible_og_cards(page: Page) -> list[Locator]:
    cards: list[Locator] = []
    for frame in _candidate_frames(page):
        candidates = frame.locator(".se-component.se-oglink")
        for index in range(candidates.count()):
            card = candidates.nth(index)
            if card.is_visible():
                cards.append(card)
    return cards


def _og_visible_fields(container: Locator) -> list[str]:
    return container.evaluate(
        """root => [...root.querySelectorAll('*')]
          .filter(element => element.getClientRects().length
            && !element.closest('button')
            && ![...element.children].some(child => child.innerText?.trim()))
          .map(element => element.innerText?.trim())
          .filter(Boolean)"""
    )


def _visible_images_after_footer_card(card: Locator) -> int:
    images = card.locator("xpath=following::img").filter(visible=True)
    return sum(
        images.nth(index).evaluate(
            "image => Boolean(image.closest('.se-component-content') "
            "&& !image.closest('.se-oglink'))"
        )
        for index in range(images.count())
    )


def _visible_footer_guide_image_count(page: Page, image_path: Path) -> int:
    return sum(
        1
        for frame in _candidate_frames(page)
        for index in range(
            frame.locator(".se-component.se-image img").count()
        )
        if frame.locator(".se-component.se-image img").nth(index).is_visible()
        and image_path.name
        in (frame.locator(".se-component.se-image img").nth(index).get_attribute("src") or "")
    )


def _visible_footer_guide_images_after_card(
    card: Locator, image_path: Path
) -> int:
    images = card.locator("xpath=following::img").filter(visible=True)
    return sum(
        image_path.name in (images.nth(index).get_attribute("src") or "")
        and bool(
            images.nth(index).evaluate(
                "image => image.closest('.se-component.se-image')"
            )
        )
        for index in range(images.count())
    )


def _verify_footer_card(
    page: Page,
    original_text: str,
    card: Locator,
    metadata: tuple[str, str, str],
    url: str,
    *,
    require_image: bool = False,
    footer_image_path: Path | None = None,
) -> None:
    body = _editor_text_body_text(page)
    if body.strip("\n") != original_text.strip("\n") or url in body:
        raise RuntimeError(
            "The Naver footer changed the original body; no draft was saved."
        )
    if (
        len(_visible_og_cards(page)) != 1
        or not card.is_visible()
        or "se-l-large_image" not in (card.get_attribute("class") or "").split()
        or card.locator(".se-section-oglink .se-module-oglink").count() != 1
        or _og_visible_fields(card.locator(".se-module-oglink")) != list(metadata)
    ):
        raise RuntimeError(
            "Could not verify the Naver footer OG preview; no draft was saved."
        )
    if not card.evaluate(
        """card => [...card.ownerDocument.querySelectorAll('.se-section-text')]
          .filter(section => section.getClientRects().length && section.innerText.trim())
          .every(section => Boolean(section.compareDocumentPosition(card) & 4))"""
    ):
        raise RuntimeError(
            "The Naver footer OG card is before the body; no draft was saved."
        )
    if require_image and (
        footer_image_path is None
        or _visible_footer_guide_images_after_card(card, footer_image_path) != 1
    ):
        raise RuntimeError(
            "The Naver footer guide image is not after its OG card; no draft was saved."
        )


def _insert_footer_og_card(
    page: Page, url: str
) -> tuple[Locator, tuple[str, str, str]]:
    before_count = len(_visible_og_cards(page))
    toolbar = _wait_for_visible(
        page, ('button[data-name="oglink"]',), "OG link toolbar button"
    )
    toolbar.click()
    popup = _wait_for_visible(page, (".se-popup-oglink",), "OG link popup")
    url_input = popup.locator('input.se-popup-oglink-input[placeholder="URL을 입력하세요."]')
    if url_input.count() != 1 or not url_input.is_visible():
        raise RuntimeError("Could not find the Naver OG link URL input; no draft was saved.")
    url_input.fill(url)
    search = popup.locator('button[data-log="pog.search"]')
    if search.count() != 1 or not search.is_enabled():
        raise RuntimeError("Could not search the Naver OG link; no draft was saved.")
    search.click()
    confirm = popup.locator('button[data-log="pog.ok"]')
    metadata: tuple[str, str, str] | None = None
    for _ in range(50):
        previews = popup.locator("img").filter(visible=True)
        if (
            previews.count() == 1
            and previews.first.evaluate("image => image.complete && image.naturalWidth > 0")
            and confirm.count() == 1
            and confirm.is_enabled()
        ):
            fields = previews.first.evaluate(
                """image => {
                  const popup = image.closest('.se-popup-oglink');
                  for (let node = image.parentElement; node && node !== popup;
                       node = node.parentElement) {
                    const fields = [...node.querySelectorAll('*')]
                      .filter(element => element.getClientRects().length
                        && !element.closest('button')
                        && ![...element.children].some(child => child.innerText?.trim()))
                      .map(element => element.innerText?.trim()).filter(Boolean);
                    if (fields.length >= 3) return fields;
                  }
                  return [];
                }"""
            )
            if len(fields) == 3 and fields[2] == urlsplit(url).hostname:
                metadata = tuple(fields)
                break
        page.wait_for_timeout(100)
    else:
        raise RuntimeError(
            "Naver did not provide a complete OG preview; no draft was saved."
        )
    if metadata is None or url_input.input_value() != url:
        raise RuntimeError("The Naver OG preview URL changed; no draft was saved.")
    confirm.click()
    for _ in range(50):
        cards = _visible_og_cards(page)
        if len(cards) == before_count + 1:
            added = cards[-1]
            # The editor component has no target attribute. The retained modal
            # input proves the URL at confirm; the added card must match its preview.
            if (
                "se-l-large_image" in (added.get_attribute("class") or "").split()
                and _og_visible_fields(added.locator(".se-module-oglink"))
                == list(metadata)
            ):
                return added, metadata
        page.wait_for_timeout(100)
    raise RuntimeError("Naver did not insert the matching footer OG card; no draft was saved.")


def _remove_standalone_legacy_footer(page: Page, url: str) -> str:
    body = _editor_text_body_text(page)
    before, separator, after = body.partition(url)
    _verify_footer_after_body(page, before, url)
    if not separator or after.strip("\n"):
        raise RuntimeError("The legacy Naver footer is not standalone; no draft was saved.")
    paragraphs = [
        paragraph
        for frame in _candidate_frames(page)
        for paragraph in (
            frame.locator(".se-section-text .se-text-paragraph").nth(index)
            for index in range(frame.locator(".se-section-text .se-text-paragraph").count())
        )
        if paragraph.is_visible() and url in (paragraph.inner_text() or "")
    ]
    if len(paragraphs) != 1 or (paragraphs[0].inner_text() or "") != url:
        raise RuntimeError("The legacy Naver footer is not standalone; no draft was saved.")
    paragraph = paragraphs[0]
    links = paragraph.locator(".se-link").filter(visible=True)
    if links.count() != 1 or (links.first.inner_text() or "") != url:
        raise RuntimeError(
            "Could not identify the exact legacy footer link; no draft was saved."
        )
    link = links.first
    box = link.bounding_box()
    if not box or box["width"] < 4:
        raise RuntimeError("Could not focus the legacy footer link; no draft was saved.")
    link.click(position={"x": box["width"] - 2, "y": box["height"] / 2})
    for _ in url:
        page.keyboard.press("Backspace")
    if _editor_text_body_text(page).strip("\n") != before.strip("\n"):
        raise RuntimeError("Removing the legacy footer changed the body; no draft was saved.")
    return before


def _verify_footer_after_body(page: Page, original_text: str, url: str) -> None:
    body = _editor_text_body_text(page)
    before, separator, after = body.partition(url)
    if (
        not separator
        or body.count(url) != 1
        or before.strip("\n") != original_text.strip("\n")
        or after.strip("\n")
    ):
        raise RuntimeError(
            "The Naver footer split or changed the original body; no draft was saved."
        )


def _visible_images_after_footer(page: Page, url: str) -> int:
    paragraph = _find_paragraph_containing(page, url)
    images = paragraph.locator("xpath=following::img").filter(visible=True)
    return sum(
        images.nth(index).evaluate(
            "image => Boolean(image.closest('.se-component-content'))"
        )
        for index in range(images.count())
    )


def _find_paragraph_containing(page: Page, text: str) -> Locator:
    match: Locator | None = None
    for frame in _candidate_frames(page):
        paragraphs = frame.locator(".se-text-paragraph")
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            if paragraph.is_visible() and text in (paragraph.inner_text() or ""):
                match = paragraph
    if match is None:
        raise RuntimeError("Could not find the inserted Naver footer text.")
    return match


def _make_text_link(page: Page, url: str) -> None:
    paragraph = _find_paragraph_containing(page, url)
    paragraph.click()
    page.keyboard.press("End")
    for _ in url:
        page.keyboard.press("Shift+ArrowLeft")

    toolbar_button = _wait_for_visible(
        page,
        ('button[data-name="text-link"]',),
        "text-link toolbar button",
    )
    toolbar_button.click()
    url_input = _wait_for_visible(
        page,
        ('input[placeholder="URL을 입력하세요."]',),
        "text-link URL input",
    )
    url_input.fill(url)
    apply_button = _wait_for_visible(
        page,
        ("button.se-custom-layer-link-apply-button",),
        "text-link apply button",
    )
    apply_button.click()
    page.wait_for_timeout(500)
    if _footer_link_count(page, url) == 0:
        raise RuntimeError("Naver did not create a clickable footer link.")


def _insert_text_link_at_cursor(page: Page, url: str) -> None:
    toolbar_button = _wait_for_visible(
        page,
        ('button[data-name="text-link"]',),
        "text-link toolbar button",
    )
    toolbar_button.click()
    url_input = _wait_for_visible(
        page,
        ('input[placeholder="URL을 입력하세요."]',),
        "text-link URL input",
    )
    url_input.fill(url)
    apply_button = _wait_for_visible(
        page,
        ("button.se-custom-layer-link-apply-button",),
        "text-link apply button",
    )
    apply_button.click()
    page.wait_for_timeout(500)
    if _footer_link_count(page, url) == 0:
        raise RuntimeError("Naver did not insert a clickable footer link.")


def _footer_link_count(page: Page, url: str) -> int:
    count = 0
    for frame in _candidate_frames(page):
        links = frame.locator(".se-component-content a, .se-component-content .se-link")
        for index in range(min(links.count(), 500)):
            link = links.nth(index)
            target = link.get_attribute("href") or link.get_attribute("data-href")
            if target == url:
                count += 1
    return count


def _find_last_visible_text_paragraph(page: Page) -> Locator:
    visible: list[Locator] = []
    for frame in _candidate_frames(page):
        paragraphs = frame.locator(".se-section-text .se-text-paragraph")
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            if paragraph.is_visible():
                visible.append(paragraph)
    if not visible:
        raise RuntimeError("Could not find the end of the Naver draft body.")
    return next(
        (
            paragraph
            for paragraph in reversed(visible)
            if (paragraph.inner_text() or "").strip()
        ),
        visible[-1],
    )


def _find_or_create_footer_paragraph(page: Page) -> Locator:
    try:
        return _find_last_visible_text_paragraph(page)
    except RuntimeError:
        add_body = _find_visible(
            page,
            ("button.se-canvas-bottom-button",),
            "add-body button",
        )
        add_body.click()
        return _wait_for_visible(
            page,
            (".se-section-text .se-text-paragraph",),
            "new body text editor",
        )


def _place_caret_at_end(paragraph: Locator, *, body_focused: bool = False) -> None:
    if not _editor_body_has_focus(paragraph, allow_input_buffer=body_focused):
        paragraph.scroll_into_view_if_needed()
        position = paragraph.evaluate(
            """element => {
              const doc = element.ownerDocument;
              const range = doc.createRange();
              range.selectNodeContents(element);
              const box = element.getBoundingClientRect();
              for (const rect of [...range.getClientRects(), box]) {
                if (!rect.width || !rect.height) continue;
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const hit = doc.elementFromPoint(x, y);
                if (hit && element.contains(hit)
                    && !hit.closest('.se-selection, .se-caret')) {
                  return {x: x - box.left - element.clientLeft,
                          y: y - box.top - element.clientTop};
                }
              }
              return null;
            }"""
        )
        if position is None:
            raise RuntimeError("Could not focus the Naver body without an overlay.")
        paragraph.click(position=position)
        body_focused = False
    if not _editor_body_has_focus(paragraph, allow_input_buffer=body_focused):
        raise RuntimeError("Could not confirm Naver body focus; no draft was saved.")
    paragraph.page.keyboard.press("ControlOrMeta+End")


def _editor_body_has_focus(paragraph: Locator, *, allow_input_buffer: bool) -> bool:
    return paragraph.evaluate(
        """(element, allowInputBuffer) => {
          const doc = element.ownerDocument;
          if (!doc.hasFocus()) return false;
          const active = doc.activeElement;
          const anchor = doc.getSelection()?.anchorNode;
          const anchorElement = anchor?.nodeType === 1 ? anchor : anchor?.parentElement;
          const section = element.closest('.se-section-text');
          if (!section) return false;
          const selectionInParagraph = Boolean(anchorElement
            && element.contains(anchorElement)
            && anchorElement.closest('.se-section-text') === section);
          if (active?.tagName === 'IFRAME') {
            if (![active.id, active.name].some(value => value?.startsWith('input_buffer'))) {
              return false;
            }
            const bufferDoc = active.contentDocument;
            const bufferBody = bufferDoc?.body;
            // The buffer BODY has no id and cannot contain nodes from the parent document.
            return Boolean(bufferBody?.isContentEditable
              && bufferDoc.activeElement === bufferBody
              && (allowInputBuffer || selectionInParagraph));
          }
          return Boolean(active?.isContentEditable && active.contains(element)
            && selectionInParagraph);
        }""",
        allow_input_buffer,
    )


def _paragraph_is_in_list(paragraph: Locator) -> bool:
    return paragraph.locator("xpath=ancestor::li").count() > 0


def _insert_text_link_after_list(page: Page, paragraph: Locator, url: str) -> None:
    _exit_list_at_end(page, paragraph)
    _insert_text_link_at_cursor(page, url)


def _exit_list_at_end(page: Page, paragraph: Locator) -> None:
    original_text = paragraph.inner_text() or ""
    section = paragraph.locator("xpath=ancestor::div[contains(@class,'se-section-text')]")
    _place_caret_at_end(paragraph)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")

    tail: Locator | None = None
    for _ in range(50):
        candidate = section.locator(".se-text-paragraph").last
        if (
            candidate.count() == 1
            and candidate.is_visible()
            and not _paragraph_is_in_list(candidate)
        ):
            tail = candidate
            break
        page.wait_for_timeout(100)
    if tail is None:
        raise RuntimeError("Could not exit the Naver list before appending the footer.")
    if (paragraph.inner_text() or "") != original_text:
        raise RuntimeError(
            "The Naver list item changed while positioning the footer; no draft was saved."
        )

    tail.click()
    page.keyboard.insert_text(".")
    page.keyboard.press("Backspace")


def _visible_image_count(page: Page) -> int:
    count = 0
    for frame in _candidate_frames(page):
        images = frame.locator(".se-component-content img")
        count += sum(
            images.nth(index).is_visible()
            and not images.nth(index).evaluate(
                "image => Boolean(image.closest('.se-oglink'))"
            )
            for index in range(images.count())
        )
    return count


def _verify_cover(
    page: Page, image_path: Path, *, select_representative: bool = False
) -> None:
    for frame in _candidate_frames(page):
        images = frame.locator(".se-component.se-image")
        if not images.count():
            continue
        cover = images.first
        picture = cover.locator("img.se-image-resource").first
        if not picture.count() or (
            picture.get_attribute("alt") != image_path.name
            and image_path.name not in (picture.get_attribute("src") or "")
        ):
            break
        if not picture.evaluate("image => image.complete && image.naturalWidth > 0"):
            break
        if not cover.evaluate(
            """element => ![...document.querySelectorAll('.se-component.se-text')]
              .some(text => text.compareDocumentPosition(element) &
                Node.DOCUMENT_POSITION_FOLLOWING && text.innerText.trim())"""
        ):
            break
        representative = cover.locator(".se-set-rep-image-button").first
        if not representative.count():
            break
        if select_representative and "se-is-selected" not in (
            representative.get_attribute("class") or ""
        ).split():
            representative.click()
        if "se-is-selected" in (representative.get_attribute("class") or "").split():
            return
        break
    raise RuntimeError(
        "Could not verify the first Naver image as the representative cover; "
        "the source item remains pending."
    )


def _upload_video(page: Page, path: Path, title: str) -> None:
    toolbar_button = _find_visible(page, VIDEO_BUTTON_SELECTORS, "video upload button")
    toolbar_button.click()
    popup = _find_visible(page, (".se-popup-video-upload",), "video upload dialog")
    local_upload = popup.locator(".nvu_local").first
    try:
        local_upload.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Could not find Naver's local video upload control."
        ) from error

    with page.expect_file_chooser(timeout=10_000) as chooser_info:
        local_upload.click()
    chooser_info.value.set_files(str(path))

    title_input = popup.get_by_placeholder("제목을 입력하세요. (최대 40자, 필수)")
    title_input.wait_for(state="visible", timeout=10_000)
    title_input.fill(title[:40])
    popup.get_by_text("업로드 완료", exact=True).wait_for(
        state="visible", timeout=120_000
    )
    done = popup.get_by_role("button", name="완료", exact=True)
    done.click()
    popup.wait_for(state="hidden", timeout=30_000)
