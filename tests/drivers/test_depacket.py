"""Сборка RTP в Annex-B: разбор упаковок и проверка настоящим декодером."""

import base64
import json
import struct
import subprocess

import pytest

from qa_tool.drivers.camera.depacket import (
    START_CODE,
    Depacketizer,
    jpeg_size,
    parameter_sets,
)
from qa_tool.drivers.camera.rtsp import RtpPacket, RtspClient, parse_sdp
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.mock_rtsp import MockRtspServer

needs_video = pytest.mark.skipif(
    mock_rtsp.test_video() is None, reason="нет ffmpeg для тестового видео"
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_h264_single_nal():
    assert Depacketizer("H264").push(b"\x65abc") == START_CODE + b"\x65abc"


def test_h264_stap_a():
    payload = b"\x18" + struct.pack("!H", 2) + b"\x67a" + struct.pack("!H", 2) + b"\x68b"
    assert Depacketizer("H264").push(payload) == START_CODE + b"\x67a" + START_CODE + b"\x68b"


def test_h264_stap_a_with_lying_size_keeps_the_good_part():
    payload = b"\x18" + struct.pack("!H", 2) + b"\x67a" + struct.pack("!H", 900) + b"\x68b"
    assert Depacketizer("H264").push(payload) == START_CODE + b"\x67a"


def test_h264_fragments():
    d = Depacketizer("H264")
    first = d.push(bytes([0x7C, 0x85]) + b"aa")  # NRI 3, начало, тип 5
    middle = d.push(bytes([0x7C, 0x05]) + b"bb")
    last = d.push(bytes([0x7C, 0x45]) + b"cc")
    assert first + middle + last == START_CODE + b"\x65aabbcc"


def test_h264_fragment_without_start_is_dropped():
    d = Depacketizer("H264")
    assert d.push(bytes([0x7C, 0x05]) + b"bb") == b""
    assert d.push(bytes([0x7C, 0x45]) + b"cc") == b""


def test_h264_new_nal_breaks_unfinished_fragment():
    d = Depacketizer("H264")
    d.push(bytes([0x7C, 0x85]) + b"aa")
    d.push(b"\x41x")
    assert d.push(bytes([0x7C, 0x45]) + b"cc") == b""


@pytest.mark.parametrize("payload", [b"", b"\x7c", b"\x00abc", b"\x1eabc"])
def test_h264_rubbish_is_dropped(payload):
    assert Depacketizer("H264").push(payload) == b""


def test_h265_single_nal():
    nal = bytes([0x26, 0x01]) + b"abc"  # тип 19, IDR
    assert Depacketizer("H265").push(nal) == START_CODE + nal


def test_h265_aggregation():
    vps, sps = bytes([0x40, 0x01, 0xAA]), bytes([0x42, 0x01, 0xBB])
    payload = bytes([0x60, 0x01]) + struct.pack("!H", 3) + vps + struct.pack("!H", 3) + sps
    assert Depacketizer("HEVC").push(payload) == START_CODE + vps + START_CODE + sps


def test_h265_fragments():
    d = Depacketizer("H265")
    head = bytes([0x62, 0x01])  # тип 49
    first = d.push(head + bytes([0x80 | 19]) + b"aa")
    last = d.push(head + bytes([0x40 | 19]) + b"bb")
    assert first + last == START_CODE + bytes([19 << 1, 0x01]) + b"aabb"


def test_h265_paci_is_dropped():
    assert Depacketizer("H265").push(bytes([50 << 1, 0x01, 0x00])) == b""


def test_parameter_sets_h264():
    fmtp = f"packetization-mode=1;sprop-parameter-sets={b64(b'sps')},{b64(b'pps')}"
    assert parameter_sets(fmtp) == [b"sps", b"pps"]


def test_parameter_sets_h265_in_decoder_order():
    fmtp = f"sprop-pps={b64(b'pps')}; sprop-sps={b64(b'sps')}; sprop-vps={b64(b'vps')}"
    assert parameter_sets(fmtp) == [b"vps", b"sps", b"pps"]


def test_parameter_sets_without_padding_and_broken():
    fmtp = "sprop-parameter-sets=c3Bz,@@@"
    assert parameter_sets(fmtp) == [b"sps"]


def test_start_puts_sets_first():
    assert Depacketizer("H264", [b"a", b"b"]).start() == START_CODE + b"a" + START_CODE + b"b"


def test_jpeg_size():
    assert jpeg_size(bytes([0, 0, 0, 0, 1, 80, 240, 135])) == (1920, 1080)
    assert jpeg_size(b"\x00") is None


def test_sdp_keeps_fmtp_of_its_track():
    sdp = (
        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
        "a=fmtp:96 packetization-mode=1;sprop-parameter-sets=c3Bz\r\na=control:t1\r\n"
    )
    assert parse_sdp(sdp, "rtsp://h/")[0].fmtp == "packetization-mode=1;sprop-parameter-sets=c3Bz"


def capture(server: MockRtspServer, seconds: float = 2.5) -> bytes:
    client = RtspClient(f"rtsp://127.0.0.1:{server.port}/live", timeout=3)
    with client:
        _, tracks = client.describe()
        track = next(t for t in tracks if t.media == "video")
        client.setup(track)
        client.play()
        d = Depacketizer(track.encoding, parameter_sets(track.fmtp))
        out = bytearray(d.start())
        whole = [len(out)]

        def keep(packet: RtpPacket) -> None:
            out.extend(d.push(packet.payload))
            if packet.marker:
                whole[0] = len(out)

        client.watch(keep, seconds)
    # срок обрывает чтение посреди кадра: хвост декодер счёл бы ошибкой
    return bytes(out[: whole[0]])


def decode(tmp_path, data: bytes) -> tuple[str, dict]:
    sample = tmp_path / "sample.h264"
    sample.write_bytes(data)
    done = subprocess.run(
        [tool_path("ffmpeg"), "-v", "error", "-i", str(sample), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    probe = subprocess.run(
        [
            tool_path("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name",
            "-of",
            "json",
            str(sample),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stderr, json.loads(probe.stdout)["streams"][0]


@needs_video
def test_assembled_stream_decodes(tmp_path):
    server = MockRtspServer(metadata=False).start()
    try:
        data = capture(server)
    finally:
        server.stop()
    errors, stream = decode(tmp_path, data)
    assert errors == ""
    assert (stream["width"], stream["height"]) == (mock_rtsp.WIDTH, mock_rtsp.HEIGHT)
    assert stream["codec_name"] == "h264"


@needs_video
def test_sets_only_in_sdp_still_decode(tmp_path):
    server = MockRtspServer(metadata=False, sets_in_stream=False).start()
    try:
        data = capture(server)
    finally:
        server.stop()
    errors, stream = decode(tmp_path, data)
    assert errors == ""
    assert stream["width"] == mock_rtsp.WIDTH
