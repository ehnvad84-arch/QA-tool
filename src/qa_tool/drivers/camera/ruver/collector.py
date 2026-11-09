"""Сборщик карты «Рувер» по OpenAPI-карте адаптера `data/ruver_api_map.json` (S1, S5, S8).

Самоописание здесь — карта программы, а не ответ камеры: операции вида «чтение», их схемы
(`type`, `enum`, `minimum`/`maximum`), значения — из ответов. Диапазон, который камера отдаёт
рядом со значением (`brightness` и `brightnessRange: {min, max}`), становится допустимыми значениями
параметра. Операции с пометкой `sensitive` (пользователи, SNMP, LTE, HTTPS, выгрузка настроек)
не читаются вовсе — раздел строится только из схемы.

К камере сборщик ходит через сеанс `RuverClient` (`ruver_fetch`): у «Рувера» свой вход, одна
попытка и общий отказ с `API-RUVER-1`. id параметра — `<путь операции>[?запрос]#<JSON-указатель>`.
"""

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from qa_tool.core.api_map import (
    NETWORK_CLASS,
    NativeMap,
    Param,
    Section,
    Sensitivity,
    allowed_length,
    allowed_range,
    allowed_values,
    finite_number,
)
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.native import NativeWrite, NotSent, ParseRefused, ReadTemplate
from qa_tool.drivers.camera.ruver.client import (
    READ,
    RETURN_ONLY,
    WRITE,
    ApiMap,
    Operation,
    RuverClient,
    load_map,
)

MAX_DEPTH = 32
MAX_ITEMS = 64  # элементов массива в карту: дальше те же параметры (зоны, пресеты)
MAX_BODY = 2 << 20
# Класс раздела по началу пути операции; не найдено — «прочее».
_CLASSES = (
    ("/api/image", "картинка"),
    ("/api/daynight", "картинка"),
    ("/api/video", "кодировщик"),
    ("/api/osd", "наложения"),
    ("/api/detector", "события"),
    ("/api/events", "события"),
    ("/api/shock_sensor", "события"),
    ("/api/network", NETWORK_CLASS),
    ("/api/users", NETWORK_CLASS),
    ("/api/snmp", NETWORK_CLASS),
    ("/api/lte", NETWORK_CLASS),
    ("/api/datetime", NETWORK_CLASS),
    ("/api/info", "система"),
    ("/api/hardware", "система"),
    ("/api/manifest", "система"),
    ("/api/service", "система"),
)
_RESTORE_CLASSES = ("картинка",)
_SCHEMA_TYPES = {"integer": "целое", "number": "число", "boolean": "логический", "string": "строка"}


def class_of(path: str) -> str:
    """Класс раздела по пути операции."""
    return next((cls for prefix, cls in _CLASSES if path.startswith(prefix)), "прочее")


def _query(op: Operation) -> str:
    """Параметры адреса операции строкой, как их отправит `RuverClient`."""
    return urlencode(op.query) if op.query else ""


def _section_id(op: Operation) -> str:
    """id раздела: путь и параметры адреса — у «Рувера» одна операция на время суток."""
    query = _query(op)
    return f"{op.path}?{query}" if query else op.path


def _text(value: Any) -> str | None:
    """Значение листа строкой как в JSON: `true`, `50`, `1.5`; объект и null — без значения."""
    if value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    return str(value)


def _escape(key: str) -> str:
    """Ключ в JSON-указателе (RFC 6901)."""
    return key.replace("~", "~0").replace("/", "~1")


def _describe(schema: dict, value: Any, sibling_range: Any) -> tuple[str, dict | None]:
    """Тип и допустимые значения листа: схема карты, затем диапазон из ответа камеры."""
    if "enum" in schema and isinstance(schema["enum"], list):
        return "перечисление", allowed_values([_text(v) or "" for v in schema["enum"]])
    kind = schema.get("type")
    type_ = _SCHEMA_TYPES.get(kind) if isinstance(kind, str) else None
    if type_ is None:
        type_ = _guess(value)
    if isinstance(sibling_range, dict) and "min" in sibling_range and "max" in sibling_range:
        return type_, allowed_range(sibling_range["min"], sibling_range["max"])
    if "minimum" in schema and "maximum" in schema:
        return type_, allowed_range(schema["minimum"], schema["maximum"])
    if type_ == "строка" and "maxLength" in schema:
        return type_, allowed_length(schema["maxLength"])
    return type_, None


