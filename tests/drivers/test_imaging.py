"""Драйвер ONVIF Imaging (этап 7, T4): диапазоны, запись одного поля, чтение назад, возврат,
белый список полей и табу."""

import pytest

from qa_tool.checks.image.settings import BY_KEY, SETTINGS
from qa_tool.core.models import Camera
from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera.imaging import (
    BadRange,
    Declared,
    ImageWriteRefused,
    ImagingWriter,
    Unsupported,
    ValueRejected,
)
from qa_tool.drivers.camera.onvif_generic import service_urls
from qa_tool.drivers.camera.soap import SoapClient
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import IMAGING_FULL, IMAGING_OPTIONS, OnvifState


@pytest.fixture
def camera_with():
    started = []

    def start(**state):
        cam = MockCamera(
            Passport("X", "M", "1", "S1"), state=OnvifState(imaging_xml=IMAGING_FULL, **state)
        ).start()
        started.append(cam)
        camera = Camera(slot=1, address="127.0.0.1", port=cam.port, username="admin")
        soap = SoapClient("admin", "admin", timeout=5)
        writer = ImagingWriter(
            soap, service_urls(soap, camera)["imaging"], "vs-1", {s.key: s for s in SETTINGS}
        )
        return cam, writer

    yield start
    for cam in started:
        cam.stop()


def test_ranges_of_all_nine_settings_are_read(camera_with):
    _, writer = camera_with()
    ranges = writer.declared()
    assert len(ranges) == 9
    assert ranges["brightness"] == Declared(0.0, 100.0, integer=True)
    assert ranges["nr"] == Declared(0.0, 1.0, integer=False)
    assert all(isinstance(r, Declared) for r in ranges.values()), ranges


def test_write_within_range_is_read_back(camera_with):
    cam, writer = camera_with()
    writer.declared()
    before = writer.read()
    report = writer.write("contrast", 100.0)
    assert report.accepted and report.read_back == 100.0 and report.form == "поле"
    assert cam.state.imaging_writes[-1] == "<tt:Contrast>100</tt:Contrast>"
    assert before.values["contrast"] == 50.0 and before.exposure_mode == "AUTO"


def test_field_write_does_not_touch_exposure_ircut_focus(camera_with):
    cam, writer = camera_with()
    writer.declared()
    writer.read()
    for key in ("brightness", "wb_red", "wdr", "nr"):
        writer.write(key, 10.0 if key != "nr" else 0.1)
    for sent in cam.state.imaging_writes:
        assert "Exposure" not in sent and "IrCutFilter" not in sent and "Focus" not in sent
    xml = cam.state.imaging_xml
    assert "<tt:Mode>AUTO</tt:Mode><tt:MinExposureTime>10" in xml
    assert "<tt:IrCutFilter>AUTO</tt:IrCutFilter>" in xml


def test_white_balance_goes_manual_with_partner_at_half(camera_with):
    cam, writer = camera_with()
    writer.declared()
    writer.read()
    report = writer.write("wb_blue", 80.0)
    assert report.accepted
    sent = cam.state.imaging_writes[-1]
    assert sent.index("Mode") < sent.index("CrGain") < sent.index("CbGain")  # порядок схемы
    assert "<tt:CrGain>50</tt:CrGain><tt:CbGain>80</tt:CbGain>" in sent and "MANUAL" in sent


def test_noise_reduction_written_at_its_extension_depth(camera_with):
    cam, writer = camera_with()
    writer.declared()
    writer.read()
    assert writer.write("nr", 0.9).accepted
    assert "<tt:NoiseReduction><tt:Level>0.9</tt:Level>" in cam.state.imaging_xml
    assert "<tt:ImageStabilization><tt:Mode>OFF</tt:Mode>" in cam.state.imaging_xml


def test_camera_fault_on_both_forms_is_value_rejected(camera_with):
    _, writer = camera_with(imaging_reject={"Sharpness"})
    writer.declared()
    writer.read()
    with pytest.raises(ValueRejected):
        writer.write("sharpness", 0.0)


def test_camera_that_needs_full_block_gets_it_with_its_own_values(camera_with):
    cam, writer = camera_with(imaging_needs_full_block=True)
    writer.declared()
    writer.read()
    report = writer.write("saturation", 0.0)
    assert report.form == "полный блок" and report.accepted
    sent = cam.state.imaging_writes[-1]
    assert "<tt:ColorSaturation>0</tt:ColorSaturation>" in sent
    assert "<tt:Brightness>50</tt:Brightness>" in sent and "MinExposureTime" in sent


