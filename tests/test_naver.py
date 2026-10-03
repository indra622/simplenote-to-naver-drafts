from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from threads_to_naver import naver
from threads_to_naver.models import ThreadPost
from threads_to_naver.naver import (
    NaverDraftWriter,
    _current_draft_count,
    _dismiss_restore_popup,
    _editor_text_body_text,
    _find_last_visible_text_paragraph,
    _find_or_create_footer_paragraph,
    _insert_text_link_at_cursor,
    _insert_verbatim,
    _is_safe_draft_label,
    _make_text_link,
    _paragraph_is_in_list,
    _place_caret_at_end,
    _upload_video,
)


def _og_controls(*, preview=True, input_override=None, card_title="Product title"):
    changed_input = (
        "" if input_override is None else
        f'document.querySelector(".se-popup-oglink-input").value={input_override!r};'
    )
    return (
        '<button data-name="oglink" onclick="document.querySelector(\'.se-popup-oglink\').style.display=\'block\'">'
        'OG</button>'
        '<div class="se-popup-oglink" style="display:none">'
        '<input class="se-popup-oglink-input" placeholder="URL을 입력하세요.">'
        '<button data-log="pog.search" onclick="window.searchOg()">검색</button>'
        '<div class="og-preview" style="display:none">'
        '<img src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'2\' height=\'2\'%3E%3C/svg%3E">'
        '<div class="se-oglink-info"><div>Product title</div>'
        '<div>Product summary</div><div>naver.me</div></div></div>'
        '<button data-log="pog.ok" disabled onclick="window.insertOg()">확인</button>'
        '</div>'
        '<script>'
        'window.searchOg = () => {'
        + ("document.querySelector('.og-preview').style.display='block';"
           "document.querySelector('[data-log=\"pog.ok\"]').disabled=false;"
           + changed_input if preview else "")
        + '};'
        'window.insertOg = () => {'
        'const card=document.createElement("div");'
        'card.className="se-component se-oglink se-l-large_image";'
        'const section=document.createElement("div");'
        'section.className="se-section se-section-oglink se-l-large_image";'
        'section.innerHTML=`<div class="se-module se-module-oglink">'
        '<div class="se-oglink-thumbnail"><img src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'2\' height=\'2\'%3E%3C/svg%3E"></div>'
        '<div class="se-oglink-info"><div>'
        + card_title
        + '</div><div>Product summary</div><div>naver.me</div></div></div>`;'
        'card.append(section);'
        'document.querySelector(".se-section-text").insertAdjacentElement("afterend",card);'
        'document.querySelector(".se-popup-oglink").style.display="none";'
        '};</script>'
    )


@pytest.fixture
def smarteditor_end_key():
    def install(frame):
        # Naver handles this shortcut; native macOS contenteditable does not.
        frame.evaluate(
            """() => document.addEventListener('keydown', event => {
              if (event.key !== 'End' || !(event.ctrlKey || event.metaKey)
                  || !event.target.isContentEditable) return;
              event.preventDefault();
              let editor = event.target;
              while (editor.parentElement?.isContentEditable) editor = editor.parentElement;
              const selection = document.getSelection();
              selection.selectAllChildren(editor);
              selection.collapseToEnd();
            })"""
        )
    return install


@pytest.mark.parametrize("focused", [False, True])
def test_caret_reaches_document_end_past_overlay_in_nested_frame(
    focused, smarteditor_end_key
) -> None:
    source = "가나다라마바사 아자차카타파하 " * 20 + "마지막🙂"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<iframe id="outer"></iframe>')
        outer = page.frames[1]
        outer.set_content('<iframe id="inner"></iframe>')
        inner = page.frames[2]
        inner.set_content(
            '<input id="outside">'
            '<div class="se-section-text" contenteditable="true" style="width:180px">'
            '<p class="se-text-paragraph"><span class="__se-node">'
            f'{source}</span></p><p id="tail">document ending</p></div>'
        )
        smarteditor_end_key(inner)
        page.set_default_timeout(1_000)
        paragraph = inner.locator("p").first
        if focused:
            paragraph.click()
            page.keyboard.press("ControlOrMeta+End")
        else:
            inner.locator("#outside").focus()
        paragraph.evaluate(
            """element => {
              const node = element.querySelector('span').firstChild;
              const range = document.createRange();
              range.setStart(node, node.length - 2);
              range.setEnd(node, node.length);
              const rect = range.getBoundingClientRect();
              const overlay = document.createElement('div');
              overlay.className = 'se-selection';
              overlay.innerHTML = '<svg class="se-caret" width="100%" height="100%">'
                + '<rect width="100%" height="100%"/></svg>';
              Object.assign(overlay.style, {
                position: 'absolute', left: `${rect.left + scrollX}px`,
                top: `${rect.top + scrollY}px`, width: `${rect.width}px`,
                height: `${rect.height}px`, zIndex: '1000'
              });
              document.body.append(overlay);
            }"""
        )

        _place_caret_at_end(paragraph)
        page.keyboard.insert_text("FOOTER")

        assert paragraph.inner_text() == source
        assert inner.locator("#tail").inner_text() == "document endingFOOTER"
        assert inner.locator("#outside").input_value() == ""
        browser.close()


