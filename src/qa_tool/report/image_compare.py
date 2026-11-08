"""Отчёт «Сравнение картинки» (спека этапа 7, S10): камеры прогона рядом по каждой настройке.

Выпускается, если строки `IMAGE-*` есть у двух камер и больше. Лист матрицы «настройка ×
камера» и лист на настройку: снимки всех камер в положениях 0/50/100 % и график величины.
Камеры с разными диапазонами сравниваются по процентам диапазона (ось X — положение, а не
значение настройки); относительные величины (резкость, баланс белого, шум) — в % от положения
0 % своей камеры. Камеры обозначены М1…М8 — номерами мест стенда.
"""

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from qa_tool.checks.image.rules import RELATIVE_UNIT
from qa_tool.checks.image.settings import SETTINGS
from qa_tool.report.charts import Series, line_chart
from qa_tool.report.html import render_template
from qa_tool.report.model import Summary
from qa_tool.report.pdf import write_pdf
from qa_tool.report.xlsx import table

FOLDER = "compare"
STEM = "image_compare"


@dataclass
class Cell:
    """Одна камера в листе настройки."""

    slot: int
    status: str
    effect: str
    reason: str
    thumbs: list[str]
    fulls: list[str]
    values: list[str]


@dataclass
class SettingSheet:
    """Лист настройки: снимки всех камер и график."""

    check_id: str
    title: str
    metric: str
    unit: str
    labels: list[str]
    chart: str
    cells: list[Cell] = field(default_factory=list)


@dataclass
class ImageCompare:
    """Содержимое отчёта сравнения картинки."""

    run_id: str
    cameras: list[tuple[int, str]]  # (место, «Производитель Модель»)
    matrix: list[tuple[str, str, dict[int, str]]]  # (ID, настройка, место → итог)
    sheets: list[SettingSheet]


def _relative(numbers: list[float | None]) -> list[float | None]:
    first = numbers[0] if numbers else None
    if not first:
        return [None for _ in numbers]
    return [None if n is None else n / first * 100 for n in numbers]


def build_image_compare(summary: Summary, run_dir: Path) -> ImageCompare | None:
    """Собрать сравнение и нарисовать графики в `compare/`; None — камер с картинкой меньше двух."""
    protocols = [p for p in summary.protocols if p.image is not None]
    if len(protocols) < 2:
        return None
    (run_dir / FOLDER).mkdir(exist_ok=True)
    cameras = [(p.slot, p.camera_name) for p in protocols]
    lines = {p.slot: {line.check_id: line for line in p.image.lines} for p in protocols}
    matrix, sheets = [], []
    for setting in SETTINGS:
        present = {
            slot: found[setting.check_id]
            for slot, found in lines.items()
            if setting.check_id in found
        }
        if not present:
            continue
        row = {}
        for slot, line in present.items():
            row[slot] = f"{line.status}" + (f": эффект {line.effect}" if line.effect else "")
        matrix.append((setting.check_id, setting.title, row))
        labels = next((ln.labels for ln in present.values() if ln.labels), ["0 %", "50 %", "100 %"])
        relative = setting.unit == RELATIVE_UNIT
        series = [
            Series(f"М{slot}", _relative(line.numbers) if relative else line.numbers)
            for slot, line in present.items()
            if any(n is not None for n in line.numbers)
        ]
        chart = ""
        if series:
            unit = "% от положения 0 %" if relative else setting.unit
            name = f"{FOLDER}/{setting.check_id}.png"
            line_chart(run_dir / name, setting.title, unit, labels, series)
            chart = name
        sheet = SettingSheet(
            setting.check_id,
            setting.title,
            next((ln.metric for ln in present.values() if ln.metric), ""),
            setting.unit,
            labels,
            chart,
        )
        for slot, line in present.items():
            sheet.cells.append(
                Cell(
                    slot,
                    line.status,
                    line.effect,
                    line.reason,
                    line.thumbs,
                    line.links,
                    line.values,
                )
            )
        sheets.append(sheet)
    return ImageCompare(summary.run_id, cameras, matrix, sheets)


def publish_image_compare(compare: ImageCompare, run_dir: Path) -> list[Path]:
    """Выпустить `image_compare.html`, `.pdf` и `.xlsx` в папку прогона."""
    html = render_template("image_compare.html", c=compare)
    html_path = run_dir / f"{STEM}.html"
    html_path.write_text(html, encoding="utf-8")
    pdf = write_pdf(run_dir / f"{STEM}.pdf", html, f"Сравнение картинки {compare.run_id}", run_dir)
    return [html_path, pdf, _xlsx(compare, run_dir / f"{STEM}.xlsx")]


def _xlsx(compare: ImageCompare, path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Матрица"
    headers = ["Проверка", "Настройка", *(f"М{slot}. {name}" for slot, name in compare.cameras)]
    rows = [
        [check_id, title, *(results.get(slot, "—") for slot, _ in compare.cameras)]
        for check_id, title, results in compare.matrix
    ]
    table(ws, headers, rows, {1: 18, 2: 30})
    for sheet in compare.sheets:
        ws = wb.create_sheet(sheet.check_id[:31])
        heads = ["Место", "Камера", "Результат", "Эффект", *(f"Величина {x}" for x in sheet.labels)]
        heads += [f"Снимок {x}" for x in sheet.labels]
        names = dict(compare.cameras)
        body = [
            [
                f"М{cell.slot}",
                names.get(cell.slot, ""),
                cell.status,
                cell.effect,
                *(cell.values + [""] * len(sheet.labels))[: len(sheet.labels)],
                *[""] * len(sheet.labels),
            ]
            for cell in sheet.cells
        ]
        table(ws, heads, body, {2: 28, 3: 14})
        first_link = 5 + len(sheet.labels)
        for index, cell in enumerate(sheet.cells, start=2):
            for offset, link in enumerate(cell.fulls[: len(sheet.labels)]):
                target = ws.cell(index, first_link + offset)
                target.value = link.rsplit("/", 1)[-1]
                target.data_type = "s"
                target.hyperlink = link
                target.font = Font(color="1155CC", underline="single")
    wb.save(path)
    return path
