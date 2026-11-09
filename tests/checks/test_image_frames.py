"""Кадр для проверки картинки (этап 7, T5): своя RTSP-сессия, стабильность, превью, уборка."""

import io
import subprocess
import tempfile
from pathlib import Path

import pytest

from qa_tool.checks.image import frames
from qa_tool.checks.image.frames import (
    FrameTooLarge,
    RtspFrameSource,
    StreamRefused,
    StreamUnavailable,
    luma,
    read_ppm,
)
from qa_tool.checks.rtsp_refusal import MEMORY_KEY
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place, Threshold
from qa_tool.core.tempfiles import IMAGE_PREFIX
from tests import mock_rtsp
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import IMAGING_FULL, OnvifState

needs_video = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None or frames.tool_path("ffprobe") is None,
    reason="нет ffmpeg или ffprobe",
)


@pytest.fixture
def camera():
    cam = MockCamera(
        Passport("X", "M", "1", "S1"),
        state=OnvifState(imaging_xml=IMAGING_FULL, imaging_video=True),
    ).start()
    yield cam
    cam.stop()


def context(cam, **thresholds) -> CheckContext:
    target = Camera(
        slot=1, address="127.0.0.1", port=cam.port, rtsp_port=cam.rtsp_port, username="admin"
    )
    limits = {f"image.{k}": Threshold(v, "с", "тест") for k, v in thresholds.items()}
    return CheckContext(target, "admin", Place.COLOR_BOX, thresholds=limits)


def live(cam) -> str:
    return "rtsp://10.9.9.9:554/live"  # хост и порт из ответа камеры подменяются стендом


@needs_video
def test_brightness_of_mock_changes_mean_luma_of_frame(camera):
    source = RtspFrameSource(context(camera), live(camera))
    dark = source.capture()
    camera.state.imaging_xml = IMAGING_FULL.replace(
        "<tt:Brightness>50</tt:Brightness>", "<tt:Brightness>100</tt:Brightness>"
    )
    bright = source.capture()
    assert luma(bright.work).mean() > luma(dark.work).mean() + 10
    assert dark.stable and dark.width == mock_rtsp.WIDTH and dark.codec == "h264"
    assert dark.work.shape == (mock_rtsp.HEIGHT, mock_rtsp.WIDTH, 3)
    assert len(dark.recent) == frames.RING and dark.clip is None
    assert dark.preview_jpeg[:2] == b"\xff\xd8" and dark.thumb_jpeg[:2] == b"\xff\xd8"
    assert any(line.startswith("PLAY") for line in source.exchanges)


@needs_video
def test_clip_is_kept_only_when_asked(camera):
    capture = RtspFrameSource(context(camera), live(camera)).capture(keep_clip=True)
    assert capture.clip and capture.clip.startswith((b"\x00\x00\x00\x01", b"\x00\x00\x01"))


@needs_video
def test_decoder_gets_only_a_temp_file_and_folder_is_removed(camera, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    commands = []
    real_popen, real_run = subprocess.Popen, subprocess.run

    def popen(command, *args, **kwargs):
        commands.append(command)
        return real_popen(command, *args, **kwargs)

    def run(command, *args, **kwargs):
        commands.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(frames.subprocess, "Popen", popen)
    monkeypatch.setattr(frames.subprocess, "run", run)
    RtspFrameSource(context(camera), live(camera)).capture()
    assert commands
    for command in commands:
        # пользователь раннера Windows — runneradmin: «admin» в домашней и временной папке
        # (pytest-of-runneradmin) — не пароль камеры
        joined = " ".join(map(str, command)).replace(str(tmp_path), "<tmp>")
        joined = joined.replace(str(Path.home()), "~")
        assert "rtsp://" not in joined and "admin" not in joined and "127.0.0.1" not in joined
    assert not list(tmp_path.glob(f"{IMAGE_PREFIX}*"))


def test_huge_frame_is_rejected_before_reading_it():
    stream = io.BytesIO(b"P6\n100000 100000\n255\n" + b"\0" * 16)
    with pytest.raises(FrameTooLarge):
        read_ppm(stream, 1280 * 2000 * 3)


def test_ppm_stream_end_is_none_and_garbage_is_decoder_failure():
    assert read_ppm(io.BytesIO(b""), 100) is None
    one = read_ppm(io.BytesIO(b"P6\n2 1\n255\n" + bytes(range(6))), 100)
    assert one.shape == (1, 2, 3) and one[0, 1].tolist() == [3, 4, 5]
    with pytest.raises(frames.DecoderFailed):
        read_ppm(io.BytesIO(b"GIF89a 1 1 255\n"), 100)


@needs_video
def test_no_keyframe_within_limit_is_stream_unavailable(camera):
    camera.rtsp.silent = True
    source = RtspFrameSource(context(camera, keyframe_wait_s=1.0), live(camera))
    with pytest.raises(StreamUnavailable, match="ключевого кадра"):
        source.capture()


@needs_video
def test_refused_password_earlier_means_no_new_attempt(camera):
    ctx = context(camera)
    ctx.extra[MEMORY_KEY] = "камера не приняла логин и пароль для видео: 401"
    with pytest.raises(StreamRefused):
        RtspFrameSource(ctx, live(camera)).capture()
    assert camera.rtsp.requests == []