@pytest.mark.parametrize("body_focused", [False, True])
def test_caret_reuses_nested_input_buffer_only_after_body_edit(
    smarteditor_end_key, body_focused,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(1_000)
        page.set_content(
            '<div class="se-section-text"><p class="se-text-paragraph">'
            '<span class="__se-node">body ending</span></p></div>'
            '<iframe id="input_buffer1790555631048"></iframe>'
            '<div class="se-selection" style="position:fixed;inset:0;z-index:100">'
            '<svg class="se-caret" width="100%" height="100%">'
            '<rect width="100%" height="100%"/></svg></div>'
        )
        buffer_frame = page.frames[1]
        buffer_frame.set_content('<body contenteditable="true"></body>')
        smarteditor_end_key(buffer_frame)
        buffer_frame.locator("body").focus()
        page.keyboard.insert_text("source just inserted")

        if body_focused:
            _place_caret_at_end(page.locator("p"), body_focused=True)
            page.keyboard.insert_text("FOOTER")
        else:
            with pytest.raises(RuntimeError, match="focus"):
                _place_caret_at_end(page.locator("p"))

        expected = "source just inserted" + ("FOOTER" if body_focused else "")
        assert buffer_frame.locator("body").inner_text() == expected
        assert page.locator("p").inner_text() == "body ending"
        browser.close()


@pytest.fixture
def nested_input_buffer(smarteditor_end_key):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(1_000)
        page.set_content('<input id="outside"><iframe id="PostWriteForm"></iframe>')
        editor = page.frames[1]
        editor.set_content(
            '<input id="outside">'
            '<div class="se-documentTitle"><span id="title">title</span></div>'
            '<div class="se-section-text"><p id="target" class="se-text-paragraph">'
            '<span class="se-node">body ending</span></p>'
            '<p id="sibling">another paragraph</p></div>'
            '<div class="se-section-text"><p id="other">another section</p></div>'
            '<iframe id="input_buffer1790555631048"></iframe>'
        )
        buffer_frame = page.frames[2]
        buffer_frame.set_content('<body contenteditable="true">source just inserted</body>')
        smarteditor_end_key(buffer_frame)
        page.evaluate("window.endPresses = 0")
        for frame in page.frames:
            frame.evaluate(
                """() => document.addEventListener('keydown', event => {
                  if (event.key === 'End') top.endPresses++;
                })"""
            )
        editor.evaluate(
            """() => {
              window.clickAnchor = '#target .se-node';
              document.querySelector('#target').addEventListener('click', () => {
                const selection = document.getSelection();
                selection.removeAllRanges();
                if (window.clickAnchor) {
                  selection.selectAllChildren(document.querySelector(window.clickAnchor));
                  selection.collapseToStart();
                }
                document.querySelector('iframe').contentDocument.body.focus();
              });
            }"""
        )
        yield page, editor, buffer_frame
        browser.close()


@pytest.mark.parametrize("frame_attribute", ["id", "name"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("overlaid", [False, True])
def test_caret_confirms_real_input_buffer_focus_before_end(
    nested_input_buffer, frame_attribute, focused, overlaid,
):
    page, editor, buffer_frame = nested_input_buffer
    editor.locator("iframe").evaluate(
        """(frame, attribute) => {
          frame.removeAttribute('id');
          frame.setAttribute(attribute, 'input_buffer1790555631048');
        }""",
        frame_attribute,
    )
    paragraph = editor.locator("#target")
    paragraph.click()
    assert buffer_frame.locator("body").get_attribute("id") is None
    if not focused:
        editor.locator("#outside").focus()
    assert naver._editor_body_has_focus(paragraph, allow_input_buffer=False) is focused
    if overlaid:
        editor.evaluate(
            """() => document.body.insertAdjacentHTML('beforeend',
              '<div class="se-selection" style="position:fixed;inset:0;z-index:100">'
              + '<svg class="se-caret" width="100%" height="100%"></svg></div>')"""
        )

    if overlaid and not focused:
        with pytest.raises(RuntimeError, match="focus"):
            _place_caret_at_end(paragraph, body_focused=True)
        assert page.evaluate("window.endPresses") == 0
        assert buffer_frame.locator("body").inner_text() == "source just inserted"
    else:
        _place_caret_at_end(paragraph)
        assert page.evaluate("window.endPresses") == 1
        page.keyboard.insert_text("FOOTER")
        assert buffer_frame.locator("body").inner_text() == "source just insertedFOOTER"
    assert paragraph.inner_text() == "body ending"
    assert editor.locator("#outside").input_value() == ""


@pytest.mark.parametrize("anchor", [None, "#title", "#sibling", "#other"])
@pytest.mark.parametrize("body_focused", [False, True])
def test_caret_rejects_input_buffer_click_without_target_selection(
    nested_input_buffer, anchor, body_focused,
):
    page, editor, buffer_frame = nested_input_buffer
    editor.evaluate("(anchor) => window.clickAnchor = anchor", anchor)
    editor.locator("#outside").focus()

    with pytest.raises(RuntimeError, match="focus"):
        _place_caret_at_end(editor.locator("#target"), body_focused=body_focused)

    assert page.evaluate("window.endPresses") == 0
    assert buffer_frame.locator("body").inner_text() == "source just inserted"


@pytest.mark.parametrize("invalid", ["frame", "noneditable", "inner_div", "outer_focus"])
def test_input_buffer_focus_fails_closed(nested_input_buffer, invalid):
    page, editor, buffer_frame = nested_input_buffer
    paragraph = editor.locator("#target")
    paragraph.click()
    if invalid == "frame":
        editor.locator("iframe").evaluate("frame => frame.id = 'unrelated_frame'")
        buffer_frame.locator("body").evaluate("body => body.id = 'input_buffer'")
    elif invalid == "noneditable":
        buffer_frame.locator("body").evaluate("body => body.contentEditable = 'false'")
    elif invalid == "inner_div":
        buffer_frame.set_content('<body><div contenteditable="true">other editor</div></body>')
        buffer_frame.locator("div").focus()
    else:
        page.locator("#outside").focus()

    for allow_input_buffer in (False, True):
        assert not naver._editor_body_has_focus(
            paragraph, allow_input_buffer=allow_input_buffer,
        )
    assert page.evaluate("window.endPresses") == 0


@pytest.mark.parametrize("body_focused", [False, True])
def test_caret_rejects_paragraph_click_that_does_not_focus_editor(body_focused) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<input id="outside" value="untouched">'
            '<div class="se-section-text"><p class="se-text-paragraph">'
            '<span class="__se-node">body ending</span></p></div>'
        )
        page.locator("#outside").focus()

        with pytest.raises(RuntimeError, match="focus"):
            _place_caret_at_end(page.locator("p"), body_focused=body_focused)

        assert page.locator("#outside").input_value() == "untouched"
        browser.close()


@pytest.mark.parametrize("source", ["\n\nfirst\n\nlast\n", "first\n\nlast"])
def test_footer_validation_tolerates_only_boundary_newlines(source, monkeypatch):
    url = "https://naver.me/example"
    monkeypatch.setattr(
        naver, "_editor_text_body_text", lambda page: f"first\n\nlast\n\n{url}\n"
    )
    naver._verify_footer_after_body(None, source, url)


@pytest.mark.parametrize(
    "body",
    ["first\nlast", " first\n\nlast", "first\n\nlast ", "first\r\n\nlast"],
)
def test_footer_validation_rejects_nonboundary_changes(body, monkeypatch):
    url = "https://naver.me/example"
    monkeypatch.setattr(naver, "_editor_text_body_text", lambda page: f"{body}\n{url}")
    with pytest.raises(RuntimeError, match="body"):
        naver._verify_footer_after_body(None, "\n\nfirst\n\nlast\n", url)


@pytest.mark.parametrize(
    "failure", ["split", "changed", "image_before_url", "missing_image", "image_splits_body"]
)
def test_append_footer_rejects_body_damage_before_save(
    config, tmp_path, monkeypatch, failure
) -> None:
    image = tmp_path / "footer.png"
    image.write_bytes(b"test")
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content"><div class="se-section-text">'
            '<p class="se-text-paragraph"><span class="__se-node">'
            'original body ending</span></p></div></div>'
            '<button onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(naver, "_place_caret_at_end", lambda paragraph, **kwargs: None)

        def insert_card(page, url):
            before = "original" if failure in {"split", "changed"} else "original body ending"
            after = " body ending" if failure == "split" else ""
            page.locator("p").evaluate(
                "(p, values) => { p.innerHTML = values; }",
                f"{before}{after}",
            )
            page.locator(".se-section-text").evaluate(
                "(section, url) => section.insertAdjacentHTML('afterend', "
                "`<div class='se-component se-oglink se-l-large_image'>"
                "<div class='se-section-oglink'><div class='se-module-oglink'>"
                "<div>Product title</div><div>Product summary</div>"
                "<div>naver.me</div></div></div></div>`)",
                url,
            )
            return page.locator(".se-oglink"), (
                "Product title", "Product summary", "naver.me"
            )

        def upload(page, paths, title):
            if failure == "missing_image":
                return
            if failure == "image_splits_body":
                page.locator("p").evaluate(
                    "(p, url) => p.innerHTML = 'original<br>' + url + ' body ending'",
                    url,
                )
            page.locator(".se-component-content").evaluate(
                "element => element.insertAdjacentHTML('afterbegin', "
                "'<img style=\"width:20px;height:20px\" src=\"data:,footer.png\">')"
            )

        monkeypatch.setattr(naver, "_insert_footer_og_card", insert_card)
        monkeypatch.setattr(writer, "_upload_media", upload)

        with pytest.raises(RuntimeError, match="footer|body"):
            writer.append_footer_to_temp_draft("test", url, image)
        assert page.evaluate("Boolean(window.saved)") is False
        browser.close()


@pytest.mark.parametrize("linked", [False, True])
def test_existing_footer_in_middle_is_not_saved_or_accepted(config, monkeypatch, linked):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        link = f'<a href="{url}">{url}</a>' if linked else url
        page.set_content(
            '<div class="se-component-content"><div class="se-section-text">'
            f'<p class="se-text-paragraph">original<br>{link}<br>body ending</p>'
            '</div></div><button onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)

        with pytest.raises(RuntimeError, match="footer"):
            writer.append_footer_to_temp_draft("test", url, Path("unused.png"))
        assert page.evaluate("Boolean(window.saved)") is False
        browser.close()


def test_create_draft_rejects_source_mismatch_before_save(config, monkeypatch):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle" contenteditable="true"></div>'
            '<div class="se-section-text" contenteditable="true">'
            '<p class="se-text-paragraph"><br></p></div>'
            '<button onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(naver, "_insert_verbatim", lambda page, text: None)
        post = ThreadPost("test", "original", datetime.now(UTC), "", "TEXT_POST")

        with pytest.raises(RuntimeError, match="source text"):
            writer.create_draft(post, [])
        assert page.evaluate("Boolean(window.saved)") is False
        browser.close()


@pytest.mark.parametrize(
    "inserted, accepted",
    [("first\n\nlast", True), ("first\nlast", False), (" first\n\nlast ", False)],
)
def test_create_draft_normalizes_only_boundary_newlines(
    config, monkeypatch, inserted, accepted
):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle" contenteditable="true"></div>'
            '<div class="se-section-text" contenteditable="true">'
            '<p class="se-text-paragraph"><br></p></div>'
            '<button onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
        monkeypatch.setattr(naver, "_select_draft_category", lambda page: None)
        monkeypatch.setattr(
            naver, "_insert_verbatim", lambda page, text: _insert_verbatim(page, inserted)
        )
        post = ThreadPost("test", "\n\nfirst\n\nlast\n", datetime.now(UTC), "", "TEXT_POST")

        if accepted:
            writer.create_draft(post, [])
        else:
            with pytest.raises(RuntimeError, match="source text"):
                writer.create_draft(post, [])
        assert page.evaluate("Boolean(window.saved)") is accepted
        browser.close()


def test_create_draft_accepts_naver_paragraph_boundaries(config, monkeypatch):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle" contenteditable="true"></div>'
            '<div class="se-section-text" contenteditable="true">'
            '<p class="se-text-paragraph"><span class="se-placeholder">placeholder</span></p>'
            '</div>'
            '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
        monkeypatch.setattr(naver, "_select_draft_category", lambda page: None)
        monkeypatch.setattr(
            naver,
            "_insert_verbatim",
            lambda page, text: page.locator(".se-section-text").evaluate(
                "section => section.innerHTML = "
                "'<p class=\"se-text-paragraph\">first</p>' + "
                "'<p class=\"se-text-paragraph\">second</p>' + "
                "'<p class=\"se-text-paragraph\"></p>' + "
                "'<p class=\"se-text-paragraph\">last</p>'"
            ),
        )
        post = ThreadPost(
            "test", "first\nsecond\n\nlast", datetime.now(UTC), "", "TEXT_POST"
        )

        writer.create_draft(post, [])

        assert page.evaluate("Boolean(window.saved)") is True
        assert _editor_text_body_text(page) == post.text
        browser.close()


def _category_editor_html(
    *, target_present=True, update_label=True, check_radio=True
):
    click_actions = []
    if update_label:
        click_actions.append("document.querySelector('#selected').textContent=this.innerText")
        click_actions.append("document.querySelector('#menu').hidden=true")
    if not check_radio:
        click_actions.append("event.preventDefault()")
    on_click = f' onclick="{";".join(click_actions)}"' if click_actions else ""
    target = (
        '<li><input id="target" type="radio" name="category">'
        f'<label for="target" role="button"{on_click}>'
        '직접 쓰는 AI교양</label></li>'
        if target_present else ""
    )
    return (
        '<div class="se-documentTitle" contenteditable="true"></div>'
        '<div class="se-section-text" contenteditable="true">'
        '<p class="se-text-paragraph"><br></p></div>'
        '<button style="position:fixed;top:0" '
        'onclick="window.saved=true;window.panelOpenAtSave='
        '!document.querySelector(\'#panel\').hidden">저장</button>'
        '<button data-click-area="tpb.publish" '
        'onclick="document.querySelector(\'#panel\').hidden=false">발행</button>'
        '<div id="panel" hidden><button aria-label="카테고리 목록 버튼" '
        'onclick="const menu=document.querySelector(\'#menu\');menu.hidden=!menu.hidden">'
        '<span id="selected">일기</span></button>'
        '<div id="menu" role="menu" hidden>'
        '<li><input id="default" type="radio" name="category" checked>'
        '<label for="default" role="button">일기</label></li>'
        '<li><input id="other" type="radio" name="category">'
        '<label for="other" role="button">AI</label></li>'
        f'{target}</div></div>'
    )


@pytest.mark.parametrize(
    "target_present, update_label, check_radio, should_save",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_new_draft_requires_confirmed_target_category(
    config, monkeypatch, target_present, update_label, check_radio, should_save
):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            _category_editor_html(
                target_present=target_present,
                update_label=update_label,
                check_radio=check_radio,
            )
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
        post = ThreadPost("test", "body", datetime.now(UTC), "", "TEXT_POST")

        if should_save:
            writer.create_draft(post, [])
        else:
            with pytest.raises(RuntimeError, match="category"):
                writer.create_draft(post, [])

        assert page.evaluate("Boolean(window.saved)") is should_save
        if should_save:
            assert page.evaluate("window.panelOpenAtSave") is True
        if target_present:
            assert page.locator("#target").is_checked() is check_radio
        browser.close()


@pytest.mark.parametrize("delayed_step", ["panel", "menu", "selection", "radio"])
def test_category_waits_for_populated_editor_transitions(delayed_step):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_category_editor_html())
        page.locator(".se-documentTitle").fill("Unsaved synthetic title")
        page.locator(".se-section-text").fill("Synthetic body\n" * 25)
        page.evaluate(
            """step => {
              const panel = document.querySelector('#panel');
              const menu = document.querySelector('#menu');
              const toggle = document.querySelector('[aria-label="카테고리 목록 버튼"]');
              if (step === 'panel') {
                document.querySelector('[data-click-area="tpb.publish"]').onclick =
                  () => setTimeout(() => panel.hidden = false, 150);
              } else if (step === 'menu') {
                toggle.onclick = () => setTimeout(() => menu.hidden = !menu.hidden, 150);
              } else {
                document.querySelector('label[for="target"]').onclick = event => {
                  event.preventDefault();
                  const select = () => {
                    document.querySelector('#selected').textContent = '직접 쓰는 AI교양';
                    menu.hidden = true;
                  };
                  const check = () => document.querySelector('#target').checked = true;
                  if (step === 'selection') setTimeout(() => { select(); check(); }, 150);
                  else { select(); setTimeout(check, 250); }
                };
              }
            }""",
            delayed_step,
        )

        naver._select_draft_category(page)

        assert page.locator("#target").is_checked()
        assert page.locator("#selected").inner_text() == naver.NEW_DRAFT_CATEGORY
        assert not page.evaluate("Boolean(window.saved)")
        browser.close()


@pytest.mark.parametrize("problem", ["duplicate", "non-radio", "unassociated"])
def test_ambiguous_category_controls_never_save(config, monkeypatch, problem):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_category_editor_html())
        page.evaluate(
            """problem => {
              const label = document.querySelector('label[for="target"]');
              if (problem === 'duplicate') label.after(label.cloneNode(true));
              else if (problem === 'non-radio') document.querySelector('#target').type = 'checkbox';
              else label.removeAttribute('for');
            }""",
            problem,
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        post = ThreadPost("test", "body", datetime.now(UTC), "", "TEXT_POST")

        with pytest.raises(RuntimeError, match="category.*at (target option|selected radio)"):
            writer.create_draft(post, [])

        assert not page.evaluate("Boolean(window.saved)")
        browser.close()


def test_insert_verbatim_waits_for_editor_updates():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-section-text" contenteditable="true">'
            '<p class="se-text-paragraph"><span class="se-placeholder">placeholder</span></p>'
            '</div>'
            '<script>'
            'const section = document.querySelector(".se-section-text");'
            'let pending = null;'
            'section.addEventListener("beforeinput", event => {'
            '  if (event.inputType !== "insertText") return;'
            '  event.preventDefault();'
            '  const text = event.data;'
            '  const current = section.lastElementChild;'
            '  pending = setTimeout(() => {'
            '    current.textContent = text; pending = null;'
            '  }, 80);'
            '});'
            'section.addEventListener("keydown", event => {'
            '  if (event.key !== "Enter" || !event.shiftKey) return;'
            '  event.preventDefault();'
            '  if (pending) clearTimeout(pending);'
            '  pending = null;'
            '  const paragraph = document.createElement("p");'
            '  paragraph.className = "se-text-paragraph";'
            '  section.append(paragraph);'
            '});'
            '</script>'
        )
        page.locator(".se-section-text").click()

        _insert_verbatim(page, "first\nsecond")

        assert _editor_text_body_text(page) == "first\nsecond"
        browser.close()


def test_append_footer_preserves_wrapped_body(
    config, tmp_path, monkeypatch, smarteditor_end_key
):
    source = "가나다라마바사 아자차카타파하 " * 20 + "마지막🙂"
    url = "https://naver.me/example"
    image = tmp_path / "footer.png"
    image.write_bytes(b"test")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true" style="width:180px">'
            '<div class="se-section-text"><p class="se-text-paragraph">'
            f'<span class="__se-node">{source}</span></p></div></div>'
            + _og_controls()
        )
        smarteditor_end_key(page)
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)

        def upload(page, paths, title):
            page.locator(".se-oglink").evaluate(
                "element => element.insertAdjacentHTML('afterend', "
                "'<div class=\"se-component se-image\"><div class=\"se-component-content\">"
                "<img style=\"width:20px;height:20px\" src=\"data:,footer.png\">"
                "</div></div>')"
            )

        monkeypatch.setattr(writer, "_upload_media", upload)
        writer._append_footer(page, url, image)

        assert _editor_text_body_text(page).rstrip("\n") == source
        assert naver._visible_images_after_footer_card(page.locator(".se-oglink")) == 1
        browser.close()


