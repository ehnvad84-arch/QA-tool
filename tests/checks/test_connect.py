import re

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.core import connect
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.checks import CREDENTIALS_REJECTED, REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.drivers.camera.probe import RtspProbe
from tests.mock_camera import start_fleet

load_all()
SPEC = REGISTRY.get("CORE-CONNECT-1")


@pytest.fixture
def mock():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def ctx_for(mock, password="admin"):
    camera = Camera(
        slot=1, address="127.0.0.1", port=mock.port, rtsp_port=mock.rtsp_port, username="admin"
    )
    return CheckContext(
        camera=camera, password=password, place=Place.COLOR_BOX, extra={"timeout_s": 1}
    )


def test_live_camera_passes_with_passport(mock):
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason
    passport = result.data[PASSPORT_KEY]
    assert (passport["model"], passport["serial"], passport["firmware"]) == (
        "MOCK-1",
        "SN0001",
        "0.1.0",
    )
    assert result.evidence and "***</wsse:Password>" in result.evidence[0].content


def test_switched_off_camera_fails_with_reason(mock):
    ctx = ctx_for(mock)
    mock.stop()
    result = run_check(SPEC, ctx)
    assert result.status is Status.FAILED
    assert f"нет связи по HTTP (порт {ctx.camera.port})" in result.reason


def test_wrong_password_is_stand_error_not_camera_defect(mock):
    result = run_check(SPEC, ctx_for(mock, password="bad"))
    assert result.status is Status.STAND_ERROR
    assert "не приняла логин или пароль места 1" in result.reason


@pytest.mark.parametrize("reply", ["onvif", "gsoap", "http401", "http401page", "soap11"])
def test_rejected_password_is_stand_error_for_every_kind_of_refusal(mock, reply):
    mock.auth_reply = reply
    result = run_check(SPEC, ctx_for(mock, password="wrong"))
    assert result.status is Status.STAND_ERROR, result.reason
    assert result.data[CREDENTIALS_REJECTED]


def test_rtsp_closing_silently_is_written_down_not_hidden(mock, monkeypatch):
    monkeypatch.setattr(connect, "rtsp_probe", lambda *_: RtspProbe.CLOSED)
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason
    assert "закрыл его без ответа" in result.actual


def test_rtsp_not_answering_fails(mock, monkeypatch):
    monkeypatch.setattr(connect, "rtsp_probe", lambda *_: RtspProbe.NO_ANSWER)
    assert run_check(SPEC, ctx_for(mock)).status is Status.FAILED


def test_refusal_keeps_exchange_as_evidence(mock):
    result = run_check(SPEC, ctx_for(mock, password="wrong"))
    assert result.status is Status.STAND_ERROR and result.evidence
    assert "***</wsse:Password>" in result.evidence[0].content


def test_wrong_clock_with_right_password_is_not_a_rejected_password(mock):
    mock.auth_reply, mock.clock_skew_s = "gsoap", 3600
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason
    assert PASSPORT_KEY in result.data


def test_clock_is_corrected_once_so_later_checks_spend_no_login_attempts(mock):
    mock.auth_reply, mock.clock_skew_s = "gsoap", 3600
    run_check(SPEC, ctx_for(mock))
    mock.requests.clear()
    assert run_check(SPEC, ctx_for(mock)).status is Status.PASSED
    assert mock.requests.count("GetDeviceInformation") == 1
    assert "GetSystemDateAndTime" not in mock.requests


@pytest.mark.parametrize("new_skew_s", [-86400, 2], ids=["перезагрузка", "время вернули"])
def test_camera_clock_jump_after_correction_is_corrected_again(mock, new_skew_s):
    mock.auth_reply, mock.clock_skew_s = "gsoap", 3600
    assert run_check(SPEC, ctx_for(mock)).status is Status.PASSED
    mock.clock_skew_s = new_skew_s
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.PASSED, result.reason


def test_wrong_password_costs_one_extra_attempt_per_clock_correction(mock):
    mock.auth_reply, mock.clock_skew_s = "gsoap", 3600
    run_check(SPEC, ctx_for(mock, password="wrong"))
    mock.requests.clear()
    result = run_check(SPEC, ctx_for(mock, password="wrong"))
    assert result.status is Status.STAND_ERROR and result.data[CREDENTIALS_REJECTED]
    assert mock.requests.count("GetDeviceInformation") == 1


def test_wrong_password_with_right_clock_is_tried_once(mock):
    mock.auth_reply, mock.clock_skew_s = "gsoap", 1  # часы почти верны: повтор не поможет
    mock.requests.clear()
    result = run_check(SPEC, ctx_for(mock, password="wrong"))
    assert result.status is Status.STAND_ERROR and result.data[CREDENTIALS_REJECTED]
    assert mock.requests.count("GetDeviceInformation") == 1


def test_refusal_with_wrong_clock_names_the_clock(mock):
    mock.clock_skew_s = 3600  # ter:NotAuthorized: onvif-tt поправит часы только на другой код
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.STAND_ERROR
    # часы камеры идут целыми секундами, поэтому разница может быть на секунду меньше
    assert re.match(r"часы камеры расходятся с компьютером стенда на 3(599|600) с", result.reason)
    assert result.data[CREDENTIALS_REJECTED]
