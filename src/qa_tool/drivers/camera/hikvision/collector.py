"""Сборщик карты Hikvision ISAPI (S1, S5): что читать, как разобрать XML, как вернуть параметр.

ISAPI описывает себя сам: ресурс `…/capabilities` повторяет дерево настроек, а у листьев есть
атрибуты `min`/`max` (число), `opt` (перечисление; `true,false` — логический), `max` у текста
(длина). Значение берётся из ресурса настроек, а если его не читали — из текста `capabilities`.
Образец — Hikvision .114 09.11: 13 разделов из 18 есть, остальные — 404.

Сеть, безопасность, время — только `capabilities` и только описание (значения отбрасывает
`api_map`). У потока блок `Transport` (мультикаст, способ входа) по словам не узнать целиком,
поэтому он помечен чувствительным здесь. XML — недоверенный ввод: `defusedxml` (без DTD и
сущностей), обход без рекурсии, глубина ≤ 32.
"""

import re
import xml.etree.ElementTree as ET

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

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
from qa_tool.drivers.camera.native import NativeWrite, ParseRefused, ReadTemplate

ISAPI = "/ISAPI/"
HIK_NS = "http://www.hikvision.com/ver20/XMLSchema"
MAX_DEPTH = 32
MAX_STREAMS = 4  # основной, дополнительный и до двух ещё: дальше — те же параметры
MAX_BODY = 16 << 10
# Подресурсы картинки, которые ISAPI принимает отдельным PUT: элемент дерева → имя ресурса.
# Возврат родным API разрешён только для них (AD-3, дополнение 09.11).
RESTORABLE = {
    "color": "color",
    "sharpness": "sharpness",
    "wdr": "WDR",
    "blc": "BLC",
    "whitebalance": "whiteBalance",
    "gain": "gain",
    "exposure": "exposure",
    "shutter": "shutter",
    "noisereduce": "noiseReduce",
    "dehaze": "dehaze",
    "hlc": "HLC",
}
# Только описание: значения этих разделов и так отбрасываются, но сами настройки не читаются.
_CAPS_ONLY = (
    ("System/IO", "события"),
    ("Event", "события"),
    ("Smart", "события"),
    ("ContentMgmt", "прочее"),
    ("PTZCtrl/channels/1", "прочее"),
    ("System/time", NETWORK_CLASS),
    ("Security", NETWORK_CLASS),
    ("System/Network", NETWORK_CLASS),
)
_SENSITIVE_BLOCKS = {"Transport"}  # у Streaming/channels/<id>: мультикаст, unicast, вход
# паспорт: серийный номер и имя уже в шапке протокола, MAC — сеть; в карту — только описание
_PASSPORT_LEAVES = {"serialNumber", "deviceName", "deviceID", "macAddress"}
_DIGITS = re.compile(r"^\d{1,4}$")
_STREAM_ID = re.compile(r"^Streaming/channels/StreamingChannel(?:\[\d+\])?/id$")
_IMAGE_PARAM = re.compile(r"^Image/channels/(\d{1,3})/([A-Za-z]+)/")


def _local(tag: object) -> str:
    """Имя элемента без пространства имён; комментарии и инструкции — пусто."""
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _root(body: bytes) -> ET.Element:
    """Разобрать XML камеры без DTD и сущностей."""
    try:
        return fromstring(body, forbid_dtd=True)
    except (DefusedXmlException, ET.ParseError, ValueError) as error:
        raise ParseRefused(f"XML отвергнут: {type(error).__name__}") from None


