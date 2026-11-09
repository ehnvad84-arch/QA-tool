"""Отчёт «Сравнение кодеков» (спека codec-quality, S9): кривые качества камер прогона рядом.

Выпускается, если приложение «Кодеки» есть у двух камер и больше. Лист матрицы «разрешение ·
кодек × камера» и лист на каждую пару точного разрешения и кодека: кривые всех камер на одном
графике и таблица «камера × ступень». Ступени у камер одного разрешения стоят на общей сетке
битрейтов, поэтому сравниваются точно, без интерполяции. Каждая камера считается к своему
эталону: отчёт показывает, сколько камера теряет при сжатии, а не чья картинка лучше вообще.
Камеры обозначены М1…М8 — номерами мест стенда.
"""

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook

from qa_tool.report.charts import XYSeries, xy_chart
from qa_tool.report.codec import CodecLine, mbit, num, rate_ticks
from qa_tool.report.html import render_template
from qa_tool.report.model import Protocol, Summary
from qa_tool.report.pdf import write_pdf
from qa_tool.report.xlsx import table

FOLDER = "compare"
STEM = "codec_compare"


@dataclass
class CameraCurve:
    """Кривая одной камеры на листе: ступени от большего битрейта к меньшему."""

    slot: int
    status: str
    fps: float | None
    steps: list[tuple[str, str]]  # (битрейт задан, VMAF)


@dataclass
class PairSheet:
    """Лист «разрешение · кодек»: график всех камер, таблица и камеры без такого профиля."""

    size: str
    codec: str
    chart: str
    curves: list[CameraCurve] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    approximate: bool = False  # разная частота кадров — сравнение приблизительное


@dataclass
class CodecCompare:
    """Содержимое отчёта сравнения кодеков."""

    run_id: str
    allowed: bool
    cameras: list[tuple[int, str, str, str]]  # (место, производитель, модель, прошивка)
    matrix: list[tuple[str, dict[int, str]]]  # («1920×1080 · H.264», место → итог)
    sheets: list[PairSheet]
    states: list[tuple[int, list[str]]]  # текущие настройки камер — без галочки «изменяющие»
    points: list[tuple[int, CodecLine]] = field(default_factory=list)  # для листа «Точки»


def _first_profile(lines: list[CodecLine]) -> list[CodecLine]:
    """Ступени профиля с наименьшим номером: у камеры два профиля одного размера — берём первый."""
    if not lines:
        return []
    first = min(line.profile for line in lines)
    return [line for line in lines if line.profile == first]


def _status(protocol: Protocol, check_id: str) -> str:
    return next((r.status for r in protocol.rows if r.check_id == check_id), "")


def _range_text(lines: list[CodecLine]) -> str:
    values = [line.vmaf for line in lines if line.vmaf is not None]
    if not values:
        return ""
    return f"VMAF {num(min(values), 0)}–{num(max(values), 0)}"


