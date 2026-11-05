"""Отвергнутый пароль: на всю камеру одна попытка, какие бы проверки потока ни шли за ней.

Неверный пароль может заблокировать вход на камере, поэтому считается каждый запрос с паролем.
"""

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.rtsp_refusal import is_refusal
from qa_tool.checks.stream import network, params, stress
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.rtsp import RtspError
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState

load_all()
pytestmark = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)
STREAMS = tuple(MockStream(name, path=f"/{name}") for name in ("a", "b", "c", "d"))


@pytest.fixture
def camera():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(streams=STREAMS)).start()
    cam.rtsp.reject_password = True
    yield cam
    cam.stop()


def context(cam):
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    return CheckContext(camera, "admin", Place.COLOR_BOX, extra={"stress_seconds": 2})


def rows_of(check_id, ctx):
    result = run_check(REGISTRY.get(check_id), ctx)
    return [row.result for row in result.children]


def test_params_tries_once_for_all_profiles(camera):
    first, *others = rows_of(params.CHECK_ID, context(camera))
    assert first.status is Status.STAND_ERROR
    assert "логин и пароль" in first.reason
    assert all(row.status is Status.SKIPPED for row in others)
    assert camera.rtsp.password_tries == 1


def test_network_tries_once_for_all_profiles(camera):
    first, *others = rows_of(network.CHECK_ID, context(camera))
    assert first.status is Status.STAND_ERROR
    assert all(row.status is Status.SKIPPED for row in others)
    assert camera.rtsp.password_tries == 1


def test_stress_tries_once_for_all_profiles(camera):
    rows = rows_of(stress.CHECK_ID, context(camera))
    assert rows[0].status is Status.STAND_ERROR
    assert all(row.status is Status.SKIPPED for row in rows[1:])
    assert camera.rtsp.password_tries == 1


def test_one_refusal_covers_every_check_of_the_camera(camera):
    ctx = context(camera)
    for check_id in (params.CHECK_ID, network.CHECK_ID, stress.CHECK_ID):
        rows_of(check_id, ctx)
    assert camera.rtsp.password_tries == 1


def test_forbidden_counts_as_refusal():
    assert is_refusal(RtspError("DESCRIBE → 403 Forbidden", 403))
    assert not is_refusal(RtspError("DESCRIBE → 404 Not Found", 404))
    assert not is_refusal(OSError("нет связи"))