def test_footer_verification_reads_body_beyond_one_hundred_sections():
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-section-text">original</div>'
            + '<div class="se-section-text">line</div>' * 99
            + f'<div class="se-section-text">{url}</div>'
            + '<div class="se-section-text">body ending</div>'
        )
        with pytest.raises(RuntimeError, match="footer"):
            naver._verify_footer_after_body(page, "original" + "\nline" * 99, url)
        browser.close()


@pytest.mark.parametrize("preview,wrong_input", [(False, False), (True, True)])
def test_og_preview_requires_image_and_exact_input(
    config, tmp_path, monkeypatch, preview, wrong_input
):
    url = "https://naver.me/example"
    image = tmp_path / "footer.png"
    image.write_bytes(b"test")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            '<div class="se-section-text"><p class="se-text-paragraph">body</p></div></div>'
            + _og_controls(
                preview=preview,
                input_override="https://wrong.example" if wrong_input else None,
            )
            + '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
        with pytest.raises(RuntimeError, match="preview|URL"):
            writer.append_footer_to_temp_draft("test", url, image)
        assert page.evaluate("Boolean(window.saved)") is False
        assert _editor_text_body_text(page) == "body"
        browser.close()


@pytest.mark.parametrize("failure", ["missing_component", "wrong_metadata"])
def test_og_insertion_must_add_matching_editor_component(
    config, tmp_path, monkeypatch, failure
):
    url = "https://naver.me/example"
    image = tmp_path / "guide.png"
    image.write_bytes(b"test")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            '<div class="se-section-text"><p class="se-text-paragraph">body</p></div></div>'
            + _og_controls(card_title="Wrong title" if failure == "wrong_metadata" else "Product title")
            + '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        if failure == "missing_component":
            page.evaluate("window.insertOg = () => {}")
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)

        with pytest.raises(RuntimeError, match="matching footer OG card"):
            writer.append_footer_to_temp_draft("test", url, image)
        assert page.evaluate("Boolean(window.saved)") is False
        assert _editor_text_body_text(page) == "body"
        assert page.locator(".se-component.se-oglink").count() == (
            0 if failure == "missing_component" else 1
        )
        browser.close()


