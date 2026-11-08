"""Качество выборки к эталону: VMAF, SSIM и PSNR одним вызовом ffmpeg (codec-quality, S1–S3, S7).

Обе выборки — временные файлы `RtspFrameSource` (Annex-B от ключевого кадра); ffmpeg получает
только их, адреса камеры и пароля в его аргументах нет. Кадры сводятся по порядку, без
покадрового совмещения: сцена бокса неподвижна, а группа кадров при смене битрейта не меняется,
поэтому кадры I и P стоят на одних местах. Ступень приводится к размеру эталона; больше
1920×1080 — оба к 1920×1080, иначе расчёт 4K на восьми камерах разом не уложится в фазу.

VMAF считает фильтр libvmaf (модель vmaf_v0.6.1; он же даёт PSNR и SSIM). В ffmpeg без libvmaf
(системный на сервере разработки) — фильтры `ssim` и `psnr`, VMAF нет: строки кривых тогда
«ошибка стенда». Лог ffmpeg — недоверенные данные: размер ограничен, числа проверяются.
"""

import functools
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.checks.stream.common import CODEC_FORMATS, DecoderFailed, ToolMissing
from qa_tool.tools import tool_path

VMAF_MODEL = "vmaf_v0.6.1"
FRAMES = 60  # кадров в расчёте: 2–6 с потока, меньше — VMAF скачет от шума сцены
MIN_FRAMES = 10
MAX_SIZE = (1920, 1080)
LOG_LIMIT = 4 << 20  # лог libvmaf на 60 кадров — десятки КБ; больше — что-то не то
QUALITY_TIMEOUT_S = 180.0  # 60 кадров 4K двумя нитями на медленном стенде
# PSNR одинаковых кадров бесконечен; фильтр `psnr` пишет «inf», а libvmaf обрезает по 8-битному
# пределу. Берём верх допустимого диапазона, чтобы одинаковые кадры не стали «ошибкой стенда».
PSNR_CAP = 100.0
RANGES = {"vmaf": (0.0, 100.0), "ssim": (0.0, 1.0), "psnr": (0.0, PSNR_CAP)}


class FrameMismatch(RuntimeError):
    """Кадров для сравнения меньше `min_frames` — «ошибка стенда»."""


@dataclass(frozen=True)
class Clip:
    """Выборка потока на диске: файл Annex-B, кодек из SDP и родной размер кадра."""

    path: Path
    codec: str
    width: int
    height: int


@dataclass(frozen=True)
class Stat:
    """Величина по кадрам: среднее, СКО (по совокупности), минимум и сами значения."""

    mean: float
    sd: float
    low: float
    values: tuple[float, ...] = field(repr=False)

    @classmethod
    def of(cls, values: list[float]) -> "Stat":
        """Сводка по кадрам; список не пуст."""
        mean = sum(values) / len(values)
        sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        return cls(round(mean, 3), round(sd, 3), round(min(values), 3), tuple(values))


@dataclass(frozen=True)
class Quality:
    """Итог сравнения выборки с эталоном. `vmaf` — None, если в ffmpeg нет libvmaf."""

    vmaf: Stat | None
    ssim: Stat
    psnr: Stat
    frames: int
    size: tuple[int, int]
    model: str


@functools.cache
def has_libvmaf(ffmpeg: str) -> bool:
    """Есть ли в этом ffmpeg фильтр libvmaf; спрашивается раз на процесс."""
    listed = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return " libvmaf " in listed


def work_size(ref: Clip) -> tuple[int, int]:
    """Размер расчёта: эталон как есть, больше 1920×1080 — вписан в него с чётными сторонами."""
    if ref.width * ref.height <= MAX_SIZE[0] * MAX_SIZE[1]:
        return ref.width, ref.height
    scale = min(MAX_SIZE[0] / ref.width, MAX_SIZE[1] / ref.height)
    return max(2, round(ref.width * scale / 2) * 2), max(2, round(ref.height * scale / 2) * 2)


def _inputs(clip: Clip) -> list[str]:
    form = CODEC_FORMATS.get(clip.codec.upper())
    if form is None:
        raise DecoderFailed(f"кодек {clip.codec} стенд не сравнивает")
    return ["-f", form, "-i", str(clip.path.resolve())]


def _graph(dist: Clip, ref: Clip, frames: int, size: tuple[int, int]) -> str:
    """Оба входа: номера кадров вместо меток, первые `frames`, yuv420p, один размер."""
    width, height = size
    shrink = size != (ref.width, ref.height)
    flags = "area" if shrink else "bicubic"
    common = f"setpts=N,trim=end_frame={frames},format=yuv420p"
    graph = f"[0:v]{common},scale={width}:{height}:flags={flags}[d];"
    graph += f"[1:v]{common}" + (f",scale={width}:{height}:flags=area" if shrink else "") + "[r]"
    return graph


