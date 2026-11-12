"""Библиотека карт и пар `<папка прогонов>/api-library/` (S6, S7).

Прогон кладёт сюда карту и найденные пары камеры; следующий прогон той же модели берёт пары
отсюда и сверяет без автопоиска. Папка — `<бренд>/<модель>/<прошивка>/`, каждая часть —
`safe_name` от строки паспорта плюс 8 знаков sha256 исходной строки: разные модели не сливаются,
`..` и имена Windows не проходят. Файл на камеру и прогон — блокировки между процессами не нужны.

Данные библиотеки — недоверенные (прошлые прогоны, правка человеком `confirmed.json`): размер,
схема версии 1, `n` из каталога, файлы только внутри корня, ссылки не читаются. Из них берутся
номер, родной id и шкала — запросов к камере и значений для записи они не задают никогда.

Старшинство пар по номеру: найдено в этом прогоне > ручная таблица `data/api_pairs` >
`confirmed.json` > найденное прошлыми прогонами той же прошивки (самое позднее) > другой прошивки
(с пометкой). Найденная и ручная совпали — источник «найдено в прогоне …, подтверждено вручную»;
разошлись — берётся ручная с пометкой, что нашёл прогон.
"""

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path

from qa_tool.checks.api.catalog import check_pairs
from qa_tool.core.api_map import (
    MapFormatError,
    NativeMap,
    Pair,
    PairTable,
    dumps_map,
    dumps_pairs,
    loads_pairs,
)
from qa_tool.core.store import safe_name
from qa_tool.core.write_journal import write_atomic

CONFIRMED = "confirmed.json"
MAX_FILE_BYTES = 2 << 20
MAX_RUN_FILES = 50


def part_name(text: str) -> str:
    """Имя папки из строки паспорта: очищенное и с хешем исходной строки."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{safe_name(text.strip().casefold() or 'нет', 40)}-{digest}"


def model_dir(root: Path, brand: str, model: str) -> Path:
    """Папка модели (все прошивки)."""
    return root / part_name(brand) / part_name(model)


def firmware_dir(root: Path, brand: str, model: str, firmware: str) -> Path:
    """Папка модели и прошивки."""
    return model_dir(root, brand, model) / part_name(firmware)


@dataclass
class LibraryPairs:
    """Пары из библиотеки по источникам и что не прочиталось."""

    confirmed: list[Pair] = field(default_factory=list)
    past: list[Pair] = field(default_factory=list)
    other_firmware: list[Pair] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _inside(root: Path, path: Path) -> bool:
    """Файл внутри корня и не ссылка: подложенная ссылка не уведёт чтение с диска стенда."""
    try:
        return not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _read(root: Path, path: Path, problems: list[str]) -> PairTable | None:
    """Таблица из файла библиотеки; не прочиталась — None и причина."""
    if not _inside(root, path):
        problems.append(f"{path.name}: вне библиотеки или ссылка — пропущен")
        return None
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise MapFormatError(f"больше {MAX_FILE_BYTES} байт")
        table = loads_pairs(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, MapFormatError) as error:
        problems.append(f"{path.name}: не прочитан ({error})")
        return None
    bad = check_pairs(table)
    if bad:
        problems.append(f"{path.name}: " + "; ".join(bad))
        table.pairs = [p for p in table.pairs if not any(f"n={p.n}:" in b for b in bad)]
    return table


def _run_files(folder: Path) -> list[Path]:
    """Таблицы прогонов папки: самые поздние первыми (номера прогонов упорядочены по имени)."""
    if not folder.is_dir():
        return []
    return sorted(folder.glob("pairs-*.json"), key=lambda p: p.name, reverse=True)[:MAX_RUN_FILES]


def load_library(root: Path, brand: str, model: str, firmware: str) -> LibraryPairs:
    """Пары модели: подтверждённые человеком, прошлых прогонов и других прошивок.

    Кандидаты («кандидат») не берутся: в сверке они не участвуют.
    """
    found = LibraryPairs()
    if not root.is_dir():
        return found
    own = firmware_dir(root, brand, model, firmware)
    confirmed = own / CONFIRMED
    if confirmed.is_file():
        table = _read(root, confirmed, found.problems)
        if table is not None:
            found.confirmed = [p for p in table.pairs if p.state != "кандидат"]
    for path in _run_files(own):
        table = _read(root, path, found.problems)
        if table is not None:
            found.past += [p for p in table.pairs if p.state == "найдена"]
    model_folder = model_dir(root, brand, model)
    others = (
        sorted(p for p in model_folder.iterdir() if p.is_dir() and p != own)
        if model_folder.is_dir()
        else []
    )
    for folder in others:
        for path in _run_files(folder):
            table = _read(root, path, found.problems)
            if table is None:
                continue
            note = f" (найдено на прошивке {table.firmware})"
            found.other_firmware += [
                replace(p, source=p.source + note) for p in table.pairs if p.state == "найдена"
            ]
    return found


def _same(a: Pair, b: Pair) -> bool:
    """Одна и та же пара: тот же родной параметр и вид шкалы."""
    return a.native == b.native and a.scale.kind == b.scale.kind


def resolve(this_run: list[Pair], manual: list[Pair], library: LibraryPairs) -> list[Pair]:
    """Итоговая таблица: по номеру — пара старшего источника (см. модуль)."""
    chosen: dict[int, Pair] = {}
    for pair in [*manual, *library.confirmed, *library.past, *library.other_firmware]:
        chosen.setdefault(pair.n, pair)
    for pair in this_run:
        if pair.state != "найдена":
            continue
        older = next((p for p in manual if p.n == pair.n), None)
        if older is None:
            chosen[pair.n] = pair
        elif _same(older, pair):
            chosen[pair.n] = replace(older, source=f"{pair.source}, подтверждено вручную")
        else:
            chosen[pair.n] = replace(
                older, source=f"{older.source}; {pair.source} нашёл иначе: {pair.native}"
            )
    return [chosen[n] for n in sorted(chosen)]


def save(root: Path, native: NativeMap, pairs: PairTable) -> list[str]:
    """Положить карту и пары прогона в библиотеку атомарно; вернуть, что не записалось.

    Имена файлов — из номера прогона и места стенда, не из ответа камеры.
    """
    folder = firmware_dir(root, native.brand, native.model, native.firmware)
    stem = f"{safe_name(native.run or 'прогон', 40)}-{native.place}"
    problems = []
    for name, render in (
        (f"map-{stem}.json", lambda: dumps_map(native)),
        (f"pairs-{stem}.json", lambda: dumps_pairs(pairs)),
    ):
        try:
            write_atomic(folder / name, render())
        except (OSError, MapFormatError) as error:
            problems.append(f"библиотека: {name} не записан ({error})")
    return problems
