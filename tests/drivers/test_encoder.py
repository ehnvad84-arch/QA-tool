"""Драйвер кодировщиков (T4): Media2 и запасной Media1, белый список, Fault, «не та запись»."""

import copy

import pytest

from qa_tool.core.models import Camera
from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera.encoder import (
    CEILING,
    CONSTANT,
    BadRange,
    CodecOptions,
    EncoderWriter,
    EncoderWriteRefused,
    Unsupported,
    ValueRejected,
    choose_profile,
    mismatch,
)
from qa_tool.drivers.camera.onvif_generic import EncoderSettings, service_urls
from qa_tool.drivers.camera.soap import SoapClient
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState


@pytest.fixture
def camera_with():
    started = []

    def start(**state):
        cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(**state)).start()
        started.append(cam)
        camera = Camera(slot=1, address="127.0.0.1", port=cam.port, username="admin")
        soap = SoapClient("admin", "admin", timeout=5)
        pauses = []
        writer = EncoderWriter(soap, service_urls(soap, camera), sleep=pauses.append)
        writer.pauses = pauses
        return cam, writer

    yield start
    for cam in started:
        cam.stop()


TWO = (
    MockStream("main", kbps=4000, width=1920, height=1080, fps=25, gop=50),
    MockStream("sub", path="/sub", kbps=512, width=640, height=360, fps=25, gop=50),
)


def test_configs_read_every_encoder_from_media2_with_uri_and_snapshot(camera_with):
    _, writer = camera_with(streams=TWO)
    configs = writer.configs()
    assert [c.token for c in configs] == ["ve-main", "ve-sub"]
    main = configs[0]
    assert main.service == "media2" and main.profiles == (("main", "Основной"),)
    assert main.settings.encoding == "H264" and main.settings.kbps == 4000
    assert (main.settings.width, main.settings.height, main.settings.gop) == (1920, 1080, 50)
    assert main.codec_profile == "Main" and main.quality == 4
    assert main.uri.endswith("/live") and configs[1].uri.endswith("/sub")
    assert main.xml.startswith("<tr2:Configurations") and 'token="ve-main"' in main.xml
    assert set(writer.snapshots) == {"ve-main", "ve-sub"}


def test_options_of_both_codecs(camera_with):
    _, writer = camera_with(streams=TWO)
    writer.configs()
    options = writer.options("ve-main")
    h264, h265 = options["H264"], options["H265"]
    assert isinstance(h264, CodecOptions) and isinstance(h265, CodecOptions)
    assert (h265.kbps_min, h265.kbps_max) == (256, 8192)
    assert h265.profiles == ("Main", "Main10") and h265.cbr and h265.gov_range == (1, 100)
    assert (1920, 1080) in h265.resolutions and 25 in h265.fps
    assert mismatch(writer.snapshots["ve-main"].settings, h265) == ""


def test_constant_bitrate_write_is_read_back_and_keeps_everything_else(camera_with):
    cam, writer = camera_with(streams=TWO)
    writer.configs()
    report = writer.write("ve-main", "H264", 2048, constant=True)
    assert report.accepted and report.form == CONSTANT and report.note == ""
    assert report.read_back.settings.kbps == 2048 and report.read_back.settings.constant
    sent = cam.state.encoder_writes[-1]
    assert "<tt:BitrateLimit>2048</tt:BitrateLimit>" in sent
    assert "<tt:Width>1920</tt:Width>" in sent and 'GovLength="50"' in sent
    assert cam.state.stream_restarts == 0 and writer.pauses == []


def test_codec_change_keeps_profile_main_and_restarts_stream(camera_with):
    cam, writer = camera_with(streams=TWO)
    writer.configs()
    report = writer.write("ve-main", "H265", 1024)
    assert report.accepted and report.read_back.settings.encoding == "H265"
    assert report.read_back.codec_profile == "Main"
    assert cam.state.stream_restarts == 1


def test_codec_only_in_main10_is_written_with_a_note(camera_with):
    options = (
        '<tr2:Options ProfilesSupported="Main10" ConstantBitRateSupported="true">'
        "<tt:Encoding>H265</tt:Encoding><tt:BitrateRange><tt:Min>256</tt:Min><tt:Max>8192</tt:Max>"
        "</tt:BitrateRange></tr2:Options>"
        '<tr2:Options ProfilesSupported="Main High"><tt:Encoding>H264</tt:Encoding>'
        "<tt:BitrateRange><tt:Min>256</tt:Min><tt:Max>8192</tt:Max></tt:BitrateRange></tr2:Options>"
    )
    _, writer = camera_with(encoder_options_xml=options)
    writer.configs()
    report = writer.write("ve-main", "H265", 1024)
    assert report.read_back.codec_profile == "Main10" and "Main10" in report.note


def test_profile_choice():
    assert choose_profile("High", ("Main", "High")) == ("High", "")
    assert choose_profile("High", ("Main", "Main10")) == ("Main", "")
    assert choose_profile("High", ("Main10", "Main12")) == ("Main12", "")
    assert choose_profile("High", ("Main10",))[0] == "Main10"


def test_codec_that_does_not_fit_current_resolution_fps_gop_is_named():
    settings = EncoderSettings("H264", 2560, 1440, 30.0, 4000, True, 200)
    options = CodecOptions("H265", 256, 8192, ((1920, 1080),), (25.0,), (1, 100), ("Main",), True)
    assert mismatch(settings, options) == "при текущих разрешении, частоте, GOP камера его не даёт"


def test_camera_fault_is_value_rejected(camera_with):
    _, writer = camera_with(encoder_reject=True)
    writer.configs()
    with pytest.raises(ValueRejected):
        writer.write("ve-main", "H264", 1024)


