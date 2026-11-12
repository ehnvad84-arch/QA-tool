"""Разбор ответов «ключ=значение» (Dahua CGI, Beward) — недоверенный текст камеры.

Строка ответа `table.VideoColor[0][1].Brightness=50` даёт ключ `VideoColor[0][1].Brightness`:
приставка `table.` снимается, индексы остаются — так id параметра устойчив между прошивками.
Ключ — только буквы, цифры, «_», «.», «-» и скобки индексов, до 128 знаков; иное пропускается
со счётом, чтобы мусор камеры не стал id в карте и в протоколе.
"""

import re
from dataclasses import dataclass, field

from qa_tool.core.api_map import MAX_PARAMS

MAX_KEY_LEN = 128
_KEY = re.compile(r"^[\w.\[\]-]{1,128}$")
_INDEX = re.compile(r"\[\d+\]")


@dataclass
class KeyValues:
    """Разобранный ответ: пары по порядку ответа и число пропущенных строк."""

    pairs: dict[str, str] = field(default_factory=dict)
    skipped: int = 0
    cut: bool = False


def parse_key_value(text: str, prefix: str = "table.", limit: int = MAX_PARAMS) -> KeyValues:
    """Разобрать ответ «ключ=значение» построчно.

    Args:
        text: тело ответа (размер уже ограничен `http.request(max_bytes=…)`).
        prefix: приставка ключа, которую камера ставит перед именем таблицы.
        limit: наибольшее число ключей; сверх — `cut`.

    Returns:
        `KeyValues`: повтор ключа не перекрывает первый; пустые строки не считаются пропуском.
    """
    found = KeyValues()
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        key = key.strip().removeprefix(prefix)
        if not sep or not _KEY.match(key):
            found.skipped += 1
            continue
        if len(found.pairs) >= limit:
            found.cut = True
            break
        found.pairs.setdefault(key, value.strip())
    return found


def is_error_reply(text: str) -> bool:
    """Ответ CGI «Error …» вместо данных: у Dahua так выглядит неизвестная таблица или действие."""
    return text.lstrip().lower().startswith("error")


def table_of(key: str) -> str:
    """Имя таблицы ключа: `VideoColor[0][1].Brightness` → `VideoColor`."""
    return re.split(r"[.\[]", key, maxsplit=1)[0]


def without_indexes(key: str) -> str:
    """Ключ без индексов: копии по каналам и профилям — один параметр (S5, решение 09.11)."""
    return _INDEX.sub("[*]", key)
