"""Приложение 1 протокола камеры «Картинка: снимки и замеры» (спека этапа 7, UI).

Собирается из строк `IMAGE-*` базы прогона и файлов их доказательств: `measure.json` (что
записано, прочитано и измерено в каждом положении), `state.txt` и снимки. Пути к снимкам —
относительно папки прогона: протокол HTML лежит в ней же, а PDF собирается с ней как с базой.
Содержимое `measure.json` и `state.txt` — числа и тексты от камеры, недоверенные: шаблон
экранирует их, а пути к файлам строит программа из ID строки, а не из ответа камеры.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.checks.image.rules import AE_SKIPPED, RELATIVE_UNIT
from qa_tool.checks.image.settings import METRIC_TITLES, SETTINGS
from qa_tool.core.checks import Status

PREFIX = "IMAGE-"
STATE_ID, RESTORE_ID = "IMAGE-STATE-1", "IMAGE-RESTORE-1"
EXCLUDED = ("IMAGE-WRITE-LOST", STATE_ID, RESTORE_ID)


@dataclass
class Shot:
    """Снимок одного положения настройки."""

    label: str  # «0 %», «50 %», «100 %»
    thumb: str  # путь к миниатюре относительно папки прогона
    full: str
    value: str  # «121,0 (+38,6)»
    written: str  # «записано 50, прочитано 50»
    weak: bool = False  # на этом шаге эффект меньше порога


@dataclass
class SettingBlock:
    """Блок настройки в приложении."""

    check_id: str
    title: str
    status: str
    reason: str
    metric: str
    side: str  # «растёт» или «уменьшается»
    threshold: str
    shots: list[Shot] = field(default_factory=list)


@dataclass
class ImageLine:
    """Строка листа «Картинка» Excel: настройка целиком, со значениями всех положений."""

    check_id: str
    title: str
    onvif_field: str
    span: str
    original: str
    written: list[str]
    read: list[str]
    metric: str
    unit: str
    values: list[str]
    side: str
    threshold: str
    status: str
    effect: str
    reason: str
    links: list[str]  # пути к снимкам 0/50/100 % относительно папки прогона
    numbers: list[float | None] = field(default_factory=list)  # величины положений — для графика
    thumbs: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)  # «0 %»… или «выкл», «вкл»


@dataclass
class ImageAppendix:
    """Приложение «Картинка» к протоколу одной камеры."""

    allowed: bool  # галочка «изменяющие»
    exposure: str
    restore_status: str
    restore_reason: str
    state_image: str
    state_lines: list[str]
    blocks: list[SettingBlock]
    without_shots: list[tuple[str, str, str]]  # (настройка, результат, причина)
    ae_settings: list[str]  # для пояснения А: «Яркость (IMAGE-BRIGHT-1)»
    ae_has_blc: bool
    lines: list[ImageLine] = field(default_factory=list)


def _number(value) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.1f}".replace(".", ",")


def _finite(value) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_measure(folder: Path) -> dict:
    try:
        data = json.loads(_read(folder / "measure.json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _shots(evidence: str, folder: Path, measure: dict, relative: bool) -> list[Shot]:
    """Снимки положений; шаг, не дотянувший до порога в нужную сторону, помечен `weak`."""
    positions = [p for p in measure.get("положения", []) if isinstance(p, dict)]
    rule = measure.get("правило") or {}
    step, side = rule.get("шаг"), rule.get("сторона", 1) or 1
    first = _finite(positions[0].get("величина")) if positions else None
    two = len(positions) == 2
    shots, previous = [], None
    for index, position in enumerate(positions):
        share = position.get("доля", 0) or 0
        name = f"p{round(share * 100):03d}"
        if not (folder / f"{name}_t.jpg").exists():
            continue
        value = position.get("величина")
        text = _number(value)
        weak = False
        if (
            previous is not None
            and isinstance(value, int | float)
            and isinstance(previous, int | float)
        ):
            delta = value - previous
            text += f" ({'+' if delta >= 0 else ''}{_number(delta)})"
            # порог относительных величин — в % от положения 0 %, как в verdict()
            scaled = delta / first * 100 if relative and first else delta
            weak = isinstance(step, int | float) and side * scaled < step
        previous = value
        label = ("выкл", "вкл")[index] if two else f"{round(share * 100)} %"
        written = (
            f"записано {_number(position.get('значение'))}, "
            f"прочитано {_number(position.get('прочитано'))}"
        )
        shots.append(
            Shot(label, f"{evidence}/{name}_t.jpg", f"{evidence}/{name}.jpg", text, written, weak)
        )
    return shots


AE_EXPLANATION = (
    "Камера сама держит яркость кадра постоянной: когда программа меняла настройку, "
    "автоматическая экспозиция подстраивала выдержку и усиление и возвращала картинку к прежней "
    "яркости. По кадрам нельзя сказать, работает настройка или нет, поэтому это не дефект камеры. "
    "Проверить вручную: включить в камере ручную экспозицию, прогнать «Картинку» с галочкой "
    "«изменяющие», затем вернуть режим экспозиции. Программа режим экспозиции не меняет никогда."
)
_UNITS = {s.check_id: s.unit for s in SETTINGS}


def _effect(status: str, reason: str) -> str:
    if status == Status.PASSED.value:
        return "есть"
    for mark, effect in (
        ("эффект обратный", "обратный"),
        ("эффекта нет", "нет"),
        ("эффект неровный", "неровный"),
        ("эффект на части диапазона", "на части диапазона"),
    ):
        if reason.startswith(mark):
            return effect
    return ""


def _line(row, measure: dict, shots: list[Shot]) -> ImageLine:
    positions = [p for p in measure.get("положения", []) if isinstance(p, dict)]
    span = measure.get("диапазон")
    rule = measure.get("правило") or {}
    reason = row.reason
    if row.status == Status.SKIPPED.value and reason.startswith(AE_SKIPPED):
        reason = f"{reason}. {AE_EXPLANATION}"
    return ImageLine(
        check_id=row.check_id,
        title=row.title,
        onvif_field=measure.get("поле", ""),
        span=f"{_number(span[0])}…{_number(span[1])}"
        if isinstance(span, list) and len(span) == 2
        else "",
        original=_number(measure.get("исходное")),
        written=[_number(p.get("значение")) for p in positions],
        read=[_number(p.get("прочитано")) for p in positions],
        metric=METRIC_TITLES.get(measure.get("величина", ""), ""),
        unit=_UNITS.get(row.check_id, ""),
        values=[_number(p.get("величина")) for p in positions],
        side=("растёт" if rule.get("сторона", 1) > 0 else "уменьшается") if rule else "",
        threshold=row.threshold,
        status=row.status,
        effect=_effect(row.status, row.reason),
        reason=reason,
        links=[s.full for s in shots],
        numbers=[_finite(p.get("величина")) for p in positions],
        thumbs=[s.thumb for s in shots],
        labels=[s.label for s in shots],
    )


def build_image_appendix(rows: list, run_dir: Path, allowed: bool) -> ImageAppendix | None:
    """Приложение по строкам протокола; None — строк `IMAGE-*` нет.

    Args:
        rows: строки протокола (`report.model.Row`).
        run_dir: папка прогона — от неё отсчитаны пути доказательств.
        allowed: стояла ли галочка «изменяющие».
    """
    image = [r for r in rows if r.check_id.startswith(PREFIX)]
    if not image:
        return None
    state = next((r for r in image if r.check_id == STATE_ID), None)
    restore = next((r for r in image if r.check_id == RESTORE_ID), None)
    state_lines = _read(run_dir / state.evidence / "state.txt").splitlines() if state else []
    exposure = next(
        (ln.split(":", 1)[1].strip() for ln in state_lines if ln.startswith("режим")), ""
    )
    state_image = (
        f"{state.evidence}/state.jpg"
        if state and state.evidence and (run_dir / state.evidence / "state.jpg").exists()
        else ""
    )
    blocks, without, ae, lines = [], [], [], []
    for row in image:
        if row.check_id in EXCLUDED:
            continue
        if row.status == Status.SKIPPED.value and row.reason.startswith(AE_SKIPPED):
            ae.append(f"{row.title} ({row.check_id})")
        folder = run_dir / row.evidence if row.evidence else None
        measure = _load_measure(folder) if folder else {}
        relative = _UNITS.get(row.check_id) == RELATIVE_UNIT
        shots = _shots(row.evidence, folder, measure, relative) if folder else []
        lines.append(_line(row, measure, shots))
        if not shots:
            without.append((row.title, row.status, row.reason))
            continue
        side = "растёт" if (measure.get("правило") or {}).get("сторона", 1) > 0 else "уменьшается"
        metric = measure.get("величина", "")
        blocks.append(
            SettingBlock(
                row.check_id,
                row.title,
                row.status,
                row.reason,
                METRIC_TITLES.get(metric, metric),
                side,
                row.threshold,
                shots,
            )
        )
    return ImageAppendix(
        allowed=allowed,
        exposure=exposure or "не сообщён",
        restore_status=restore.status if restore else "",
        restore_reason=restore.reason if restore else "",
        state_image=state_image,
        state_lines=state_lines,
        blocks=blocks,
        without_shots=without,
        ae_settings=ae,
        ae_has_blc=any("IMAGE-BLC-" in item for item in ae),
        lines=lines,
    )
