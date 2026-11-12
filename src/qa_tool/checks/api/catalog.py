"""Каталог настроек ONVIF для сверки с родным API (S3, S4, S6) и ручные таблицы пар.

Номер `n` — общий для всех камер: на нём стоят строки `API-MATCH-<n>` и `API-RANGE-<n>` и отчёт
сравнения камер, поэтому номера не переиспользуются, новые — только в конец. «S4 пишет» — можно ли
автопоиску менять эту настройку по ONVIF: экспозиция, ИК-фильтр (защищённые блоки «Картинки»,
`imaging.PROTECTED`), разрешение, частота и GOP не меняются — их пары берутся только из ручных
таблиц.

Ручные таблицы `data/api_pairs/<бренд>.json` лежат в репозитории и меняются через ревью; в них
только пары, проверенные на живых камерах. Из таблицы берутся номер, родной id и шкала — адресов и
значений для запросов к камере в ней нет (AD-4, дополнение 09.11).
"""

from dataclasses import dataclass
from pathlib import Path

from qa_tool.core.api_map import MapFormatError, Pair, PairTable, loads_pairs

PAIRS_DIR = Path(__file__).resolve().parents[2] / "data" / "api_pairs"
IMAGE, ENCODER = "картинка", "кодировщик"


@dataclass(frozen=True)
class Entry:
    """Настройка каталога.

    `path` — путь от ImagingSettings (картинка) или от конфигурации кодировщика профиля 1;
    `options` — путь диапазона в ImagingOptions, где он назван иначе (YrGain у CrGain);
    `setting` — ключ записи автопоиска (`SETTINGS` «Картинки» или поле `EncoderWriter`), пусто —
    S4 не пишет; `mode_of` — номера уровней, при записи которых камера включает этот режим
    (пара режима ищется попутно, S4 п. 9).
    """

    n: int
    title: str
    cls: str
    path: tuple[str, ...]
    setting: str = ""
    numeric: bool = True
    mode_of: tuple[int, ...] = ()
    options: tuple[str, ...] = ()  # путь диапазона в GetOptions, если он не совпадает с `path`

    @property
    def onvif(self) -> str:
        """Лист ONVIF для протокола: «Imaging/Brightness», «VideoEncoder/Encoding»."""
        root = "Imaging" if self.cls == IMAGE else "VideoEncoder"
        return "/".join((root, *self.path))

    @property
    def writes(self) -> bool:
        """Пишет ли автопоиск эту настройку сам."""
        return bool(self.setting)


CATALOG: tuple[Entry, ...] = (
    Entry(1, "Яркость", IMAGE, ("Brightness",), "brightness"),
    Entry(2, "Контраст", IMAGE, ("Contrast",), "contrast"),
    Entry(3, "Насыщенность", IMAGE, ("ColorSaturation",), "saturation"),
    Entry(4, "Резкость", IMAGE, ("Sharpness",), "sharpness"),
    Entry(
        5, "Баланс белого: режим", IMAGE, ("WhiteBalance", "Mode"), numeric=False, mode_of=(6, 7)
    ),
    Entry(
        6,
        "Баланс белого: красный",
        IMAGE,
        ("WhiteBalance", "CrGain"),
        "wb_red",
        options=("WhiteBalance", "YrGain"),
    ),
    Entry(
        7,
        "Баланс белого: синий",
        IMAGE,
        ("WhiteBalance", "CbGain"),
        "wb_blue",
        options=("WhiteBalance", "YbGain"),
    ),
    Entry(8, "WDR: режим", IMAGE, ("WideDynamicRange", "Mode"), numeric=False, mode_of=(9,)),
    Entry(9, "WDR: уровень", IMAGE, ("WideDynamicRange", "Level"), "wdr"),
    Entry(10, "BLC: режим", IMAGE, ("BacklightCompensation", "Mode"), numeric=False, mode_of=(11,)),
    Entry(11, "BLC: уровень", IMAGE, ("BacklightCompensation", "Level"), "blc"),
    Entry(12, "Экспозиция: режим", IMAGE, ("Exposure", "Mode"), numeric=False),
    Entry(13, "Экспозиция: наибольшее усиление", IMAGE, ("Exposure", "MaxGain")),
    Entry(14, "ИК-фильтр", IMAGE, ("IrCutFilter",), numeric=False),
    Entry(15, "Битрейт", ENCODER, ("RateControl", "BitrateLimit"), "kbps"),
    Entry(16, "Кодек", ENCODER, ("Encoding",), "encoding", numeric=False),
    Entry(17, "Разрешение", ENCODER, ("Resolution",), numeric=False),
    Entry(18, "Частота кадров", ENCODER, ("RateControl", "FrameRateLimit")),
    Entry(19, "Длина GOP", ENCODER, ("GovLength",)),
)
BY_N = {e.n: e for e in CATALOG}
MAX_TABLE_BYTES = 256 << 10  # ручная таблица — десятки пар, больше — не наш файл


@dataclass(frozen=True)
class ManualTable:
    """Ручная таблица бренда и причина, если она не прочиталась."""

    pairs: tuple[Pair, ...] = ()
    problem: str = ""


def check_pairs(table: PairTable) -> list[str]:
    """Пары с номером не из каталога; пусто — все годны."""
    return [f"пара n={p.n}: номера нет в каталоге" for p in table.pairs if p.n not in BY_N]


def manual_pairs(brand: str, model: str = "", firmware: str = "") -> ManualTable:
    """Подтверждённые вручную пары бренда, подходящие модели и прошивке камеры.

    Args:
        brand: бренд сборщика («hikvision»…); имя файла строится из него, не из ответа камеры.
        model: модель из паспорта; у пары с пустым списком моделей — любая.
        firmware: прошивка из паспорта; так же.

    Returns:
        `ManualTable`; файла нет — пусто без причины, файл негодный — пусто с причиной
        (в сводку приложения), пара с чужим номером — отброшена с причиной.
    """
    if not brand.isalnum():
        return ManualTable(problem=f"бренд «{brand}»: имя таблицы не годится")
    path = PAIRS_DIR / f"{brand}.json"
    if not path.is_file():
        return ManualTable()
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_TABLE_BYTES:
            raise MapFormatError(f"файл больше {MAX_TABLE_BYTES} байт")
        table = loads_pairs(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, MapFormatError) as error:
        return ManualTable(problem=f"ручная таблица {brand}.json не прочитана: {error}")
    problems = check_pairs(table)
    fits = tuple(
        p
        for p in table.pairs
        if p.n in BY_N
        and (not p.models or model in p.models)
        and (not p.firmwares or firmware in p.firmwares)
    )
    return ManualTable(fits, "; ".join(problems))
