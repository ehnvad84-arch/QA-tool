import datetime

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import storage
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
ACTIVE = [{"token": "job-1", "recording": "rec-1", "mode": "Active"}]


def old_recording(minutes: int):
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    end = now - datetime.timedelta(minutes=minutes)
    return [{"token": "rec-1", "start": end - datetime.timedelta(hours=1), "end": end}]


@pytest.fixture
def camera(request):
    cam = MockCamera(
        Passport("X", "M", "1", "S1"), state=OnvifState(**getattr(request, "param", {}))
    )
    cam.start()
    yield cam
    cam.stop()


def run(cam, check_id):
    camera = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    return run_check(REGISTRY.get(check_id), CheckContext(camera, "admin", Place.COLOR_BOX))


def test_storage_reported(camera):
    result = run(camera, storage.CHECK_STORAGE)
    assert result.status is Status.PASSED, result.reason
    assert "LocalStorage /mnt/sd" in result.actual and storage.NO_CAPACITY in result.actual


@pytest.mark.parametrize("camera", [{"has_sd": False}], indirect=True)
def test_recording_camera_without_storage_fails(camera):
    result = run(camera, storage.CHECK_STORAGE)
    assert result.status is Status.FAILED and "о накопителе молчит" in result.reason


@pytest.mark.parametrize("camera", [{"storage_fault": True, "profiles": ("S",)}], indirect=True)
def test_camera_without_recording_and_storage_skipped(camera):
    result = run(camera, storage.CHECK_STORAGE)
    assert result.status is Status.SKIPPED and "GetStorageConfigurations" in result.reason


@pytest.mark.parametrize(
    "camera",
    [{"jobs": ACTIVE, "recordings": old_recording(30), "clock_offset_s": -600}],
    indirect=True,
)
def test_recording_fresh_by_camera_clock(camera):
    # часы камеры отстают на 10 минут, а запись идёт — это не ошибка записи
    result = run(camera, storage.CHECK_FRESH)
    assert result.status is Status.PASSED, result.reason
    assert result.threshold.value == 120 and "записей: 1" in result.actual


@pytest.mark.parametrize(
    "camera",
    [{"jobs": ACTIVE, "recordings": old_recording(30), "recording_stalled": True}],
    indirect=True,
)
def test_recording_stalled_fails(camera):
    result = run(camera, storage.CHECK_FRESH)
    assert result.status is Status.FAILED and "не пополняется" in result.reason


def test_recording_not_enabled_skipped(camera):
    result = run(camera, storage.CHECK_FRESH)
    assert result.status is Status.SKIPPED and "Idle" in result.reason
    assert camera.state.jobs[0]["mode"] == "Idle"  # проверка ничего не включает


@pytest.mark.parametrize("camera", [{"profiles": ("S",)}], indirect=True)
def test_no_profile_g_skipped(camera):
    assert run(camera, storage.CHECK_FRESH).status is Status.SKIPPED


def test_nas_password_not_in_actual():
    assert storage.redact("smb://nas:S3cr3t@10.0.0.9/rec") == "smb://nas:***@10.0.0.9/rec"
    assert storage.parse_time("2026-11-03T10:00:00Z").tzinfo is not None
    assert storage.parse_time("вчера") is None