def compare(
    dist: Clip, ref: Clip, folder: Path, frames: int = FRAMES, min_frames: int = MIN_FRAMES
) -> Quality:
    """Сравнить выборку ступени с эталоном.

    Args:
        dist: выборка ступени («искажённая»).
        ref: выборка эталона.
        folder: временная папка: в ней ffmpeg пишет лог по относительному пути (двоеточие
            пути Windows ломает граф фильтров).
        frames: сколько первых кадров сравнивать.
        min_frames: меньше кадров вышло — `FrameMismatch`.

    Returns:
        Величины по кадрам и размер расчёта.

    Raises:
        ToolMissing: нет ffmpeg.
        DecoderFailed: ffmpeg упал, не уложился в срок или его лог негоден.
        FrameMismatch: кадров меньше `min_frames`.
    """
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        raise ToolMissing("нет ffmpeg")
    size = work_size(ref)
    graph = _graph(dist, ref, frames, size)
    vmaf = has_libvmaf(ffmpeg)
    if vmaf:
        graph += (
            f";[d][r]libvmaf=model=version={VMAF_MODEL}:feature=name=psnr|name=float_ssim"
            ":log_fmt=json:log_path=vmaf.json:n_threads=2:shortest=1"
        )
        outputs = ["-f", "null", "-"]
    else:
        graph += (
            ";[d]split[d1][d2];[r]split[r1][r2]"
            ";[d1][r1]ssim=stats_file=ssim.log[o1];[d2][r2]psnr=stats_file=psnr.log[o2]"
        )
        outputs = ["-map", "[o1]", "-f", "null", "-", "-map", "[o2]", "-f", "null", "-"]
    command = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", *_inputs(dist), *_inputs(ref)]
    command += ["-filter_complex", graph, *outputs]
    try:
        done = subprocess.run(
            command,
            cwd=folder,
            capture_output=True,
            timeout=QUALITY_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as e:
        raise DecoderFailed(f"расчёт качества не уложился в {QUALITY_TIMEOUT_S:g} с") from e
    if done.returncode:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        raise DecoderFailed(f"ffmpeg не посчитал качество (код {done.returncode}): {tail}")
    if vmaf:
        values = _vmaf_log(folder / "vmaf.json")
    else:
        values = {
            "ssim": _stats_log(folder / "ssim.log", "Y"),
            "psnr": _stats_log(folder / "psnr.log", "psnr_y"),
        }
    counts = {len(v) for v in values.values()}
    count = min(counts) if counts else 0
    if count < min_frames:
        raise FrameMismatch(f"сравнено кадров: {count}, нужно не меньше {min_frames}")
    for name, series in values.items():
        _check_range(name, series)
    return Quality(
        vmaf=Stat.of(values["vmaf"][:count]) if vmaf else None,
        ssim=Stat.of(values["ssim"][:count]),
        psnr=Stat.of(values["psnr"][:count]),
        frames=count,
        size=size,
        model=VMAF_MODEL if vmaf else "",
    )


def _read_log(path: Path) -> str:
    try:
        if path.stat().st_size > LOG_LIMIT:
            raise DecoderFailed(f"лог расчёта качества больше {LOG_LIMIT >> 20} МБ")
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DecoderFailed("ffmpeg не записал лог расчёта качества") from e


def _number(value: str | float) -> float:
    """Число из лога; «inf» PSNR — одинаковые кадры, остальное нечисловое — негодный лог."""
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise DecoderFailed(f"в логе расчёта не число: {str(value)[:40]!r}") from e
    if number == math.inf:
        return PSNR_CAP
    return number


def _vmaf_log(path: Path) -> dict[str, list[float]]:
    """Покадровые VMAF, SSIM (яркость) и PSNR (яркость) из JSON-лога libvmaf."""
    try:
        frames = json.loads(_read_log(path))["frames"]
        metrics = [frame["metrics"] for frame in frames]
        return {
            "vmaf": [_number(m["vmaf"]) for m in metrics],
            "ssim": [_number(m["float_ssim"]) for m in metrics],
            "psnr": [min(_number(m["psnr_y"]), PSNR_CAP) for m in metrics],
        }
    except (ValueError, KeyError, TypeError) as e:
        raise DecoderFailed(f"лог libvmaf не разобран: {type(e).__name__}") from e


def _stats_log(path: Path, key: str) -> list[float]:
    """Покадровые значения `key` из файла статистики фильтра `ssim` или `psnr`."""
    values = []
    for line in _read_log(path).splitlines():
        fields = dict(part.split(":", 1) for part in line.split() if ":" in part)
        if key in fields:
            values.append(min(_number(fields[key]), PSNR_CAP))
    return values


def _check_range(name: str, values: list[float]) -> None:
    low, high = RANGES[name]
    for value in values:
        if not math.isfinite(value) or not low <= value <= high:
            raise DecoderFailed(f"{name} вне диапазона {low:g}–{high:g}: {value!r}")
