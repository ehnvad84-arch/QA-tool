"""Карта родного API камеры и таблица соответствий «ONVIF ↔ родной параметр» (версия 1).

Карта — что камера объявила о своём родном API (Hikvision ISAPI, Dahua CGI, «Рувер» OpenAPI…):
разделы, параметры, тип, допустимые значения и текущее значение. Её снимают сборщики брендов
(`drivers/camera/<бренд>/collector.py`), по ней сверяют ONVIF (S3) и ищут соответствия (S4), её
кладут в библиотеку (S7). Файлы `api-map.json` и `api-pairs.json` — общий контракт сборщиков и
будущих адаптеров (AD-4), поэтому у них есть формат и версия, а ключи — по-русски, как
в `measure.json` «Кодеков».

Чувствительность решается здесь, а не в сборщике: значения разделов сети, учётных записей и
безопасности не сохраняются, секреты (пароли, ключи, токены) отбрасываются ещё при разборе ответа и
в снимки не попадают. Всё, что пришло от камеры или из прошлых файлов, — недоверенные данные:
числа проверяются на конечность и предел, строки — на длину, файлы — по размеру и схеме.
"""

import json
import math
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from jsonschema import Draft7Validator

from qa_tool.core.taboo import web_forbidden_word, words
from qa_tool.core.write_journal import write_atomic

MAP_FORMAT = "qa-tool/api-map"
PAIRS_FORMAT = "qa-tool/api-pairs"
VERSION = 1
MAP_FILE = "api-map.json"
PAIRS_FILE = "api-pairs.json"

# Пределы защиты от враждебного ввода (раздел «Безопасность» спеки): это не допуски проверки,
# поэтому константы кода, а не thresholds.yaml.
MAX_MAP_BYTES = 8 << 20
MAX_PAIRS_BYTES = 2 << 20
MAX_PARAMS = 5000
MAX_ID_LEN = 256
MAX_VALUE_LEN = 1024
MAX_OPTIONS = 256
MAX_NUMBER = 1e9
CLIPPED = "…"

SECTION_CLASSES = (
    "картинка",
    "кодировщик",
    "наложения",
    "события",
    "система",
    "сеть и безопасность",
    "прочее",
)
NETWORK_CLASS = "сеть и безопасность"
PARAM_TYPES = ("целое", "число", "логический", "перечисление", "строка", "объект", "неизвестно")
SOURCES = ("самоописание", "трафик веб-обхода")
SCALE_KINDS = ("линейная", "по диапазонам", "перечисление", "по точкам")
PAIR_STATES = ("найдена", "кандидат", "подтверждена вручную")


class Sensitivity(StrEnum):
    """Можно ли сохранять значение параметра.

    «чувствительный» — сеть, учётные записи, безопасность, время, облако, почта: описание есть,
    значения нет. «секрет» — пароли, ключи, токены, сертификаты: значение отбрасывается при разборе.
    """

    NORMAL = "обычный"
    SENSITIVE = "чувствительный"
    SECRET = "секрет"


_RANK = {Sensitivity.NORMAL: 0, Sensitivity.SENSITIVE: 1, Sensitivity.SECRET: 2}

# Сверх широкого списка веб-обхода: адреса мультикаста и почтовые и FTP-серверы — тоже сеть.
_EXTRA_SENSITIVE_WORDS = frozenset({"ftp", "sftp", "smtp", "mail", "email"})
_EXTRA_SENSITIVE_PREFIXES = ("multicast", "email", "smtp")
# Сравнение по целым словам camelCase и по парам соседних слов («private key», «apiKey»):
# так «keyFrameInterval» не секрет, а «PrivateKey» — секрет.
_SECRET_WORD = re.compile(
    r"(?:pass(?:word|wd|phrase)?|pwd|psw|secret|token|salt|credential|cert(?:ificate)?|psk"
    r"|privatekey|apikey|encrypt(?:ion)?key|парол\w*)s?"
)


def is_secret(path: str) -> bool:
    """Похоже ли имя параметра на секрет: пароль, ключ, токен, соль, сертификат."""
    found = words(path)
    candidates = found + [a + b for a, b in zip(found, found[1:], strict=False)]
    return any(_SECRET_WORD.fullmatch(w) for w in candidates)


