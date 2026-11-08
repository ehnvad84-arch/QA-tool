"""Правила итогов проверки картинки: пороги, ожидаемая сторона и `verdict()` (спека этапа 7, S11).

Пороги — в `thresholds.yaml` стенда (ключи `image.<настройка>.step|stability|direction` и
`image.*` для сроков и условий сцены), иначе стартовые `DEFAULTS` с источником «уточнить по
эталонным камерам» (conventions.md: пороги — в конфигурации, не в коде). Правило читает их
каждый раз из контекста проверки: изменённый порог меняет итог без правки программы.
"""

import math
from dataclasses import dataclass

from qa_tool.checks.image.frames import DEFAULTS as CAPTURE_DEFAULTS
from qa_tool.checks.image.frames import START
from qa_tool.checks.image.settings import BY_KEY, Exposure
from qa_tool.core.checks import CheckContext, Severity, Status
from qa_tool.core.models import Threshold

# (ожидаемая сторона, шаг эффекта, разброс без изменений) — таблица настроек спеки
_START_RULES = {
    "brightness": (1, 3.0, 1.0),
    "contrast": (1, 2.0, 0.7),
    "saturation": (1, 2.0, 0.5),
    "sharpness": (1, 10.0, 3.0),
    "wb_red": (1, 3.0, 1.0),
    "wb_blue": (-1, 3.0, 1.0),
    "wdr": (-1, 4.0, 1.0),
    "blc": (1, 3.0, 1.0),
    "nr": (-1, 10.0, 5.0),
}
DEFAULTS: dict[str, Threshold] = {
    **CAPTURE_DEFAULTS,
    "image.apply_delay_s": Threshold(1.5, "с", START),
    "image.before_gap_s": Threshold(1.0, "с", START),
    "image.scene_diff": Threshold(3.0, "уровень", START),
    "image.wdr_scene_clip_pct": Threshold(1.0, "%", START),
    "image.nr_noise_floor": Threshold(0.5, "уровень", START),
    "image.clip_pct": Threshold(5.0, "%", START),
    "image.reconnect_wait_s": Threshold(120.0, "с", START),
}
for _key, (_direction, _step, _stability) in _START_RULES.items():
    _unit = BY_KEY[_key].unit
    DEFAULTS[f"image.{_key}.direction"] = Threshold(float(_direction), "", START)
    DEFAULTS[f"image.{_key}.step"] = Threshold(_step, _unit, START)
    DEFAULTS[f"image.{_key}.stability"] = Threshold(_stability, _unit, START)

RELATIVE_UNIT = "% от исходного"
# начало причины «пропущена» из-за автоэкспозиции: по нему протокол собирает пояснение А
AE_SKIPPED = "эффект погашен автоэкспозицией камеры"


def limit(ctx: CheckContext, key: str) -> Threshold:
    """Порог `image.*` из настроек стенда, иначе стартовый."""
    return ctx.thresholds.get(key, DEFAULTS[key])


@dataclass(frozen=True)
class Rule:
    """Правило одной настройки: в какую сторону и на сколько должна сдвинуться величина."""

    key: str
    direction: float
    step: Threshold
    stability: Threshold
    clip_pct: float
    relative: bool  # величина в % от значения в положении 0 %
    exposure: Exposure

    @property
    def problem(self) -> str:
        """Чем правило негодно; пусто — годно."""
        if self.direction not in (1.0, -1.0):
            return f"сторона image.{self.key}.direction = {self.direction:g}, нужна 1 или −1"
        if self.step.value <= self.stability.value:
            return (
                f"шаг image.{self.key}.step ({self.step.value:g}) не больше разброса "
                f"image.{self.key}.stability ({self.stability.value:g})"
            )
        return ""

    @property
    def expected(self) -> str:
        """Ожидаемое для протокола: сторона, шаг и разброс."""
        side = "растёт" if self.direction > 0 else "падает"
        unit = f" {self.step.unit}" if self.step.unit else ""
        return (
            f"величина {side} не меньше чем на {self.step.value:g}{unit} на каждой половине "
            f"диапазона (разброс без изменений — {self.stability.value:g}{unit})"
        )


def rule_for(ctx: CheckContext, key: str) -> Rule:
    """Правило настройки из порогов стенда."""
    return Rule(
        key,
        limit(ctx, f"image.{key}.direction").value,
        limit(ctx, f"image.{key}.step"),
        limit(ctx, f"image.{key}.stability"),
        limit(ctx, "image.clip_pct").value,
        BY_KEY[key].unit == RELATIVE_UNIT,
        BY_KEY[key].exposure,
    )


