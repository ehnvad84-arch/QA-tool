"""Качество к эталону (этап 8, T7): VMAF, SSIM, PSNR одним вызовом ffmpeg, проверка лога."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from qa_tool.checks.codec import quality
from qa_tool.checks.codec.quality import (
    PSNR_CAP,
    Clip,
    FrameMismatch,
    compare,
    has_libvmaf,
    work_size,
)
from qa_tool.checks.stream.common import DecoderFailed
from qa_tool.tools import tool_path
from tests import mock_rtsp

needs_ffmpeg = pytest.mark.skipif(
    tool_path("ffmpeg") is None or mock_rtsp.find_ffmpeg() is None, reason="нет ffmpeg"
)
needs_vmaf = pytest.mark.skipif(
    tool_path("ffmpeg") is None or not has_libvmaf(tool_path("ffmpeg") or ""),
    reason="в ffmpeg нет libvmaf (он есть в ffmpeg сборки из кэша)",
)


def clip(tmp_path, kbps: int, name: str) -> Clip:
    data = mock_rtsp.codec_video("H264", kbps)
    assert data
    path = tmp_path / f"{name}.h264"
    path.write_bytes(data)
    return Clip(path, "H264", mock_rtsp.WIDTH, mock_rtsp.HEIGHT)


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


@needs_ffmpeg
@needs_vmaf
def test_same_sample_scores_near_perfect(tmp_path, work):
    ref = clip(tmp_path, 2048, "ref")
    found = compare(ref, ref, work)
    assert found.vmaf is not None and found.vmaf.mean >= 95
    assert found.ssim.mean > 0.99 and found.psnr.mean > 50
    # ролик mock короче 60 кадров: сравниваются все его кадры
    assert found.frames == mock_rtsp.CODEC_SECONDS * mock_rtsp.FPS and found.size == (320, 240)
    assert found.model == "vmaf_v0.6.1" and len(found.vmaf.values) == found.frames


@needs_ffmpeg
@needs_vmaf
def test_eighth_of_bitrate_loses_at_least_ten_vmaf(tmp_path, work):
    # неподвижная сцена 320×240 сжимается почти без потерь до 128 кбит/с: эталон — 256
    ref = clip(tmp_path, 256, "ref")
    low = compare(clip(tmp_path, 32, "low"), ref, work)
    same = compare(ref, ref, work)
    assert low.vmaf is not None and same.vmaf is not None
    assert same.vmaf.mean - low.vmaf.mean >= 10, (low.vmaf, same.vmaf)
    assert low.psnr.mean < same.psnr.mean and low.ssim.mean < same.ssim.mean


@needs_ffmpeg
def test_without_libvmaf_ssim_and_psnr_are_still_counted(tmp_path, work, monkeypatch):
    monkeypatch.setattr(quality, "has_libvmaf", lambda _ffmpeg: False)
    ref = clip(tmp_path, 2048, "ref")
    found = compare(clip(tmp_path, 256, "low"), ref, work)
    assert found.vmaf is None and found.model == ""
    assert 0 < found.ssim.mean < 1 and 0 < found.psnr.mean < PSNR_CAP
    same = compare(ref, ref, work)
    assert same.psnr.mean == PSNR_CAP  # «inf» одинаковых кадров — верх диапазона


@needs_ffmpeg
def test_ffmpeg_gets_only_files_and_writes_log_by_relative_path(tmp_path, work, monkeypatch):
    calls = []
    real = subprocess.Popen

    def spy(command, **kwargs):
        calls.append((command, kwargs))
        return real(command, **kwargs)

    monkeypatch.setattr(quality.subprocess, "Popen", spy)
    ref = clip(tmp_path, 512, "ref")
    compare(ref, ref, work)
    command, kwargs = calls[-1]
    assert kwargs["cwd"] == work
    # домашняя папка раннера Windows — runneradmin: «admin» из неё — не пароль камеры
    args = [str(arg).replace(str(Path.home()), "~") for arg in command]
    assert not any("rtsp" in arg or "admin" in arg for arg in args)
    graph = command[command.index("-filter_complex") + 1]
    assert str(work) not in graph and str(tmp_path) not in graph


@needs_ffmpeg
def test_ffmpeg_runs_below_normal_priority(tmp_path, work, monkeypatch):
    lowered = []
    monkeypatch.setattr(
        quality.os, "setpriority", lambda *args: lowered.append(args), raising=False
    )
    ref = clip(tmp_path, 512, "ref")
    compare(ref, ref, work)
    if sys.platform == "win32":
        assert subprocess.BELOW_NORMAL_PRIORITY_CLASS  # флаг процесса, setpriority нет
    else:
        assert lowered and lowered[-1][2] == quality.NICE > 0


@needs_ffmpeg
def test_too_few_frames_is_frame_mismatch(tmp_path, work):
    ref = clip(tmp_path, 512, "ref")
    with pytest.raises(FrameMismatch):
        compare(ref, ref, work, frames=5, min_frames=10)


def write_vmaf_log(folder, metrics: list[dict]) -> None:
    frames = [{"frameNum": n, "metrics": m} for n, m in enumerate(metrics)]
    (folder / "vmaf.json").write_text(json.dumps({"frames": frames}), encoding="utf-8")


@pytest.mark.parametrize(
    "metrics",
    [
        {"vmaf": 101.0, "float_ssim": 0.9, "psnr_y": 40.0},
        {"vmaf": 90.0, "float_ssim": 1.5, "psnr_y": 40.0},
        {"vmaf": "NaN", "float_ssim": 0.9, "psnr_y": 40.0},
        {"vmaf": -1, "float_ssim": 0.9, "psnr_y": 40.0},
    ],
)
def test_values_out_of_range_are_rejected(tmp_path, metrics):
    write_vmaf_log(tmp_path, [metrics] * 12)
    values = quality._vmaf_log(tmp_path / "vmaf.json")
    with pytest.raises(DecoderFailed):
        for name, series in values.items():
            quality._check_range(name, series)


def test_broken_or_huge_log_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "vmaf.json").write_text('{"frames": [{"metrics": {}}]}', encoding="utf-8")
    with pytest.raises(DecoderFailed):
        quality._vmaf_log(tmp_path / "vmaf.json")
    monkeypatch.setattr(quality, "LOG_LIMIT", 10)
    write_vmaf_log(tmp_path, [{"vmaf": 90.0, "float_ssim": 0.9, "psnr_y": 40.0}])
    with pytest.raises(DecoderFailed, match="больше"):
        quality._vmaf_log(tmp_path / "vmaf.json")
    with pytest.raises(DecoderFailed, match="не записал"):
        quality._vmaf_log(tmp_path / "missing.json")


def test_filter_stats_log_is_read_per_frame(tmp_path):
    (tmp_path / "psnr.log").write_text(
        "n:1 mse_avg:0.1 psnr_avg:inf psnr_y:inf\nn:2 mse_avg:2.0 psnr_avg:45.1 psnr_y:44.0\n",
        encoding="utf-8",
    )
    assert quality._stats_log(tmp_path / "psnr.log", "psnr_y") == [PSNR_CAP, 44.0]


@pytest.mark.parametrize(
    ("size", "fitted"),
    [((3840, 2160), (1920, 1080)), ((2560, 1920), (1440, 1080)), ((1280, 720), (1280, 720))],
)
def test_large_reference_is_fitted_into_full_hd(size, fitted):
    assert work_size(Clip(Path("ref.h264"), "H264", *size)) == fitted