def classify(
    path: str, section_class: str = "прочее", inherited: Sensitivity = Sensitivity.NORMAL
) -> Sensitivity:
    """Чувствительность параметра или раздела.

    Порядок: секрет по словам имени; затем наследованное от раздела (параметр его не понижает);
    затем класс раздела «сеть и безопасность»; затем слова-признаки веб-обхода
    (`taboo.web_forbidden_word`) и мультикаст, почта, FTP.

    Args:
        path: id раздела или параметра (путь ресурса, ключ CGI, JSON-указатель).
        section_class: класс раздела от сборщика, один из `SECTION_CLASSES`.
        inherited: чувствительность раздела, в котором лежит параметр.

    Returns:
        Уровень чувствительности.
    """
    if inherited is Sensitivity.SECRET or is_secret(path):
        return Sensitivity.SECRET
    if inherited is Sensitivity.SENSITIVE or section_class == NETWORK_CLASS:
        return Sensitivity.SENSITIVE
    if web_forbidden_word(path):
        return Sensitivity.SENSITIVE
    for word in words(path):
        if word in _EXTRA_SENSITIVE_WORDS or word.startswith(_EXTRA_SENSITIVE_PREFIXES):
            return Sensitivity.SENSITIVE
    return Sensitivity.NORMAL


def finite_number(raw: object) -> float | None:
    """Число из ответа камеры: конечное и не больше 10⁹ по модулю, иначе None.

    Строка из тысяч цифр, `NaN`, `1e999` и `True` числами не считаются.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        number = float(raw)
    elif isinstance(raw, str) and len(raw) <= 64:
        try:
            number = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or abs(number) > MAX_NUMBER:
        return None
    return number


def _plain(number: float) -> int | float:
    """Целое число записывается без «.0»: в карте и в протоколе так, как его объявила камера."""
    return int(number) if number.is_integer() else number


def allowed_range(lo: object, hi: object, step: object = None) -> dict:
    """Допустимые значения «от и до» из ответа камеры.

    Args:
        lo: нижняя граница как прислала камера (число или строка).
        hi: верхняя граница.
        step: шаг, если объявлен; негодный шаг просто не записывается.

    Returns:
        `{"мин", "макс"[, "шаг"]}` или `{"негодный": причина}` — тогда строка `API-RANGE`
        «не прошла: камера объявила негодный диапазон», а в запись S4 такой диапазон не идёт.
    """
    low, high = finite_number(lo), finite_number(hi)
    if low is None or high is None:
        return {
            "негодный": f"граница не число или вне ±10⁹: {_clip(str(lo), 40)}…{_clip(str(hi), 40)}"
        }
    if low > high:
        return {"негодный": f"мин больше макс: {_plain(low)} > {_plain(high)}"}
    result = {"мин": _plain(low), "макс": _plain(high)}
    pace = finite_number(step)
    if pace is not None and pace > 0:
        result["шаг"] = _plain(pace)
    return result


def allowed_values(values: list) -> dict:
    """Допустимые значения перечислением; больше `MAX_OPTIONS` — список негодный."""
    if len(values) > MAX_OPTIONS:
        return {"негодный": f"значений больше {MAX_OPTIONS}: {len(values)}"}
    return {"значения": [_clip(str(v), MAX_VALUE_LEN) for v in values]}


def allowed_length(limit: object) -> dict | None:
    """Наибольшая длина строки; негодное число — «не объявлены» (None)."""
    number = finite_number(limit)
    if number is None or number < 0 or not number.is_integer():
        return None
    return {"длина_макс": int(number)}


def _clip(text: str, limit: int) -> str:
    """Строка не длиннее `limit` знаков; обрезанная кончается «…»."""
    return text if len(text) <= limit else text[: limit - len(CLIPPED)] + CLIPPED


@dataclass(frozen=True)
class Param:
    """Лист раздела родного API.

    `id` устойчив между прошивками: Hikvision — путь ресурса без `/ISAPI/` и имена элементов
    (`Image/channels/1/Color/brightnessLevel`), Dahua — ключ без `table.`
    (`VideoColor[0][0].Brightness`), «Рувер» — `<путь операции>#<JSON-указатель>`.
    `value` — строка как у камеры; None — не читалось, чувствительный параметр или секрет.
    """

    id: str
    type: str = "неизвестно"
    allowed: dict | None = None
    value: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    volatile: bool = False
    writable: bool | None = None


@dataclass
class Section:
    """Один ресурс родного API: адрес ISAPI, конфигурация CGI, операция OpenAPI.

    Параметры добавляются только через `add_param`: он решает чувствительность и не даёт значению
    секрета или сети попасть в карту.
    """

    id: str
    cls: str
    read: str
    describe: str | None = None
    code: int | None = None
    size: int = 0
    sensitivity: Sensitivity = Sensitivity.NORMAL
    params: list[Param] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Раздел не бывает мягче, чем говорят его класс и имя."""
        level = classify(self.id, self.cls)
        if _RANK[level] > _RANK[self.sensitivity]:
            self.sensitivity = level

    def add_param(
        self,
        param_id: str,
        type_: str = "неизвестно",
        allowed: dict | None = None,
        value: object = None,
        *,
        volatile: bool = False,
        writable: bool | None = None,
    ) -> Param | None:
        """Добавить параметр из разобранного ответа камеры.

        Args:
            param_id: id параметра (см. `Param`).
            type_: тип из `PARAM_TYPES`; иной — «неизвестно».
            allowed: результат `allowed_range`, `allowed_values`, `allowed_length` или None.
            value: значение как у камеры; у чувствительных и секретов отбрасывается здесь же.
            volatile: сборщик знает, что параметр меняется сам (состояние, счётчик, время).
            writable: False — камера объявила «только чтение»; None — неизвестно.

        Returns:
            Добавленный параметр или None, если id пустой или длиннее `MAX_ID_LEN` — сборщик
            считает такие в «ошибках» карты.
        """
        if not param_id or len(param_id) > MAX_ID_LEN:
            return None
        level = classify(param_id, self.cls, self.sensitivity)
        text = None
        if level is Sensitivity.NORMAL and value is not None and type_ != "объект":
            text = _clip(str(value), MAX_VALUE_LEN)
        param = Param(
            id=param_id,
            type=type_ if type_ in PARAM_TYPES else "неизвестно",
            allowed=allowed,
            value=text,
            sensitivity=level,
            volatile=volatile,
            writable=writable,
        )
        self.params.append(param)
        return param


