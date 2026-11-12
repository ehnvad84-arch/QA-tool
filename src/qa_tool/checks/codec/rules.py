"""Пороги и итоги проверки кодеков (спека codec-quality, «Правило итогов», S2–S4, S7).

Строка протокола — кодировщик × кодек; кривая — ступени битрейта с качеством к эталону. Пороги —
в `thresholds.yaml` (ключи `codec.*`), иначе стартовые с источником: изменённый порог меняет итог
без правки кода. Порог VMAF зависит от кодека и класса разрешения и действует только на ступенях
не ниже `kbps_floor`: на минимуме камеры низкий VMAF — физика сжатия, а не дефект.

Порядок итога: сцена менялась → «ошибка стенда» (величины всё равно в строке); смена кодека не
принята → «не прошла»; на ступенях — не принято, поток не вернулся, битрейт мимо, ниже порога,
не растёт → «не прошла» с причинами по ступеням; кадров нет или в ffmpeg нет VMAF → «ошибка
стенда»; иначе «прошла».
"""

import math
from dataclasses import dataclass, field

from qa_tool.checks.codec import ladder
from qa_tool.checks.codec.quality import Quality, Stat
from qa_tool.checks.stream.params import bitrate_problem
from qa_tool.core.checks import CheckContext, Severity, Status
from qa_tool.core.models import Threshold
from qa_tool.drivers.camera.onvif_generic import EncoderSettings

START = ladder.START
CODECS = ("h264", "h265")
# Классы порога VMAF по высоте кадра: (имя, наименьшая высота). Их четыре, у сетки ступеней пять:
# 4 Мп и 8 Мп держат один порог, пока нет замеров эталонных камер.
VMAF_CLASSES = (("uhd", 1440), ("fhd", 1080), ("hd", 720), ("sd", 0))
FLOORS = {
    "h264": {"sd": 512, "hd": 1024, "fhd": 2048, "uhd": 6144},
    "h265": {"sd": 256, "hd": 512, "fhd": 1024, "uhd": 3072},
}
DEFAULTS: dict[str, Threshold] = {
    "codec.apply_delay_s": Threshold(3.0, "с", START),
    "codec.restart_wait_s": Threshold(20.0, "с", START),
    "codec.keyframe_wait_s": Threshold(12.0, "с", START),
    "codec.clip_s": Threshold(6.0, "с", START),
    "codec.clip_max_s": Threshold(12.0, "с", START),
    "codec.frames": Threshold(60.0, "кадр", START),
    "codec.min_frames": Threshold(30.0, "кадр", START),
    "codec.bitrate_pct": Threshold(20.0, "%", "как stream.bitrate_pct этапа 4"),
    "codec.vmaf_spread": Threshold(2.0, "VMAF", START),
    "codec.repeat_vmaf_min": Threshold(90.0, "VMAF", START),
    **{
        f"codec.{codec}.{name}.vmaf_min": Threshold(70.0, "VMAF", START)
        for codec in CODECS
        for name, _lowest in VMAF_CLASSES
    },
    **{
        f"codec.{codec}.{name}.kbps_floor": Threshold(float(kbps), "кбит/с", START)
        for codec, floors in FLOORS.items()
        for name, kbps in floors.items()
    },
}
NO_VMAF = "в ffmpeg стенда нет VMAF (libvmaf): переустановите программу"


def limit(ctx: CheckContext, key: str) -> Threshold:
    """Порог `codec.*` из настроек стенда, иначе стартовый (ключи ступеней — в `ladder`)."""
    if key in ladder.DEFAULTS:
        return ladder.limit(ctx, key)
    return ctx.thresholds.get(key, DEFAULTS[key])


def vmaf_class(height: int) -> str:
    """Класс порога VMAF по высоте кадра."""
    return next(name for name, lowest in VMAF_CLASSES if height >= lowest)


def codec_key(codec: str) -> str:
    """Ключ кодека в порогах: H264 → h264, H265 и HEVC → h265."""
    return "h265" if codec.upper() in ("H265", "HEVC") else "h264"


def num(value: float) -> str:
    """Число для протокола ОТК: одна цифра после запятой, десятичная запятая."""
    return f"{value:.1f}".replace(".", ",")


def rate(kbps: float) -> str:
    """Битрейт словами протокола: до 512 — «256 кбит/с», дальше — «0,5 Мбит/с», «3,1 Мбит/с»."""
    if kbps < 512:
        return f"{round(kbps)} кбит/с"
    return f"{round(kbps / 1024, 2):g} Мбит/с".replace(".", ",")


