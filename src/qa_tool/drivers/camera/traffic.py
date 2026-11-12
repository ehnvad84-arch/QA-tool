"""Карта родного API по трафику веб-обхода (api-discovery, S2): для камеры без сборщика.

Источник — только то, что страница камеры уже запросила при обходе (`BrowserSession.traffic`);
своих запросов к камере здесь нет. Значения — из ответов GET (JSON, XML, «ключ=значение»);
у записей — только имена полей; остановленные табу запросы — строкой без значений.
Допустимые значения в трафике не видны, поэтому сверки с ONVIF и автопоиска по такой карте нет.

Ответы камеры — недоверенный ввод: XML без DTD, JSON без NaN, глубина и число элементов
ограничены, обход без рекурсии; чувствительность разделов и параметров решает `Section.add_param`.
"""

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from qa_tool.core.api_map import (
    NETWORK_CLASS,
    MapError,
    NativeMap,
    Section,
    finite_number,
    is_secret,
)
from qa_tool.core.taboo import web_forbidden_word
from qa_tool.drivers.camera.browser import StoppedRequest, TrafficEntry
from qa_tool.drivers.camera.key_value import is_error_reply, parse_key_value

SOURCE = "трафик веб-обхода"
MAX_DEPTH = 32
MAX_ITEMS = 64  # элементов списка JSON: дальше — те же параметры следующих каналов
MAX_QUERY_VALUE = 64
STOPPED = "запись замечена, остановлена табу"
# Класс раздела по словам адреса: так приложение и S5 понимают, что это за раздел.
_CLASS_WORDS = (
    ("картинка", ("image", "color", "exposure", "videoinoptions", "videocolor", "isp", "daynight")),
    ("кодировщик", ("encode", "video", "stream", "codec")),
    ("наложения", ("osd", "overlay", "widget", "title")),
    ("события", ("event", "alarm", "motion", "detect")),
    ("система", ("system", "device", "info", "version", "time")),
)
_TOKEN_LIKE = re.compile(r"^[0-9a-fA-F-]{16,}$")


def _class_of(path: str) -> str:
    """Класс раздела по адресу; сеть, учётные записи и безопасность — всегда свой класс."""
    if web_forbidden_word(path):
        return NETWORK_CLASS
    low = path.lower()
    return next((cls for cls, words in _CLASS_WORDS if any(w in low for w in words)), "прочее")


def _section_id(method: str, url: str) -> tuple[str, str]:
    """id раздела и путь: путь и параметры адреса без значений-секретов и похожих на токен."""
    parts = urlsplit(url)
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        hidden = is_secret(key) or len(value) > MAX_QUERY_VALUE or _TOKEN_LIKE.match(value)
        kept.append(f"{key}=…" if hidden else f"{key}={value}")
    query = "&".join(kept)
    path = parts.path or "/"
    target = f"{path}?{query}" if query else path
    return (target if method == "GET" else f"{method} {target}"), path


def _guess(value: Any) -> str:
    """Тип по значению: допустимые в трафике не видны."""
    if isinstance(value, bool) or (isinstance(value, str) and value.lower() in ("true", "false")):
        return "логический"
    number = finite_number(value) if isinstance(value, int | float | str) else None
    if number is None:
        return "строка" if value is not None else "неизвестно"
    return "целое" if float(number).is_integer() and "." not in str(value) else "число"


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape(key: str) -> str:
    """Ключ JSON для указателя (RFC 6901): «~» и «/» не ломают путь."""
    return key.replace("~", "~0").replace("/", "~1")