@dataclass(frozen=True)
class MapError:
    """Запрос сборщика, не давший раздела: 404 — раздела нет у модели, 403 — нет прав и т. п."""

    request: str
    code: int | None
    reason: str


@dataclass
class NativeMap:
    """Карта родного API одной камеры за один прогон."""

    brand: str
    collector: str
    collector_version: int = 1
    source: str = "самоописание"
    maker: str = ""
    model: str = ""
    firmware: str = ""
    run: str = ""
    place: int = 0
    time: str = ""
    login: str = ""
    sections: list[Section] = field(default_factory=list)
    errors: list[MapError] = field(default_factory=list)
    cut: bool = False
    cut_reason: str = ""

    def params(self) -> dict[str, Param]:
        """Все параметры по id; повтор id в другом разделе не перекрывает первый."""
        found: dict[str, Param] = {}
        for section in self.sections:
            for param in section.params:
                found.setdefault(param.id, param)
        return found

    def param_count(self) -> int:
        """Сколько параметров в карте: строка «карта снята, разделов N, параметров M»."""
        return sum(len(s.params) for s in self.sections)


# --- Разность снимков (S4, S5) ---------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """Параметр, который сдвинулся между двумя снимками карты."""

    id: str
    before: str
    after: str


def canonical(value: str) -> tuple[int, float | str]:
    """Значение для сравнения: число — как число («50» = «50.0»), строка — без регистра и пробелов.

    Первый элемент разводит числа и строки, чтобы «1» и «1.0» совпали, а «1» и «один» — нет.
    """
    number = finite_number(value)
    if number is not None:
        return (0, number)
    return (1, value.strip().casefold())


