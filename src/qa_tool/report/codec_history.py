"""Прошлая прошивка той же модели (спека codec-quality, S9): кривые «эта прошивка / прошлая».

Ищется в папке прогонов рядом с текущим: прогоны `QA-*` с номером меньше текущего, от
последнего к раннему, не больше `MAX_RUNS`. Подходит камера той же модели — производитель и
модель совпадают без учёта регистра и крайних пробелов — с другой прошивкой, у которой есть
строки `CODEC-*` с `measure.json`. Берётся самый поздний такой прогон.

Старые прогоны — недоверенные данные: база открывается только на чтение, папка доказательств
после `resolve()` обязана лежать внутри папки своего прогона, `measure.json` — не больше 1 МБ и
знакомой версии, числа проверяются в `report/codec.py`. Не прочитался прогон — он пропускается.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt

from qa_tool.core.store import RunStore
from qa_tool.report.charts import XYSeries, xy_chart
from qa_tool.report.codec import (
    CODEC_TITLES,
    PLACES,
    CodecAppendix,
    curve_points,
    rate_ticks,
    read_measure,
)

MAX_RUNS = 100  # глубже искать долго, а сравнивать с прошивкой годичной давности незачем


@dataclass
class PastRun:
    """Кривые прошлого прогона той же модели: (разрешение, кодек) → точки (кбит/с, VMAF)."""

    run_id: str
    firmware: str
    curves: dict[tuple[str, str], list[tuple[float, float]]] = field(default_factory=dict)


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _inside(run_dir: Path, evidence: str) -> Path | None:
    """Папка доказательств строки, если она внутри папки прогона; иначе None."""
    root = run_dir.resolve()
    folder = (run_dir / evidence).resolve()
    return folder if folder.is_relative_to(root) and folder != root else None


def _curves(run_dir: Path, rows: list) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Кривые по строкам кодеков; у двух профилей одного разрешения — профиль с меньшим номером."""
    found: dict[tuple[str, str], tuple[int, list[tuple[float, float]]]] = {}
    for row in rows:
        check_id = row["check_id"] or ""
        number = check_id.rsplit("-", 1)[-1]
        if not check_id.startswith(("CODEC-H264-", "CODEC-H265-")) or not number.isdigit():
            continue
        folder = _inside(run_dir, row["evidence_dir"] or "")
        if folder is None:
            continue
        measure = read_measure(folder)
        size = measure.get("разрешение")
        points = curve_points(measure)
        if not points or not isinstance(size, list) or len(size) != 2:
            continue
        codec = CODEC_TITLES.get(str(measure.get("кодек")), str(measure.get("кодек")))
        key = (f"{size[0]}×{size[1]}", codec)
        if key not in found or int(number) < found[key][0]:
            found[key] = (int(number), points)
    return {key: points for key, (_number, points) in found.items()}


def find(run_dir: Path, passport: dict[str, str]) -> PastRun | None:
    """Самый поздний прошлый прогон той же модели с другой прошивкой и кривыми кодеков.

    Args:
        run_dir: папка текущего прогона.
        passport: паспорт камеры текущего прогона (производитель, модель, прошивка).

    Returns:
        Кривые прошлого прогона; None — такого нет или модель не известна.
    """
    if (
        not (passport.get("manufacturer") or "").strip()
        or not (passport.get("model") or "").strip()
    ):
        return None
    earlier = sorted(
        (d for d in run_dir.parent.glob("QA-*") if d.is_dir() and d.name < run_dir.name),
        reverse=True,
    )[:MAX_RUNS]
    for folder in earlier:
        if not (folder / "run.sqlite").exists():
            continue
        try:
            store = RunStore(folder, readonly=True)
        except sqlite3.Error:
            continue
        try:
            found = _from_store(store, folder, passport)
        except sqlite3.Error:
            found = None
        finally:
            store.close()
        if found is not None:
            return found
    return None


def _from_store(store: RunStore, folder: Path, passport: dict[str, str]) -> PastRun | None:
    for camera in store.cameras():
        if not (
            _same(camera["manufacturer"], passport.get("manufacturer"))
            and _same(camera["model"], passport.get("model"))
        ):
            continue
        if _same(camera["firmware"], passport.get("firmware")):
            continue
        curves = _curves(folder, store.results(slot=camera["slot"]))
        if curves:
            return PastRun(store.run_id, camera["firmware"] or "?", curves)
    return None


NONE_FOUND = "Прогонов этой модели с другой прошивкой нет."


def attach_history(appendix: CodecAppendix, run_dir: Path, passport: dict[str, str]) -> None:
    """Добавить к листам приложения графики «эта прошивка / прошлая» (раздел 4 приложения).

    Эта прошивка — сплошные линии, прошлая — точечные той же метки; каждый прогон считается к
    своему эталону. Графики ложатся рядом с графиком листа в папке доказательств.
    """
    if not appendix.sheets:
        return
    past = find(run_dir, passport)
    if past is None:
        appendix.history = NONE_FOUND
        return
    this = passport.get("firmware") or "?"
    appendix.history = (
        f"Эта прошивка {this} (прогон {run_dir.name}) и прошлая {past.firmware} "
        f"(прогон {past.run_id}). Каждый прогон считается к своему эталону."
    )
    for sheet in appendix.sheets:
        lines = [line for line in appendix.lines if line.profile == sheet.number]
        series = []
        missing = []
        for codec in dict.fromkeys(line.codec for line in lines):
            own = [
                (line.measured_kbps or line.target_kbps, line.vmaf)
                for line in lines
                if line.codec == codec
                and (line.measured_kbps or line.target_kbps)
                and line.vmaf is not None
            ]
            size = next((line.size for line in lines if line.codec == codec), "")
            before = past.curves.get((size, codec), [])
            place = PLACES.get(codec.replace(".", ""), 2)
            if own:
                label = f"{codec} · эта {this} ({run_dir.name})"
                series.append(XYSeries(label, own, place, Qt.PenStyle.SolidLine))
            if before:
                label = f"{codec} · прошлая {past.firmware} ({past.run_id})"
                series.append(XYSeries(label, before, place, Qt.PenStyle.DotLine))
            else:
                missing.append(f"в прогоне {past.run_id} нет профиля {size} {codec}")
        sheet.history_note = "; ".join(missing)
        if not any(s.dash == Qt.PenStyle.DotLine for s in series):
            continue
        target = (
            Path(sheet.chart).with_name(f"codec-history-{sheet.number}.png")
            if sheet.chart
            else None
        )
        if target is None:
            continue
        xy_chart(
            run_dir / target,
            f"Профиль {sheet.number}: эта прошивка и прошлая",
            "Битрейт, Мбит/с (логарифмическая шкала)",
            "VMAF",
            series,
            rate_ticks([p for s in series for p in s.points]),
        )
        sheet.history_chart = target.as_posix()