def _guess(value: Any) -> str:
    """Тип по значению, если схема его не называет."""
    if isinstance(value, bool):
        return "логический"
    if isinstance(value, int):
        return "целое"
    if isinstance(value, float):
        return "число"
    if isinstance(value, str):
        return "строка"
    return "неизвестно"


def _load(body: bytes) -> Any:
    """JSON ответа камеры: размер до разбора, без NaN, вложенность и огромные числа — отказ."""
    if len(body) > MAX_BODY:
        raise ParseRefused("ответ больше 2 МБ")

    def reject(name: str) -> None:
        raise ParseRefused(f"нечисловое значение {name}")

    try:
        return json.loads(body.decode("utf-8"), parse_constant=reject)
    except ParseRefused:
        raise
    except (RecursionError, ValueError, UnicodeDecodeError) as error:
        raise ParseRefused(f"не JSON: {type(error).__name__}") from None


def _walk(data: Any, schema: dict | None, section: Section, base: str, volatile: bool) -> None:
    """Листья ответа (или одной схемы, если ответа нет) в параметры раздела; без рекурсии."""
    stack: list[tuple[Any, dict, str, int, Any]] = [(data, schema or {}, "", 0, None)]
    while stack:
        value, node, pointer, depth, sibling_range = stack.pop()
        if depth > MAX_DEPTH:
            raise ParseRefused(f"вложенность JSON больше {MAX_DEPTH}")
        node = node if isinstance(node, dict) else {}
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        is_object = isinstance(value, dict) or (value is None and node.get("type") == "object")
        if is_object:
            keys = list(value) if isinstance(value, dict) else list(props)
            children = []
            for key in keys:
                if not isinstance(key, str) or (key.endswith("Range") and key[:-5] in keys):
                    continue  # диапазон идёт в допустимые значения соседа, а не отдельным листом
                child = value.get(key) if isinstance(value, dict) else None
                near = value.get(key + "Range") if isinstance(value, dict) else None
                children.append((child, props.get(key, {}), f"{pointer}/{_escape(key)}", near))
            stack.extend((c, s, p, depth + 1, r) for c, s, p, r in reversed(children))
            continue
        if isinstance(value, list):
            items = node.get("items") if isinstance(node.get("items"), dict) else {}
            stack.extend(
                (item, items, f"{pointer}/{i}", depth + 1, None)
                for i, item in reversed(list(enumerate(value[:MAX_ITEMS])))
            )
            continue
        type_, allowed = _describe(node, value, sibling_range)
        section.add_param(
            f"{base}#{pointer or '/'}", type_, allowed, _text(value), volatile=volatile
        )