def _comparable(param: Param, volatile: frozenset[str]) -> bool:
    """В разности участвуют только обычные неизменчивые параметры со значением."""
    return (
        param.sensitivity is Sensitivity.NORMAL
        and param.value is not None
        and not param.volatile
        and param.id not in volatile
    )


def diff(m0: NativeMap, m1: NativeMap, volatile: frozenset[str] = frozenset()) -> list[Change]:
    """Параметры, сдвинувшиеся от снимка `m0` к снимку `m1`.

    Появление или исчезновение параметра — не сдвиг (смена кодека пересобирает раздел
    кодировщика). Изменчивые, чувствительные и секреты не участвуют.

    Args:
        m0: прежний снимок.
        m1: новый снимок.
        volatile: id параметров, которые меняются сами (`volatile_ids`).

    Returns:
        Сдвиги в порядке параметров `m0`.
    """
    after = m1.params()
    changes = []
    for param_id, before in m0.params().items():
        now = after.get(param_id)
        if now is None or not _comparable(before, volatile) or not _comparable(now, volatile):
            continue
        if canonical(before.value) != canonical(now.value):
            changes.append(Change(param_id, before.value, now.value))
    return changes


def volatile_ids(m0a: NativeMap, m0b: NativeMap) -> frozenset[str]:
    """Изменчивые параметры: разошлись в двух исходных снимках без записи или помечены сборщиком."""
    marked = {p.id for p in m0a.params().values() if p.volatile}
    marked |= {p.id for p in m0b.params().values() if p.volatile}
    return frozenset(marked | {c.id for c in diff(m0a, m0b)})


# --- Таблица соответствий ------------------------------------------------------------------


@dataclass(frozen=True)
class Scale:
    """Перевод значения ONVIF в родное.

    «линейная» — `родное = a·onvif + b`; «по диапазонам» — `a`, `b` считаются при сверке из
    объявленных диапазонов; «перечисление» — `values` ONVIF → родное; «по точкам» — шкала не
    линейная, пара остаётся кандидатом.
    """

    kind: str
    a: float | None = None
    b: float | None = None
    values: dict[str, str] | None = None


@dataclass(frozen=True)
class Pair:
    """Соответствие листа ONVIF (номер `n` каталога) и родного параметра.

    `models` и `firmwares` — только у ручных таблиц: пусто — все модели или прошивки бренда.
    """

    n: int
    onvif: str
    native: str
    scale: Scale
    points: tuple[tuple[float, float], ...] = ()
    state: str = "найдена"
    source: str = ""
    models: tuple[str, ...] = ()
    firmwares: tuple[str, ...] = ()


@dataclass
class PairTable:
    """Таблица соответствий: прогона, библиотеки или ручная."""

    maker: str = ""
    model: str = ""
    firmware: str = ""
    pairs: list[Pair] = field(default_factory=list)


# --- Файлы версии 1 ------------------------------------------------------------------------


class MapFormatError(ValueError):
    """Файл карты или таблицы не прочитан: размер, JSON, чужой формат или версия, схема."""