def test_existing_editor_card_cannot_prove_footer_target(config, monkeypatch):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-section-text"><p class="se-text-paragraph">body</p></div>'
            '<div class="se-component se-oglink se-l-large_image">'
            '<div class="se-section se-section-oglink se-l-large_image">'
            '<div class="se-module se-module-oglink">'
            '<div class="se-oglink-thumbnail"><img src="data:,thumbnail"></div>'
            '<div class="se-oglink-info"><div>Product title</div>'
            '<div>Product summary</div><div>naver.me</div></div></div></div></div>'
            '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)

        with pytest.raises(RuntimeError, match="unverifiable target"):
            writer.append_footer_to_temp_draft("test", url, Path("unused.png"))
        assert page.evaluate("Boolean(window.saved)") is False
        assert _editor_text_body_text(page) == "body"
        assert page.locator(".se-oglink a, .se-oglink [href]").count() == 0
        browser.close()


def test_standalone_legacy_footer_becomes_card_before_existing_image(
    config, tmp_path, monkeypatch
):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-text"><div class="se-component-content" '
            'contenteditable="true"><div class="se-section-text">'
            '<p class="se-text-paragraph">body</p>'
            f'<p class="se-text-paragraph"><span class="se-link">{url}</span></p>'
            '</div></div></div>'
            '<div class="se-component se-image"><div class="se-component-content">'
            '<img style="width:20px;height:20px" src="data:,missing.png"></div></div>'
            + _og_controls()
            + '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)

        assert writer.append_footer_to_temp_draft(
            "test", url, tmp_path / "missing.png"
        ) is True
        assert page.evaluate("Boolean(window.saved)") is True
        assert _editor_text_body_text(page).strip("\n") == "body"
        assert len(naver._visible_og_cards(page)) == 1
        assert naver._visible_images_after_footer_card(page.locator(".se-oglink")) == 1
        browser.close()