def leaves(root: ET.Element) -> dict[str, ET.Element]:
    """Листья дерева по относительным путям; повторы соседей — `[i]` с единицы.

    Обход без рекурсии; глубже `MAX_DEPTH` — `ParseRefused`, а не `RecursionError`.
    """
    found: dict[str, ET.Element] = {}
    stack: list[tuple[ET.Element, str, int]] = [(root, "", 0)]
    while stack:
        element, path, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise ParseRefused(f"вложенность XML больше {MAX_DEPTH}")
        children = [c for c in element if _local(c.tag)]
        if not children and path:
            found.setdefault(path, element)
            continue
        counts: dict[str, int] = {}
        for child in children:
            counts[_local(child.tag)] = counts.get(_local(child.tag), 0) + 1
        seen: dict[str, int] = {}
        named = []
        for child in children:
            name = _local(child.tag)
            seen[name] = seen.get(name, 0) + 1
            if counts[name] > 1:
                name = f"{name}[{seen[name]}]"
            named.append((child, f"{path}/{name}" if path else name))
        stack.extend((child, child_path, depth + 1) for child, child_path in reversed(named))
    return found


def _describe(element: ET.Element | None) -> tuple[str, dict | None]:
    """Тип и допустимые значения листа по атрибутам `capabilities`."""
    if element is None:
        return "", None
    low, high, opt = element.get("min"), element.get("max"), element.get("opt")
    if opt is not None:
        options = [o for o in opt.split(",") if o != ""]
        if options and {o.lower() for o in options} <= {"true", "false"}:
            return "логический", allowed_values(options)
        return "перечисление", allowed_values(options)
    if low is not None and high is not None:
        allowed = allowed_range(low, high)
        whole = all((n := finite_number(x)) is not None and n.is_integer() for x in (low, high))
        return ("целое" if whole else "число"), allowed
    if high is not None:
        return "строка", allowed_length(high)
    return "", None


def _guess(value: str | None) -> str:
    """Тип по значению, когда камера его не объявила."""
    if value is None or value == "":
        return "неизвестно"
    if value.lower() in ("true", "false"):
        return "логический"
    number = finite_number(value)
    if number is not None:
        return "целое" if number.is_integer() and "." not in value else "число"
    return "строка"


