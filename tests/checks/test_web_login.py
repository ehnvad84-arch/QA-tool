"""Вход в веб-интерфейс: одна попытка при любом исходе, судит свидетель mock."""

import pytest

from qa_tool.checks.rtsp_refusal import MEMORY_KEY, refused_password
from qa_tool.checks.web.login import Entry, login, logout, web_address
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.drivers.camera.browser import BrowserSession
from tests.mock_web import MockWebCamera

HOME = """<h1>Камера</h1><nav><a href='#/image'>Изображение</a></nav>
<button onclick="fetch('/api/auth/logout', {method: 'POST'})">Выйти</button>"""


def context(password="secret"):
    camera = Camera(slot=1, address="127.0.0.1", port=80, username="admin")
    return CheckContext(camera, password, Place.COLOR_BOX)


def enter(ctx, **options):
    with MockWebCamera({"/": HOME}, auth="form", **options) as camera:
        with BrowserSession(camera.url) as session:
            result = login(ctx, session, camera.url + "/")
        return camera, result


def test_correct_password_enters():
    camera, result = enter(context())
    assert result.entry is Entry.OK
    assert result.walk_allowed
    assert camera.login_attempts == 1
    assert camera.sessions == 1


def test_form_drawn_late_is_not_a_camera_without_login():
    form = "'<input type=password><button>Войти</button>'"
    page = f"<script>setTimeout(() => document.body.innerHTML = {form}, 4000)</script>"
    ctx = context()
    ctx.thresholds["web.login_timeout_s"] = Threshold(1.0, "с", "тест")
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        result = login(ctx, session, camera.url + "/")
    assert result.entry is Entry.REFUSED


def test_wrong_password_tries_once_and_is_remembered():
    ctx = context("wrong")
    camera, result = enter(ctx)
    assert result.entry is Entry.REFUSED
    assert not result.walk_allowed
    assert camera.login_attempts == 1
    assert "веб-интерфейса" in refused_password(ctx)


def test_refusal_from_video_does_not_open_form():
    ctx = context()
    ctx.extra[MEMORY_KEY] = "камера не приняла логин и пароль для видео: 401"
    camera, result = enter(ctx)
    assert result.entry is Entry.REFUSED
    assert camera.login_attempts == 0


def test_change_password_screen_gets_nothing():
    ctx = context()
    camera, result = enter(ctx, must_change_password=True)
    assert result.entry is Entry.CHANGE_REQUIRED
    assert camera.login_attempts == 0
    assert camera.bodies == []
    assert not refused_password(ctx)


def test_sessions_full_is_not_a_refusal():
    ctx = context()
    camera, result = enter(ctx, sessions_limit=0)
    assert result.entry is Entry.SESSIONS_FULL
    assert camera.login_attempts == 1
    assert not refused_password(ctx)


def test_no_password_page_needs_no_login():
    with MockWebCamera({"/": HOME}) as camera, BrowserSession(camera.url) as session:
        result = login(context(), session, camera.url + "/")
    assert result.entry is Entry.NOT_NEEDED
    assert camera.login_attempts == 0


@pytest.mark.parametrize("password", ["network1", "resetNew1"])
def test_password_value_does_not_stop_login(password):
    camera, result = enter(context(password), password=password)
    assert result.entry is Entry.OK
    assert camera.login_attempts == 1


@pytest.mark.parametrize("auth", ["basic", "digest"])
def test_http_login_enters_once(auth):
    pages = {"/": "<img src='/a.png'><img src='/b.png'>", "/a.png": "x", "/b.png": "x"}
    with MockWebCamera(pages, auth=auth) as camera, BrowserSession(camera.url) as session:
        result = login(context(), session, camera.url + "/")
        session.wait(200)
    assert result.entry is Entry.OK
    assert camera.login_attempts == 1


