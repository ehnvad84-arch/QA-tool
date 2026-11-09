"""Настройки картинки по ONVIF Imaging 2.0: диапазоны, чтение, запись одного поля и возврат.

Ходит к камере только через `SoapClient` (табу AD-7 — `check_onvif` до отправки). Пишет только
поле проверяемой настройки: сначала одним этим полем (схема ImagingSettings20 допускает любой
набор полей), а если камера требует полный блок — полным, где всё прочее совпадает с тем, что
она сама вернула. Экспозиция, ИК-фильтр, фокус и стабилизация в уходящем блоке обязаны совпадать
со снимком до первой записи — иначе `ImageWriteRefused`, и запрос не уходит.

Ответы камеры — недоверенные данные: числа проверяются на конечность и разумный размер, XML
настроек — не больше `MAX_SETTINGS_XML`.
"""

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from xml.etree.ElementTree import Element, register_namespace, tostring
from xml.sax.saxutils import escape

from defusedxml import ElementTree

from qa_tool.drivers.camera.soap import OnvifFault, SoapClient, credentials_rejected

TT = "http://www.onvif.org/ver10/schema"
# без имени префикса блок уходит с ns0: для камеры верно, в доказательстве нечитаемо
register_namespace("tt", TT)

MAX_SETTINGS_XML = 64_000  # символов: настройки одного источника в разы меньше
MAX_ABS_VALUE = 1e6  # граница диапазона больше — ответ камеры не число настройки
TOLERANCE_SHARE = 0.005  # 0,5 % диапазона: камеры округляют значение до своего шага
# Блоки, которые проверка не меняет никогда: экспозиция задаёт условия съёмки всех настроек,
# ИК-фильтр и фокус — другие модули (день/ночь, объектив).
PROTECTED = ("Exposure", "IrCutFilter", "Focus", "ImageStabilization")

# Порядок элементов по схеме ONVIF (onvif.xsd, ImagingSettings20 и вложенные типы): камеры на
# gSOAP отвергают блок с элементами не по порядку.
_ORDER = {
    "ImagingSettings": (
        "BacklightCompensation",
        "Brightness",
        "ColorSaturation",
        "Contrast",
        "Exposure",
        "Focus",
        "IrCutFilter",
        "Sharpness",
        "WideDynamicRange",
        "WhiteBalance",
        "Extension",
    ),
    "BacklightCompensation": ("Mode", "Level"),
    "WideDynamicRange": ("Mode", "Level"),
    "WhiteBalance": ("Mode", "CrGain", "CbGain", "Extension"),
    "NoiseReduction": ("Level",),
}
_EXTENSION_ORDER = (
    ("ImageStabilization", "Extension"),
    ("IrCutFilterAutoAdjustment", "Extension"),
    ("ToneCompensation", "Defogging", "NoiseReduction", "Extension"),
)
NUMBER, MODE_LEVEL, WHITE_BALANCE = "число", "режим и уровень", "баланс белого"


class FieldSpec(Protocol):
    """Что драйвер знает о настройке (каталог — `checks/image/settings.py`)."""

    title: str
    path: tuple[str, ...]
    options: tuple[str, ...]
    kind: str
    mode: tuple[str, ...]
    partner: tuple[str, ...]
    partner_options: tuple[str, ...]


class ImageWriteRefused(RuntimeError):
    """Уходящий блок меняет то, что проверка менять не вправе. Запрос не отправлен."""


class ValueRejected(RuntimeError):
    """Камера ответила SOAP Fault на запись и полем, и полным блоком."""

    def __init__(self, fault: OnvifFault):
        super().__init__(str(fault))
        self.fault = fault


@dataclass(frozen=True)
class Declared:
    """Диапазон из GetOptions. `two_positions` — режим без уровня: 0 — выкл, 1 — вкл."""

    min: float
    max: float
    integer: bool
    two_positions: bool = False

    def at(self, share: float) -> float:
        """Значение в положении `share` (0…1) диапазона; целые границы — целое значение."""
        value = self.min + share * (self.max - self.min)
        return float(round(value)) if self.integer or self.two_positions else value

    @property
    def tolerance(self) -> float:
        """Допуск чтения назад: камеры округляют до своего шага."""
        return max(TOLERANCE_SHARE * (self.max - self.min), 1e-6)


@dataclass(frozen=True)
class Unsupported:
    """Камера настройку не объявляет — строка «пропущена»."""

    reason: str