_NUMBER = {"type": "number", "minimum": -MAX_NUMBER, "maximum": MAX_NUMBER}
_TEXT = {"type": "string", "maxLength": MAX_VALUE_LEN}
_ID = {"type": "string", "minLength": 1, "maxLength": MAX_ID_LEN}
_LEVELS = [s.value for s in Sensitivity]
_CAMERA = {
    "type": "object",
    "properties": {"производитель": _TEXT, "модель": _TEXT, "прошивка": _TEXT},
    "required": ["производитель", "модель", "прошивка"],
    "additionalProperties": False,
}
_ALLOWED = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {"мин": _NUMBER, "макс": _NUMBER, "шаг": _NUMBER},
            "required": ["мин", "макс"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"значения": {"type": "array", "items": _TEXT, "maxItems": MAX_OPTIONS}},
            "required": ["значения"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"длина_макс": {"type": "integer", "minimum": 0, "maximum": MAX_NUMBER}},
            "required": ["длина_макс"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"негодный": _TEXT},
            "required": ["негодный"],
            "additionalProperties": False,
        },
    ]
}
_PARAM = {
    "type": "object",
    "properties": {
        "id": _ID,
        "тип": {"enum": list(PARAM_TYPES)},
        "допустимые": _ALLOWED,
        "значение": {"type": ["string", "null"], "maxLength": MAX_VALUE_LEN},
        "чувствительность": {"enum": _LEVELS},
        "изменчивый": {"type": "boolean"},
        "запись": {"type": "boolean"},
    },
    "required": ["id", "тип", "допустимые", "значение", "чувствительность", "изменчивый"],
    "additionalProperties": False,
}
_SECTION = {
    "type": "object",
    "properties": {
        "id": _ID,
        "класс": {"enum": list(SECTION_CLASSES)},
        "чтение": _TEXT,
        "описание": {"type": ["string", "null"], "maxLength": MAX_VALUE_LEN},
        "код": {"type": ["integer", "null"], "minimum": 0, "maximum": 999},
        "байт": {"type": "integer", "minimum": 0},
        "чувствительность": {"enum": _LEVELS},
        "параметры": {"type": "array", "items": _PARAM, "maxItems": MAX_PARAMS},
    },
    "required": ["id", "класс", "чтение", "код", "байт", "чувствительность", "параметры"],
    "additionalProperties": False,
}
MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "формат": {"const": MAP_FORMAT},
        "версия": {"const": VERSION},
        "сборщик": {
            "type": "object",
            "properties": {
                "бренд": _ID,
                "имя": _ID,
                "версия": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["бренд", "имя", "версия"],
            "additionalProperties": False,
        },
        "источник": {"enum": list(SOURCES)},
        "камера": _CAMERA,
        "прогон": _TEXT,
        "место": {"type": "integer", "minimum": 0, "maximum": 1000},
        "время": _TEXT,
        "вход": _TEXT,
        "разделы": {"type": "array", "items": _SECTION, "maxItems": MAX_PARAMS},
        "ошибки": {
            "type": "array",
            "maxItems": MAX_PARAMS,
            "items": {
                "type": "object",
                "properties": {
                    "запрос": _TEXT,
                    "код": {"type": ["integer", "null"], "minimum": 0, "maximum": 999},
                    "причина": _TEXT,
                },
                "required": ["запрос", "код", "причина"],
                "additionalProperties": False,
            },
        },
        "пределы": {
            "type": "object",
            "properties": {"обрезано": {"type": "boolean"}, "причина": _TEXT},
            "required": ["обрезано", "причина"],
            "additionalProperties": False,
        },
    },
    "required": [
        "формат",
        "версия",
        "сборщик",
        "источник",
        "камера",
        "разделы",
        "ошибки",
        "пределы",
    ],
    "additionalProperties": False,
}
_SHORT = {"type": "string", "maxLength": MAX_ID_LEN}
_SCALE = {
    "type": "object",
    "properties": {
        "вид": {"enum": list(SCALE_KINDS)},
        "a": _NUMBER,
        "b": _NUMBER,
        "значения": {
            "type": "object",
            "additionalProperties": _SHORT,
            "propertyNames": {"maxLength": MAX_ID_LEN},
            "maxProperties": MAX_OPTIONS,
        },
    },
    "required": ["вид"],
    "additionalProperties": False,
    "allOf": [
        {"if": {"properties": {"вид": {"const": "линейная"}}}, "then": {"required": ["a", "b"]}},
        {
            "if": {"properties": {"вид": {"const": "перечисление"}}},
            "then": {"required": ["значения"]},
        },
    ],
}
PAIRS_SCHEMA = {
    "type": "object",
    "properties": {
        "формат": {"const": PAIRS_FORMAT},
        "версия": {"const": VERSION},
        "камера": _CAMERA,
        "пары": {
            "type": "array",
            "maxItems": 1000,
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 1, "maximum": 999},
                    "onvif": _SHORT,
                    "родной": _ID,
                    "шкала": _SCALE,
                    "точки": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "array", "items": _NUMBER, "minItems": 2, "maxItems": 2},
                    },
                    "состояние": {"enum": list(PAIR_STATES)},
                    "источник": _SHORT,
                    "модели": {"type": "array", "items": _SHORT, "maxItems": 100},
                    "прошивки": {"type": "array", "items": _SHORT, "maxItems": 100},
                },
                "required": ["n", "onvif", "родной", "шкала", "состояние", "источник"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["формат", "версия", "камера", "пары"],
    "additionalProperties": False,
}
_MAP_VALIDATOR = Draft7Validator(MAP_SCHEMA)
_PAIRS_VALIDATOR = Draft7Validator(PAIRS_SCHEMA)