def test_camera_keeps_its_own_value_is_not_accepted(camera_with):
    _, writer = camera_with(imaging_sticky={"Brightness": "50"})
    writer.declared()
    writer.read()
    report = writer.write("brightness", 100.0)
    assert not report.accepted and report.read_back == 50.0


@pytest.mark.parametrize(
    ("low", "high", "kind"),
    [
        ("100", "0", BadRange),
        ("NaN", "100", BadRange),
        ("0", "1e9", BadRange),
        ("50", "50", Unsupported),
    ],
)
def test_broken_ranges_are_never_written(camera_with, low, high, kind):
    options = IMAGING_OPTIONS.replace(
        "<tt:Contrast><tt:Min>0</tt:Min><tt:Max>100</tt:Max>",
        f"<tt:Contrast><tt:Min>{low}</tt:Min><tt:Max>{high}</tt:Max>",
    )
    assert options != IMAGING_OPTIONS
    cam, writer = camera_with(imaging_options_xml=options)
    assert isinstance(writer.declared()["contrast"], kind)
    writer.read()
    with pytest.raises(ImageWriteRefused):
        writer.write("contrast", 10.0)
    assert cam.state.imaging_writes == []


def test_mode_without_level_range_is_two_positions(camera_with):
    options = IMAGING_OPTIONS.replace(
        "<tt:WideDynamicRange><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode>"
        "<tt:Level><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Level>",
        "<tt:WideDynamicRange><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode>",
    )
    cam, writer = camera_with(imaging_options_xml=options)
    assert writer.declared()["wdr"].two_positions
    assert writer.read().values["wdr"] == 0.0
    assert writer.write("wdr", 1.0).accepted
    assert cam.state.imaging_writes[-1] == (
        "<tt:WideDynamicRange><tt:Mode>ON</tt:Mode></tt:WideDynamicRange>"
    )


def test_restore_returns_everything_and_reports_what_stayed(camera_with):
    cam, writer = camera_with()
    writer.declared()
    snapshot = writer.read()
    writer.write("brightness", 0.0)
    writer.write("wb_red", 100.0)
    report = writer.restore(snapshot, ["brightness", "wb_red"])
    assert report.restored and report.other == []
    assert writer.read().values == snapshot.values
    assert "<tt:WhiteBalance><tt:Mode>AUTO</tt:Mode>" in cam.state.imaging_xml
    cam.state.imaging_sticky = {"Brightness": "0"}
    writer.write("brightness", 0.0)
    left = writer.restore(snapshot, ["brightness"])
    assert left.left == [(BY_KEY["brightness"].title, "50", "0")]


def test_write_before_snapshot_is_refused(camera_with):
    cam, writer = camera_with()
    writer.declared()
    with pytest.raises(ImageWriteRefused):
        writer.write("brightness", 10.0)
    assert cam.state.imaging_writes == []


def test_exposure_changed_by_someone_else_blocks_full_block_write(camera_with):
    # снимок с AUTO, а камера перед полной записью вернула MANUAL: такой блок не уходит
    cam, writer = camera_with(imaging_needs_full_block=True)
    writer.declared()
    writer.read()
    cam.state.imaging_xml = cam.state.imaging_xml.replace(
        "<tt:Mode>AUTO</tt:Mode><tt:Min", "<tt:Mode>MANUAL</tt:Mode><tt:Min"
    )
    with pytest.raises(ImageWriteRefused, match="Exposure"):
        writer.write("contrast", 0.0)
    assert len(cam.state.imaging_writes) == 1  # только попытка одним полем


def test_full_block_with_nested_set_user_is_taboo(camera_with):
    # камера вернула в настройках чужую операцию: полный блок с ней не уходит (AD-7)
    hidden = IMAGING_FULL.replace(
        "<tt:Extension><tt:ImageStabilization>",
        '<tt:Extension><tds:SetUser xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
        "<tt:ImageStabilization>",
    )
    cam, writer = camera_with(imaging_needs_full_block=True)
    cam.state.imaging_xml = hidden
    writer.declared()
    writer.read()
    with pytest.raises(TabooViolation):
        writer.write("brightness", 10.0)
    assert len(cam.state.imaging_writes) == 1 and "SetUser" not in cam.state.imaging_writes[0]


def test_white_balance_mode_left_manual_is_not_restored(camera_with):
    cam, writer = camera_with()
    writer.declared()
    snapshot = writer.read()
    writer.write("wb_red", 100.0)
    cam.state.imaging_sticky = {"Mode": "MANUAL"}  # камера не выходит из ручных режимов
    report = writer.restore(snapshot, ["wb_red"])
    assert ("Баланс белого: красный — режим", "AUTO", "MANUAL") in report.left
    assert any(line.startswith("Exposure") for line in report.other)
