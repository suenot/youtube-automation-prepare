import pytest

import human
import publish

from publish import (details_validation_issues, format_validation_issues, save,
                     _fill_upload_contenteditable, _upload_route,
                     _wait_for_upload_launcher, find_by_title, verify_before_save,
                     verify_saved_video, wait_for_upload, upload_completion_confirmed)


def test_upload_route_targets_channel_and_opens_dialog():
    assert _upload_route("UCtarget") == (
        "https://studio.youtube.com/channel/UCtarget/videos/upload?d=ud"
    )


class DelayedLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return int(self.selector == "ytcp-icon-button#upload-icon")

    async def is_visible(self):
        return self.page.waits >= 2


class DelayedStudioPage:
    def __init__(self):
        self.waits = 0

    def locator(self, selector):
        return DelayedLocator(self, selector)

    async def wait_for_timeout(self, _timeout_ms):
        self.waits += 1


@pytest.mark.asyncio
async def test_wait_for_upload_launcher_allows_studio_to_mount():
    page = DelayedStudioPage()

    locator, opens_dialog = await _wait_for_upload_launcher(page, timeout_ms=2_000)

    assert locator.selector == "ytcp-icon-button#upload-icon"
    assert opens_dialog is True
    assert page.waits == 2


class ValidationPage:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def evaluate(self, _script):
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_details_validation_returns_invalid_and_red_fields():
    issues = [
        {"field": "Title", "reason": "invalid", "text": "Title is too long"},
        {"field": "Category", "reason": "visual-red", "text": "Select"},
    ]

    assert await details_validation_issues(ValidationPage(issues)) == issues
    assert format_validation_issues(issues) == (
        "Title [invalid]: Title is too long; Category [visual-red]: Select"
    )


@pytest.mark.asyncio
async def test_details_validation_fails_closed_when_dom_inspection_breaks():
    issues = await details_validation_issues(
        ValidationPage(error=RuntimeError("shadow root detached")))

    assert issues == [{
        "field": "details form",
        "reason": "inspection-failed",
        "text": "shadow root detached",
    }]


class ContenteditableKeyboard:
    """Keys land in the field; nothing else does.

    Studio's Polymer editor updates its persisted model from the input events
    that real keys produce. The field below refuses `fill`, so a publisher
    that went back to putting text in without typing it fails this test.
    """

    def __init__(self, field, paste_lands=False):
        self.field = field
        self.paste_lands = paste_lands
        self.typed = ""

    async def press(self, keys):
        if keys == "Delete":
            self.field.value = ""
        elif keys == human.PASTE_KEY and self.paste_lands:
            self.field.value += self.field.clipboard

    async def insert_text(self, value):
        self.typed += value
        self.field.value += value

    async def type(self, value, delay=0):
        self.typed += value
        self.field.value += value


class ContenteditableMouse:
    async def move(self, _x, _y):
        return None

    async def down(self):
        return None

    async def up(self):
        return None


class ContenteditablePage:
    def __init__(self, field, paste_lands=False):
        self.keyboard = ContenteditableKeyboard(field, paste_lands)
        self.mouse = ContenteditableMouse()

    async def wait_for_timeout(self, _timeout_ms):
        return None

    async def evaluate(self, _script):
        return None


class PolymerContenteditable:
    def __init__(self):
        self.value = ""
        self.clipboard = ""

    async def scroll_into_view_if_needed(self, timeout=0):
        return None

    async def bounding_box(self):
        return {"x": 10, "y": 20, "width": 400, "height": 90}

    async def click(self, timeout=0):
        return None

    async def focus(self, timeout=0):
        return None

    async def fill(self, _value):
        raise AssertionError("The field must never be filled")

    async def press(self, _keys):
        return None

    async def inner_text(self):
        return self.value


@pytest.mark.asyncio
async def test_a_description_is_pasted_the_way_a_person_pastes_one(monkeypatch):
    """Nobody composes a description in the upload form; they paste it."""
    field = PolymerContenteditable()
    page = ContenteditablePage(field, paste_lands=True)
    monkeypatch.setattr(human, "read_clipboard", lambda: field.clipboard)
    monkeypatch.setattr(human, "write_clipboard",
                        lambda text: setattr(field, "clipboard", text) or True)

    long_description = "Plymouth lies buried under volcanic deposits. " * 4
    assert await _fill_upload_contenteditable(
        page, field, long_description, "description") is True
    assert field.value == long_description
    assert page.keyboard.typed == "", "The description was typed, not pasted"


@pytest.mark.asyncio
async def test_without_a_clipboard_the_text_is_typed(monkeypatch):
    """A browser that cannot reach the clipboard still gets real key events."""
    field = PolymerContenteditable()
    page = ContenteditablePage(field)
    monkeypatch.setattr(human, "read_clipboard", lambda: None)
    monkeypatch.setattr(human, "write_clipboard", lambda _text: False)

    assert await _fill_upload_contenteditable(
        page, field, "Описание", "description") is True
    assert field.value == "Описание"
    assert page.keyboard.typed == "Описание"