def build_codec_compare(summary: Summary, run_dir: Path) -> CodecCompare | None:
    """Собрать сравнение и нарисовать графики в `compare/`.

    Returns:
        Содержимое отчёта; None — приложение «Кодеки» меньше чем у двух камер.
    """
    protocols = [p for p in summary.protocols if p.codec is not None]
    if len(protocols) < 2:
        return None
    (run_dir / FOLDER).mkdir(exist_ok=True)
    cameras = [
        (
            p.slot,
            p.passport.get("manufacturer", ""),
            p.passport.get("model", ""),
            p.passport.get("firmware", ""),
        )
        for p in protocols
    ]
    by_pair: dict[tuple[str, str], dict[int, list[CodecLine]]] = {}
    for p in protocols:
        for line in p.codec.lines:
            if line.size:
                by_pair.setdefault((line.size, line.codec), {}).setdefault(p.slot, []).append(line)
    slots = [p.slot for p in protocols]
    protocol_of = {p.slot: p for p in protocols}
    matrix, sheets, points = [], [], []
    for index, (pair, found) in enumerate(sorted(by_pair.items()), start=1):
        size, codec = pair
        sheet = PairSheet(size, codec, "")
        results: dict[int, str] = {}
        series = []
        rates = set()
        for slot in slots:
            lines = _first_profile(found.get(slot, []))
            if not lines:
                sheet.missing.append(slot)
                continue
            status = _status(protocol_of[slot], lines[0].check_id)
            results[slot] = "; ".join(filter(None, [status, _range_text(lines)]))
            sheet.curves.append(
                CameraCurve(
                    slot,
                    status,
                    lines[0].fps,
                    [(mbit(line.target_kbps), num(line.vmaf)) for line in lines],
                )
            )
            rates.add(lines[0].fps)
            points += [(slot, line) for line in lines]
            curve = [
                (line.measured_kbps or line.target_kbps, line.vmaf)
                for line in lines
                if (line.measured_kbps or line.target_kbps) and line.vmaf is not None
            ]
            if curve:
                series.append(XYSeries(f"М{slot}", curve, place=(slot - 1) % 8))
        sheet.approximate = len(rates) > 1
        if series:
            name = f"{FOLDER}/codec_{index}.png"
            level = next(
                (ln.vmaf_min for lines in found.values() for ln in lines if ln.vmaf_min), None
            )
            threshold = (level, f"порог {size}: {num(level, 0)}") if level else None
            xy_chart(
                run_dir / name,
                f"{size} · {codec}: качество к своему эталону",
                "Битрейт, Мбит/с (логарифмическая шкала)",
                "VMAF",
                series,
                rate_ticks([pt for s in series for pt in s.points]),
                threshold=threshold,
            )
            sheet.chart = name
        matrix.append((f"{size} · {codec}", results))
        sheets.append(sheet)
    states = [(p.slot, p.codec.state_lines) for p in protocols]
    allowed = any(p.codec.allowed for p in protocols)
    return CodecCompare(summary.run_id, allowed, cameras, matrix, sheets, states, points)


def publish_codec_compare(compare: CodecCompare, run_dir: Path) -> list[Path]:
    """Выпустить `codec_compare.html`, `.pdf` и `.xlsx` в папку прогона."""
    html = render_template("codec_compare.html", c=compare)
    html_path = run_dir / f"{STEM}.html"
    html_path.write_text(html, encoding="utf-8")
    pdf = write_pdf(run_dir / f"{STEM}.pdf", html, f"Сравнение кодеков {compare.run_id}", run_dir)
    return [html_path, pdf, _xlsx(compare, run_dir / f"{STEM}.xlsx")]


def _xlsx(compare: CodecCompare, path: Path) -> Path:
    """Листы «Камеры», «Итоги», «Точки»: числа числами, графиков нет — ОТК строит свои."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Камеры"
    table(
        ws,
        ["Место", "Производитель", "Модель", "Прошивка"],
        [
            [f"М{slot}", vendor, model, firmware]
            for slot, vendor, model, firmware in compare.cameras
        ],
        {2: 20, 3: 24, 4: 16},
    )
    slots = [slot for slot, *_rest in compare.cameras]
    table(
        wb.create_sheet("Итоги"),
        ["Разрешение · кодек", *(f"М{slot}" for slot in slots)],
        [
            [pair, *(results.get(slot, "нет профиля") for slot in slots)]
            for pair, results in compare.matrix
        ],
        {1: 24},
    )
    table(
        wb.create_sheet("Точки"),
        [
            "Место",
            "Разрешение",
            "Кодек",
            "Частота, к/с",
            "Ступень",
            "Битрейт задан, кбит/с",
            "Битрейт измерен, кбит/с",
            "VMAF",
            "SSIM",
            "PSNR, дБ",
            "Итог ступени",
        ],
        [
            [
                f"М{slot}",
                line.size,
                line.codec,
                line.fps,
                line.step,
                line.target_kbps,
                line.measured_kbps,
                line.vmaf,
                line.ssim,
                line.psnr,
                line.status,
            ]
            for slot, line in compare.points
        ],
        {2: 14},
    )
    wb.save(path)
    return path