def _camera_json(maker: str, model: str, firmware: str) -> dict:
    """Шапка «камера»: строки из паспорта ONVIF, обрезанные до предела схемы."""
    return {
        "производитель": _clip(maker, MAX_VALUE_LEN),
        "модель": _clip(model, MAX_VALUE_LEN),
        "прошивка": _clip(firmware, MAX_VALUE_LEN),
    }


def _param_json(param: Param) -> dict:
    """Параметр в файл; значение пишется только у обычного — второй рубеж после `add_param`."""
    data = {
        "id": param.id,
        "тип": param.type,
        "допустимые": param.allowed,
        "значение": param.value if param.sensitivity is Sensitivity.NORMAL else None,
        "чувствительность": param.sensitivity.value,
        "изменчивый": param.volatile,
    }
    if param.writable is False:
        data["запись"] = False
    return data


def _section_json(section: Section) -> dict:
    """Раздел в файл."""
    return {
        "id": section.id,
        "класс": section.cls,
        "чтение": _clip(section.read, MAX_VALUE_LEN),
        "описание": None if section.describe is None else _clip(section.describe, MAX_VALUE_LEN),
        "код": section.code,
        "байт": section.size,
        "чувствительность": section.sensitivity.value,
        "параметры": [_param_json(p) for p in section.params],
    }


def _map_json(native: NativeMap, sections: list[dict], cut: bool, cut_reason: str) -> dict:
    """Карта в словарь файла с уже отобранными разделами."""
    return {
        "формат": MAP_FORMAT,
        "версия": VERSION,
        "сборщик": {
            "бренд": native.brand,
            "имя": native.collector,
            "версия": native.collector_version,
        },
        "источник": native.source,
        "камера": _camera_json(native.maker, native.model, native.firmware),
        "прогон": native.run,
        "место": native.place,
        "время": native.time,
        "вход": native.login,
        "разделы": sections,
        "ошибки": [
            {
                "запрос": _clip(e.request, MAX_VALUE_LEN),
                "код": e.code,
                "причина": _clip(e.reason, MAX_VALUE_LEN),
            }
            for e in native.errors[:MAX_PARAMS]
        ],
        "пределы": {"обрезано": cut, "причина": _clip(cut_reason, MAX_VALUE_LEN)},
    }


def _limit_params(native: NativeMap) -> tuple[list[Section], str]:
    """Разделы, в которых вместе не больше `MAX_PARAMS` параметров, и причина обрезки."""
    if native.param_count() <= MAX_PARAMS:
        return native.sections, ""
    kept, left = [], MAX_PARAMS
    for section in native.sections:
        if left <= 0:
            break
        kept.append(replace(section, params=section.params[:left]))
        left -= len(kept[-1].params)
    return kept, f"параметров {native.param_count()}, записаны первые {MAX_PARAMS}"


