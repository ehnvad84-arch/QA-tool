"""Excel: протокол камеры (Сводка, Проверки, Задачи) и сводный отчёт (+ Сравнение)."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from qa_tool.core.checks import Status
from qa_tool.report.api_map import number as api_number
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


def table(
    ws: Worksheet, headers: list[str], rows: list[list], widths: dict[int, int] | None = None
):
    """Лист-таблица: заголовок, строки текстом (без формул), заливка итогов, ширины столбцов."""
    ws.append(headers)
    for cell in ws[1]:
        cell.data_type = "s"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for row in rows:
        # управляющий символ из ответа камеры иначе обрывает выпуск всех протоколов прогона
        ws.append([ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in row])
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
    """Excel протокола камеры: «Сводка», «Проверки», «Задачи» и, если есть, «Профили ONVIF»."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    table(ws, _SUMMARY_HEADERS, _summary_rows([p]), {2: 20, 3: 28, 11: 40})
    table(wb.create_sheet("Проверки"), _CHECK_HEADERS, _check_rows(p), _CHECK_WIDTHS)
    table(wb.create_sheet("Задачи"), _TASK_HEADERS, _task_rows(p), _TASK_WIDTHS)
    if p.image is not None:
        _image_sheet(wb.create_sheet("Картинка"), p)
    if p.codec is not None:
        _codec_sheet(wb.create_sheet("Кодеки"), p)
    if p.api is not None and not p.api.traffic:
        _api_match_sheet(wb.create_sheet("Сверка API"), p)
        _api_pairs_sheet(wb.create_sheet("Пары API"), p)
    if p.profiles.get("matrix"):
        declared = set(p.profiles.get("declared", []))
        rows = [
            [
                v["profile"],
                "да" if v["profile"] in declared else "нет",
                v["feature"],
                v["verdict"],
                v["basis"],
                v.get("note", ""),
            ]
            for v in p.profiles["matrix"]
        ]
        table(
            wb.create_sheet("Профили ONVIF"),
            ["Профиль", "Объявлен", "Обязательная функция", "Итог", "Основание", "Примечание"],
            rows,
            {3: 34, 4: 14, 5: 60, 6: 40},
        )
    wb.save(path)
    return path


_IMAGE_HEADERS = [
    "Проверка",
    "Настройка",
    "Поле ONVIF",
    "Диапазон",
    "Исходное значение",
    "Режим экспозиции",
    "Записано / прочитано 0 %",
    "Записано / прочитано 50 %",
    "Записано / прочитано 100 %",
    "Величина",
    "Единица",
    "Величина 0 %",
    "Величина 50 %",
    "Величина 100 %",
    "Сторона",
    "Порог (источник)",
    "Результат",
    "Эффект",
    "Причина",
    "Снимок 0 %",
    "Снимок 50 %",
    "Снимок 100 %",
]


def _image_sheet(ws: Worksheet, p: Protocol) -> None:
    """Лист «Картинка»: строка состояния и строка на настройку; снимки — ссылками на файлы
    папки прогона (относительный путь: папку можно переносить целиком), без формул."""
    a = p.image
    assert a is not None
    rows = [["IMAGE-STATE-1", "Текущее состояние", "", "", "", a.exposure, *[""] * 16]]
    for line in a.lines:
        pairs = [f"{w} / {r}" for w, r in zip(line.written, line.read, strict=False)]
        rows.append(
            [
                line.check_id,
                line.title,
                line.onvif_field,
                line.span,
                line.original,
                a.exposure,
                *(pairs + [""] * 3)[:3],
                line.metric,
                line.unit,
                *(line.values + [""] * 3)[:3],
                line.side,
                line.threshold,
                line.status,
                line.effect,
                line.reason,
                *[""] * 3,
            ]
        )
    table(ws, _IMAGE_HEADERS, rows, {2: 26, 3: 30, 16: 24, 19: 60})
    for index, line in enumerate(a.lines, start=3):  # строка 1 — заголовок, 2 — состояние
        for column, link in enumerate(line.links[:3], start=20):
            cell = ws.cell(index, column)
            cell.value = link.rsplit("/", 1)[-1]
            cell.data_type = "s"
            cell.hyperlink = link
            cell.font = Font(color="1155CC", underline="single")


_CODEC_HEADERS = [
    "Проверка",
    "Профиль",
    "Имя профиля",
    "Разрешение",
    "Частота, к/с",
    "Кодек",
    "Ступень",
    "Битрейт задан, кбит/с",
    "Битрейт измерен, кбит/с",
    "Отклонение, %",
    "VMAF",
    "SSIM",
    "PSNR, дБ",
    "Эталон",
    "Порог VMAF",
    "Источник порога",
    "Итог ступени",
    "Причина строки",
    "Снимок ступени",
    "Снимок эталона",
]
_CODEC_NUMBERS = {5: "0", 8: "0", 9: "0", 10: "0.0", 11: "0.0", 12: "0.000", 13: "0.0", 15: "0.0"}