@pytest.mark.parametrize("auth", ["basic", "digest"])
def test_http_wrong_password_tries_once(auth):
    pages = {"/": "<img src='/a.png'><img src='/b.png'>", "/a.png": "x", "/b.png": "x"}
    ctx = context("wrong")
    with MockWebCamera(pages, auth=auth) as camera, BrowserSession(camera.url) as session:
        result = login(ctx, session, camera.url + "/")
        session.wait(300)
    assert result.entry is Entry.REFUSED
    assert camera.login_attempts == 1
    assert refused_password(ctx)


def test_write_with_digest_is_answered():
    page = "<script>fetch('/api/image/save', {method: 'POST', body: '{}'})</script>"
    with (
        MockWebCamera({"/": page}, auth="digest") as camera,
        BrowserSession(camera.url) as session,
        session.write_window(),
    ):
        login(context(), session, camera.url + "/")
        session.wait(400)
    assert ("POST", "/api/image/save") in camera.arrived
    assert session.answers[-1] == 200


def test_page_write_right_after_login_does_not_reach_camera():
    page = "<script>fetch('/api/config/read', {method: 'POST', body: '{}'})</script>"
    with MockWebCamera({"/": page}, auth="form") as camera, BrowserSession(camera.url) as session:
        login(context(), session, camera.url + "/")
        session.wait(300)
    assert camera.writes() == [("POST", "/api/auth/login")]


def test_logout_frees_session():
    with MockWebCamera({"/": HOME}, auth="form") as camera, BrowserSession(camera.url) as session:
        login(context(), session, camera.url + "/")
        assert camera.sessions == 1
        assert logout(session, camera.url + "/") == ""
    assert camera.sessions == 0


def test_logout_without_button_is_noted():
    with MockWebCamera({"/": "<h1>x</h1>"}) as camera, BrowserSession(camera.url) as session:
        assert logout(session, camera.url + "/")


def test_alarm_output_is_not_logout():
    page = "<button>Тревожный выход</button>"
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        assert logout(session, camera.url + "/")
    assert camera.writes() == []


def test_web_address_uses_first_open_port():
    with MockWebCamera({"/": "x"}) as camera:
        port = int(camera.url.rsplit(":", 1)[1])
        ctx = context()
        ctx.camera = Camera(slot=1, address="127.0.0.1", port=port, username="admin")
        assert web_address(ctx) == f"http://127.0.0.1:{port}/"


def test_web_address_all_closed(monkeypatch):
    monkeypatch.setattr("qa_tool.checks.web.login.tcp_open", lambda *_: False)
    assert web_address(context()) is None


def test_self_signed_certificate_is_accepted(tmp_path):
    with MockWebCamera({"/": "x"}, tls_dir=tmp_path) as camera:
        assert camera.url.startswith("https://")
        with BrowserSession(camera.url) as session:
            assert session.open(camera.url + "/").status == 200


def test_login_stopped_by_taboo_is_not_a_refusal():
    # Hikvision .112 (11.11): страница входа при загрузке спрашивает GET /SDK/activateStatus —
    # табу его останавливает, форма без ответа не работает, пароль до камеры не доходит
    page = (
        "<script>fetch('/SDK/activateStatus').catch(() => {})</script>"
        "<input type=password><button>Войти</button>"
    )
    ctx = context()
    ctx.thresholds["web.login_timeout_s"] = Threshold(1.0, "с", "тест")
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        result = login(ctx, session, camera.url + "/")
    assert result.entry is Entry.STOPPED and not result.walk_allowed
    assert "/SDK/activateStatus" in result.reason
    assert refused_password(ctx) == ""  # отказа не было — вход для других проверок открыт
    assert camera.arrived == [("GET", "/")]


def test_login_request_stopped_on_click_is_not_a_refusal():
    page = "<input type=password><button onclick=\"fetch('/SDK/activateStatus')\">Войти</button>"
    ctx = context()
    ctx.thresholds["web.login_timeout_s"] = Threshold(1.0, "с", "тест")
    with MockWebCamera({"/": page}) as camera, BrowserSession(camera.url) as session:
        result = login(ctx, session, camera.url + "/")
    assert result.entry is Entry.STOPPED and refused_password(ctx) == ""
