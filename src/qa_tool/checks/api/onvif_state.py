"""Состояние ONVIF до записи (state0) по каталогу настроек: значение и объявленный диапазон.

Читается только: GetImagingSettings и GetOptions источника видео, конфигурация и варианты
кодировщика профиля 1 (`EncoderWriter.configs`, `options`). Профили 2 и дальше — следующая
итерация (спека, «Каталог»). Ответы камеры — недоверенные числа: конечные и не больше 10⁹.
"""

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from qa_tool.checks.api.catalog import CATALOG, ENCODER, IMAGE
from qa_tool.checks.api.compare import OnvifSide, number
from qa_tool.core.api_map import finite_number
from qa_tool.drivers.camera.encoder import CodecOptions, EncoderConfig, EncoderWriter
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _at(element: Element | None, path: tuple[str, ...]) -> Element | None:
    """Потомок по локальным именам; без пространств имён — камеры пишут префиксы по-разному."""
    for name in path:
        if element is None:
            return None
        element = next((c for c in element if _local(c.tag) == name), None)
    return element


def imaging_sides(settings: Element, options: Element | None) -> dict[int, OnvifSide]:
    """Листы картинки каталога: текст из ImagingSettings, диапазон Min…Max из ImagingOptions."""
    sides: dict[int, OnvifSide] = {}
    for entry in CATALOG:
        if entry.cls != IMAGE:
            continue
        found = _at(settings, entry.path)
        value = None if found is None or found.text is None else found.text.strip()
        low = high = None
        if entry.numeric and options is not None:
            span = _at(options, entry.options or entry.path)
            low = finite_number(getattr(_at(span, ("Min",)), "text", None))
            high = finite_number(getattr(_at(span, ("Max",)), "text", None))
        sides[entry.n] = OnvifSide(value or None, low, high)
    return sides


def encoder_sides(config: EncoderConfig, options: dict) -> dict[int, OnvifSide]:
    """Листы кодировщика профиля 1: битрейт с диапазоном кодека, кодек, размер, частота, GOP."""
    s = config.settings
    codec = options.get(s.encoding.upper()) if s.encoding else None
    low = codec.kbps_min if isinstance(codec, CodecOptions) else None
    high = codec.kbps_max if isinstance(codec, CodecOptions) else None
    size = f"{s.width}x{s.height}" if s.width and s.height else None
    values = {
        15: OnvifSide(None if s.kbps is None else number(s.kbps).replace(",", "."), low, high),
        16: OnvifSide(s.encoding or None),
        17: OnvifSide(size),
        18: OnvifSide(None if s.fps is None else number(s.fps).replace(",", ".")),
        19: OnvifSide(None if s.gop is None else str(s.gop)),
    }
    return {e.n: values[e.n] for e in CATALOG if e.cls == ENCODER}


@dataclass
class State0:
    """Состояние ONVIF до записи и что не прочиталось (в сводку приложения)."""

    sides: dict[int, OnvifSide] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    imaging_xml: str = ""
    encoder: EncoderConfig | None = None


def read_state0(soap: SoapClient, services: dict[str, str], source: str) -> State0:
    """Прочитать state0 по каталогу; сбой одной службы не мешает другой.

    Args:
        soap: клиент ONVIF камеры.
        services: адреса служб (`service_urls`).
        source: токен источника видео профиля 1.

    Returns:
        `State0`; не прочиталось — листы без значений и причина в `problems`.
    """
    state = State0()
    imaging = services.get("imaging")
    if imaging and source:
        # токен источника — из ответа камеры: в SOAP только экранированным
        token = f"<timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken>"
        try:
            answer = soap.call(
                imaging, f"<timg:GetImagingSettings>{token}</timg:GetImagingSettings>"
            )
            settings = next((e for e in answer.iter() if _local(e.tag) == "ImagingSettings"), None)
            options_answer = soap.call(imaging, f"<timg:GetOptions>{token}</timg:GetOptions>")
            options = next(
                (e for e in options_answer.iter() if _local(e.tag) == "ImagingOptions"), None
            )
            if settings is None:
                state.problems.append("ONVIF: GetImagingSettings без ImagingSettings")
            else:
                state.sides.update(imaging_sides(settings, options))
        except (CameraUnreachable, OnvifFault) as error:
            state.problems.append(f"ONVIF: настройки картинки не прочитаны: {error}")
    else:
        state.problems.append("камера не объявляет службу Imaging (ONVIF)")
    try:
        writer = EncoderWriter(soap, services)
        configs = writer.configs()
        if configs:
            state.encoder = configs[0]
            state.sides.update(encoder_sides(configs[0], writer.options(configs[0].token)))
        else:
            state.problems.append("ONVIF: у камеры нет кодировщиков видео")
    except (CameraUnreachable, OnvifFault) as error:
        state.problems.append(f"ONVIF: кодировщик не прочитан: {error}")
    return state
