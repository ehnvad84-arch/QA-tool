"""Каталог настроек картинки, которые проверяет `IMAGE-EFFECT-1` (спека этапа 7, S6, S11).

Каждая настройка — поле ONVIF Imaging (ImagingSettings20, Imaging Service Specification), величина
на кадре, по которой виден её эффект, и связь с автоэкспозицией: яркость, контраст и WDR камера
при автоэкспозиции может гасить сама, поэтому «нет эффекта» там не дефект (S4).
Пороги величин — не здесь, а в `rules.py` и `thresholds.yaml` (conventions.md).
"""

from dataclasses import dataclass
from enum import StrEnum

from qa_tool.drivers.camera import imaging


class Kind(StrEnum):
    """Как настройка пишется в ImagingSettings; значения — те же, что знает драйвер Imaging."""

    NUMBER = imaging.NUMBER  # одно поле с диапазоном из GetOptions
    MODE_LEVEL = imaging.MODE_LEVEL  # Mode=ON и Level; без диапазона уровня — «выкл/вкл»
    WHITE_BALANCE = imaging.WHITE_BALANCE  # Mode=MANUAL, проверяемое усиление и второе на 50 %


class Exposure(StrEnum):
    """Связь настройки с автоэкспозицией камеры."""

    INDEPENDENT = ""  # автоэкспозиция эффекта не гасит
    DAMPED = "гасит"  # при AUTO «нет эффекта» — «пропущена», а не «не прошла»
    NEEDS_AUTO = "нужна"  # компенсация засветки работает только при автоэкспозиции


@dataclass(frozen=True)
class Setting:
    """Одна проверяемая настройка.

    `path` — путь от ImagingSettings до значения по локальным именам элементов; `options` — путь
    от ImagingOptions до диапазона (элемента с Min и Max). `mode` — путь до режима у MODE_LEVEL
    и WHITE_BALANCE; `partner` — второе усиление баланса белого, которое держится на 50 %.
    """

    key: str
    check_id: str
    title: str
    path: tuple[str, ...]
    options: tuple[str, ...]
    metric: str
    unit: str
    kind: Kind = Kind.NUMBER
    exposure: Exposure = Exposure.INDEPENDENT
    mode: tuple[str, ...] = ()
    partner: tuple[str, ...] = ()
    partner_options: tuple[str, ...] = ()


# Путь NoiseReduction — ImagingSettingsExtension20 → 202 → 203 (ONVIF 17.06 и новее).
_NR = ("Extension", "Extension", "Extension", "NoiseReduction", "Level")

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "brightness",
        "IMAGE-BRIGHT-1",
        "Яркость",
        ("Brightness",),
        ("Brightness",),
        "mean_luma",
        "уровень",
        exposure=Exposure.DAMPED,
    ),
    Setting(
        "contrast",
        "IMAGE-CONTRAST-1",
        "Контраст",
        ("Contrast",),
        ("Contrast",),
        "luma_std",
        "уровень",
        exposure=Exposure.DAMPED,
    ),
    Setting(
        "saturation",
        "IMAGE-SAT-1",
        "Насыщенность",
        ("ColorSaturation",),
        ("ColorSaturation",),
        "saturation",
        "%",
    ),
    Setting(
        "sharpness",
        "IMAGE-SHARP-1",
        "Резкость",
        ("Sharpness",),
        ("Sharpness",),
        "laplacian_var",
        "% от исходного",
    ),
    Setting(
        "wb_red",
        "IMAGE-WB-1",
        "Баланс белого: красный",
        ("WhiteBalance", "CrGain"),
        ("WhiteBalance", "YrGain"),
        "red_blue",
        "% от исходного",
        kind=Kind.WHITE_BALANCE,
        mode=("WhiteBalance", "Mode"),
        partner=("WhiteBalance", "CbGain"),
        partner_options=("WhiteBalance", "YbGain"),
    ),
    Setting(
        "wb_blue",
        "IMAGE-WB-2",
        "Баланс белого: синий",
        ("WhiteBalance", "CbGain"),
        ("WhiteBalance", "YbGain"),
        "red_blue",
        "% от исходного",
        kind=Kind.WHITE_BALANCE,
        mode=("WhiteBalance", "Mode"),
        partner=("WhiteBalance", "CrGain"),
        partner_options=("WhiteBalance", "YrGain"),
    ),
    Setting(
        "wdr",
        "IMAGE-WDR-1",
        "Широкий динамический диапазон (WDR)",
        ("WideDynamicRange", "Level"),
        ("WideDynamicRange", "Level"),
        "luma_spread",
        "уровень",
        kind=Kind.MODE_LEVEL,
        exposure=Exposure.DAMPED,
        mode=("WideDynamicRange", "Mode"),
    ),
    Setting(
        "blc",
        "IMAGE-BLC-1",
        "Компенсация засветки (BLC)",
        ("BacklightCompensation", "Level"),
        ("BacklightCompensation", "Level"),
        "center_luma",
        "уровень",
        kind=Kind.MODE_LEVEL,
        exposure=Exposure.NEEDS_AUTO,
        mode=("BacklightCompensation", "Mode"),
    ),
    Setting(
        "nr",
        "IMAGE-NR-1",
        "Шумоподавление",
        _NR,
        ("NoiseReductionOptions",),
        "temporal_noise",
        "% от исходного",
    ),
)

BY_KEY = {s.key: s for s in SETTINGS}
