"""Документ «Карта API» камеры: `api_map_<место>.html/.pdf/.xlsx` (спека api-discovery, T18).

Справочный документ без итогов и подписей: полная карта родного API из `api-map.json` строки
`API-MAP-1` и пометки пар и расхождений из приложения протокола. Повторы по каналам, профилям и
потокам с одинаковыми допустимыми значениями сворачиваются в строку `[*]`. Разделы сети,
учётных записей и безопасности — только описание. Значения и пути — от камеры: шаблон их
экранирует, Excel пишет текстом без формул.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.report.api_map import _dict, _list, _read_json, _text, number, shown
from qa_tool.report.model import Protocol

MAP_ID = "API-MAP-1"
MAX_OPTIONS_SHOWN = 8
# индекс копии: «[0]» у Dahua и ISAPI, «/0/» у JSON-указателя «Рувер» и трафика
_INDEX = re.compile(r"\[\d+\]|(?<=/)\d+(?=/|$)")


@dataclass
class ParamLine:
    """Строка параметра в документе (после свёртки копий)."""

    name: str
    kind: str
    current: str
    note: str
    bad: bool = False


@dataclass
class DocSection:
    """Раздел карты: заголовок и параметры."""

    anchor: str
    title: str
    cls: str
    read: str
    count: int
    sensitive: bool
    open: bool
    params: list[ParamLine] = field(default_factory=list)


@dataclass
class ApiMapDocument:
    """Содержимое документа «Карта API» для HTML, PDF и Excel."""

    title: str
    protocol_id: str
    protocol_file: str
    place: str
    address: str
    camera: str
    source: str
    login: str
    total: str
    cut: str
    sections: list[DocSection] = field(default_factory=list)
    errors: list[tuple[str, str, str]] = field(default_factory=list)
    raw_sections: list[dict] = field(default_factory=list)  # для листов Excel без свёртки
    pairs_by_native: dict[str, str] = field(default_factory=dict)


def describe(param: dict) -> str:
    """Тип и допустимые значения словами: «число 0…100», «список: auto, manual», «не объявлены»."""
    kind = _text(param.get("тип"))
    allowed = _dict(param.get("допустимые"))
    if "негодный" in allowed:
        return f"{kind}; негодный: {_text(allowed['негодный'], 120)}"
    if "мин" in allowed and "макс" in allowed:
        step = f", шаг {shown(allowed['шаг'])}" if "шаг" in allowed else ""
        return f"{kind} {shown(allowed['мин'])}…{shown(allowed['макс'])}{step}"
    values = [_text(v, 40) for v in _list(allowed.get("значения"))]
    if values:
        if len(values) > MAX_OPTIONS_SHOWN:
            values = [*values[:3], "…", values[-1], f"({len(values)} значений)"]
        return "список: " + ", ".join(values)
    if "длина_макс" in allowed:
        return f"текст до {allowed['длина_макс']} знаков"
    return f"{kind}; не объявлены" if kind else "не объявлены"


def _local_name(param_id: str, section_id: str) -> str:
    """Путь параметра внутри раздела: без id раздела и «#»."""
    rest = param_id[len(section_id) :] if param_id.startswith(section_id) else ""
    # ключ Dahua «VideoColor[0]…» начинается с имени раздела, но это не путь внутри него
    if rest[:1] not in ("#", "/"):
        return param_id
    return rest.lstrip("#/") or param_id


def _fold(params: list[dict], section_id: str, notes: dict[str, str], bad: set[str]):
    """Свернуть копии с одинаковыми допустимыми значениями в строку `[*]`."""
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for param in params:
        pid = _text(param.get("id"), 400)
        key = (
            _INDEX.sub(lambda m: "[*]" if m.group().startswith("[") else "*", pid),
            describe(param),
        )
        groups.setdefault(key, []).append((pid, param))
    lines = []
    for (folded, kind), items in groups.items():
        ids = [pid for pid, _ in items]
        if len(items) == 1:
            pid, param = items[0]
            current = _current(param)
            name = _local_name(pid, section_id)
        else:
            parts = []
            for pid, param in items[:12]:
                index = "".join(_INDEX.findall(pid)[-1:]) or pid
                index = index if index.startswith("[") else f"[{index}]"
                parts.append(f"{index} {_current(param)}")
            current = " · ".join(parts) + (f" · … ({len(items)})" if len(items) > 12 else "")
            name = _local_name(folded, section_id)
        note = "; ".join(dict.fromkeys(n for i in ids for n in [notes.get(i, "")] if n))
        flags = {_flag(param) for _pid, param in items} - {""}
        note = "; ".join(filter(None, [note, *sorted(flags)]))
        lines.append(ParamLine(name, kind, current, note, bool(bad & set(ids))))
    return lines


def _current(param: dict) -> str:
    if param.get("чувствительность") == "секрет":
        return "скрыто"
    if param.get("чувствительность") == "чувствительный":
        return "не сохраняется"
    return shown(param.get("значение"))