def test_camera_keeping_its_own_bitrate_is_not_accepted_after_three_reads(camera_with):
    _, writer = camera_with(encoder_sticky_kbps=3000)
    writer.configs()
    report = writer.write("ve-main", "H264", 1024)
    assert not report.accepted and report.read_back.settings.kbps == 3000
    assert writer.pauses == [1.0, 1.0]


@pytest.mark.parametrize(
    ("codec", "kbps", "why"),
    [("H264", 100_000, "вне диапазона"), ("MJPEG", 1024, "не объявляет")],
)
def test_bad_requests_are_refused_before_network(camera_with, codec, kbps, why):
    cam, writer = camera_with()
    writer.configs()
    with pytest.raises(EncoderWriteRefused, match=why):
        writer.write("ve-main", codec, kbps)
    assert cam.state.encoder_writes == []


def test_write_before_snapshot_is_refused(camera_with):
    cam, writer = camera_with()
    with pytest.raises(EncoderWriteRefused, match="нет снимка"):
        writer.write("ve-main", "H264", 1024)
    assert cam.state.encoder_writes == []


def test_block_changing_whitelisted_field_is_never_sent(camera_with):
    cam, writer = camera_with(encoder_multicast=True)
    base = writer.configs()[0]
    for change in ("Width", "Port", "FrameRateLimit", "Quality"):
        block = copy.deepcopy(base.element)
        next(e for e in block.iter() if e.tag.endswith("}" + change)).text = "7"
        with pytest.raises(EncoderWriteRefused, match="отличается от снимка"):
            writer._guard(block, base)
    block = copy.deepcopy(base.element)
    block.set("GovLength", "1")
    with pytest.raises(EncoderWriteRefused, match="GOP"):
        writer._guard(block, base)
    assert cam.state.encoder_writes == []


def test_multicast_goes_exactly_as_read(camera_with):
    cam, writer = camera_with(encoder_multicast=True)
    writer.configs()
    assert writer.write("ve-main", "H264", 2048).accepted
    assert "<tt:IPv4Address>239.0.0.1</tt:IPv4Address>" in cam.state.encoder_writes[-1]


def test_restore_returns_codec_and_bitrate_and_reports_what_stayed(camera_with):
    _, writer = camera_with()
    base = writer.configs()[0]
    writer.write("ve-main", "H265", 1024)
    report = writer.restore(base)
    assert report.restored and report.other == []
    assert writer.read("ve-main").settings == base.settings


def test_restore_that_camera_does_not_take_is_left(camera_with):
    cam, writer = camera_with()
    base = writer.configs()[0]
    cam.state.encoder_sticky_kbps = 1500
    writer.write("ve-main", "H264", 1024)
    report = writer.restore(base)
    assert not report.restored and ("битрейт, кбит/с", "4000", "1500") in report.left


def test_media1_writes_only_bitrate_as_ceiling_and_persists_on_restore(camera_with):
    cam, writer = camera_with(media2_absent=True)
    base = writer.configs()[0]
    assert base.service == "media" and base.codec_profile == "Main"
    options = writer.options("ve-main")
    assert isinstance(options["H264"], CodecOptions) and isinstance(options["H265"], Unsupported)
    report = writer.write("ve-main", "H264", 1024)
    assert report.accepted and report.form == CEILING
    assert cam.state.encoder_persist == ["false"]
    with pytest.raises(EncoderWriteRefused, match="Media1"):
        writer.write("ve-main", "H265", 1024)
    assert writer.restore(base).restored and cam.state.encoder_persist[-1] == "true"


@pytest.mark.parametrize(
    ("low", "high", "kind"),
    [("9000", "100", BadRange), ("abc", "100", BadRange), ("500", "500", Unsupported)],
)
def test_broken_bitrate_ranges(camera_with, low, high, kind):
    options = (
        "<tr2:Options><tt:Encoding>H264</tt:Encoding><tt:BitrateRange>"
        f"<tt:Min>{low}</tt:Min><tt:Max>{high}</tt:Max></tt:BitrateRange></tr2:Options>"
    )
    cam, writer = camera_with(encoder_options_xml=options)
    writer.configs()
    assert isinstance(writer.options("ve-main")["H264"], kind)
    with pytest.raises(EncoderWriteRefused):
        writer.write("ve-main", "H264", 1024)
    assert cam.state.encoder_writes == []


def test_restore_xml_from_journal(camera_with):
    cam, writer = camera_with()
    saved = writer.configs()[0].xml
    writer.write("ve-main", "H265", 1024)
    fresh = EncoderWriter(writer.soap, writer.services)
    report = fresh.restore_xml("media2", "ve-main", saved)
    assert report.restored and fresh.read("ve-main").settings.encoding == "H264"


@pytest.mark.parametrize(
    ("xml", "error"),
    [
        ('<tr2:Configurations xmlns:tr2="x" token="other"/>', EncoderWriteRefused),
        ('<tt:Name xmlns:tt="x" token="ve-main"/>', EncoderWriteRefused),
        ("<oops", ValueError),
    ],
)
def test_restore_xml_of_something_else_is_not_sent(camera_with, xml, error):
    cam, writer = camera_with()
    writer.configs()
    with pytest.raises(error):
        writer.restore_xml("media2", "ve-main", xml)
    assert cam.state.encoder_writes == []


def test_restore_xml_with_nested_set_user_is_taboo(camera_with):
    cam, writer = camera_with()
    saved = writer.configs()[0].xml
    hidden = saved.replace(
        "<tt:Quality>",
        '<tds:SetUser xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/><tt:Quality>',
    )
    with pytest.raises(TabooViolation):
        writer.restore_xml("media2", "ve-main", hidden)
    assert cam.state.encoder_writes == []
