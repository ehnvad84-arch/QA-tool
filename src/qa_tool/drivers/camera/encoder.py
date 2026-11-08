"""Кодировщики видео по ONVIF Media2 (запасной — Media1): варианты, чтение, запись и возврат.

Ходит к камере только через `SoapClient` (табу AD-7 — `check_onvif` до отправки, мультикаст —
`MulticastGuard` в `http.py`). Блок записи строится из снимка — первого чтения конфигурации — и
меняет только кодек, профиль кодека, битрейт и режим битрейта. Поля белого списка (`KEPT`) в
уходящем блоке обязаны совпадать со снимком, иначе `EncoderWriteRefused`, и запрос не уходит:
разрешение, частоту и GOP проверка кодеков не меняет, а мультикаст — сетевая настройка.

Media1 пишется только битрейтом: в её схеме нет H.265 и нет флага постоянного битрейта.

Ответы камеры — недоверенные данные: числа проверяются на конечность и размер, XML конфигурации —
не больше `MAX_CONFIG_XML`, XML из журнала прошлого прогона переразбирается defusedxml.
"""

import copy
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, register_namespace, tostring
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree

from qa_tool.drivers.camera.imaging import canonical
from qa_tool.drivers.camera.onvif_generic import EncoderSettings, parse_encoder, stream_uri
from qa_tool.drivers.camera.soap import NS, OnvifFault, SoapClient, credentials_rejected

register_namespace("tt", NS["tt"])
register_namespace("tr2", NS["tr2"])
register_namespace("trt", NS["trt"])

MAX_CONFIG_XML = 64_000  # символов: конфигурация одного кодировщика в разы меньше
MAX_KBPS = 1e6  # граница битрейта больше 1 Гбит/с — ответ камеры не битрейт
CODECS = ("H264", "H265")
# Поля, которые проверка кодеков не меняет никогда (спека, «Белый список»)
KEPT = (
    "Name",
    "Resolution",
    "FrameRateLimit",
    "EncodingInterval",
    "GovLength",
    "Quality",
    "Multicast",
    "SessionTimeout",
)
# Камеры применяют запись не сразу: чтение назад повторяется (спека, «После записи»)
READ_BACK_TRIES = 3
READ_BACK_PAUSE_S = 1.0
# Камеры округляют битрейт до своего шага (64 кбит/с и т. п.): 2 % — с запасом на шаг
KBPS_TOLERANCE_SHARE = 0.02
CONSTANT, CEILING = "постоянный битрейт", "потолок переменного битрейта"
# Порядок детей RateControl по схеме: камеры на gSOAP отвергают блок не по порядку
_RATE_ORDER = ("FrameRateLimit", "EncodingInterval", "BitrateLimit")


class EncoderWriteRefused(RuntimeError):
    """Уходящий блок меняет то, что проверка менять не вправе. Запрос не отправлен."""


class ValueRejected(RuntimeError):
    """Камера ответила SOAP Fault на запись кодировщика."""

    def __init__(self, fault: OnvifFault):
        super().__init__(str(fault))
        self.fault = fault


@dataclass(frozen=True)
class Unsupported:
    """Камера кодек не объявляет или не даёт при текущих настройках — строки «пропущена»."""

    reason: str


@dataclass(frozen=True)
class BadRange:
    """Камера объявила негодный диапазон битрейта — «не прошла» без записи."""

    reason: str


@dataclass(frozen=True)
class CodecOptions:
    """Варианты одного кодека из GetVideoEncoderConfigurationOptions.

    `encoding` — строка кодека, как её пишет камера (H265 или HEVC). Пустые `resolutions`,
    `fps`, `profiles` и `gov_range` None — камера их не объявила, и по ним не судим.
    """

    encoding: str
    kbps_min: float
    kbps_max: float
    resolutions: tuple[tuple[int, int], ...] = ()
    fps: tuple[float, ...] = ()
    gov_range: tuple[int, int] | None = None
    profiles: tuple[str, ...] = ()
    cbr: bool = False


