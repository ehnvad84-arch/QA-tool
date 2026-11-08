import json
import shutil
import socket
import struct
import subprocess
from pathlib import Path

import pytest

from tests.mock_camera import start_fleet
from tests.mock_rtsp import find_ffmpeg

FFMPEG = find_ffmpeg()
pytestmark = pytest.mark.skipif(
    FFMPEG is None, reason="нет ffmpeg (packaging/fetch.py ffmpeg-linux)"
)


def ffprobe() -> str:
    return str(Path(FFMPEG).with_name(Path(FFMPEG).name.replace("ffmpeg", "ffprobe")))


@pytest.fixture
def cam():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def probe_packets(url: str, seconds: int) -> list[dict]:
    out = subprocess.run(
        [
            ffprobe(),
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-read_intervals",
            f"%+{seconds}",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=seconds + 30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_replay_gives_ten_seconds_of_h264(cam):
    data = probe_packets(f"rtsp://127.0.0.1:{cam.rtsp_port}/replay/rec-1", 11)
    stream = data["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"]) == ("h264", 320, 240)
    times = [float(p["pts_time"]) for p in data["packets"] if p.get("pts_time")]
    assert times and max(times) - min(times) >= 9.5
    assert any("K" in p.get("flags", "") for p in data["packets"])


def test_udp_refused_and_metadata_stream_sends_onvif_xml(cam):
    with socket.create_connection(("127.0.0.1", cam.rtsp_port), timeout=5) as s:
        f = s.makefile("rwb")

        def ask(method, path, extra=""):
            f.write(
                f"{method} rtsp://127.0.0.1:{cam.rtsp_port}{path} RTSP/1.0\r\nCSeq: 1\r\n{extra}\r\n".encode()
            )
            f.flush()
            lines = []
            while line := f.readline().decode().strip():
                lines.append(line)
            length = next(
                (int(x.split(":")[1]) for x in lines if x.lower().startswith("content-length")), 0
            )
            return lines[0], f.read(length).decode() if length else ""

        assert "vnd.onvif.metadata" in ask("DESCRIBE", "/metadata")[1]
        assert (
            "461"
            in ask(
                "SETUP", "/metadata/track1", "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"
            )[0]
        )
        assert (
            "200"
            in ask("SETUP", "/metadata/track1", "Transport: RTP/AVP/TCP;interleaved=0-1\r\n")[0]
        )
        assert "200" in ask("PLAY", "/metadata", "Session: 1A2B3C\r\n")[0]
        head = f.read(4)
        assert head[:1] == b"$"
        packet = f.read(struct.unpack("!H", head[2:])[0])
        assert b"tt:MetadataStream" in packet[12:]


def test_ffprobe_available_for_tests():
    assert shutil.which(ffprobe()) or Path(ffprobe()).exists()