def dumps_map(native: NativeMap) -> str:
    """Карта в текст `api-map.json` версии 1 не больше `MAX_MAP_BYTES`.

    Сверх предела число параметров или размер файла режутся с конца, в «пределы» пишется причина:
    протокол показывает «карта обрезана», а не молча неполную карту.

    Raises:
        MapFormatError: карта не проходит схему версии 1 — ошибка сборщика, а не камеры.
    """
    sections, reason = _limit_params(native)
    cut = native.cut or bool(reason)
    reason = "; ".join(r for r in (native.cut_reason, reason) if r)
    parts = [_section_json(s) for s in sections]
    text = _dumps_valid(_map_json(native, parts, cut, reason), _MAP_VALIDATOR)
    if len(text.encode("utf-8")) <= MAX_MAP_BYTES:
        return text
    # Разделов не больше сотни, поэтому подбор с конца по одному дёшев.
    while parts:
        parts.pop()
        why = f"файл больше {MAX_MAP_BYTES >> 20} МБ, записаны первые {len(parts)} разделов"
        text = _dumps_valid(
            _map_json(native, parts, True, "; ".join(r for r in (reason, why) if r)), _MAP_VALIDATOR
        )
        if len(text.encode("utf-8")) <= MAX_MAP_BYTES:
            return text
    raise MapFormatError("карта больше предела даже без разделов")


def _dumps_valid(data: dict, validator: Draft7Validator) -> str:
    """Словарь в JSON после проверки схемой: в файл не уходит то, что потом не прочитается."""
    problem = next(iter(validator.iter_errors(data)), None)
    if problem is not None:
        raise MapFormatError(_problem_text(problem))
    return json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)


def _problem_text(problem) -> str:
    """Причина отказа схемы коротко: где и что; значение из файла обрезается."""
    where = "/".join(str(p) for p in problem.absolute_path) or "корень"
    return _clip(f"{where}: {problem.message}", 300)


def write_map(path: Path, native: NativeMap) -> None:
    """Записать `api-map.json` атомарно.

    Raises:
        MapFormatError: карта не проходит схему.
        OSError: файл не записан.
    """
    write_atomic(path, dumps_map(native))


def _reject_constant(name: str) -> float:
    """`NaN` и `Infinity` в JSON не числа для нас: файл с ними отвергается."""
    raise MapFormatError(f"в файле нечисловое значение {name}")


def _load_json(text: str, limit: int, validator: Draft7Validator) -> dict:
    """Разобрать недоверенный JSON: размер до разбора, без NaN, вложенность и цифры — в причину."""
    if len(text.encode("utf-8")) > limit:
        raise MapFormatError(f"файл больше {limit} байт")
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (RecursionError, ValueError) as error:
        if isinstance(error, MapFormatError):
            raise
        raise MapFormatError(f"не JSON: {_clip(str(error), 200)}") from None
    if not isinstance(data, dict):
        raise MapFormatError("корень файла не объект")
    try:
        problem = next(iter(validator.iter_errors(data)), None)
    except RecursionError:
        raise MapFormatError("слишком глубокая вложенность") from None
    if problem is not None:
        raise MapFormatError(_problem_text(problem))
    return data


def _read_text(path: Path, limit: int) -> str:
    """Прочитать не больше `limit + 1` байт: огромный файл не попадает в память целиком."""
    try:
        with path.open("rb") as file:
            raw = file.read(limit + 1)
    except OSError as error:
        raise MapFormatError(f"файл не прочитан: {error.strerror or error}") from None
    if len(raw) > limit:
        raise MapFormatError(f"файл больше {limit} байт")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MapFormatError("файл не в UTF-8") from None


def loads_map(text: str) -> NativeMap:
    """Карта из текста `api-map.json` версии 1.

    Чувствительность пересчитывается по id и классу раздела: подправленный файл не вернёт значение
    пароля как «обычное».

    Raises:
        MapFormatError: размер, JSON, формат, версия или схема не те.
    """
    data = _load_json(text, MAX_MAP_BYTES, _MAP_VALIDATOR)
    camera, collector, limits = data["камера"], data["сборщик"], data["пределы"]
    native = NativeMap(
        brand=collector["бренд"],
        collector=collector["имя"],
        collector_version=collector["версия"],
        source=data["источник"],
        maker=camera["производитель"],
        model=camera["модель"],
        firmware=camera["прошивка"],
        run=data.get("прогон", ""),
        place=data.get("место", 0),
        time=data.get("время", ""),
        login=data.get("вход", ""),
        errors=[MapError(e["запрос"], e["код"], e["причина"]) for e in data["ошибки"]],
        cut=limits["обрезано"],
        cut_reason=limits["причина"],
    )
    for raw in data["разделы"]:
        section = Section(
            id=raw["id"],
            cls=raw["класс"],
            read=raw["чтение"],
            describe=raw.get("описание"),
            code=raw["код"],
            size=raw["байт"],
            sensitivity=Sensitivity(raw["чувствительность"]),
        )
        for item in raw["параметры"]:
            param = section.add_param(
                item["id"],
                item["тип"],
                item["допустимые"],
                item["значение"],
                volatile=item["изменчивый"],
                writable=item.get("запись"),
            )
            stored = Sensitivity(item["чувствительность"])
            if param is not None and _RANK[stored] > _RANK[param.sensitivity]:
                section.params[-1] = replace(param, sensitivity=stored, value=None)
        native.sections.append(section)
    return native