class MustNotClickSavePage:
    def locator(self, _selector):
        raise AssertionError("Save control must not be queried")


@pytest.mark.asyncio
async def test_save_stops_before_button_when_form_is_invalid(monkeypatch):
    async def invalid(_page, _debug, action):
        assert action == "Save"
        return False

    monkeypatch.setattr("publish.validate_details_before_action", invalid)

    assert await save(MustNotClickSavePage(), debug=False) is False


class UnreadablePage:
    async def goto(self, *_args, **_kwargs):
        raise RuntimeError("offline")

    async def evaluate(self, _script):
        raise RuntimeError("closed")


@pytest.mark.asyncio
async def test_duplicate_check_stops_on_navigation_failure():
    with pytest.raises(RuntimeError, match="refusing upload"):
        await find_by_title(UnreadablePage(), "UCtarget", "Title")


class ContentListLocator:
    def __init__(self):
        self.first = self

    async def wait_for(self, **_kwargs):
        return None


class FlakyContentListPage:
    """Studio content list that shows its error state on the first load of a tab."""

    def __init__(self, rows_by_tab):
        self.rows_by_tab = rows_by_tab
        self.loads = []
        self.url = ""

    async def goto(self, url, **_kwargs):
        self.url = url
        self.loads.append(url.rsplit("/", 1)[1])

    async def wait_for_timeout(self, _ms):
        return None

    def locator(self, _selector):
        return ContentListLocator()

    async def evaluate(self, script):
        tab = self.loads[-1]
        if script == publish.CONTENT_ROWS_JS:
            if self.loads.count(tab) == 1:
                return []
            return self.rows_by_tab[tab]
        return "oops, something went wrong. retry"


@pytest.mark.asyncio
async def test_duplicate_check_reloads_a_content_list_that_failed_to_render():
    page = FlakyContentListPage({
        "short": [{"id": "abcdefghijk", "text": "0:35 Fifteen Caribbean Places Have Fewer Residents"}],
        "upload": [],
    })

    assert await find_by_title(
        page, "UCtarget", "Fifteen Caribbean Places Have Fewer Residents") == "abcdefghijk"
    assert page.loads == ["short", "short"]


@pytest.mark.asyncio
async def test_duplicate_check_refuses_after_every_load_fails():
    page = FlakyContentListPage({"short": [], "upload": []})

    async def always_broken(script):
        if script == publish.CONTENT_ROWS_JS:
            return []
        return "oops, something went wrong. retry"

    page.evaluate = always_broken
    with pytest.raises(RuntimeError, match="Duplicate check on /short failed"):
        await find_by_title(page, "UCtarget", "Title")
    assert page.loads == ["short"] * publish.DUPLICATE_CHECK_ATTEMPTS


@pytest.mark.asyncio
async def test_unreadable_upload_page_is_not_transfer_success():
    with pytest.raises(RuntimeError, match="closed"):
        await wait_for_upload(UnreadablePage(), debug=False)


@pytest.mark.asyncio
async def test_channel_mismatch_stops_before_visibility_query():
    page = MustNotClickSavePage()
    page.url = "https://studio.youtube.com/channel/UCwrong/videos/upload"
    assert await verify_before_save(page, "UCexpected", "public") is False


@pytest.mark.asyncio
async def test_missing_transfer_text_does_not_confirm_completion(monkeypatch):
    class Page:
        waits = 0

        async def evaluate(self, _script):
            return "details visibility public"

        async def wait_for_timeout(self, _ms):
            self.waits += 1

    ticks = iter([0, 0, 2])
    monkeypatch.setattr("publish.time.time", lambda: next(ticks))
    monkeypatch.setattr("publish.UPLOAD_WAIT_S", 1)
    page = Page()
    assert await wait_for_upload(page, debug=False) is False
    assert page.waits == 1


@pytest.mark.asyncio
async def test_saved_public_settings_do_not_hide_failed_processing(monkeypatch):
    class Page:
        url = ""

        async def goto(self, url, **_kwargs):
            self.url = url

        async def evaluate(self, _script):
            return "processing abandoned public"

    async def fields(_page, _selectors, _timeout):
        return object()

    monkeypatch.setattr("publish.ui.first_visible", fields)
    assert await verify_saved_video(Page(), "wZqnYESB3Us", {"title": "Title", "description": ""}, "public") is False


@pytest.mark.parametrize("status", [
    "upload complete uploading 70%", "video processing still uploading",
    "checks complete keep this browser tab open", "upload complete video uploading",
])
def test_completion_phrase_cannot_override_active_transfer(status):
    assert upload_completion_confirmed(status) is False


def test_transfer_completion_needs_positive_status():
    assert upload_completion_confirmed("upload complete processing will begin") is True
    assert upload_completion_confirmed("details visibility public") is False
