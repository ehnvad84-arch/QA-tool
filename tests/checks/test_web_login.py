"""Вход в веб-интерфейс: одна попытка при любом исходе, судит свидетель mock."""

import pytest

from qa_tool.checks.rtsp_refusal import MEMORY_KEY, refused_password
from qa_tool.checks.web.login import Entry, login
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.browser import BrowserSession
from tests.mock_web import MockWebCamera

HOME = "<h1>Камера</h1><nav><a href='#/image'>Изображение</a></nav>"


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
