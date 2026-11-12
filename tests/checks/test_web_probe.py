"""Открытие страницы и нажатие без записи: судит свидетель mock."""

from qa_tool.checks.web.discover import Page, classify
from qa_tool.checks.web.probe import open_page, press
from qa_tool.core.checks import CheckContext, Severity, Status
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.browser import BrowserSession
from tests.mock_web import MockWebCamera


def context(**limits):
    camera = Camera(slot=1, address="127.0.0.1", port=80, username="admin")
    thresholds = {key: Threshold(value, "с", "тест") for key, value in limits.items()}
    return CheckContext(camera, "secret", Place.COLOR_BOX, thresholds=thresholds)


def opened(html, **limits):
    with MockWebCamera({"/": html}) as camera, BrowserSession(camera.url) as session:
        row = open_page(context(**limits), session, Page(camera.url + "/", "Главная"))
    return camera, row


def pressed(html, label="Кнопка", **limits):
    limits.setdefault("web.click_response_s", 0.4)
    ctx = context(**limits)
    with MockWebCamera({"/": html}) as camera, BrowserSession(camera.url) as session:
        page = open_page(ctx, session, Page(camera.url + "/", "Главная"))
        element = next(item for item in page.elements if item.label == label)
        row = press(ctx, session, classify(element))
    return camera, row


def test_clean_page_passes():
    _, row = opened("<h1>Камера</h1>")
    assert row.result.status is Status.PASSED


def test_script_error_fails_with_text_and_snapshot():
    _, row = opened("<script>throw new Error('сломано')</script>")
    assert row.result.status is Status.FAILED
    assert "сломано" in row.result.actual
    assert row.severity is Severity.MAJOR
    assert any(item.name == "page.png" for item in row.result.evidence)


def test_console_error_fails():
    _, row = opened("<script>console.error('нет данных')</script>")
    assert row.result.status is Status.FAILED


def test_failed_resource_fails_with_address():
    _, row = opened("<script>fetch('/api/missing')</script>")
    assert row.result.status is Status.FAILED
    assert "/api/missing" in row.result.actual


def test_missing_favicon_is_not_an_error():
    _, row = opened("<h1>Камера</h1><link rel='icon' href='/favicon.ico'>")
    assert row.result.status is Status.PASSED


def test_error_from_our_stop_is_not_counted():
    page = "<script>fetch('/api/a', {method: 'POST'}).then((r) => r.json())</script>"
    camera, row = opened(page)
    assert row.result.status is Status.PASSED, row.result.actual
    assert camera.writes() == []


def test_slow_page_fails_with_time_and_limit():
    _, row = opened("<h1>Камера</h1>", **{"web.page_load_s": 0.0})
    assert row.result.status is Status.FAILED
    assert row.result.threshold is not None


def test_plugin_page_is_skipped():
    _, row = opened("<h1>Please install the plug-in</h1>")
    assert row.result.status is Status.SKIPPED
    assert "плагин" in row.result.reason


def test_button_with_request_passes():
    _, row = pressed("<button onclick=\"fetch('/seen')\">Кнопка</button>")
    assert row.result.status is Status.PASSED


def test_stopped_write_passes_with_note_and_does_not_arrive():
    html = "<button onclick=\"fetch('/api/image/save', {method: 'POST'})\">Кнопка</button>"
    camera, row = pressed(html)
    assert row.result.status is Status.PASSED
    assert "до камеры не дошло" in row.result.actual
    assert "/api/image/save" in row.result.actual
    assert camera.writes() == []


def test_button_that_changes_page_passes():
    html = "<button onclick=\"document.body.append('готово')\">Кнопка</button>"
    _, row = pressed(html)
    assert row.result.status is Status.PASSED


def test_dead_button_is_minor_failure():
    _, row = pressed("<button>Кнопка</button>")
    assert row.result.status is Status.FAILED
    assert row.severity is Severity.MINOR


def test_ticking_clock_is_not_a_reaction():
    html = """<button>Кнопка</button><div id=c>0</div>
    <script>setInterval(() => c.innerText = Date.now(), 50)</script>"""
    _, row = pressed(html)
    assert row.result.status is Status.FAILED


def test_late_reaction_fails_with_time():
    html = "<button onclick=\"setTimeout(() => document.body.append('да'), 600)\">Кнопка</button>"
    _, row = pressed(html)
    assert row.result.status is Status.FAILED
    assert row.result.threshold is not None


def test_taboo_request_is_stand_error_and_does_not_arrive():
    html = "<button onclick=\"fetch('/api/network/set', {method: 'POST'})\">Кнопка</button>"
    camera, row = pressed(html)
    assert row.result.status is Status.STAND_ERROR
    assert camera.writes() == []


def test_danger_request_is_noted_and_does_not_arrive():
    html = "<button onclick=\"fetch('/api/system/reboot', {method: 'POST'})\">Кнопка</button>"
    camera, row = pressed(html)
    assert row.result.status is Status.SKIPPED
    assert "не нажат" in row.result.reason or "опасн" in row.result.reason
    assert camera.writes() == []


def test_danger_element_is_not_pressed():
    html = "<button onclick=\"fetch('/seen')\">Перезагрузка</button>"
    camera, row = pressed(html, label="Перезагрузка")
    assert row.result.status is Status.SKIPPED
    assert ("GET", "/seen") not in camera.arrived


def test_disabled_element_is_skipped_with_reason():
    camera, row = pressed("<button disabled onclick=\"fetch('/seen')\">Кнопка</button>")
    assert row.result.status is Status.SKIPPED
    assert row.result.reason


def test_input_field_is_not_pressed():
    camera, row = pressed("<input aria-label='Кнопка' onclick=\"fetch('/seen')\">")
    assert row.result.status is Status.SKIPPED
    assert ("GET", "/seen") not in camera.arrived