@dataclass
class EncoderConfig:
    """Конфигурация кодировщика на момент чтения.

    `profiles` — (токен, имя) медиапрофилей с этим кодировщиком; `uri` — RTSP первого из них.
    `xml` — вся конфигурация с корнем: её журнал хранит для возврата (`restore_xml`).
    """

    token: str
    service: str  # media2 или media
    profiles: tuple[tuple[str, str], ...]
    settings: EncoderSettings
    codec_profile: str
    quality: float | None
    uri: str
    element: Element
    xml: str


@dataclass
class WriteReport:
    """Чем кончилась запись кодека и битрейта.

    `form` — режим, в котором ушёл битрейт (`CONSTANT` или `CEILING`); `note` — пометка для
    строки протокола (например, профиль Main10); `body` — отправленный блок для доказательств.
    """

    accepted: bool
    read_back: EncoderConfig | None
    form: str
    body: str
    note: str = ""


@dataclass
class RestoreReport:
    """Возврат: что не вернулось (поле, было, стало) и прочие расхождения — для сведения."""

    left: list[tuple[str, str, str]] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @property
    def restored(self) -> bool:
        """Кодек, битрейт, режим, профиль и поля белого списка вернулись."""
        return not self.left


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _first(element: Element | None, name: str) -> Element | None:
    """Первый потомок с таким локальным именем на любой глубине."""
    if element is None:
        return None
    return next((e for e in element.iter() if _local(e.tag) == name and e is not element), None)