def test_legacy_guide_before_url_is_reused_after_card(config, tmp_path, monkeypatch):
    url = "https://naver.me/example"
    guide = tmp_path / "guide.png"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-image"><div class="se-component-content">'
            '<img style="width:20px;height:20px" src="data:,guide.png"></div></div>'
            '<div class="se-component se-text"><div class="se-component-content" '
            'contenteditable="true"><div class="se-section-text">'
            '<p class="se-text-paragraph">body</p>'
            f'<p class="se-text-paragraph"><span class="se-link">{url}</span></p>'
            '</div></div></div>'
            + _og_controls()
            + '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        page.evaluate(
            """() => {
              const insert = window.insertOg;
              window.insertOg = () => {
                insert();
                const text = document.querySelector('.se-component.se-text');
                const guide = document.querySelector('.se-component.se-image');
                text.insertAdjacentElement('afterend', guide);
              };
            }"""
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(writer, "_save_artifact", lambda *args: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)

        assert writer.append_footer_to_temp_draft("test", url, guide) is True
        assert page.evaluate("Boolean(window.saved)") is True
        assert _editor_text_body_text(page).strip("\n") == "body"
        assert page.locator(".se-component.se-image img").count() == 1
        assert naver._visible_footer_guide_images_after_card(
            page.locator(".se-oglink"), guide
        ) == 1
        browser.close()


