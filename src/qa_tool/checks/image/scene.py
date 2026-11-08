"""Сцена, которую снимают камеры при проверке картинки (спека этапа 7, S5).

На этапе 7 сцена — «обычная»: то, что стоит перед камерами в боксе, без управления светом.
Проверка не может сама сделать в ней пересвет или шум, поэтому WDR и компенсация засветки
проверяются, только если в кадре «до» есть пересвет, а шумоподавление — если есть шум; иначе
строка «пропущена: нужна сцена этапа 6». Этап 6 добавит сцену «таблица и свет» с тем же
интерфейсом `Scene`: `prepare` проверит готовность, а светом управляет решение этапа 6.

Сцена должна стоять: два кадра «до» с паузой и кадр «после» возврата сравниваются по средней
яркости; разница больше `image.scene_diff` — сцену двигали или мигает свет, и итоги по
настройке ничего не доказывают («ошибка стенда»).
"""

from typing import Protocol

import numpy as np

from qa_tool.checks.image.frames import Capture
from qa_tool.checks.image.metrics import clipped, mean_luma, temporal_noise
from qa_tool.checks.image.rules import limit
from qa_tool.core.checks import CheckContext

SCENE_NEEDED = "нужна сцена этапа 6"


class Scene(Protocol):
    """Сцена проверки картинки; реализации — `OrdinaryScene` (этап 7) и сцена этапа 6."""

    title: str

    def prepare(self, ctx: CheckContext) -> str | None:
        """Готова ли сцена; строка — причина «ошибки стенда»."""
        ...

    def unmet(self, key: str, before: Capture, ctx: CheckContext) -> str:
        """Чего не хватает сцене для настройки `key`; пусто — условие выполнено."""
        ...

    def regions(self, shape: tuple[int, ...]) -> dict[str, np.ndarray]:
        """Области кадра, по которым считаются величины; у обычной сцены — весь кадр."""
        ...


def moved(first: Capture, second: Capture, ctx: CheckContext) -> str:
    """Сдвинулась ли сцена между кадрами: средняя |разница| Y больше `image.scene_diff`.

    Returns:
        Причина «ошибки стенда»; пусто — сцена стоит.
    """
    allowed = limit(ctx, "image.scene_diff")
    if first.work.shape != second.work.shape:
        return "размер кадра изменился между снимками — камера перенастроила поток"
    diff = abs(mean_luma(first.work) - mean_luma(second.work))
    if diff > allowed.value:
        return (
            f"сцена менялась между снимками: средняя яркость разошлась на {diff:.1f} "
            f"(допуск {allowed.value:g} {allowed.unit}) — в боксе двигались или мигает свет"
        )
    return ""


class OrdinaryScene:
    """То, что стоит перед камерами, без управления светом (этап 7)."""

    title = "обычная сцена"

    def prepare(self, ctx: CheckContext) -> str | None:
        """Готовить нечего: сцену проверяют кадры «до»."""
        return None

    def unmet(self, key: str, before: Capture, ctx: CheckContext) -> str:
        """WDR и компенсации засветки нужен пересвет в кадре, шумоподавлению — шум."""
        if key in ("wdr", "blc"):
            need = limit(ctx, "image.wdr_scene_clip_pct")
            clip, _crush = clipped(before.work)
            if clip < need.value:
                return (
                    f"{SCENE_NEEDED}: в кадре нет пересвета ({clip:.1f} % при нужных "
                    f"{need.value:g} {need.unit}), эффект настройки не виден"
                )
        if key == "nr":
            floor = limit(ctx, "image.nr_noise_floor")
            noise = temporal_noise(before.recent)
            if not noise >= floor.value:  # NaN — тоже «нет шума»
                return (
                    f"{SCENE_NEEDED}: в кадре нет шума ({noise:.2f} при нужных "
                    f"{floor.value:g} {floor.unit}), шумоподавлению нечего убирать"
                )
        return ""

    def regions(self, shape: tuple[int, ...]) -> dict[str, np.ndarray]:
        """Весь кадр."""
        return {"кадр": np.ones(shape[:2], dtype=bool)}