def _flag(param: dict) -> str:
    flags = []
    if param.get("изменчивый"):
        flags.append("меняется сам")
    if param.get("запись") is False:
        flags.append("только чтение")
    return ", ".join(flags)


def build_api_document(p: Protocol, run_dir: Path) -> ApiMapDocument | None:
    """Документ по протоколу камеры; None — карты нет (`API-MAP-1` не «прошла»).

    Args:
        p: протокол камеры с приложением «API камеры».
        run_dir: папка прогона.
    """
    if p.api is None or p.api.map_status != "прошла":
        return None
    head = next((r for r in p.rows if r.check_id == MAP_ID), None)
    if head is None or not head.evidence:
        return None
    data = _dict(_read_json(run_dir / head.evidence / "api-map.json"))
    if not data:
        return None
    notes: dict[str, str] = {}
    bad: set[str] = set()
    for m in p.api.matches:
        if not m.native_path or not m.check_id.startswith("API-MATCH"):
            continue
        mark = f"пара: {m.title.split(':')[0]}, {m.check_id}"
        if m.bad:
            mark = f"! расхождение с ONVIF — протокол, {m.check_id}"
            bad.add(m.native_path)
        notes[m.native_path] = mark
    for left in p.api.left:
        param = left.param.split(" ", 1)[-1]
        notes[param] = f"! не вернулось: было {left.was}"
        bad.add(param)
    sections = []
    raw = [_dict(s) for s in _list(data.get("разделы"))]
    params_total = 0
    for index, section in enumerate(raw, start=1):
        sid = _text(section.get("id"), 300)
        params = [_dict(x) for x in _list(section.get("параметры"))]
        params_total += len(params)
        sensitive = section.get("чувствительность") not in (None, "обычный")
        lines = [] if sensitive else _fold(params, sid, notes, bad)
        sections.append(
            DocSection(
                anchor=f"s{index}",
                title=sid,
                cls=_text(section.get("класс")),
                read=_text(section.get("чтение"), 300),
                count=len(params),
                sensitive=sensitive,
                open=any(line.note for line in lines),
                params=lines,
            )
        )
    camera = _dict(data.get("камера"))
    limits = _dict(data.get("пределы"))
    collector = _dict(data.get("сборщик"))
    return ApiMapDocument(
        title=f"Карта API {p.run_id}-{p.slot}",
        protocol_id=p.protocol_id,
        protocol_file=f"protocol_{p.slot}.html",
        place=p.place,
        address=p.address,
        camera=" · ".join(
            _text(camera.get(k)) for k in ("производитель", "модель", "прошивка") if camera.get(k)
        ),
        source=f"{_text(data.get('источник'))} · сборщик {_text(collector.get('имя'))}",
        login=_text(data.get("вход")),
        total=f"разделов {len(raw)} · параметров {params_total}",
        cut=_text(limits.get("причина"), 300) if limits.get("обрезано") else "",
        sections=sections,
        errors=[
            (_text(e.get("запрос"), 300), _text(e.get("код")), _text(e.get("причина"), 300))
            for e in map(_dict, _list(data.get("ошибки")))
        ],
        raw_sections=raw,
        pairs_by_native=notes,
    )


def parameter_rows(doc: ApiMapDocument) -> list[list]:
    """Лист «Параметры»: строка на параметр без свёртки; числа — числами."""
    rows = []
    for section in doc.raw_sections:
        sid = _text(section.get("id"), 300)
        for param in map(_dict, _list(section.get("параметры"))):
            pid = _text(param.get("id"), 400)
            allowed = _dict(param.get("допустимые"))
            value = param.get("значение")
            rows.append(
                [
                    sid,
                    pid,
                    "".join(_INDEX.findall(pid)),
                    _text(param.get("тип")),
                    number(allowed.get("мин")),
                    number(allowed.get("макс")),
                    number(allowed.get("шаг")),
                    "; ".join(_text(v, 60) for v in _list(allowed.get("значения"))),
                    number(allowed.get("длина_макс")),
                    "да" if param.get("запись") is False else "нет",
                    number(value),
                    _current(param) if value is None else _text(value),
                    _text(param.get("чувствительность")),
                    "да" if param.get("изменчивый") else "нет",
                    doc.pairs_by_native.get(pid, ""),
                    doc.source.split(" · ")[0],
                ]
            )
    return rows


def section_rows(doc: ApiMapDocument) -> list[list]:
    """Лист «Разделы»."""
    rows = []
    for section, shown_section in zip(doc.raw_sections, doc.sections, strict=True):
        rows.append(
            [
                _text(section.get("id"), 300),
                _text(section.get("класс")),
                _text(section.get("чтение"), 300),
                _text(section.get("описание"), 300),
                section.get("код") if isinstance(section.get("код"), int) else None,
                number(section.get("байт")),
                _text(section.get("чувствительность")),
                shown_section.count,
                sum(1 for line in shown_section.params if line.note.startswith(("пара", "!"))),
                sum(1 for line in shown_section.params if line.bad),
            ]
        )
    return rows
