"""Выборка кодека (этап 8, T6): поток mock-камеры по настройкам кодировщика, битрейт той же
выборки через `StreamMeter`, H.265 по RFC 7798, неисправность «битрейт мимо», уборка."""

import tempfile

import pytest

from qa_tool.checks.image import frames
from qa_tool.checks.image.frames import RtspFrameSource, Timings
from qa_tool.checks.stream.meter import StreamMeter
from qa_tool.checks.stream.params import bitrate_problem
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place
from qa_tool.core.tempfiles import CODEC_PREFIX
from qa_tool.drivers.camera.onvif_generic import EncoderSettings
from qa_tool.drivers.camera.rtsp import RtspClient
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

needs_video = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None or frames.tool_path("ffprobe") is None,
    reason="нет ffmpeg или ffprobe",
)
needs_hevc = pytest.mark.skipif(
    not mock_rtsp.hevc_available(), reason="в ffmpeg нет кодировщика H.265 (libx265, kvazaar)"
)
ONE_SAMPLE = Timings(keyframe_wait_s=12.0, clip_s=2.0, settle_frames=2, settle_max_s=0.0)
TOLERANCE_PCT = 15.0  # выборка 2 с захватывает ключевой кадр следующей группы


@pytest.fixture
def camera_with():
    started: list[MockCamera] = []

    def make(**state) -> MockCamera:
        cam = MockCamera(
            Passport("X", "M", "1", "S1"), state=OnvifState(encoder_video=True, **state)
        ).start()
        started.append(cam)
        return cam

    yield make
    for cam in started:
        cam.stop()


def measure(cam, codec: str = "H264", kbps: int = 1024) -> tuple[frames.Capture, dict, str]:
    cam.state.encoder_now["main"] = {"encoding": codec, "kbps": kbps, "constant": True}
    target = Camera(
        slot=3, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    ctx = CheckContext(target, "admin", Place.COLOR_BOX)
    source = RtspFrameSource(
        ctx, "rtsp://10.9.9.9:554/live", timings=ONE_SAMPLE, prefix=CODEC_PREFIX
    )
    meter: StreamMeter | None = None

    def count(packet) -> None:
        nonlocal meter
        meter = meter or StreamMeter(codec)
        meter.add(packet)

    capture = source.capture(on_packet=count)
    assert meter is not None
    # SDP — отдельным DESCRIBE: в обмене источника только «метод → код», без тела
    with RtspClient(source.url, "admin", "admin", timeout=10) as client:
        sdp, _tracks = client.describe()
    return capture, meter.stats(), sdp


@needs_video
@pytest.mark.parametrize("kbps", [256, 2048])
def test_bitrate_of_one_sample_follows_encoder_setting(camera_with, kbps):
    capture, stats, _sdp = measure(camera_with(), kbps=kbps)
    assert capture.codec == "h264" and (capture.width, capture.height) == (320, 240)
    assert stats["keyframes"] >= 2 and stats["seconds"] >= 1.9
    setting = EncoderSettings("H264", kbps=kbps, constant=True)
    assert bitrate_problem(setting, stats["kbps"], TOLERANCE_PCT) == "", stats


@needs_video
def test_bitrate_off_fault_is_caught(camera_with):
    _capture, stats, _sdp = measure(camera_with(encoder_kbps_factor=0.5), kbps=2048)
    setting = EncoderSettings("H264", kbps=2048, constant=True)
    assert "ниже постоянных" in bitrate_problem(setting, stats["kbps"], TOLERANCE_PCT)


@needs_video
@needs_hevc
def test_h265_stream_is_packed_by_rfc7798_and_decodes(camera_with):
    capture, stats, sdp = measure(camera_with(), codec="H265", kbps=1024)
    assert "H265/90000" in sdp and "sprop-vps=" in sdp
    assert capture.codec == "h265" and capture.work.shape[1] == 320
    setting = EncoderSettings("H265", kbps=1024, constant=True)
    assert bitrate_problem(setting, stats["kbps"], TOLERANCE_PCT) == "", stats


@needs_video
def test_flat_quality_fault_keeps_bitrate_but_not_detail(camera_with):
    honest = mock_rtsp.codec_video("H264", 2048)
    flat = mock_rtsp.codec_video("H264", 2048, quality_kbps=256)
    assert honest and flat

    def picture_bytes(stream: bytes) -> int:
        return sum(len(n) for n in mock_rtsp.split_nals(stream) if mock_rtsp.nal_type(n) != 12)

    # байтов в потоке почти поровну, но на саму картинку у «мыльного» ролика их меньше
    assert abs(len(flat) - len(honest)) < 0.15 * len(honest)
    assert picture_bytes(flat) < 0.6 * picture_bytes(honest)


@needs_video
def test_codec_sample_folder_is_removed(camera_with, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    measure(camera_with(), kbps=512)
    assert not list(tmp_path.glob(f"{CODEC_PREFIX}*"))


def test_h265_fragments_carry_type_and_start_end_bits():
    nal = bytes([19 << 1, 1]) + bytes(range(256)) * 12  # IDR_W_RADL, больше MTU
    parts = mock_rtsp.rtp_packets(nal, True, h265=True)
    assert len(parts) > 1
    headers = [p[0][:3] for p in parts]
    assert all(h[0] >> 1 & 0x3F == mock_rtsp.H265_FU for h in headers)
    assert headers[0][2] == 0x80 | 19 and headers[-1][2] == 0x40 | 19
    assert [m for _p, m in parts] == [False] * (len(parts) - 1) + [True]
    assert b"".join(p[3:] for p, _m in parts) == nal[2:]


def test_padding_reaches_target_and_keeps_frames():
    frame = b"\x00\x00\x00\x01" + bytes([9, 0xF0]) + b"\x00\x00\x00\x01" + bytes([0x65]) + b"x" * 90
    stream = frame * mock_rtsp.FPS * 2
    padded = mock_rtsp.pad_to_bitrate(stream, False, 64)
    frames_before, _ = mock_rtsp.coded_frames(stream)
    frames_after, _ = mock_rtsp.coded_frames(padded)
    assert len(frames_after) == len(frames_before)
    assert abs(len(padded) - 2 * 64_000 / 8) < 0.02 * 2 * 64_000 / 8