def _number(text: str | None) -> float | None:
    """Число из ответа камеры; None — не число, NaN, бесконечность или слишком большое."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and abs(value) <= MAX_KBPS else None


def _numbers(text: str | None) -> tuple[float, ...]:
    """Список чисел из атрибута через пробел; негодные отбрасываются."""
    found = (_number(part) for part in (text or "").split())
    return tuple(value for value in found if value is not None)


def _codec(encoding: str) -> str:
    """H264 или H265 из строки камеры; прочее — как есть в верхнем регистре."""
    name = encoding.strip().upper().replace(".", "")
    return "H265" if name in ("HEVC", "H265") else name


def _inner(element: Element) -> str:
    """Дети элемента текстом; префиксы tt, tr2 и trt объявлены в конверте SOAP."""
    text = "".join(tostring(child, encoding="unicode") for child in element)
    for prefix in ("tt", "tr2", "trt"):
        text = text.replace(f' xmlns:{prefix}="{NS[prefix]}"', "")
    return text


def _format_kbps(value: float) -> str:
    return str(int(round(value)))


def choose_profile(current: str, supported: tuple[str, ...]) -> tuple[str, str]:
    """Профиль кодека при смене кодека и пометка для строки.

    Текущий, если новый кодек его знает; иначе Main; иначе первый не Main10; Main10 — только
    если других нет: 10 бит меняют сравнение с 8-битным эталоном.
    """
    if not supported or current in supported:
        return current, ""
    if "Main" in supported:
        return "Main", ""
    plain = [name for name in supported if name != "Main10"]
    if plain:
        return plain[0], ""
    return supported[0], "камера даёт этот кодек только в профиле Main10"


def mismatch(settings: EncoderSettings, options: CodecOptions) -> str:
    """Почему кодек нельзя включить, не меняя разрешение, частоту и GOP; пусто — можно."""
    parts = []
    size = (settings.width, settings.height)
    if options.resolutions and size not in options.resolutions:
        parts.append("разрешении")
    if options.fps and settings.fps and all(abs(settings.fps - f) > 0.01 for f in options.fps):
        parts.append("частоте")
    low_high = options.gov_range
    if low_high and settings.gop and not low_high[0] <= settings.gop <= low_high[1]:
        parts.append("GOP")
    if not parts:
        return ""
    return f"при текущих {', '.join(parts)} камера его не даёт"


def _bitrate_range(options: Element, codec: str) -> CodecOptions | Unsupported | BadRange:
    """Диапазон битрейта из `BitrateRange`; прочие поля дополняет вызывающий."""
    found = _first(options, "BitrateRange")
    low_el, high_el = _child(found, "Min"), _child(found, "Max")
    if low_el is None or high_el is None:
        return Unsupported(f"камера не объявляет диапазон битрейта {codec}")
    low, high = _number(low_el.text), _number(high_el.text)
    if low is None or high is None or low <= 0:
        shown = f"{(low_el.text or '').strip()!r}…{(high_el.text or '').strip()!r}"
        return BadRange(f"диапазон битрейта {codec} не число: {shown[:80]}")
    if low > high:
        return BadRange(f"диапазон битрейта {codec} перевёрнут: {low:g} > {high:g}")
    if low == high:
        return Unsupported(f"диапазон битрейта {codec} из одной точки ({low:g})")
    return CodecOptions(codec, low, high)


def _resolutions(options: Element) -> tuple[tuple[int, int], ...]:
    sizes = []
    for item in (e for e in options.iter() if _local(e.tag) == "ResolutionsAvailable"):
        width, height = _number(_text(item, "Width")), _number(_text(item, "Height"))
        if width and height:
            sizes.append((int(width), int(height)))
    return tuple(dict.fromkeys(sizes))


def _text(element: Element | None, name: str) -> str:
    found = _child(element, name)
    return "" if found is None else (found.text or "").strip()


class EncoderWriter:
    """Кодировщики видео одной камеры.

    `configs()` до первой записи обязателен: первое чтение каждого кодировщика — снимок, из
    которого строится каждый уходящий блок и с которым сверяется белый список. Чтение нужно и
    сторожу мультикаста в `http.py`: конфигурацию, которую ещё не читали, он не пропустит.
    Не потокобезопасен: один писатель на камеру, как и её рабочий процесс.
    """

    def __init__(
        self,
        soap: SoapClient,
        services: dict[str, str],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.soap = soap
        self.services = services
        self.sleep = sleep
        self.snapshots: dict[str, EncoderConfig] = {}
        self._service: dict[str, str] = {}
        self._profiles: dict[str, tuple[tuple[str, str], ...]] = {}
        self._uri: dict[str, str] = {}
        self._options: dict[str, dict[str, CodecOptions | Unsupported | BadRange]] = {}

    # --- чтение --------------------------------------------------------------------------------

    def configs(self) -> list[EncoderConfig]:
        """Все кодировщики видео камеры; профили с общим кодировщиком — в одной конфигурации.

        Media2, а если в ней нет ни одного кодировщика — Media1.

        Raises:
            CameraUnreachable: камера не ответила.
            OnvifFault: нет службы медиа, отказ или конфигурация не разобрана.
        """
        found: dict[str, list[tuple[str, str]]] = {}
        service = ""
        for key in ("media2", "media"):
            if key not in self.services:
                continue
            found = self._encoders_in(key)
            if found:
                service = key
                break
        if not service:
            raise OnvifFault("камера не объявляет кодировщиков видео ни в Media2, ни в Media1")
        out = []
        for token, profiles in found.items():
            self._service[token] = service
            self._profiles[token] = tuple(profiles)
            try:
                self._uri[token] = self.stream_uri(token)
            except OnvifFault:
                self._uri[token] = ""
            out.append(self.read(token))
        return out

    def _encoders_in(self, service: str) -> dict[str, list[tuple[str, str]]]:
        """Токен кодировщика → (токен, имя) профилей с ним, по порядку GetProfiles."""
        media2 = service == "media2"
        body = "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>"
        try:
            answer = self.soap.call(
                self.services[service], body if media2 else "<trt:GetProfiles/>"
            )
        except OnvifFault as fault:
            if credentials_rejected(fault):
                raise
            return {}
        found: dict[str, list[tuple[str, str]]] = {}
        for profile in (e for e in answer.iter() if _local(e.tag) == "Profiles"):
            config = next(
                (
                    e
                    for e in profile.iter()
                    if _local(e.tag) in ("VideoEncoder", "VideoEncoderConfiguration")
                ),
                None,
            )
            token = "" if config is None else config.get("token", "")
            if not token or parse_encoder(config) is None:
                continue
            name = _text(profile, "Name")
            found.setdefault(token, []).append((profile.get("token", ""), name))
        return found

    def stream_uri(self, token: str) -> str:
        """Адрес RTSP первого профиля кодировщика — заново: после смены кодека он мог смениться.

        Raises:
            CameraUnreachable, OnvifFault: GetStreamUri не прошёл.
        """
        service = self._service[token]
        profile = self._profiles[token][0][0]
        uri = stream_uri(self.soap, self.services[service], service == "media2", profile)
        self._uri[token] = uri
        return uri

    def read(self, token: str) -> EncoderConfig:
        """Текущая конфигурация кодировщика; первое чтение запоминается как снимок.

        Raises:
            CameraUnreachable: камера не ответила.
            OnvifFault: отказ, ответ без конфигурации, без кодека или больше `MAX_CONFIG_XML`.
            KeyError: кодировщик не из `configs()`.
        """
        service = self._service[token]
        media2 = service == "media2"
        prefix = "tr2" if media2 else "trt"
        operation = "GetVideoEncoderConfigurations" if media2 else "GetVideoEncoderConfiguration"
        answer = self.soap.call(
            self.services[service],
            f"<{prefix}:{operation}><{prefix}:ConfigurationToken>{escape(token)}"
            f"</{prefix}:ConfigurationToken></{prefix}:{operation}>",
        )
        names = ("Configurations",) if media2 else ("Configuration",)
        element = next(
            (e for e in answer.iter() if _local(e.tag) in names and e.get("token") == token),
            None,
        )
        if element is None:
            raise OnvifFault(f"{operation} без конфигурации {token!r}")
        xml = tostring(element, encoding="unicode")
        if len(xml) > MAX_CONFIG_XML:
            raise OnvifFault(f"конфигурация кодировщика больше {MAX_CONFIG_XML} символов")
        settings = parse_encoder(element)
        if settings is None:
            raise OnvifFault(f"в конфигурации {token!r} не указан кодек")
        if media2:
            codec_profile = element.get("Profile", "").strip()
        else:
            codec_profile = _text(_first(element, "H264"), "H264Profile")
        quality = _number(_text(element, "Quality") or None)
        config = EncoderConfig(
            token,
            service,
            self._profiles[token],
            settings,
            codec_profile,
            quality,
            self._uri.get(token, ""),
            copy.deepcopy(element),
            xml,
        )
        self.snapshots.setdefault(token, config)
        return config

    def options(self, token: str) -> dict[str, CodecOptions | Unsupported | BadRange]:
        """Варианты по каждому кодеку (H264, H265): объявленные или причина, почему их нет.

        Media1 — только H264: H.265 в её схеме нет.

        Raises:
            CameraUnreachable, OnvifFault: камера не ответила или отказала.
            KeyError: кодировщик не из `configs()`.
        """
        service = self._service[token]
        media2 = service == "media2"
        prefix = "tr2" if media2 else "trt"
        answer = self.soap.call(
            self.services[service],
            f"<{prefix}:GetVideoEncoderConfigurationOptions><{prefix}:ConfigurationToken>"
            f"{escape(token)}</{prefix}:ConfigurationToken>"
            f"</{prefix}:GetVideoEncoderConfigurationOptions>",
        )
        options = next((e for e in answer.iter() if _local(e.tag) == "Options"), None)
        if options is None:
            raise OnvifFault("GetVideoEncoderConfigurationOptions без Options")
        found = self._media2_options(answer) if media2 else self._media1_options(options)
        out: dict[str, CodecOptions | Unsupported | BadRange] = {}
        for codec in CODECS:
            out[codec] = found.get(codec) or Unsupported(f"камера не объявляет {codec}")
        self._options[token] = out
        return out

    def _media2_options(self, answer: Element) -> dict[str, CodecOptions | Unsupported | BadRange]:
        found: dict[str, CodecOptions | Unsupported | BadRange] = {}
        for item in (e for e in answer.iter() if _local(e.tag) == "Options"):
            encoding = _text(item, "Encoding")
            codec = _codec(encoding)
            if codec not in CODECS or codec in found:
                continue
            declared = _bitrate_range(item, codec)
            if isinstance(declared, CodecOptions):
                gov = _numbers(item.get("GovLengthRange"))
                declared = CodecOptions(
                    encoding,
                    declared.kbps_min,
                    declared.kbps_max,
                    _resolutions(item),
                    _numbers(item.get("FrameRatesSupported")),
                    (int(gov[0]), int(gov[1])) if len(gov) == 2 else None,
                    tuple(item.get("ProfilesSupported", "").split()),
                    item.get("ConstantBitRateSupported", "").strip().lower() in ("true", "1"),
                )
            found[codec] = declared
        return found

    def _media1_options(self, options: Element) -> dict[str, CodecOptions | Unsupported | BadRange]:
        # битрейт Media1 объявляет только в Extension/H264, остальное — в Options/H264
        extension = _child(_child(options, "Extension"), "H264")
        plain = _child(options, "H264")
        if extension is None and plain is None:
            return {}
        declared = _bitrate_range(extension if extension is not None else plain, "H264")
        if not isinstance(declared, CodecOptions):
            return {"H264": declared}
        source = plain if plain is not None else extension
        gov = _first(source, "GovLengthRange")
        gov_low, gov_high = _number(_text(gov, "Min")), _number(_text(gov, "Max"))
        profiles = tuple(
            (e.text or "").strip()
            for e in source.iter()
            if _local(e.tag) == "H264ProfilesSupported" and (e.text or "").strip()
        )
        return {
            "H264": CodecOptions(
                "H264",
                declared.kbps_min,
                declared.kbps_max,
                _resolutions(source),
                (),
                (int(gov_low), int(gov_high)) if gov_low and gov_high else None,
                profiles,
                False,
            )
        }

    # --- запись --------------------------------------------------------------------------------

    def write(self, token: str, codec: str, kbps: float, constant: bool = True) -> WriteReport:
        """Записать кодек и битрейт и прочитать назад.

        Args:
            token: кодировщик из `configs()`.
            codec: H264 или H265 — из `options(token)`.
            kbps: битрейт, кбит/с, в пределах объявленного диапазона.
            constant: постоянный битрейт; камера без `ConstantBitRateSupported` и Media1
                получают его как потолок переменного (`form`).

        Raises:
            EncoderWriteRefused: нет снимка, кодек не объявлен, Media1 и смена кодека, битрейт
                вне диапазона или блок меняет поле белого списка — не отправлен.
            ValueRejected: камера ответила Fault.
            CameraUnreachable, OnvifFault: нет связи; отказ входа — как есть.
        """
        base = self.snapshots.get(token)
        if base is None:
            raise EncoderWriteRefused("нет снимка кодировщика: запись до чтения запрещена")
        media2 = base.service == "media2"
        if not media2 and codec != base.settings.encoding:
            raise EncoderWriteRefused("Media1 не меняет кодек: H.265 в её схеме нет")
        options = self._options.get(token) or self.options(token)
        target = options.get(codec)
        if not isinstance(target, CodecOptions):
            raise EncoderWriteRefused(f"камера не объявляет годный {codec} — запись запрещена")
        if not target.kbps_min <= kbps <= target.kbps_max:
            raise EncoderWriteRefused(
                f"битрейт {kbps:g} вне диапазона {codec} {target.kbps_min:g}…{target.kbps_max:g}"
            )
        block = copy.deepcopy(base.element)
        note = ""
        if media2:
            encoding = _first(block, "Encoding")
            if encoding is not None:
                encoding.text = target.encoding
            if codec != base.settings.encoding or base.codec_profile not in target.profiles:
                profile, note = choose_profile(base.codec_profile, target.profiles)
                if profile:
                    block.set("Profile", profile)
        rate = _first(block, "RateControl")
        if rate is None:
            raise EncoderWriteRefused("в снимке нет RateControl — некуда писать битрейт")
        _put_rate(rate, "BitrateLimit", _format_kbps(kbps))
        form = CEILING
        if media2 and target.cbr:
            rate.set("ConstantBitRate", str(constant).lower())
            form = CONSTANT if constant else CEILING
        self._send(base.service, block, base, persist=False)
        read_back = self._read_back(token, codec, kbps, form == CONSTANT)
        accepted = read_back is not None and _matches(read_back, codec, kbps, form == CONSTANT)
        return WriteReport(accepted, read_back, form, _inner(block), note)

    def _read_back(
        self, token: str, codec: str, kbps: float, constant: bool
    ) -> EncoderConfig | None:
        """Чтение назад до `READ_BACK_TRIES` раз: камеры применяют запись не сразу."""
        last = None
        for attempt in range(READ_BACK_TRIES):
            if attempt:
                self.sleep(READ_BACK_PAUSE_S)
            last = self.read(token)
            if _matches(last, codec, kbps, constant):
                return last
        return last

    def _send(self, service: str, block: Element, base: EncoderConfig, persist: bool) -> None:
        self._guard(block, base)
        media2 = service == "media2"
        prefix = "tr2" if media2 else "trt"
        attrs = "".join(f" {name}={quoteattr(value)}" for name, value in block.attrib.items())
        tail = (
            "" if media2 else f"<trt:ForcePersistence>{str(persist).lower()}</trt:ForcePersistence>"
        )
        try:
            self.soap.call(
                self.services[service],
                f"<{prefix}:SetVideoEncoderConfiguration><{prefix}:Configuration{attrs}>"
                f"{_inner(block)}</{prefix}:Configuration>{tail}"
                f"</{prefix}:SetVideoEncoderConfiguration>",
            )
        except OnvifFault as fault:
            if credentials_rejected(fault):
                raise
            raise ValueRejected(fault) from fault

    @staticmethod
    def _guard(block: Element, base: EncoderConfig) -> None:
        """Белый список: токен, GOP и поля `KEPT` — как в снимке, иначе запрос не уходит."""
        if block.get("token") != base.token:
            raise EncoderWriteRefused("токен блока не совпадает со снимком — запись не отправлена")
        if block.get("GovLength") != base.element.get("GovLength"):
            raise EncoderWriteRefused("GOP отличается от снимка — запись не отправлена")
        for name in KEPT:
            if canonical(_first(block, name)) != canonical(_first(base.element, name)):
                raise EncoderWriteRefused(
                    f"поле {name} отличается от снимка — запись не отправлена"
                )

    # --- возврат -------------------------------------------------------------------------------

    def restore(self, config: EncoderConfig) -> RestoreReport:
        """Вернуть конфигурацию `config` (снимок) целиком и сверить; Media1 — с сохранением.

        Raises:
            ValueRejected: камера ответила Fault — возврат не подтверждён.
            CameraUnreachable, OnvifFault: нет связи или отказ.
        """
        self._send(config.service, config.element, config, persist=True)
        now = self.read(config.token)
        return compare(config, now)

    def restore_xml(self, service: str, token: str, xml: str) -> RestoreReport:
        """Отправить конфигурацию из журнала прошлого прогона и сверить с ней.

        XML — недоверенный: переразбирается defusedxml; корень — конфигурация кодировщика с тем
        же токеном. Перед отправкой кодировщик читается: так сторож мультикаста знает, что
        у камеры сейчас, а белый список сверяется с журналом, а не с текущим состоянием.

        Raises:
            EncoderWriteRefused: XML не конфигурация этого кодировщика или больше предела.
            ValueError: XML не разбирается.
            ValueRejected, CameraUnreachable, OnvifFault: отказ или нет связи.
        """
        if len(xml) > MAX_CONFIG_XML:
            raise EncoderWriteRefused(f"конфигурация из журнала больше {MAX_CONFIG_XML} символов")
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as e:
            raise ValueError(f"конфигурация из журнала не XML: {e}") from e
        if (
            _local(root.tag) not in ("Configurations", "Configuration")
            or root.get("token") != token
        ):
            raise EncoderWriteRefused("в журнале не конфигурация этого кодировщика")
        settings = parse_encoder(root)
        if settings is None:
            raise EncoderWriteRefused("в конфигурации из журнала не указан кодек")
        if token not in self._service:
            self._service[token] = service
        self._profiles.setdefault(token, ())
        self.read(token)
        saved = EncoderConfig(token, service, (), settings, "", None, "", root, xml)
        if service == "media2":
            saved.codec_profile = root.get("Profile", "").strip()
        self._send(service, root, saved, persist=True)
        return compare(saved, self.read(token))


def _put_rate(rate: Element, name: str, text: str) -> None:
    """Поле RateControl; нет — создаётся на своём месте по схеме."""
    found = _child(rate, name)
    if found is None:
        namespace = rate.tag[1:].split("}")[0] if rate.tag.startswith("{") else NS["tt"]
        found = Element(f"{{{namespace}}}{name}")
        rank = _RATE_ORDER.index(name)
        position = len(rate)
        for i, sibling in enumerate(rate):
            sibling_name = _local(sibling.tag)
            if sibling_name in _RATE_ORDER and _RATE_ORDER.index(sibling_name) > rank:
                position = i
                break
        rate.insert(position, found)
    found.text = text


def _matches(config: EncoderConfig, codec: str, kbps: float, constant: bool) -> bool:
    """Камера держит записанное: кодек, битрейт в пределах шага и режим, если он писался."""
    got = config.settings
    if got.encoding != codec or got.kbps is None:
        return False
    if abs(got.kbps - kbps) > KBPS_TOLERANCE_SHARE * kbps:
        return False
    return not constant or got.constant is not False


def _shown(value: object) -> str:
    if value is None or value == "":
        return "нет"
    return f"{value:g}" if isinstance(value, float) else str(value)


def compare(was: EncoderConfig, now: EncoderConfig) -> RestoreReport:
    """Сверка «снимок / сейчас»: изменяемые поля и белый список — точно, прочее — для сведения."""
    report = RestoreReport()
    pairs = (
        ("кодек", was.settings.encoding, now.settings.encoding),
        ("битрейт, кбит/с", was.settings.kbps, now.settings.kbps),
        ("постоянный битрейт", was.settings.constant, now.settings.constant),
        ("профиль кодека", was.codec_profile, now.codec_profile),
    )
    for title, before, after in pairs:
        if before != after:
            report.left.append((title, _shown(before), _shown(after)))
    if was.element.get("GovLength") != now.element.get("GovLength"):
        report.left.append(
            ("GOP", _shown(was.element.get("GovLength")), _shown(now.element.get("GovLength")))
        )
    for name in KEPT:
        before, after = _first(was.element, name), _first(now.element, name)
        if canonical(before) != canonical(after):
            report.left.append((name, _brief(before), _brief(after)))
    known = {"Encoding", "RateControl", "BitrateLimit", "UseCount", *KEPT}
    for child in was.element:
        name = _local(child.tag)
        if name not in known and canonical(child) != canonical(_child(now.element, name)):
            report.other.append(f"{name}: после возврата не совпадает со снимком")
    return report


def _brief(element: Element | None) -> str:
    """Поле для строки «было / осталось»: текст или содержимое, не длиннее 120 символов."""
    if element is None:
        return "нет"
    text = (element.text or "").strip() if len(element) == 0 else _inner(element)
    return text[:120] or "пусто"