def _json_leaves(body: bytes) -> list[tuple[str, Any]]:
    """Листья JSON по JSON-указателям; NaN, глубина и огромные списки — отказ или обрезка."""

    def reject(name: str) -> None:
        raise ValueError(f"нечисловое значение {name}")

    data = json.loads(body.decode("utf-8"), parse_constant=reject)
    found: list[tuple[str, Any]] = []
    stack: list[tuple[Any, str, int]] = [(data, "", 0)]
    while stack:
        value, pointer, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise ValueError(f"вложенность JSON больше {MAX_DEPTH}")
        if isinstance(value, dict):
            items = [(v, f"{pointer}/{_escape(str(k))}") for k, v in value.items()]
        elif isinstance(value, list):
            items = [(v, f"{pointer}/{i}") for i, v in enumerate(value[:MAX_ITEMS])]
        else:
            found.append((pointer or "/", value))
            continue
        stack.extend((v, p, depth + 1) for v, p in reversed(items))
    return found


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_leaves(body: bytes) -> list[tuple[str, Any]]:
    """Листья XML по путям локальных имён; повторы соседей — `[i]`; без DTD и рекурсии."""
    root: Element = fromstring(body, forbid_dtd=True)
    found: list[tuple[str, Any]] = []
    stack: list[tuple[Element, str, int]] = [(root, "", 0)]
    while stack:
        element, path, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise ValueError(f"вложенность XML больше {MAX_DEPTH}")
        children = [c for c in element if _local(c.tag)]
        if not children:
            if path:
                found.append((path, (element.text or "").strip()))
            continue
        names = [_local(c.tag) for c in children]
        seen: dict[str, int] = {}
        named = []
        for child, name in zip(children, names, strict=True):
            seen[name] = seen.get(name, 0) + 1
            label = f"{name}[{seen[name]}]" if names.count(name) > 1 else name
            named.append((child, f"{path}/{label}" if path else label))
        stack.extend((c, p, depth + 1) for c, p in reversed(named[:MAX_ITEMS]))
    return found


def _leaves(entry: TrafficEntry) -> list[tuple[str, Any]]:
    """Листья тела ответа по его виду; не разобралось — ValueError."""
    kind, text = entry.content_type, entry.body.lstrip()
    if "json" in kind or text[:1] in (b"{", b"["):
        return [(f"#{p}", v) for p, v in _json_leaves(entry.body)]
    if "xml" in kind or text[:1] == b"<":
        try:
            return [(f"#/{p}", v) for p, v in _xml_leaves(entry.body)]
        except (DefusedXmlException, ParseError) as error:
            raise ValueError(f"XML отвергнут: {type(error).__name__}") from None
    decoded = entry.body.decode("utf-8", "replace")
    if is_error_reply(decoded):
        raise ValueError("камера ответила ошибкой")
    return [(f"#{k}", v) for k, v in parse_key_value(decoded).pairs.items()]


def from_traffic(
    entries: Iterable[TrafficEntry], stopped: Iterable[StoppedRequest], meta: NativeMap
) -> NativeMap:
    """Карта из трафика обхода.

    Args:
        entries: запросы страницы к камере по порядку обхода.
        stopped: запросы, которые программа не пустила к камере (табу, опасные действия).
        meta: шапка карты (бренд, модель, прогон); разделы добавляются в неё же.

    Returns:
        `meta` с разделами: GET с данными — параметры со значениями; запись — имена полей
        без значений; ответ не 2xx или неразборчивый — в «ошибки» карты.
    """
    meta.source = SOURCE
    seen: set[str] = set()
    for entry in entries:
        section_id, path = _section_id(entry.method, entry.url)
        if section_id in seen:
            continue
        if not 200 <= entry.status < 300:
            meta.errors.append(MapError(section_id, entry.status, "страница получила ошибку"))
            seen.add(section_id)
            continue
        if entry.method == "GET" and not entry.body:
            continue  # страница или ресурс без данных: в карту не идёт
        seen.add(section_id)
        section = Section(section_id, _class_of(path), f"{entry.method} {section_id}", code=200)
        if entry.method != "GET":
            for name in entry.fields:
                section.add_param(f"{section_id}#/{name}", writable=True)
        else:
            try:
                leaves = _leaves(entry)
            except (ValueError, RecursionError, UnicodeDecodeError) as error:
                meta.errors.append(MapError(section_id, entry.status, f"не разобран: {error}"))
                continue
            for pointer, value in leaves:
                section.add_param(f"{section_id}{pointer}", _guess(value), None, _text(value))
        if section.params:
            meta.sections.append(section)
    for request in stopped:
        section_id, _path = _section_id(request.method, request.url)
        if section_id not in seen:
            seen.add(section_id)
            meta.errors.append(MapError(section_id, None, STOPPED))
    return meta
