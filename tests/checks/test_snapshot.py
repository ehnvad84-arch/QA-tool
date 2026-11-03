import pytest

from qa_tool.checks.onvif.snapshot import SnapshotTaker
from qa_tool.core.models import Camera
from qa_tool.drivers.camera.soap import SoapClient
from tests.mock_camera import start_fleet


@pytest.fixture
def mock():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def taker(mock) -> SnapshotTaker:
    camera = Camera(slot=1, address="127.0.0.1", port=mock.port, username="admin")
    return SnapshotTaker(camera, SoapClient("admin", "admin"))


def spoil(mock):
    """Что делают изменяющие тесты onvif-tt: Manual-время, реле, фокус, PTZ."""
    s = mock.state
    s.datetime_type, s.tz = "Manual", "UTC0"
    s.relay_xml = "<tt:Mode>Monostable</tt:Mode><tt:DelayTime>PT9S</tt:DelayTime><tt:IdleState>open</tt:IdleState>"
    s.relay_state = "active"
    s.imaging_xml = "<tt:Brightness>90</tt:Brightness><tt:Focus><tt:AutoFocusMode>MANUAL</tt:AutoFocusMode></tt:Focus>"
    s.focus_position = "0.9"
    s.ptz_config_xml = s.ptz_config_xml.replace("PT5S", "PT9S")
    s.ptz_position = (0.8, 0.8, 0.5)


def test_everything_restored_and_verified(mock):
    original = (
        mock.state.datetime_type,
        mock.state.tz,
        mock.state.relay_xml,
        mock.state.imaging_xml,
        mock.state.focus_position,
        mock.state.ptz_position,
    )
    t = taker(mock)
    snap = t.take()
    assert not snap.skipped, snap.skipped
    areas = {i.area.split()[0] for i in snap.items}
    assert {"дата", "тревожный", "изображение", "настройки", "положение"} <= areas
    spoil(mock)
    restored, problems = t.restore(snap)
    assert problems == [], problems
    s = mock.state
    assert (s.datetime_type, s.tz) == original[:2]
    assert s.relay_state == "inactive"
    assert "Bistable" in s.relay_xml and ">50<" in s.imaging_xml and s.focus_position == original[4]
    assert "PT5S" in s.ptz_config_xml and s.ptz_position == pytest.approx(original[5])
    assert len(restored) == len(snap.items)


def test_failed_restore_reported_as_camera_left_changed(mock):
    t = taker(mock)
    snap = t.take()
    spoil(mock)
    mock.state.reject_set = True
    restored, problems = t.restore(snap)
    assert restored == [] and len(problems) == len(snap.items)
    assert any(p.startswith("дата и время") for p in problems)