@dataclass(frozen=True)
class Verdict:
    """Итог настройки. `code`: effect, none, reverse, uneven, partial, skipped_ae, skipped_scene,
    bad_rule, no_value; `severity` — только если отличается от важности строки."""

    code: str
    status: Status
    reason: str
    severity: Severity | None = None


def _num(value: float, signed: bool = False) -> str:
    """Число для протокола ОТК: одна цифра после запятой, десятичная запятая."""
    return (f"{value:+.1f}" if signed else f"{value:.1f}").replace(".", ",")


def verdict(
    rule: Rule,
    values: list[float],
    clipped: list[float],
    exposure_mode: str | None,
    scene_ok: bool = True,
) -> Verdict:
    """Итог по величинам в положениях 0/50/100 % (или «выкл/вкл»).

    Args:
        rule: правило настройки.
        values: величины в положениях: три (0, 50, 100 %) или два (выкл, вкл).
        clipped: доля пересвета или черноты в тех же положениях, %.
        exposure_mode: AUTO, MANUAL или None — камера не сообщила.
        scene_ok: условие сцены для настройки выполнено.

    Returns:
        Итог по «Правилу итогов» спеки; неверное правило — «ошибка стенда».
    """
    if rule.problem:
        return Verdict("bad_rule", Status.STAND_ERROR, f"проверьте thresholds.yaml: {rule.problem}")
    if not scene_ok:
        return Verdict(
            "skipped_scene", Status.SKIPPED, "нужна сцена этапа 6: в обычной сцене эффект не виден"
        )
    if rule.exposure is Exposure.NEEDS_AUTO and exposure_mode == "MANUAL":
        return Verdict(
            "skipped_ae",
            Status.SKIPPED,
            "компенсация засветки работает только при автоэкспозиции, а она выключена",
        )
    if len(values) not in (2, 3) or not all(math.isfinite(v) for v in values):
        return Verdict(
            "no_value", Status.STAND_ERROR, "величину по кадру не определить: кадр пуст или упёрся"
        )
    if rule.relative:
        if values[0] <= 0:
            return Verdict(
                "no_value", Status.STAND_ERROR, "исходная величина 0 — сравнивать в % не с чем"
            )
        values = [v / values[0] * 100 for v in values]
    s, step = rule.direction, rule.step.value
    limit_text = f"при пороге {_num(step)}"
    grows = "рост" if s > 0 else "уменьшение"
    whole = values[-1] - values[0]
    if abs(whole) < step:
        if rule.exposure is Exposure.DAMPED and exposure_mode != "MANUAL":
            return Verdict(
                "skipped_ae",
                Status.SKIPPED,
                f"{AE_SKIPPED} — см. Приложение 1, пояснение А (изменение {_num(abs(whole))})",
            )
        span = "выкл → вкл" if len(values) == 2 else "от 0 до 100 %"
        return Verdict(
            "none",
            Status.FAILED,
            f"эффекта нет: настройка не меняет изображение, {span} изменение "
            f"{_num(abs(whole))} {limit_text}",
        )
    if s * whole <= -step:
        moved = "уменьшилась" if whole < 0 else "выросла"
        return Verdict(
            "reverse",
            Status.FAILED,
            f"эффект обратный: величина {moved} на {_num(abs(whole))}, ожидалось {grows}",
        )
    if len(values) == 2:
        return Verdict(
            "effect", Status.PASSED, f"эффект есть: выкл → вкл {_num(whole, True)} {limit_text}"
        )
    d1, d2 = values[1] - values[0], values[2] - values[1]
    steps = (("0 → 50 %", d1), ("50 → 100 %", d2))
    for name, delta in steps:
        if s * delta <= -step:
            moved = "уменьшилась" if delta < 0 else "выросла"
            return Verdict(
                "uneven",
                Status.FAILED,
                f"эффект неровный: на шаге {name} величина {moved} на {_num(abs(delta))}, "
                f"ожидалось {grows}",
            )
    if s * d1 >= step and s * d2 >= step:
        return Verdict(
            "effect",
            Status.PASSED,
            f"эффект есть: на шагах 0 → 50 % и 50 → 100 % {_num(d1, True)} и {_num(d2, True)} "
            f"{limit_text}",
        )
    weak_index = 0 if s * d1 < step else 1
    name, delta = steps[weak_index]
    weak = (0, 1) if weak_index == 0 else (1, 2)
    if any(clipped[i] >= rule.clip_pct for i in weak):
        return Verdict(
            "effect",
            Status.PASSED,
            f"эффект есть: на шаге {name} изменение {_num(abs(delta))} {limit_text} — "
            "упор в пределы сцены (пересвет или чернота)",
        )
    return Verdict(
        "partial",
        Status.FAILED,
        f"эффект на части диапазона: на шаге {name} изменение {_num(abs(delta))} {limit_text}",
        Severity.MINOR,
    )
