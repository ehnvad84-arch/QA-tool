"""Сборщик-заготовка: бренд опознаётся, а читать родной API пока нечем (S2).

Tiandy: веб — через плагин браузера, родной протокол — собственный SDK не по HTTP; Beward: через
туннель 09.11 «нет формы входа», похоже на CGI за Digest, но адреса не подтверждены. Решение
директора 09.11: в первой версии — заготовки, адреса — после прямого доступа к камерам или
документа поставщика. Пустой список запросов ведёт проверку к карте из трафика веб-обхода.
"""

from qa_tool.core.api_map import NativeMap, Param, Section
from qa_tool.drivers.camera.native import NativeWrite, ParseRefused, ReadTemplate


class StubCollector:
    """Сборщик без запросов: `reads()` пуст, возврата родным API нет."""

    version = 0
    login = "digest"

    def __init__(self, brand: str, name: str, names: frozenset[str], why: str) -> None:
        """Заготовка бренда `brand`; `why` — почему родной API не читается (для протокола)."""
        self.brand, self.name, self.names, self.why = brand, name, names, why

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Запросов нет: адреса родного API не подтверждены."""
        return []

    def parse(
        self, template: ReadTemplate, body: bytes, content_type: str, describe: bytes | None
    ) -> Section:
        """Не вызывается: запросов нет."""
        raise ParseRefused(self.why)

    def expected(self, kind: str) -> tuple[str, ...]:
        """Ожидаемых разделов нет."""
        return ()

    def observed(self, native: NativeMap) -> list[str]:
        """Наблюдаемых разделов нет."""
        return []

    def restore_request(self, param: Param, m0: NativeMap) -> NativeWrite | None:
        """Возврата родным API нет."""
        return None