def codec_title(codec: str) -> str:
    """H264 → H.264, H265 → H.265 — как кодеки пишет ОТК."""
    return "H.265" if codec_key(codec) == "h265" else "H.264"


@dataclass
class Point:
    """Ступень кривой: что задали, что камера дала и как это сравнилось с эталоном.

    `refused` — камера не приняла значение (Fault или чтение назад другое), `no_stream` — поток
    не вернулся после записи; в обоих случаях замера нет.
    """

    kbps: int
    measured_kbps: float | None = None
    quality: Quality | None = None
    refused: str = ""
    no_stream: str = ""


@dataclass
class Curve:
    """Кривая кодека одного кодировщика.

    Args (поля):
        codec: H264 или H265.
        width, height: разрешение профиля, пикселей.
        constant: битрейт постоянный (цель) или переменный (потолок); None — камера не сказала.
        points: ступени в любом порядке.
        switch_refused: камера не приняла смену кодека — ступеней нет.
        scene: сцена менялась (текст `scene_problem`); пусто — сцена держалась.
    """

    codec: str
    width: int
    height: int
    constant: bool | None = True
    points: list[Point] = field(default_factory=list)
    switch_refused: str = ""
    scene: str = ""


@dataclass(frozen=True)
class Verdict:
    """Итог строки: статус, причина, порог строки и причины по ступеням для шагов S3."""

    status: Status
    reason: str
    threshold: Threshold
    problems: list[str] = field(default_factory=list)
    severity: Severity | None = None


def rules_problem(ctx: CheckContext) -> str:
    """Негодные пороги кодеков; пусто — годные. Тогда все строки — «ошибка стенда»."""
    bad = []
    if limit(ctx, "codec.vmaf_spread").value <= 0:
        bad.append("codec.vmaf_spread должен быть больше 0")
    floors = [k for k in DEFAULTS if k.endswith(".kbps_floor") and limit(ctx, k).value <= 0]
    bad += [f"{k} должен быть больше 0" for k in floors]
    if limit(ctx, "codec.frames").value < limit(ctx, "codec.min_frames").value:
        bad.append("codec.frames меньше codec.min_frames")
    return "; ".join(bad)


def scene_problem(ctx: CheckContext, repeat: Quality | None, moved: str = "") -> str:
    """Сцена менялась между эталоном в начале и в конце кодировщика (S7); пусто — держалась.

    Args:
        ctx: контекст (порог `codec.repeat_vmaf_min`).
        repeat: повтор эталона к эталону; его VMAF — предел замера (шум сцены и кодировщика).
        moved: текст `image.scene.moved` по кадрам эталонов.
    """
    retry = "повторите прогон"
    if moved:
        return f"сцена менялась: {moved} — {retry}"
    if repeat is None or repeat.vmaf is None:
        return ""
    floor = limit(ctx, "codec.repeat_vmaf_min").value
    if repeat.vmaf.mean < floor:
        return (
            f"сцена менялась: эталоны в начале и в конце профиля совпадают на VMAF "
            f"{num(repeat.vmaf.mean)} при пороге {num(floor)} — в кадре движение или мигает "
            f"свет; {retry}"
        )
    return ""


def choose_reference(
    b_to_a: Quality | None, a_to_b: Quality | None, a: str, b: str, current: str, spread: float
) -> tuple[str, str]:
    """Эталон для двух кодеков (S2): поток, к которому другой теряет больше.

    Args:
        b_to_a: максимум кодека `b` к максимуму `a` как эталону.
        a_to_b: максимум `a` к максимуму `b`.
        a, b: кодеки.
        current: текущий кодек камеры — эталон, если разницы нет.
        spread: разброс VMAF (`codec.vmaf_spread`).

    Returns:
        Кодек эталона и причина выбора для протокола.
    """
    if b_to_a is None or a_to_b is None or b_to_a.vmaf is None or a_to_b.vmaf is None:
        return current, "второго кодека нет или VMAF не посчитан — эталон: текущий кодек камеры"
    lose_b, lose_a = b_to_a.vmaf.mean, a_to_b.vmaf.mean
    if lose_b < lose_a - spread:
        return a, f"{codec_title(b)} к {codec_title(a)} — VMAF {num(lose_b)}, обратно {num(lose_a)}"
    if lose_a < lose_b - spread:
        return b, f"{codec_title(a)} к {codec_title(b)} — VMAF {num(lose_a)}, обратно {num(lose_b)}"
    return current, (
        f"максимумы кодеков равны в пределах разброса {num(spread)} "
        f"(VMAF {num(lose_b)} и {num(lose_a)}) — эталон: текущий кодек камеры"
    )