def test_inline_legacy_footer_is_rejected_without_saving(config, monkeypatch):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            '<div class="se-section-text"><p class="se-text-paragraph">'
            f'body {url}</p></div></div>'
            '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        with pytest.raises(RuntimeError, match="standalone"):
            writer.append_footer_to_temp_draft("test", url, Path("unused.png"))
        assert page.evaluate("Boolean(window.saved)") is False
        browser.close()


def test_unlinked_standalone_legacy_footer_fails_before_save(config, monkeypatch):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            '<div class="se-section-text"><p class="se-text-paragraph">body</p>'
            f'<p class="se-text-paragraph">{url}</p></div></div>'
            '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        original_text = _editor_text_body_text(page)

        with pytest.raises(RuntimeError, match="exact legacy footer link"):
            writer.append_footer_to_temp_draft("test", url, Path("unused.png"))
        assert page.evaluate("Boolean(window.saved)") is False
        assert _editor_text_body_text(page) == original_text
        browser.close()


def test_legacy_guide_must_remain_after_card(config, tmp_path, monkeypatch):
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-text"><div class="se-component-content" '
            'contenteditable="true"><div class="se-section-text">'
            '<p class="se-text-paragraph">body</p>'
            f'<p class="se-text-paragraph"><span class="se-link">{url}</span></p>'
            '</div></div></div>'
            '<div class="se-component se-image"><div class="se-component-content">'
            '<img style="width:20px;height:20px" src="data:,unused.png"></div></div>'
            + _og_controls()
            + '<button style="position:fixed;top:0" onclick="window.saved=true">임시저장</button>'
        )
        page.evaluate(
            """() => {
              const insert = window.insertOg;
              window.insertOg = () => {
                insert();
                document.querySelector('.se-image').insertAdjacentElement(
                  'afterend', document.querySelector('.se-oglink'));
              };
            }"""
        )
        writer = NaverDraftWriter(config)
        monkeypatch.setattr(writer, "_prepare_editor", lambda: page)
        monkeypatch.setattr(writer, "_load_temp_draft", lambda page, log_no: None)
        monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
        with pytest.raises(RuntimeError, match="not after the OG card"):
            writer.append_footer_to_temp_draft("test", url, tmp_path / "unused.png")
        assert page.evaluate("Boolean(window.saved)") is False
        assert naver._visible_image_count(page) == 1
        browser.close()