class HikvisionCollector:
    """Сборщик ISAPI. Без состояния: всё, что нужно, — в уже прочитанных разделах."""

    brand = "hikvision"
    name = "ISAPI"
    version = 1
    names = frozenset({"hikvision", "hik"})
    login = "digest"

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Запросы чтения: паспорт, картинка, потоки (≤ 4 из списка камеры), наложения, описания."""
        templates = [
            ReadTemplate(ISAPI + "System/deviceInfo", "System/deviceInfo", "система"),
            ReadTemplate(ISAPI + "System/capabilities", "System/capabilities", "система"),
            ReadTemplate(
                ISAPI + "Image/channels/1",
                "Image/channels/1",
                "картинка",
                describe_path=ISAPI + "Image/channels/1/capabilities",
            ),
            ReadTemplate(ISAPI + "Streaming/channels", "Streaming/channels", "кодировщик"),
            ReadTemplate(
                ISAPI + "System/Video/inputs/channels/1/overlays",
                "System/Video/inputs/channels/1/overlays",
                "наложения",
                describe_path=ISAPI + "System/Video/inputs/channels/1/overlays/capabilities",
            ),
        ]
        templates += [
            ReadTemplate(f"{ISAPI}{path}/capabilities", path, cls) for path, cls in _CAPS_ONLY
        ]
        for channel in self._stream_ids(found):
            path = f"Streaming/channels/{channel}"
            templates.append(
                ReadTemplate(
                    ISAPI + path,
                    path,
                    "кодировщик",
                    describe_path=f"{ISAPI}{path}/capabilities",
                )
            )
        return templates

    @staticmethod
    def _stream_ids(found: list[Section]) -> list[str]:
        """Номера потоков из списка камеры: только цифры, не больше `MAX_STREAMS`."""
        listing = next((s for s in found if s.id == "Streaming/channels"), None)
        if listing is None:
            return []
        ids = []
        for param in listing.params:
            own_id = _STREAM_ID.match(param.id)
            if own_id and param.value and _DIGITS.match(param.value) and param.value not in ids:
                ids.append(param.value)
        return ids[:MAX_STREAMS]

    def parse(
        self, template: ReadTemplate, body: bytes, content_type: str, describe: bytes | None
    ) -> Section:
        """Раздел из XML настроек и `capabilities` (или только из `capabilities`).

        Raises:
            ParseRefused: не XML, DTD или сущности, слишком глубокое дерево.
        """
        settings = leaves(_root(body))
        caps = leaves(_root(describe)) if describe is not None else {}
        if template.path.endswith("/capabilities"):
            caps, settings = settings, {}
        section = Section(template.section_id, template.cls, "", sensitivity=template.sensitivity)
        listing = template.section_id == "Streaming/channels"
        for rel in list(settings) + [p for p in caps if p not in settings]:
            source = settings.get(rel, caps.get(rel))
            text = (source.text or "").strip()
            type_, allowed = _describe(caps.get(rel))
            value = text if text != "" else None
            # в списке потоков блок — второй уровень (StreamingChannel[i]/Transport/…)
            parts = rel.split("/")
            block = parts[1 if listing and len(parts) > 1 else 0].split("[", 1)[0]
            sensitive = block in _SENSITIVE_BLOCKS or (
                template.section_id == "System/deviceInfo" and rel in _PASSPORT_LEAVES
            )
            section.add_param(
                f"{template.section_id}/{rel}",
                type_ or _guess(value),
                allowed,
                value,
                at_least=Sensitivity.SENSITIVE if sensitive else Sensitivity.NORMAL,
            )
        return section

    def expected(self, kind: str) -> tuple[str, ...]:
        """Разделы, где ждём сдвиг при записи по ONVIF настройки класса `kind`."""
        if kind == "картинка":
            return ("Image/channels/*",)
        if kind == "кодировщик":
            return ("Streaming/channels/*",)
        return ()

    def observed(self, native: NativeMap) -> list[str]:
        """Разделы для снимков автопоиска: картинка, потоки, наложения — с обычными значениями."""
        return [
            s.id
            for s in native.sections
            if s.cls in ("картинка", "кодировщик", "наложения")
            and s.sensitivity is Sensitivity.NORMAL
            and s.id != "Streaming/channels"
            and any(p.value is not None and p.sensitivity is Sensitivity.NORMAL for p in s.params)
        ]

    def restore_request(self, param: Param, m0: NativeMap) -> NativeWrite | None:
        """PUT подресурса картинки с поддеревом снимка M0 (так вернули GainLevel на .111 09.11).

        Returns:
            Запрос или None: не картинка, подресурс не из `RESTORABLE`, в поддереве повторы
            или нет значений, тело больше 16 КБ.
        """
        found = _IMAGE_PARAM.match(param.id)
        if found is None or found.group(2).lower() not in RESTORABLE:
            return None
        channel, block = found.groups()
        prefix = f"Image/channels/{channel}/{block}/"
        subtree = [
            (p.id[len(prefix) :], p.value)
            for p in m0.params().values()
            if p.id.startswith(prefix)
            and p.value is not None
            and p.sensitivity is Sensitivity.NORMAL
        ]
        if not subtree or any("[" in rel for rel, _ in subtree):
            return None
        body = f'<?xml version="1.0" encoding="UTF-8"?>{_xml(block, subtree)}'
        if len(body.encode("utf-8")) > MAX_BODY:
            return None
        path = f"{ISAPI}Image/channels/{channel}/{RESTORABLE[block.lower()]}"
        return NativeWrite("PUT", path, body=body)


def _xml(block: str, subtree: list[tuple[str, str]]) -> str:
    """Поддерево `<block>` из относительных путей листьев, в порядке снимка."""
    root = ET.Element(block, {"version": "2.0", "xmlns": HIK_NS})
    for rel, value in subtree:
        node = root
        for name in rel.split("/"):
            child = next((c for c in node if c.tag == name), None)
            node = child if child is not None else ET.SubElement(node, name)
        node.text = value
    return ET.tostring(root, encoding="unicode")