def _bitrate(ctx: CheckContext, curve: Curve, point: Point) -> str:
    if point.measured_kbps is None:
        return ""
    pct = limit(ctx, "codec.bitrate_pct").value
    declared = EncoderSettings(curve.codec, kbps=point.kbps, constant=curve.constant)
    if not bitrate_problem(declared, point.measured_kbps, pct):
        return ""
    off = (point.measured_kbps / point.kbps - 1) * 100
    allowed = f"±{pct:g} %" if curve.constant else f"+{pct:g} %"
    return (
        f"битрейт не соответствует заданному: задано {rate(point.kbps)}, измерено "
        f"{rate(point.measured_kbps)} ({off:+.0f} % при допуске {allowed})"
    )


def _not_growing(lo: Point, hi: Point, spread: float) -> str:
    """Ступень выше дала качество хуже ступени ниже больше разброса замера."""
    assert lo.quality and hi.quality and lo.quality.vmaf and hi.quality.vmaf
    low, high = lo.quality.vmaf, hi.quality.vmaf
    s = max(spread, _noise(high, low))
    if high.mean >= low.mean - s:
        return ""
    return (
        f"качество не растёт с битрейтом: на {rate(hi.kbps)} VMAF {num(high.mean)}, "
        f"на {rate(lo.kbps)} — {num(low.mean)}; разница {num(low.mean - high.mean)} "
        f"больше разброса {num(s)}"
    )


def _noise(high: Stat, low: Stat) -> float:
    """Удвоенная ошибка разности средних по кадрам: шум замера, а не дефект."""
    n_high, n_low = max(1, len(high.values)), max(1, len(low.values))
    return 2 * math.sqrt(high.sd**2 / n_high + low.sd**2 / n_low)


def verdict(ctx: CheckContext, curve: Curve) -> Verdict:
    """Итог строки «кодировщик × кодек» по правилу спеки."""
    key, size_class = codec_key(curve.codec), vmaf_class(curve.height)
    threshold = limit(ctx, f"codec.{key}.{size_class}.vmaf_min")
    floor = limit(ctx, f"codec.{key}.{size_class}.kbps_floor").value
    spread = limit(ctx, "codec.vmaf_spread").value
    size = f"{curve.width}×{curve.height}"
    broken = rules_problem(ctx)
    if broken:
        return Verdict(Status.STAND_ERROR, f"проверьте thresholds.yaml: {broken}", threshold)
    if curve.scene:
        return Verdict(Status.STAND_ERROR, curve.scene, threshold)
    if curve.switch_refused:
        return Verdict(Status.FAILED, curve.switch_refused, threshold, [curve.switch_refused])
    points = sorted(curve.points, key=lambda p: p.kbps)
    problems: list[str] = []
    for point in points:
        found = point.refused or point.no_stream or _bitrate(ctx, curve, point)
        vmaf = point.quality.vmaf if point.quality else None
        if not found and vmaf is not None and point.kbps >= floor and vmaf.mean < threshold.value:
            found = (
                f"качество ниже порога: на {rate(point.kbps)} VMAF {num(vmaf.mean)} при пороге "
                f"{num(threshold.value)} для {size}"
            )
        if found:
            problems.append(found)
    measured = [p for p in points if p.quality and p.quality.vmaf]
    for lo, hi in zip(measured, measured[1:], strict=False):
        found = _not_growing(lo, hi, spread)
        if found:
            problems.append(found)
    if problems:
        return Verdict(Status.FAILED, "; ".join(problems), threshold, problems, Severity.MAJOR)
    if not points or any(p.quality is None for p in points):
        return Verdict(Status.STAND_ERROR, "нет кадров для сравнения на ступени", threshold)
    if not measured or len(measured) < len(points):
        return Verdict(Status.STAND_ERROR, NO_VMAF, threshold)
    low, high = measured[0], measured[-1]
    assert low.quality and high.quality and low.quality.vmaf and high.quality.vmaf
    return Verdict(
        Status.PASSED,
        f"VMAF от {num(low.quality.vmaf.mean)} на {rate(low.kbps)} до "
        f"{num(high.quality.vmaf.mean)} на {rate(high.kbps)}; порог {size} — "
        f"{num(threshold.value)} ({threshold.source})",
        threshold,
    )
