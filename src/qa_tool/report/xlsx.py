"""Excel: протокол камеры (Сводка, Проверки, Задачи) и сводный отчёт (+ Сравнение)."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from qa_tool.core.checks import Status
from qa_tool.report.model import Protocol, Summary

_HEADER_FILL = PatternFill("solid", fgColor="E8E8E8")
_STATUS_FILL = {
    Status.PASSED.value: PatternFill("solid", fgColor="D9EAD3"),
    Status.FAILED.value: PatternFill("solid", fgColor="F4CCCC"),
    Status.STAND_ERROR.value: PatternFill("solid", fgColor="FCE5CD"),
    Status.SKIPPED.value: PatternFill("solid", fgColor="EEEEEE"),
}
_CHECK_HEADERS = [
    "Место",
    "№",
    "Проверка",
    "Название",
    "Модуль",
    "Что делали",
    "Ожидание",
    "Факт",
    "Порог (источник)",
    "Результат",
    "Причина",
    "Доказательство",
]
_TASK_HEADERS = [
    "Место",
    "№",
    "Задача",
    "Модуль",
    "Важность",
    "Шаги воспроизведения",
    "Ожидалось",
    "Получено",
    "Доказательство",
]


def _table(
    ws: Worksheet, headers: list[str], rows: list[list], widths: dict[int, int] | None = None
):
    ws.append(headers)
    for cell in ws[1]:
        cell.data_type = "s"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for row in rows:
        ws.append(row)
    for column in ws.iter_cols(min_row=2):
        for cell in column:
            if isinstance(cell.value, str):
                cell.data_type = "s"  # «=HYPERLINK(…)» из ответа камеры — текст, не формула
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if cell.value in _STATUS_FILL:
                cell.fill = _STATUS_FILL[cell.value]
    for index, width in (widths or {}).items():
        ws.column_dimensions[ws.cell(1, index).column_letter].width = width
    ws.freeze_panes = "A2"


def _check_rows(p: Protocol) -> list[list]:
    return [
        [
            p.slot,
            r.number,
            r.check_id,
            r.title,
            r.module,
            r.description,
            r.expected,
            r.actual,
            r.threshold,
            r.status,
            r.reason,
            r.evidence,
        ]
        for r in p.rows
    ]


def _task_rows(p: Protocol) -> list[list]:
    return [
        [
            p.slot,
            t.number,
            t.title,
            t.module,
            t.severity,
            "\n".join(f"{i}. {s}" for i, s in enumerate(t.steps, 1)),
            t.expected,
            t.actual,
            t.evidence,
        ]
        for t in p.tasks
    ]


_CHECK_WIDTHS = {3: 18, 4: 28, 6: 40, 7: 30, 8: 30, 9: 18, 10: 14, 11: 30, 12: 28}
_TASK_WIDTHS = {3: 50, 6: 60, 7: 30, 8: 30, 9: 28}


def _summary_rows(protocols: list[Protocol]) -> list[list]:
    return [
        [
            p.slot,
            p.protocol_id,
            p.camera_name,
            p.passport.get("firmware", ""),
            p.passport.get("serial", ""),
            *(p.counts[s.value] for s in Status),
            len(p.tasks),
            p.verdict,
        ]
        for p in protocols
    ]


_SUMMARY_HEADERS = [
    "Место",
    "Протокол",
    "Камера",
    "Прошивка",
    "Серийный",
    *(s.value for s in Status),
    "Задач",
    "Заключение",
]


def write_protocol_xlsx(path: Path, p: Protocol) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    _table(ws, _SUMMARY_HEADERS, _summary_rows([p]), {2: 20, 3: 28, 11: 40})
    _table(wb.create_sheet("Проверки"), _CHECK_HEADERS, _check_rows(p), _CHECK_WIDTHS)
    _table(wb.create_sheet("Задачи"), _TASK_HEADERS, _task_rows(p), _TASK_WIDTHS)
    wb.save(path)
    return path


def write_summary_xlsx(path: Path, s: Summary) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    _table(ws, _SUMMARY_HEADERS, _summary_rows(s.protocols), {2: 20, 3: 28, 11: 40})
    _table(
        wb.create_sheet("Проверки"),
        _CHECK_HEADERS,
        [row for p in s.protocols for row in _check_rows(p)],
        _CHECK_WIDTHS,
    )
    _table(
        wb.create_sheet("Задачи"),
        _TASK_HEADERS,
        [row for p in s.protocols for row in _task_rows(p)],
        _TASK_WIDTHS,
    )
    headers = ["Проверка", "Название", *(f"{p.slot}. {p.camera_name}" for p in s.protocols)]
    matrix = [
        [check_id, title, *(s.matrix[check_id].get(p.slot, "—") for p in s.protocols)]
        for check_id, title in s.checks
    ]
    _table(wb.create_sheet("Сравнение"), headers, matrix, {1: 18, 2: 30})
    wb.save(path)
    return path
