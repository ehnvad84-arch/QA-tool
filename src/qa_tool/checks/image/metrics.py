"""Величины на кадре, по которым виден эффект настроек картинки (спека этапа 7, таблица настроек).

Чистые функции numpy над кадром RGB uint8 — без камеры, файлов и порогов: так их можно проверить
на синтетических кадрах. Яркость Y — BT.709, 0–255. Каждая величина подобрана так, чтобы расти
от своей настройки и не замечать соседних: насыщенность и баланс белого — отношения каналов, им
всё равно, светлее ли кадр; резкость и шум — по разностям, им всё равно, сдвинута ли яркость.
"""

from dataclasses import dataclass

import numpy as np

from qa_tool.checks.image.frames import Capture, luma

CLIP_Y = 250.0  # пересвет: выше значения неотличимы
CRUSH_Y = 5.0  # чернота
CHANNEL_LOW, CHANNEL_HIGH = 16, 249  # каналы вне этих границ упёрлись и цвет по ним не судят
SHARE = 0.10  # доля самых светлых и самых тёмных точек для WDR


@dataclass(frozen=True)
class Masks:
    """Области кадра «до» № 1: самые светлые и самые тёмные 10 % точек (WDR)."""

    bright: np.ndarray
    dark: np.ndarray


@dataclass(frozen=True)
class Measurement:
    """Величина на кадре и доля упёршихся в пересвет и черноту точек, %."""

    value: float
    clip_pct: float
    crush_pct: float


def masks_of(work: np.ndarray) -> Masks:
    """Маски WDR по кадру «до»: на всех положениях сравниваются одни и те же точки."""
    y = luma(work.astype(np.float32))
    low, high = np.quantile(y, [SHARE, 1 - SHARE])
    return Masks(bright=y >= high, dark=y <= low)


def mean_luma(work: np.ndarray) -> float:
    """Средняя Y — яркость."""
    return float(luma(work.astype(np.float32)).mean())


def luma_std(work: np.ndarray) -> float:
    """СКО Y — контраст: сдвиг всей яркости его не меняет."""
    return float(luma(work.astype(np.float32)).std())


def _unclipped(rgb: np.ndarray) -> np.ndarray:
    """Точки, где все каналы внутри 16…249: в упёршихся цвет и отношения каналов ложные."""
    inside = (rgb >= CHANNEL_LOW) & (rgb <= CHANNEL_HIGH)
    return rgb[inside.all(axis=-1)].astype(np.float32)


def saturation(work: np.ndarray) -> float:
    """Средняя (max − min) / max каналов, % — насыщенность; от яркости не зависит."""
    pixels = _unclipped(work)
    if not len(pixels):
        return float("nan")
    high, low = pixels.max(axis=1), pixels.min(axis=1)
    return float(((high - low) / high).mean() * 100)


def laplacian_var(fragment: np.ndarray) -> float:
    """Дисперсия Лапласиана 3 × 3 по Y — резкость; ровный сдвиг яркости её не меняет."""
    y = luma(fragment.astype(np.float32))
    if min(y.shape) < 3:
        return float("nan")
    center = y[1:-1, 1:-1]
    lap = y[:-2, 1:-1] + y[2:, 1:-1] + y[1:-1, :-2] + y[1:-1, 2:] - 4 * center
    return float(lap.var())


def red_blue(work: np.ndarray) -> float:
    """Отношение средних R / B — баланс белого: растёт с красным усилением, падает с синим."""
    pixels = _unclipped(work)
    if not len(pixels):
        return float("nan")
    blue = pixels[:, 2].mean()
    return float(pixels[:, 0].mean() / blue) if blue else float("nan")


def luma_spread(work: np.ndarray, masks: Masks) -> float:
    """Y светлых 10 % минус Y тёмных 10 % (маски кадра «до») — WDR сжимает этот размах."""
    y = luma(work.astype(np.float32))
    if not masks.bright.any() or not masks.dark.any() or masks.bright.shape != y.shape:
        return float("nan")
    return float(y[masks.bright].mean() - y[masks.dark].mean())


def center_luma(work: np.ndarray) -> float:
    """Средняя Y центральной трети — компенсация засветки поднимает объект в центре."""
    y = luma(work.astype(np.float32))
    height, width = y.shape
    return float(y[height // 3 : 2 * height // 3, width // 3 : 2 * width // 3].mean())


def temporal_noise(recent: list[np.ndarray]) -> float:
    """Медиана по точкам СКО Y во времени — шум; неподвижная сцена его не даёт."""
    if len(recent) < 2:
        return float("nan")
    stack = np.stack([luma(f.astype(np.float32)) for f in recent])
    return float(np.median(stack.std(axis=0)))


def clipped(work: np.ndarray) -> tuple[float, float]:
    """Доли точек в пересвете и в черноте, %."""
    y = luma(work.astype(np.float32))
    return float((y >= CLIP_Y).mean() * 100), float((y <= CRUSH_Y).mean() * 100)


def measure(metric: str, capture: Capture, masks: Masks | None = None) -> Measurement:
    """Величина `metric` (имя из каталога настроек) на снимке.

    Args:
        metric: имя функции величины: mean_luma, luma_std, saturation, laplacian_var,
            red_blue, luma_spread, center_luma, temporal_noise.
        capture: снимок.
        masks: маски кадра «до» — только для luma_spread.

    Returns:
        Величина (NaN — по кадру её не определить) и упор в пересвет и черноту.

    Raises:
        KeyError: неизвестная величина — ошибка каталога, а не камеры.
    """
    work_metrics = {
        "mean_luma": mean_luma,
        "luma_std": luma_std,
        "saturation": saturation,
        "red_blue": red_blue,
        "center_luma": center_luma,
    }
    if metric in work_metrics:
        value = work_metrics[metric](capture.work)
    elif metric == "laplacian_var":
        value = laplacian_var(capture.fragment)
    elif metric == "temporal_noise":
        value = temporal_noise(capture.recent)
    elif metric == "luma_spread":
        value = luma_spread(capture.work, masks or masks_of(capture.work))
    else:
        raise KeyError(metric)
    clip, crush = clipped(capture.work)
    return Measurement(value, clip, crush)