def _codec_sheet(ws: Worksheet, p: Protocol) -> None:
    """Лист «Кодеки»: строки текущих настроек и строка на ступень; числа — числами с форматом
    (Excel покажет десятичную запятую), снимки — ссылками на файлы папки прогона, без формул."""
    c = p.codec
    assert c is not None
    rows = [["CODEC-STATE-1", "", line, *[""] * 17] for line in c.state_lines]
    for line in c.lines:
        rows.append(
            [
                line.check_id,
                line.profile,
                line.names,
                line.size,
                line.fps,
                line.codec,
                line.step,
                line.target_kbps,
                line.measured_kbps,
                line.off_pct,
                line.vmaf,
                line.ssim,
                line.psnr,
                line.reference,
                line.vmaf_min,
                line.source,
                line.status,
                line.reason,
                "",
                "",
            ]
        )
    table(ws, _CODEC_HEADERS, rows, {3: 24, 14: 18, 16: 30, 18: 60})
    first = 2 + len(c.state_lines)
    for index, line in enumerate(c.lines, start=first):
        for column, number_format in _CODEC_NUMBERS.items():
            ws.cell(index, column).number_format = number_format
        for column, link in enumerate(line.links[:2], start=19):
            cell = ws.cell(index, column)
            cell.value = link.rsplit("/", 1)[-1]
            cell.data_type = "s"
            cell.hyperlink = link
            cell.font = Font(color="1155CC", underline="single")


_API_MATCH_HEADERS = [
    "ID",
    "n",
    "Настройка",
    "ONVIF путь",
    "ONVIF значение, число",
    "ONVIF значение, текст",
    "ONVIF мин",
    "ONVIF макс",
    "Родной API",
    "Родной путь",
    "Родное значение, число",
    "Родное значение, текст",
    "Допустимые мин",
    "Допустимые макс",
    "Вид шкалы",
    "a",
    "b",
    "Фактический предел",
    "Итог",
    "Важность",
    "Причина",
    "Откуда пара",
]
_API_PAIRS_HEADERS = [
    "Настройка ONVIF",
    "n",
    "Запись №",
    "Записано",
    "Сдвинувшийся параметр",
    "Копий",
    "Было",
    "Стало",
    "Пара",
    "Возврат",
]


def _api_match_sheet(ws: Worksheet, p: Protocol) -> None:
    """Лист «Сверка API»: строка на `API-MATCH`/`API-RANGE`; у значений два столбца — число
    и текст, чтобы столбец был одного типа и автофильтр работал."""
    a = p.api
    assert a is not None
    rows = []
    for m in a.matches:
        onvif, native = m.raw.get("onvif") or {}, m.raw.get("родной") or {}
        scale = m.raw.get("шкала") or {}
        rows.append(
            [
                m.check_id,
                m.n,
                m.title,
                m.onvif_path,
                api_number(onvif.get("значение")),
                m.onvif_value,
                api_number(onvif.get("мин")),
                api_number(onvif.get("макс")),
                m.native_api,
                m.native_path,
                api_number(native.get("значение")),
                m.native_value,
                api_number(native.get("мин")),
                api_number(native.get("макс")),
                str(scale.get("вид") or ""),
                api_number(scale.get("a")),
                api_number(scale.get("b")),
                api_number(onvif.get("держит")),
                m.status,
                m.severity,
                m.reason,
                m.source,
            ]
        )
    table(ws, _API_MATCH_HEADERS, rows, {3: 24, 4: 30, 10: 40, 21: 60, 22: 30})
    ws.auto_filter.ref = ws.dimensions


def _api_pairs_sheet(ws: Worksheet, p: Protocol) -> None:
    """Лист «Пары API»: ход автопоиска — строка на «настройка × сдвинувшийся параметр × запись»;
    блокер ищется фильтром по столбцу «Возврат»."""
    a = p.api
    assert a is not None
    rows = []
    for line in a.searches:
        writes = [str(w) for w in line.raw.get("записи") or []]
        moved = [m for m in line.raw.get("сдвинулось") or [] if isinstance(m, dict)]
        for leaf in line.raw.get("onvif") or []:
            if isinstance(leaf, dict):
                moved.append(
                    {
                        "id": f"ONVIF {leaf.get('лист', '')}",
                        "значения": [leaf.get("было"), leaf.get("стало")],
                    }
                )
        if not moved:
            rows.append(
                [
                    line.title,
                    line.n,
                    None,
                    " → ".join(writes),
                    "",
                    None,
                    "",
                    "",
                    line.pair,
                    line.restored,
                ]
            )
        for change in moved:
            values = [str(v) for v in change.get("значения") or []]
            for step, (was, now) in enumerate(zip(values, values[1:], strict=False), start=1):
                rows.append(
                    [
                        line.title,
                        line.n,
                        step,
                        writes[step] if step < len(writes) else "",
                        str(change.get("id") or ""),
                        change.get("копий") if isinstance(change.get("копий"), int) else None,
                        was,
                        now,
                        line.pair,
                        line.restored,
                    ]
                )
    table(ws, _API_PAIRS_HEADERS, rows, {1: 24, 5: 40, 9: 40, 10: 40})
    ws.auto_filter.ref = ws.dimensions


def write_summary_xlsx(path: Path, s: Summary) -> Path:
    """Сводный Excel прогона: те же листы по всем камерам и «Сравнение»."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    table(ws, _SUMMARY_HEADERS, _summary_rows(s.protocols), {2: 20, 3: 28, 11: 40})
    table(
        wb.create_sheet("Проверки"),
        _CHECK_HEADERS,
        [row for p in s.protocols for row in _check_rows(p)],
        _CHECK_WIDTHS,
    )
    table(
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
    table(wb.create_sheet("Сравнение"), headers, matrix, {1: 18, 2: 30})
    wb.save(path)
    return path
