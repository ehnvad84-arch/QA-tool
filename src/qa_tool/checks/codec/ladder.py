"""Ступени битрейта для кривой качества (спека codec-quality, S1, S2, S9).

Ступени берутся из общей для всех камер сетки своего класса разрешения (решение директора 08.11):
так у камер одного разрешения точки стоят на одних и тех же битрейтах и сравнимы без
интерполяции. Сетка — `верх / шаг^i`, i = 0…n−1; верх для класса, число ступеней и шаг — в
`thresholds.yaml` (`codec.<класс>.grid_top`, `codec.grid_steps`, `codec.grid_ratio`): там хранятся
числа, а не списки. Внутри сетки берутся только точки, которые камера может дать обоими кодеками,
и не ниже `codec.min_share` её максимума: ступени в десятки кбит/с — «каша» без пользы (директор
08.11). Эталон — свой максимум камеры, он в сетку не входит.
"""

from dataclasses import dataclass

from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Threshold

START = "стартовое значение: уточнить по эталонным камерам"
# Классы по высоте кадра: (имя, наименьшая высота). Верх сетки — по спеке: 720p — 4 Мбит/с,
# 1080p — 8, 4 Мп (1440p) — 12, 8 Мп (2160p) — 16.
CLASSES = (("uhd", 2160), ("qhd", 1440), ("fhd", 1080), ("hd", 720), ("sd", 0))
DEFAULTS = {
    "codec.sd.grid_top": Threshold(2048.0, "кбит/с", START),
    "codec.hd.grid_top": Threshold(4096.0, "кбит/с", START),
    "codec.fhd.grid_top": Threshold(8192.0, "кбит/с", START),
    "codec.qhd.grid_top": Threshold(12288.0, "кбит/с", START),
    "codec.uhd.grid_top": Threshold(16384.0, "кбит/с", START),
    "codec.grid_steps": Threshold(5.0, "ступеней", START),
    "codec.grid_ratio": Threshold(2.0, "раз", START),
    "codec.min_share": Threshold(0.0625, "доля", "решение директора 08.11: не ниже 1/16 максимума"),
}
MIN_STEPS = 2  # меньше двух точек — кривой нет


@dataclass(frozen=True)
class Range:
    """Диапазон битрейта кодека из GetVideoEncoderConfigurationOptions, кбит/с."""

    kbps_min: int
    kbps_max: int


@dataclass(frozen=True)
class Ladder:
    """Ступени двух кодеков; `common` — одни и те же, сравнимы при равном битрейте (S2)."""

    steps_a: list[int]
    steps_b: list[int]
    common: bool
    note: str = ""


def limit(ctx: CheckContext, key: str) -> Threshold:
    """Порог `codec.*` из настроек стенда, иначе стартовый."""
    return ctx.thresholds.get(key, DEFAULTS[key])


def grid_class(height: int) -> str:
    """Класс разрешения по высоте кадра."""
    return next(name for name, lowest in CLASSES if height >= lowest)


def grid(ctx: CheckContext, height: int) -> list[int]:
    """Общая сетка битрейтов класса, кбит/с, по убыванию."""
    top = limit(ctx, f"codec.{grid_class(height)}.grid_top").value
    steps = max(MIN_STEPS, int(limit(ctx, "codec.grid_steps").value))
    ratio = max(1.01, limit(ctx, "codec.grid_ratio").value)
    return [round(top / ratio**i) for i in range(steps)]


def _inside(points: list[int], low: int, high: int, camera_max: int, share: float) -> list[int]:
    floor = max(low, camera_max * share)
    return [p for p in points if floor <= p <= high]


def ladder(ctx: CheckContext, height: int, a: Range, b: Range | None = None) -> Ladder:
    """Ступени для кодека `a` и, если камера его объявляет, второго кодека `b`.

    Args:
        ctx: контекст проверки (пороги стенда).
        height: высота кадра профиля, пикселей.
        a: диапазон текущего кодека.
        b: диапазон второго кодека; None — камера его не объявляет.

    Returns:
        Ступени; пустые — диапазон камеры не покрывает сетку (строка «пропущена»).
    """
    points = grid(ctx, height)
    share = limit(ctx, "codec.min_share").value
    if b is None:
        steps = _inside(points, a.kbps_min, a.kbps_max, a.kbps_max, share)
        return _checked(Ladder(steps, [], common=False))
    low, high = max(a.kbps_min, b.kbps_min), min(a.kbps_max, b.kbps_max)
    if low <= high:
        common = _inside(points, low, high, high, share)
        if len(common) >= MIN_STEPS:
            return Ladder(common, list(common), common=True)
    own_a = _inside(points, a.kbps_min, a.kbps_max, a.kbps_max, share)
    own_b = _inside(points, b.kbps_min, b.kbps_max, b.kbps_max, share)
    return _checked(Ladder(own_a, own_b, False, "при равном битрейте кодеки не сравнить"))


def _checked(found: Ladder) -> Ladder:
    """Меньше двух точек у кодека — у него ступеней нет, причина в `note`."""
    a = found.steps_a if len(found.steps_a) >= MIN_STEPS else []
    b = found.steps_b if len(found.steps_b) >= MIN_STEPS else []
    note = found.note
    if (found.steps_a and not a) or (found.steps_b and not b) or not (a or b):
        note = "; ".join(filter(None, [note, "диапазон камеры не покрывает сетку битрейтов"]))
    return Ladder(a, b, found.common and bool(a) and bool(b), note)
