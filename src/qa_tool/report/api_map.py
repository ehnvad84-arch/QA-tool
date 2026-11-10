"""Приложение протокола камеры «API камеры: карта и сверка с ONVIF» (спека api-discovery, UI).

Собирается из строк `API-*` базы прогона и их доказательств: `summary.json` и `api-map.json`
строки `API-MAP-1`, `pair.json` строк сверки, `search.json` автопоиска, `restore.json` возврата.
Пути к файлам строит программа из папки доказательства строки, а не из ответа камеры. Значения,
пути и тексты в файлах — от камеры и недоверенные: шаблон их экранирует, числа проверяются на
конечность, размер файла ограничен.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qa_tool.core.checks import Status

PREFIX = "API-"
MAP_ID, SEARCH_ID, RESTORE_ID, LOST_ID = (
    "API-MAP-1",
    "API-SEARCH-1",
    "API-RESTORE-1",
    "API-WRITE-LOST",
)
MATCH, RANGE = "API-MATCH-", "API-RANGE-"
FILE_LIMIT = 8 << 20  # api-map.json не больше 8 МБ (предел карты); остальные — килобайты
SECTIONS_SHOWN = 25  # обзор разделов в протоколе; полный список — в документе «Карта API»
TRAFFIC = "трафик веб-обхода"


def _read_json(path: Path) -> Any:
    """JSON доказательства; нет файла, больше предела или не JSON — пустой словарь."""
    try:
        if path.stat().st_size > FILE_LIMIT:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _text(value: Any, limit: int = 300) -> str:
    """Значение камеры для ячейки: строкой, без бесконечностей, не длиннее предела."""
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def number(value: Any) -> float | None:
    """Конечное число для Excel; иначе None (ячейка пустая, столбец остаётся числовым)."""
    if isinstance(value, bool):
        return None
    try:
        found = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return found if math.isfinite(found) else None


def shown(value: Any) -> str:
    """Число с запятой для протокола (ОТК читает по-русски), остальное — как есть."""
    found = number(value)
    if found is None:
        return _text(value)
    text = f"{found:g}" if abs(found) < 1e15 else _text(value)
    return text.replace(".", ",")


@dataclass
class LeftLine:
    """Строка таблицы блокера: что осталось изменённым и как вернуть."""

    param: str
    where: str
    was: str
    now: str
    how: str


@dataclass
class MatchLine:
    """Строка сверки ONVIF ↔ родной API (и строка Excel «Сверка API»)."""

    check_id: str
    n: int | None
    title: str
    onvif_path: str
    onvif_value: str
    onvif_range: str
    native_api: str
    native_path: str
    native_value: str
    native_allowed: str
    scale: str
    status: str
    severity: str
    reason: str
    source: str
    other_firmware: bool = False
    bad: bool = False
    raw: dict = field(default_factory=dict)  # pair.json как есть — для чисел Excel


@dataclass
class SearchLine:
    """Строка таблицы автопоиска (и строки Excel «Пары API»)."""

    n: int | None
    title: str
    writes: str
    moved: list[str]
    pair: str
    restored: str
    bad: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class SectionLine:
    """Строка обзора разделов карты."""

    section: str
    cls: str
    params: int
    paired: int
    mismatched: int
    note: str


@dataclass
class ApiAppendix:
    """Всё содержимое приложения «API камеры» для HTML, PDF и Excel."""

    number: int
    allowed: bool
    traffic: bool = False
    summary: list[tuple[str, str]] = field(default_factory=list)
    map_status: str = ""
    map_reason: str = ""
    alarm: str = ""
    alarm_how: str = ""
    left: list[LeftLine] = field(default_factory=list)
    lost: list[tuple[str, str]] = field(default_factory=list)
    restore: str = ""
    matches: list[MatchLine] = field(default_factory=list)
    without_pair: str = ""
    no_pairs: str = ""
    search_status: str = ""
    search_reason: str = ""
    searches: list[SearchLine] = field(default_factory=list)
    sections: list[SectionLine] = field(default_factory=list)
    sections_more: int = 0
    map_document: str = ""
    map_document_file: str = ""  # api_map_<место>.html для ссылки в HTML-протоколе


def _folder(run_dir: Path, row) -> Path | None:
    return run_dir / row.evidence if row and row.evidence else None


def _summary(data: dict, row) -> list[tuple[str, str]]:
    """Сводка «поле · значение» из `summary.json`; без файла — итог строки `API-MAP-1`."""
    order = (
        ("api", "Родной API"),
        ("бренд", "Производитель"),
        ("вход", "Вход"),
        ("модель", "Модель и прошивка"),
        ("источник", "Источник карты"),
        ("разделы", "Разделов"),
        ("параметры", "Параметров"),
        ("пары", "Таблица соответствий"),
        ("сверка", "Сверка"),
        ("автопоиск", "Автопоиск"),
        ("библиотека", "Библиотека"),
        ("карта", "Карта"),
    )
    lines = [(title, _text(data.get(key), 600)) for key, title in order if data.get(key)]
    lines.append((f"Итог {MAP_ID}", f"{row.status}: {row.actual or row.reason}"))
    return lines


def _scale(raw: dict) -> str:
    """Шкала словами: «1:1», «× 0,39 + 0», «список: AUTO ↔ auto», «по диапазонам»."""
    scale = _dict(raw.get("шкала"))
    kind = _text(scale.get("вид"))
    if kind == "перечисление":
        values = _dict(scale.get("значения"))
        pairs = [f"{_text(k, 40)} ↔ {_text(v, 40)}" for k, v in list(values.items())[:8]]
        return "список: " + "; ".join(pairs) if pairs else "список"
    if kind == "линейная":
        a, b = number(scale.get("a")), number(scale.get("b"))
        if a == 1 and not b:
            return "1:1"
        if a is not None:
            tail = f" {'+' if (b or 0) >= 0 else '−'} {shown(abs(b or 0))}" if b else ""
            return f"× {shown(a)}{tail}"
    return kind


def _range(side: dict, low: str = "мин", high: str = "макс") -> str:
    lo, hi = side.get(low), side.get(high)
    if lo is None and hi is None:
        return ""
    return f"{shown(lo)}…{shown(hi)}"


def _source(raw: dict) -> tuple[str, bool]:
    """«Откуда пара» и пометка «другая прошивка»."""
    source = _text(raw.get("источник"))
    # пометку ставит библиотека: «(найдено на прошивке V5.7.1)» к источнику пары
    other = "найдено на прошивке" in source
    return source, other


def _match_line(row, raw: dict) -> MatchLine:
    onvif, native = _dict(raw.get("onvif")), _dict(raw.get("родной"))
    source, other = _source(raw)
    title = row.title
    n = raw.get("n") if isinstance(raw.get("n"), int) else None
    return MatchLine(
        check_id=row.check_id,
        n=n,
        title=title,
        onvif_path=_text(onvif.get("путь")),
        onvif_value=shown(onvif.get("значение")),
        onvif_range=_range(onvif),
        native_api=_text(native.get("api")),
        native_path=_text(native.get("путь")),
        native_value=shown(native.get("значение")),
        native_allowed=_range(native),
        scale=_scale(raw),
        status=row.status,
        severity=row.severity,
        reason=row.actual or row.reason,
        source=source,
        other_firmware=other,
        bad=row.status == Status.FAILED.value,
        raw=raw,
    )


def _order(check_id: str) -> tuple[int, int]:
    """Строки по номеру каталога; диапазон — сразу под своей парой."""
    tail = check_id.rsplit("-", 1)[-1]
    n = int(tail) if tail.isdigit() else 0
    return n, 1 if check_id.startswith(RANGE) else 0


def _search_line(item: dict) -> SearchLine:
    writes = " → ".join(shown(v) for v in _list(item.get("записи"))[:4])
    moved = []
    for change in _list(item.get("сдвинулось"))[:12]:
        change = _dict(change)
        copies = change.get("копий")
        label = _text(change.get("id"), 160)
        if isinstance(copies, int) and copies > 1:
            label += f" ({copies} копии)"
        values = " → ".join(shown(v) for v in _list(change.get("значения"))[:4])
        mark = "! " if change.get("вне раздела") else ""
        moved.append(f"{mark}{label}: {values}" if values else f"{mark}{label}")
    for leaf in _list(item.get("onvif"))[:6]:
        leaf = _dict(leaf)
        moved.append(
            f"! в ONVIF: {_text(leaf.get('лист'), 80)} {shown(leaf.get('было'))} → "
            f"{shown(leaf.get('стало'))}"
        )
    verdict = _text(item.get("итог"))
    n = item.get("n") if isinstance(item.get("n"), int) else None
    return SearchLine(
        n=n,
        title=_text(item.get("настройка")) or (f"п. {n}" if n is not None else ""),
        writes=writes,
        moved=moved,
        pair=verdict,
        restored=_text(item.get("возврат")),
        bad=verdict.startswith("остановлен") or "блокер" in _text(item.get("возврат")),
        raw=item,
    )


def _sections(
    native_map: dict, matches: list[MatchLine], left: list[LeftLine]
) -> list[SectionLine]:
    """Обзор разделов: параметров, в парах, расхождений, примечание."""
    paired: dict[str, int] = {}
    bad: dict[str, int] = {}
    for line in matches:
        if line.check_id.startswith(MATCH) and line.native_path:
            paired[line.native_path] = paired.get(line.native_path, 0) + 1
            if line.bad:
                bad[line.native_path] = bad.get(line.native_path, 0) + 1
    stuck = {line.param for line in left}
    found = []
    for section in _list(native_map.get("разделы")):
        section = _dict(section)
        params = [_dict(p) for p in _list(section.get("параметры"))]
        ids = {_text(p.get("id"), 400) for p in params}
        notes = []
        if section.get("чувствительность") not in (None, "обычный"):
            notes.append("только описание: значения не сохраняются")
        code = section.get("код")
        if isinstance(code, int) and code != 200:
            notes.append(f"не ответил: {code}")
        hidden = sum(1 for p in params if p.get("чувствительность") == "секрет")
        if hidden:
            notes.append(f"скрыто значений: {hidden}")
        if ids & stuck:
            notes.append("! не вернулось после автопоиска")
        found.append(
            SectionLine(
                section=_text(section.get("id"), 160),
                cls=_text(section.get("класс")),
                params=len(params),
                paired=sum(paired.get(i, 0) for i in ids),
                mismatched=sum(bad.get(i, 0) for i in ids),
                note="; ".join(notes),
            )
        )
    # сначала разделы с парами и расхождениями: их ОТК ищет глазами
    return sorted(found, key=lambda s: (-s.mismatched, -s.paired, s.section))


def build_api_appendix(
    rows: list,
    run_dir: Path,
    allowed: bool,
    number_: int,
    document: str = "",
    document_file: str = "",
) -> ApiAppendix | None:
    """Приложение по строкам протокола; None — строк `API-*` (кроме API-RUVER) нет.

    Args:
        rows: строки протокола (`report.model.Row`).
        run_dir: папка прогона — от неё отсчитаны пути доказательств.
        allowed: стояла ли галочка «изменяющие».
        number_: номер приложения по порядку выпущенных.
        document: имя документа «Карта API» этой камеры для сводки («Карта API QA-…-3»).
        document_file: файл HTML этого документа рядом с протоколом.
    """
    api = [
        r for r in rows if r.check_id.startswith(PREFIX) and not r.check_id.startswith("API-RUVER")
    ]
    by_id = {r.check_id: r for r in api}
    head = by_id.get(MAP_ID)
    if head is None:
        return None
    folder = _folder(run_dir, head)
    summary = _dict(_read_json(folder / "summary.json")) if folder else {}
    native_map = _dict(_read_json(folder / "api-map.json")) if folder else {}
    appendix = ApiAppendix(
        number=number_,
        allowed=allowed,
        traffic=_text(native_map.get("источник")) == TRAFFIC,
        map_status=head.status,
        map_reason=head.actual or head.reason,
        map_document=document,
        map_document_file=document_file,
    )
    appendix.summary = _summary(summary, head)

    restore = by_id.get(RESTORE_ID)
    if restore is not None and restore.status == Status.FAILED.value:
        data = (
            _dict(_read_json(_folder(run_dir, restore) / "restore.json"))
            if restore.evidence
            else {}
        )
        appendix.alarm = "Камера осталась изменённой после автопоиска — верните вручную"
        appendix.alarm_how = _text(data.get("как вышло"), 600) or restore.actual or restore.reason
        appendix.left = [
            LeftLine(
                _text(item.get("параметр"), 200),
                _text(item.get("где"), 200),
                shown(item.get("было")),
                shown(item.get("осталось")),
                _text(item.get("как вернуть"), 400),
            )
            for item in map(_dict, _list(data.get("осталось"))[:40])
        ]
        appendix.restore = "см. рамку в начале приложения"
    elif restore is not None and restore.status == Status.PASSED.value:
        appendix.restore = "всё, что менялось, возвращено; вся карта родного API совпала с исходной"
    else:
        appendix.restore = "ничего не менялось"
    for row in api:
        if row.check_id == LOST_ID:
            appendix.lost.append((row.status, row.actual or row.reason))

    lines = []
    for row in sorted(
        (r for r in api if r.check_id.startswith((MATCH, RANGE))), key=lambda r: _order(r.check_id)
    ):
        if row.check_id == f"{MATCH}0":
            appendix.no_pairs = row.actual or row.reason
            continue
        raw = _dict(_read_json(_folder(run_dir, row) / "pair.json")) if row.evidence else {}
        lines.append(_match_line(row, raw))
    appendix.matches = lines
    appendix.without_pair = _text(summary.get("без пары"), 400)

    search = by_id.get(SEARCH_ID)
    if search is not None:
        appendix.search_status = search.status
        appendix.search_reason = search.actual or search.reason
        trace = (
            _dict(_read_json(_folder(run_dir, search) / "search.json")) if search.evidence else {}
        )
        appendix.searches = [_search_line(_dict(i)) for i in _list(trace.get("настройки"))[:40]]

    sections = _sections(native_map, appendix.matches, appendix.left)
    appendix.sections = sections[:SECTIONS_SHOWN]
    appendix.sections_more = max(0, len(sections) - SECTIONS_SHOWN)
    return appendix