@dataclass(frozen=True)
class BadRange:
    """Камера объявила негодный диапазон — «не прошла» без записи."""

    reason: str


@dataclass
class ImagingState:
    """Настройки источника на момент чтения."""

    element: Element
    xml: str
    values: dict[str, float | None]
    exposure_mode: str | None  # AUTO, MANUAL; None — камера не сообщила


@dataclass
class WriteReport:
    """Чем кончилась запись одного значения."""

    key: str
    value: float
    form: str  # «поле» или «полный блок»
    body: str  # отправленный блок ImagingSettings — в доказательства
    read_back: float | None
    accepted: bool  # прочитанное совпало с записанным в пределах допуска


@dataclass
class RestoreReport:
    """Возврат: что не вернулось (настройка, было, стало) и прочие расхождения — для сведения."""

    left: list[tuple[str, str, str]] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @property
    def restored(self) -> bool:
        """Все изменённые настройки вернулись в пределах допуска."""
        return not self.left


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _at(element: Element | None, path: tuple[str, ...]) -> Element | None:
    for name in path:
        element = _child(element, name)
    return element


def _inner(element: Element) -> str:
    """Дети элемента текстом. Префикс tt объявлен в конверте SOAP — повторять его в каждом
    поле незачем: блок в доказательстве читается так же, как его прислала камера."""
    text = "".join(tostring(child, encoding="unicode") for child in element)
    return text.replace(f' xmlns:tt="{TT}"', "")


