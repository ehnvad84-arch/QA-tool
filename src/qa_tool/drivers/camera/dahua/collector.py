"""Сборщик карты Dahua CGI (S1, S5): `configManager getConfig` по списку таблиц и описание
кодировщика `encode getConfigCaps`.

CGI отвечает строками «ключ=значение» (`drivers/camera/key_value.py`). Самоописания у таблиц
настроек нет: тип — по значению, допустимые — «не объявлены», кроме кодировщика, у которого есть
`getConfigCaps`: `…Video.BitRateOptions=512,5632` — диапазон для `…Video.BitRate`,
`…Video.CompressionTypes=H.264,H.265` — перечисление для `…Video.Compression`. Список таблиц —
по образцу .108 09.11; таблицы сети и учётных записей не читаются вовсе.

Копии одной настройки (`VideoColor[0][0..4]` — профили Day, Night, Normal…, `MainFormat[0..n]`)
остаются отдельными параметрами с индексами: свести их в один — дело автопоиска (S5).
RPC2 не используется: у него свой вход с хешем пароля, а CGI хватает.
"""

import re
from urllib.parse import quote

from qa_tool.core.api_map import (
    NativeMap,
    Param,
    Section,
    Sensitivity,
    allowed_range,
    allowed_values,
    finite_number,
)
from qa_tool.drivers.camera.key_value import is_error_reply, parse_key_value, table_of
from qa_tool.drivers.camera.native import NativeWrite, ParseRefused, ReadTemplate

CGI = "/cgi-bin/"
CONFIG = CGI + "configManager.cgi"
CAPS = CGI + "encode.cgi?action=getConfigCaps&channel=1"
# Таблицы настроек по образцу .108 09.11 и их класс; сеть, пользователи, безопасность — нет.
TABLES = (
    ("VideoColor", "картинка"),
    ("VideoInOptions", "картинка"),
    ("Encode", "кодировщик"),
    ("VideoWidget", "наложения"),
)
IMAGE_TABLES = frozenset(name for name, cls in TABLES if cls == "картинка")
MAX_VALUE = 256
_RESTORE_KEY = re.compile(r"^[A-Za-z0-9_.\[\]]{1,128}$")
# «caps[0].MainFormat[0].Video.BitRateOptions» → «Encode[0].MainFormat[0].Video.BitRate»
_CAPS_KEY = re.compile(r"^caps(\[\d+\])\.(.+?)(Options|Types)$")


def _text(body: bytes) -> str:
    """Тело CGI как текст; ответ «Error …» — не раздел."""
    text = body.decode("utf-8", "replace")
    if is_error_reply(text):
        raise ParseRefused("камера ответила «Error»: таблицы или действия нет")
    return text


def _guess(value: str) -> str:
    """Тип по значению: самоописания у таблиц CGI нет."""
    if value == "":
        return "неизвестно"
    if value.lower() in ("true", "false"):
        return "логический"
    number = finite_number(value)
    if number is not None:
        return "целое" if number.is_integer() and "." not in value else "число"
    return "строка"


def _caps(describe: bytes | None) -> dict[str, tuple[str, dict]]:
    """Допустимые значения кодировщика из `getConfigCaps`: ключ настройки → (тип, допустимые)."""
    if describe is None:
        return {}
    try:
        pairs = parse_key_value(_text(describe), prefix="").pairs
    except ParseRefused:
        return {}
    found: dict[str, tuple[str, dict]] = {}
    for key, value in pairs.items():
        match = _CAPS_KEY.match(key)
        if match is None:
            continue
        index, name, kind = match.groups()
        target = f"Encode{index}.{name}"
        items = [v.strip() for v in value.split(",") if v.strip()]
        if kind == "Options" and len(items) == 2:
            allowed = allowed_range(items[0], items[1])
            found[target] = ("целое" if "негодный" not in allowed else "неизвестно", allowed)
        elif kind == "Types" and items:
            found[target] = ("перечисление", allowed_values(items))
    return found


class DahuaCollector:
    """Сборщик Dahua CGI. Без состояния: список запросов один и тот же."""

    brand = "dahua"
    name = "CGI"
    version = 1
    names = frozenset({"dahua"})
    login = "digest"

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Паспорт (`magicBox`) и таблицы настроек; к кодировщику — его описание."""
        templates = [
            ReadTemplate(
                CGI + "magicBox.cgi", f"magicBox.{action}", "система", query=f"action={action}"
            )
            for action in ("getDeviceType", "getSoftwareVersion")
        ]
        for name, cls in TABLES:
            templates.append(
                ReadTemplate(
                    CONFIG,
                    name,
                    cls,
                    query=f"action=getConfig&name={name}",
                    describe_path=CAPS if name == "Encode" else "",
                )
            )
        # описание входа видео; без channel .108 отвечает «Bad Request»
        templates.append(
            ReadTemplate(
                CGI + "devVideoInput.cgi",
                "devVideoInput.getCaps",
                "картинка",
                query="action=getCaps&channel=1",
            )
        )
        return templates

    def parse(
        self, template: ReadTemplate, body: bytes, content_type: str, describe: bytes | None
    ) -> Section:
        """Раздел из строк «ключ=значение»; ключи не той таблицы и мусор пропускаются.

        Raises:
            ParseRefused: камера ответила «Error» или в ответе нет ни одного ключа.
        """
        parsed = parse_key_value(_text(body))
        if not parsed.pairs:
            raise ParseRefused(f"нет строк «ключ=значение» (пропущено {parsed.skipped})")
        caps = _caps(describe)
        section = Section(template.section_id, template.cls, "", sensitivity=template.sensitivity)
        own_table = template.section_id in dict(TABLES)
        for key, value in parsed.pairs.items():
            if own_table and table_of(key) != template.section_id:
                continue
            param_id = key if own_table else f"{template.section_id}.{key}"
            type_, allowed = caps.get(key, (_guess(value), None))
            section.add_param(param_id, type_, allowed, value)
        return section

    def expected(self, kind: str) -> tuple[str, ...]:
        """Таблицы, где ждём сдвиг при записи по ONVIF настройки класса `kind`."""
        return tuple(name for name, cls in TABLES if cls == kind)

    def observed(self, native: NativeMap) -> list[str]:
        """Таблицы для снимков автопоиска: все таблицы настроек с обычными значениями."""
        return [
            s.id
            for s in native.sections
            if s.id in dict(TABLES)
            and s.sensitivity is Sensitivity.NORMAL
            and any(p.value is not None for p in s.params)
        ]

    def restore_request(self, param: Param, m0: NativeMap) -> NativeWrite | None:
        """`setConfig` одним ключом таблицы картинки со значением снимка M0.

        Returns:
            Запрос или None: не таблица картинки, ключ вне белого списка знаков, значения в M0
            нет или оно длиннее 256 знаков.
        """
        if table_of(param.id) not in IMAGE_TABLES or not _RESTORE_KEY.match(param.id):
            return None
        before = m0.params().get(param.id)
        if before is None or before.value is None or before.sensitivity is not Sensitivity.NORMAL:
            return None
        if len(before.value) > MAX_VALUE:
            return None
        query = f"action=setConfig&{param.id}={quote(before.value, safe='')}"
        return NativeWrite("GET", CONFIG, query=query)