def test_footer_card_and_uploaded_guide_follow_body(config, tmp_path, monkeypatch):
    url = "https://naver.me/example"
    image = tmp_path / "guide.png"
    image.write_bytes(b"test")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-text"><div class="se-component-content" '
            'contenteditable="true"><div class="se-section-text">'
            '<p class="se-text-paragraph">first<br><br>last</p>'
            '</div></div></div>'
            + _og_controls()
        )
        writer = NaverDraftWriter(config)

        def upload(page, paths, title):
            page.locator(".se-oglink").evaluate(
                "card => card.insertAdjacentHTML('afterend', "
                "'<div class=\"se-component se-image\"><div class=\"se-component-content\">"
                "<img style=\"width:20px;height:20px\" src=\"data:,guide.png\">"
                "</div></div>')"
            )

        monkeypatch.setattr(writer, "_upload_media", upload)
        writer._append_footer(page, url, image)
        assert _editor_text_body_text(page) == "first\n\nlast"
        assert page.locator('.se-oglink a').count() == 0
        assert naver._visible_images_after_footer_card(page.locator(".se-oglink")) == 1
        browser.close()


def test_safe_draft_labels() -> None:
    assert _is_safe_draft_label("임시저장") is True
    assert _is_safe_draft_label("저장") is True
    assert _is_safe_draft_label("저장 | 3") is True


def test_publish_labels_are_never_accepted() -> None:
    assert _is_safe_draft_label("발행") is False
    assert _is_safe_draft_label("저장 후 발행") is False
    assert _is_safe_draft_label("예약 발행") is False


def test_unrelated_save_controls_are_not_accepted() -> None:
    assert _is_safe_draft_label("설정 저장") is False
    assert _is_safe_draft_label("저장하기") is False


def test_visible_blog_control_is_selectable_when_account_copy_is_hidden() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div id="account" style="display:none"><span>블로그</span></div>'
            '<a href="/blog"><span>블로그</span></a>'
        )
        control = page.get_by_text("블로그", exact=True).filter(visible=True).first
        assert control.is_visible()
        assert (
            control.evaluate("element => element.closest('a').getAttribute('href')")
            == "/blog"
        )
        browser.close()


def test_verbatim_insertion_preserves_text_and_blank_lines() -> None:
    source = "첫 줄\n\n둘째 줄 🙂\n끝"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div id="body" class="se-section-text" contenteditable="true">'
            '<p class="se-text-paragraph"><br></p></div>'
        )
        body = page.locator("#body")
        body.click()
        _insert_verbatim(page, source)
        assert _editor_text_body_text(page) == source
        browser.close()


def test_only_known_autosave_restore_popup_is_cancelled() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div data-group="popupLayer">'
            "작성 중인 글이 있습니다. 어제 작성중이던 내용이 있습니다. "
            "이어서 작성하시겠습니까?"
            '<button onclick="this.parentElement.remove()">취소</button>'
            "<button>확인</button></div>"
        )
        assert _dismiss_restore_popup(page) is True
        assert page.locator('[data-group="popupLayer"]').count() == 0
        browser.close()