def read_map(path: Path) -> NativeMap:
    """Прочитать `api-map.json`; см. `loads_map`."""
    return loads_map(_read_text(path, MAX_MAP_BYTES))


def _pair_json(pair: Pair) -> dict:
    """Пара в файл; пустые «модели» и «прошивки» не пишутся."""
    scale: dict = {"вид": pair.scale.kind}
    if pair.scale.a is not None:
        scale["a"] = pair.scale.a
    if pair.scale.b is not None:
        scale["b"] = pair.scale.b
    if pair.scale.values is not None:
        scale["значения"] = dict(pair.scale.values)
    data = {
        "n": pair.n,
        "onvif": pair.onvif,
        "родной": pair.native,
        "шкала": scale,
        "точки": [list(p) for p in pair.points],
        "состояние": pair.state,
        "источник": pair.source,
    }
    if pair.models:
        data["модели"] = list(pair.models)
    if pair.firmwares:
        data["прошивки"] = list(pair.firmwares)
    return data


def dumps_pairs(table: PairTable) -> str:
    """Таблица в текст `api-pairs.json` версии 1.

    Raises:
        MapFormatError: таблица не проходит схему (неизвестный вид шкалы, бесконечное число…).
    """
    data = {
        "формат": PAIRS_FORMAT,
        "версия": VERSION,
        "камера": _camera_json(table.maker, table.model, table.firmware),
        "пары": [_pair_json(p) for p in table.pairs],
    }
    text = _dumps_valid(data, _PAIRS_VALIDATOR)
    if len(text.encode("utf-8")) > MAX_PAIRS_BYTES:
        raise MapFormatError(f"таблица больше {MAX_PAIRS_BYTES} байт")
    return text


def write_pairs(path: Path, table: PairTable) -> None:
    """Записать `api-pairs.json` атомарно.

    Raises:
        MapFormatError: таблица не проходит схему.
        OSError: файл не записан.
    """
    write_atomic(path, dumps_pairs(table))


def loads_pairs(text: str) -> PairTable:
    """Таблица соответствий из текста `api-pairs.json` версии 1 (прогон, библиотека, ручная).

    Номер `n` против каталога ONVIF и родной `id` против карты прогона проверяет сверка: здесь
    только формат.

    Raises:
        MapFormatError: размер, JSON, формат, версия или схема не те.
    """
    data = _load_json(text, MAX_PAIRS_BYTES, _PAIRS_VALIDATOR)
    camera = data["камера"]
    table = PairTable(camera["производитель"], camera["модель"], camera["прошивка"])
    for raw in data["пары"]:
        scale = raw["шкала"]
        table.pairs.append(
            Pair(
                n=raw["n"],
                onvif=raw["onvif"],
                native=raw["родной"],
                scale=Scale(
                    kind=scale["вид"],
                    a=scale.get("a"),
                    b=scale.get("b"),
                    values=scale.get("значения"),
                ),
                points=tuple((float(x), float(y)) for x, y in raw.get("точки", [])),
                state=raw["состояние"],
                source=raw["источник"],
                models=tuple(raw.get("модели", ())),
                firmwares=tuple(raw.get("прошивки", ())),
            )
        )
    return table


def read_pairs(path: Path) -> PairTable:
    """Прочитать `api-pairs.json`; см. `loads_pairs`."""
    return loads_pairs(_read_text(path, MAX_PAIRS_BYTES))