class RuverCollector:
    """Сборщик «Рувер» по карте адаптера; карта берётся из файла программы, если не передана."""

    brand = "ruver"
    name = "OpenAPI"
    version = 1
    names = frozenset({"ruver", "ruware", "рувер"})
    login = "session"
    # операций чтения в карте 148, из них 15 только описание: запас на новые версии карты
    max_requests = 160

    def __init__(self, api: ApiMap | None = None) -> None:
        """Подготовить сборщик по карте `api` (по умолчанию — `data/ruver_api_map.json`)."""
        self.api = api or load_map()
        self._by_target = {_section_id(op): op for op in self.api.operations if self._readable(op)}

    @staticmethod
    def _readable(op: Operation) -> bool:
        """Операция годится для карты: чтение, не двоичная, все параметры адреса известны."""
        return op.kind == READ and op.method == "GET" and not op.binary and not op.missing

    def operation(self, target: str) -> Operation | None:
        """Операция чтения по пути с запросом; None — такой в карте нет."""
        return self._by_target.get(target)

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Все операции чтения карты; чувствительные — только описание, без запроса."""
        return [
            ReadTemplate(
                op.path,
                _section_id(op),
                class_of(op.path),
                query=_query(op),
                offline=op.sensitive,
                sensitivity=Sensitivity.SENSITIVE if op.sensitive else Sensitivity.NORMAL,
            )
            for op in self._by_target.values()
        ]

    def parse(
        self, template: ReadTemplate, body: bytes, content_type: str, describe: bytes | None
    ) -> Section:
        """Раздел из ответа JSON и схемы операции; без ответа — только из схемы.

        Raises:
            ParseRefused: операции нет в карте, не JSON, NaN, слишком глубокое дерево.
        """
        op = self.operation(template.section_id)
        if op is None:
            raise ParseRefused("операции нет в карте «Рувер»")
        data = None if template.offline else _load(body)
        section = Section(template.section_id, template.cls, "", sensitivity=template.sensitivity)
        _walk(data, op.schema, section, template.section_id, volatile=op.live)
        return section

    def expected(self, kind: str) -> tuple[str, ...]:
        """Разделы, где ждём сдвиг при записи по ONVIF настройки класса `kind`."""
        return tuple(f"{prefix}*" for prefix, cls in _CLASSES if cls == kind)

    def observed(self, native: NativeMap) -> list[str]:
        """Разделы для снимков автопоиска: картинка, кодировщик, наложения со значениями."""
        return [
            s.id
            for s in native.sections
            if s.cls in ("картинка", "кодировщик", "наложения")
            and s.sensitivity is Sensitivity.NORMAL
            and any(p.value is not None for p in s.params)
        ]

    def restore_request(self, param: Param, m0: NativeMap) -> NativeWrite | None:
        """Запись настройки картинки операцией карты со значением снимка M0.

        Возвращается только лист `/<setting>` операции записи того же адреса и тех же параметров
        адреса; тело — `{setting: значение M0}` с типом из карты. Выполнять — через `RuverClient`:
        он сам отказывает вне карты и без галочки «изменяющие».

        Returns:
            Запрос или None: не картинка, нет операции записи этого листа, нет значения в M0.
        """
        target, _, pointer = param.id.partition("#")
        read = self.operation(target)
        if read is None or class_of(read.path) not in _RESTORE_CLASSES:
            return None
        write = next(
            (
                op
                for op in self.api.operations
                if op.kind in (WRITE, RETURN_ONLY)
                and op.path == read.path
                and op.query == read.query
                and op.setting
                and pointer == f"/{_escape(op.setting)}"
            ),
            None,
        )
        before = m0.params().get(param.id)
        if write is None or before is None or before.value is None:
            return None
        value = _typed(before)
        if value is None:
            return None
        body = json.dumps({write.setting: value}, ensure_ascii=False)
        return NativeWrite(write.method, write.path, _query(write), body)


def _typed(param: Param) -> Any:
    """Значение M0 в тип JSON по типу параметра карты; негодное число — None."""
    if param.type == "логический":
        return param.value == "true"
    if param.type in ("целое", "число"):
        number = finite_number(param.value)
        if number is None:
            return None
        return int(number) if param.type == "целое" and number.is_integer() else number
    return param.value


def ruver_fetch(client: RuverClient, collector: RuverCollector) -> Callable[[str], http.Response]:
    """Отправка `NativeReader` через сеанс «Рувер»: только операции чтения карты.

    Raises (из возвращённой функции):
        NotSent: операции нет среди операций чтения карты.
        LoginRejected, LoginFailed, NoFreeSession, TransportError: как у `RuverClient.call`.
    """

    def fetch(target: str) -> http.Response:
        op = collector.operation(target)
        if op is None:
            raise NotSent(f"{target}: операции чтения нет в карте «Рувер»")
        return client.call(op)

    return fetch