def test_unknown_popup_is_not_touched() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div data-group="popupLayer">발행하시겠습니까?'
            '<button>취소</button><button>확인</button></div>'
        )
        assert _dismiss_restore_popup(page) is False
        assert page.locator('[data-group="popupLayer"]').is_visible()
        browser.close()


def test_video_upload_uses_nested_uploader_and_required_title(
    tmp_path: Path,
) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"not-a-real-video")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<button data-name="video" '
            "onclick=\"document.querySelector('.se-popup-video-upload').style.display='block';"
            "setTimeout(() => document.querySelector('.nvu_local').style.display='block', 100)\">"
            "동영상</button>"
            '<div class="se-popup-video-upload" style="display:none">'
            '<button class="nvu_local" style="display:none" '
            'onclick="document.querySelector(\'#file\').click()">동영상 추가</button>'
            '<input id="file" type="file" style="display:none" '
            "onchange=\"document.querySelector('#status').textContent='업로드 완료'\">"
            '<input placeholder="제목을 입력하세요. (최대 40자, 필수)">'
            '<span id="status">업로드 진행중</span>'
            '<button onclick="this.parentElement.remove()">완료</button>'
            "</div>"
        )

        _upload_video(page, video, "가" * 50)

        assert page.locator(".se-popup-video-upload").count() == 0
        browser.close()


def test_current_draft_count_uses_accessible_label() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<button aria-label="임시저장된 글 보기, 98개">98</button>'
        )
        assert _current_draft_count(page) == 98
        browser.close()


def test_footer_url_is_converted_to_clickable_link() -> None:
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            f'<p class="se-text-paragraph"><span>{url}</span></p></div>'
            '<button data-name="text-link" '
            "onclick=\"window.savedRange=getSelection().getRangeAt(0).cloneRange();"
            "document.querySelector('#link-box').style.display='block'\">링크</button>"
            '<div id="link-box" style="display:none">'
            '<input placeholder="URL을 입력하세요.">'
            '<button class="se-custom-layer-link-apply-button" '
            "onclick=\"const s=getSelection();s.removeAllRanges();"
            "s.addRange(window.savedRange);document.execCommand('createLink',false,"
            "document.querySelector('#link-box input').value)\">적용</button></div>"
        )

        _make_text_link(page, url)

        assert page.locator(f'a[href="{url}"]').count() == 1
        browser.close()


def test_clickable_link_can_be_inserted_at_empty_cursor() -> None:
    url = "https://naver.me/example"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component-content" contenteditable="true">'
            '<p id="target" class="se-text-paragraph"><br></p></div>'
            '<button data-name="text-link" style="display:none" '
            "onclick=\"window.savedRange=getSelection().getRangeAt(0).cloneRange();"
            "document.querySelector('#link-box').style.display='block'\">링크</button>"
            '<div id="link-box" style="display:none">'
            '<input placeholder="URL을 입력하세요.">'
            '<button class="se-custom-layer-link-apply-button" '
            "onclick=\"const input=document.querySelector('#link-box input');"
            "const span=document.createElement('span');span.className='se-link';"
            "span.dataset.href=input.value;span.textContent=input.value;"
            "window.savedRange.insertNode(span)\">적용</button></div>"
            '<script>setTimeout(() => {'
            'document.querySelector(\'button[data-name="text-link"]\')'
            '.style.display="block";'
            '}, 100);</script>'
        )
        page.locator("#target").click()

        _insert_text_link_at_cursor(page, url)

        assert page.locator(f'.se-link[data-href="{url}"]').count() == 1
        browser.close()


def test_footer_uses_last_nonempty_body_paragraph() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle"><p class="se-text-paragraph">제목</p></div>'
            '<div class="se-section-text">'
            '<p class="se-text-paragraph">본문 끝</p>'
            '<p class="se-text-paragraph"></p></div>'
        )

        paragraph = _find_last_visible_text_paragraph(page)

        assert paragraph.inner_text() == "본문 끝"
        browser.close()


def test_empty_text_body_ignores_title_and_video_metadata() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-documentTitle">제목</div>'
            '<div class="se-video">영상 제목</div>'
            '<div class="se-section-text"><p class="se-text-paragraph"></p></div>'
        )

        assert _editor_text_body_text(page).strip() == ""
        browser.close()


def test_list_paragraph_is_detected() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<ul><li><p id="list-item" class="se-text-paragraph">항목</p></li></ul>'
            '<p id="normal" class="se-text-paragraph">문단</p>'
        )

        assert _paragraph_is_in_list(page.locator("#list-item")) is True
        assert _paragraph_is_in_list(page.locator("#normal")) is False
        browser.close()


def test_footer_paragraph_is_created_for_media_only_draft() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="se-component se-image"></div>'
            '<button class="se-canvas-bottom-button" '
            "onclick=\"this.insertAdjacentHTML('beforebegin', "
            "'<div class=&quot;se-section-text&quot;>"
            "<p class=&quot;se-text-paragraph&quot; style=&quot;height:20px&quot;>"
            "<br></p></div>')\">"
            "본문 추가</button>"
        )

        paragraph = _find_or_create_footer_paragraph(page)

        assert paragraph.is_visible()
        assert page.locator(".se-section-text").count() == 1
        browser.close()