def _number(text: str | None) -> float | None:
    """Число из ответа камеры; None — не число, NaN, бесконечность или слишком большое."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and abs(value) <= MAX_ABS_VALUE else None


def _format(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


def canonical(element: Element | None) -> list[tuple]:
    """Содержимое без префиксов и пробелов — для сравнения «было / стало»."""
    if element is None:
        return []
    return [
        (_local(e.tag), (e.text or "").strip(), tuple(sorted(e.attrib.items())))
        for e in element.iter()
    ]


def _order_of(parent: Element, depth: int) -> tuple[str, ...]:
    """Порядок детей `parent`; `depth` — сколько Extension пройдено до него включительно."""
    name = _local(parent.tag)
    if name == "Extension":
        return _EXTENSION_ORDER[min(depth, len(_EXTENSION_ORDER)) - 1]
    return _ORDER.get(name, ())


def _ensure(parent: Element, name: str, depth: int) -> Element:
    """Дочерний элемент `name`; нет — создаётся на своём месте по схеме."""
    found = _child(parent, name)
    if found is not None:
        return found
    created = Element(f"{{{TT}}}{name}")
    order = _order_of(parent, depth)
    rank = order.index(name) if name in order else len(order)
    position = len(parent)
    for i, sibling in enumerate(parent):
        sibling_name = _local(sibling.tag)
        if sibling_name in order and order.index(sibling_name) > rank:
            position = i
            break
    parent.insert(position, created)
    return created


def _put(root: Element, path: tuple[str, ...], text: str) -> None:
    element, extensions = root, 0
    for name in path:
        element = _ensure(element, name, extensions)
        extensions += name == "Extension"
    element.text = text


class ImagingWriter:
    """Настройки картинки одного источника видео.

    `read()` до первой записи — обязателен: первый прочитанный блок — снимок, с которым
    сравниваются защищённые блоки (`PROTECTED`) каждой уходящей записи.
    Не потокобезопасен: один писатель на камеру, как и её рабочий процесс.
    """

    def __init__(
        self,
        soap: SoapClient,
        imaging_url: str,
        source_token: str,
        fields: Mapping[str, FieldSpec],
    ):
        self.soap = soap
        self.url = imaging_url
        self.source = f"<timg:VideoSourceToken>{escape(source_token)}</timg:VideoSourceToken>"
        self.fields = fields
        self.baseline: ImagingState | None = None
        self.ranges: dict[str, Declared | Unsupported | BadRange] = {}

    # --- чтение --------------------------------------------------------------------------------

    def declared(self) -> dict[str, Declared | Unsupported | BadRange]:
        """Диапазоны настроек из GetOptions.

        Raises:
            CameraUnreachable: камера не ответила.
            OnvifFault: камера отказала или ответ без ImagingOptions.
        """
        answer = self.soap.call(self.url, f"<timg:GetOptions>{self.source}</timg:GetOptions>")
        options = next((e for e in answer.iter() if _local(e.tag) == "ImagingOptions"), None)
        if options is None:
            raise OnvifFault("GetOptions без ImagingOptions")
        self.ranges = {key: self._declared(spec, options) for key, spec in self.fields.items()}
        return self.ranges

    def _declared(self, spec: FieldSpec, options: Element) -> Declared | Unsupported | BadRange:
        name = spec.title
        if spec.options[-1] == "NoiseReductionOptions":
            # ONVIF: поддержка уровня — признак true, а сам уровень всегда 0…1
            found = next((e for e in options.iter() if _local(e.tag) == spec.options[-1]), None)
            level = _child(found, "Level")
            if level is None or (level.text or "").strip().lower() != "true":
                return Unsupported(f"камера не объявляет «{name}»")
            return Declared(0.0, 1.0, integer=False)
        if spec.mode:
            parent = _at(options, spec.mode[:-1])
            listed = [] if parent is None else list(parent)
            modes = {
                (e.text or "").strip().upper() for e in listed if _local(e.tag) == spec.mode[-1]
            }
            needed = "MANUAL" if spec.kind == WHITE_BALANCE else "ON"
            if modes and needed not in modes:
                return Unsupported(f"камера не объявляет режим {needed} для «{name}»")
        declared = _range(_at(options, spec.options), name)
        if spec.kind == MODE_LEVEL and isinstance(declared, Unsupported) and spec.mode:
            mode_parent = _at(options, spec.mode[:-1])
            if mode_parent is not None and _child(mode_parent, spec.mode[-1]) is not None:
                return Declared(0.0, 1.0, integer=True, two_positions=True)
        if spec.kind == WHITE_BALANCE and isinstance(declared, Declared):
            partner = _range(_at(options, spec.partner_options), name)
            if not isinstance(partner, Declared):
                return partner
        return declared

    def read(self) -> ImagingState:
        """Текущие настройки источника; первое чтение запоминается как снимок.

        Raises:
            CameraUnreachable, OnvifFault: камера не ответила, отказала, ответ без
                ImagingSettings или больше `MAX_SETTINGS_XML`.
        """
        answer = self.soap.call(
            self.url, f"<timg:GetImagingSettings>{self.source}</timg:GetImagingSettings>"
        )
        settings = next((e for e in answer.iter() if _local(e.tag) == "ImagingSettings"), None)
        if settings is None:
            raise OnvifFault("GetImagingSettings без ImagingSettings")
        xml = _inner(settings)
        if len(xml) > MAX_SETTINGS_XML:
            raise OnvifFault(f"настройки картинки больше {MAX_SETTINGS_XML} символов")
        exposure = _at(settings, ("Exposure", "Mode"))
        mode = "" if exposure is None else (exposure.text or "").strip().upper()
        state = ImagingState(
            copy.deepcopy(settings),
            xml,
            {key: self._value(spec, settings) for key, spec in self.fields.items()},
            mode if mode in ("AUTO", "MANUAL") else None,
        )
        if self.baseline is None:
            self.baseline = state
        return state

    def _value(self, spec: FieldSpec, settings: Element) -> float | None:
        declared = self.ranges.get(_key_of(self.fields, spec))
        if isinstance(declared, Declared) and declared.two_positions:
            mode = _at(settings, spec.mode)
            if mode is None:
                return None
            return 1.0 if (mode.text or "").strip().upper() == "ON" else 0.0
        found = _at(settings, spec.path)
        return None if found is None else _number(found.text)

    def read_value(self, key: str) -> float | None:
        """Текущее значение одной настройки; None — камера его не сообщила или оно не число."""
        return self.read().values[key]

    # --- запись --------------------------------------------------------------------------------

    def write(self, key: str, value: float, persist: bool = False) -> WriteReport:
        """Записать значение одной настройки и прочитать его назад.

        Args:
            key: ключ из каталога.
            value: значение в единицах камеры, в пределах `declared()`.
            persist: ForcePersistence — сохранить во флеш (проверка пишет без сохранения).

        Raises:
            ImageWriteRefused: блок трогает защищённое или чужое поле — не отправлен.
            ValueRejected: камера отказала и полем, и полным блоком.
            CameraUnreachable, OnvifFault: нет связи; отказ входа — без повтора полным блоком.
        """
        if self.baseline is None:
            raise ImageWriteRefused("нет снимка настроек: запись до чтения запрещена")
        spec = self.fields[key]
        declared = self.ranges.get(key)
        if not isinstance(declared, Declared):
            raise ImageWriteRefused(f"у «{spec.title}» нет годного диапазона — запись запрещена")
        lone = Element("ImagingSettings")
        self._apply(lone, key, value)
        form, body = "поле", lone
        try:
            self._send(lone, key, persist)
        except OnvifFault as fault:
            if credentials_rejected(fault):
                raise
            full = copy.deepcopy(self.read().element)
            self._apply(full, key, value)
            form, body = "полный блок", full
            try:
                self._send(full, key, persist)
            except OnvifFault as again:
                if credentials_rejected(again):
                    raise
                raise ValueRejected(again) from again
        read_back = self.read_value(key)
        accepted = read_back is not None and abs(read_back - value) <= declared.tolerance
        sent = _inner(body)
        return WriteReport(key, value, form, sent, read_back, accepted)

    def _apply(self, settings: Element, key: str, value: float) -> None:
        spec, declared = self.fields[key], self.ranges[key]
        assert isinstance(declared, Declared)
        if spec.kind == MODE_LEVEL:
            _put(settings, spec.mode, "ON" if value or not declared.two_positions else "OFF")
            if not declared.two_positions:
                _put(settings, spec.path, _format(value))
            return
        if spec.kind == WHITE_BALANCE:
            _put(settings, spec.mode, "MANUAL")
            partner = self._partner_range(key)
            _put(settings, spec.partner, _format(partner.at(0.5)))
        _put(settings, spec.path, _format(value))

    def _partner_range(self, key: str) -> Declared:
        spec = self.fields[key]
        mate = next(k for k, s in self.fields.items() if s.path == spec.partner)
        declared = self.ranges.get(mate)
        if isinstance(declared, Declared):
            return declared
        raise ImageWriteRefused(f"нет диапазона второго усиления для «{spec.title}»")

    def _send(self, settings: Element, key: str | None, persist: bool) -> None:
        self._guard(settings, key)
        inner = _inner(settings)
        self.soap.call(
            self.url,
            f"<timg:SetImagingSettings>{self.source}"
            f"<timg:ImagingSettings>{inner}</timg:ImagingSettings>"
            f"<timg:ForcePersistence>{str(persist).lower()}</timg:ForcePersistence>"
            "</timg:SetImagingSettings>",
        )

    def _guard(self, settings: Element, key: str | None) -> None:
        """Белый список: защищённые блоки — как в снимке, прочие поля — как сейчас у камеры."""
        assert self.baseline is not None
        base = self.baseline.element
        for name in PROTECTED:
            sent = next((e for e in settings.iter() if _local(e.tag) == name), None)
            if sent is None:
                continue
            was = next((e for e in base.iter() if _local(e.tag) == name), None)
            if canonical(sent) != canonical(was):
                raise ImageWriteRefused(f"блок {name} отличается от снимка — запись не отправлена")
        if key is None:
            return
        own = self.fields[key].path[0]
        others = [child for child in settings if _local(child.tag) != own]
        current = self.read().element if others else None
        for child in others:
            name = _local(child.tag)
            if canonical(child) != canonical(_child(current, name)):
                raise ImageWriteRefused(f"поле {name} не относится к «{self.fields[key].title}»")

    # --- возврат -------------------------------------------------------------------------------

    def restore_xml(self, xml: str) -> None:
        """Отправить сохранённый блок ImagingSettings (журнал прошлого прогона) с ForcePersistence.

        Блок — ответ камеры из журнала, недоверенный: разбирается defusedxml, защищённые блоки
        сверяются со снимком, табу проверяет `SoapClient`.

        Raises:
            ImageWriteRefused: блок трогает защищённое или снимка ещё нет — не отправлен.
            ValueError: блок не XML.
            CameraUnreachable, OnvifFault: нет связи или отказ.
        """
        if self.baseline is None:
            raise ImageWriteRefused("нет снимка настроек: запись до чтения запрещена")
        try:
            root = ElementTree.fromstring(
                f'<ImagingSettings xmlns:tt="{TT}">{xml}</ImagingSettings>'
            )
        except ElementTree.ParseError as e:
            raise ValueError(f"блок из журнала не XML: {e}") from e
        self._send(root, None, persist=True)

    def protected_drift(self, state: ImagingState | None = None) -> list[tuple[str, str, str]]:
        """Защищённые блоки (экспозиция, ИК-фильтр, фокус), которые камера изменила сама.

        Программа их не пишет, но камера может поменять их как побочный эффект чужой записи:
        Hikvision .111 09.11 после записи настроек картинки сбросил MaxGain экспозиции со 100
        на 0 и вернуть его не дал. Поэтому их сверка — после каждой записи и при возврате.

        Args:
            state: что сейчас у камеры; None — прочитать.

        Returns:
            (блок, было, стало) по каждому изменившемуся блоку; пусто — всё как в снимке.

        Raises:
            CameraUnreachable, OnvifFault: камера не ответила.
        """
        assert self.baseline is not None
        now = state or self.read()
        found = []
        for name in PROTECTED:
            was = next((e for e in self.baseline.element.iter() if _local(e.tag) == name), None)
            got = next((e for e in now.element.iter() if _local(e.tag) == name), None)
            if canonical(was) != canonical(got):
                found.append((name, _brief(was), _brief(got)))
        return found

    def restore(self, state: ImagingState, touched: list[str]) -> RestoreReport:
        """Вернуть блок `state` целиком с ForcePersistence и сверить.

        Args:
            state: снимок до записи.
            touched: ключи настроек, которые менялись: их расхождение — «не вернулось».

        Raises:
            ImageWriteRefused: снимок трогает защищённое (не бывает: он и есть снимок).
            CameraUnreachable, OnvifFault: нет связи или отказ — возврат не подтверждён.
        """
        self._send(state.element, None, persist=True)
        now = self.read()
        report = RestoreReport()
        for key in touched:
            was, got = state.values.get(key), now.values.get(key)
            declared = self.ranges.get(key)
            tolerance = declared.tolerance if isinstance(declared, Declared) else 1e-6
            if (was is None) != (got is None) or (
                was is not None and got is not None and abs(was - got) > tolerance
            ):
                report.left.append((self.fields[key].title, _shown(was), _shown(got)))
            mode = self.fields[key].mode
            if mode:
                # ББ, WDR, BLC: число могло вернуться, а режим остаться ручным или включённым
                was_mode, now_mode = _text_at(state.element, mode), _text_at(now.element, mode)
                if was_mode != now_mode:
                    title = f"{self.fields[key].title} — режим"
                    report.left.append((title, was_mode or "нет", now_mode or "нет"))
        moved = {self.fields[key].path[0] for key in touched}
        for name, was_block, now_block in self.protected_drift(now):
            # блок, который программа не пишет, изменился — камера ушла не такой, как пришла
            title = f"{name} (камера изменила сама, программа его не пишет)"
            report.left.append((title, was_block, now_block))
        for child in state.element:
            name = _local(child.tag)
            if name in PROTECTED:
                continue
            if name not in moved and canonical(child) != canonical(_child(now.element, name)):
                report.other.append(f"{name}: после возврата не совпадает со снимком")
        return report


def _brief(element: Element | None) -> str:
    """Блок для строки «было / осталось»: «Mode AUTO, MinGain 0, MaxGain 100»."""
    if element is None:
        return "нет"
    parts = [
        f"{_local(e.tag)} {(e.text or '').strip()}"
        for e in element.iter()
        if e is not element and len(e) == 0 and (e.text or "").strip()
    ]
    if not parts and (element.text or "").strip():
        return (element.text or "").strip()[:200]
    return ", ".join(parts)[:200] or "пусто"


def _text_at(element: Element, path: tuple[str, ...]) -> str:
    found = _at(element, path)
    return "" if found is None else (found.text or "").strip().upper()


def _shown(value: float | None) -> str:
    return "не прочитано" if value is None else _format(value)


def _key_of(fields: Mapping[str, FieldSpec], spec: FieldSpec) -> str:
    return next(key for key, candidate in fields.items() if candidate is spec)


def _range(element: Element | None, name: str) -> Declared | Unsupported | BadRange:
    """Диапазон Min…Max из GetOptions."""
    if element is None:
        return Unsupported(f"камера не объявляет «{name}»")
    low_text, high_text = _child(element, "Min"), _child(element, "Max")
    if low_text is None or high_text is None:
        return Unsupported(f"камера не объявляет диапазон «{name}»")
    low, high = _number(low_text.text), _number(high_text.text)
    if low is None or high is None:
        shown = f"{(low_text.text or '').strip()!r}…{(high_text.text or '').strip()!r}"
        return BadRange(f"диапазон «{name}» не число: {shown[:80]}")
    if low > high:
        return BadRange(f"диапазон «{name}» перевёрнут: {_format(low)} > {_format(high)}")
    if low == high:
        return Unsupported(f"диапазон «{name}» из одной точки ({_format(low)})")
    return Declared(low, high, integer=low.is_integer() and high.is_integer())
