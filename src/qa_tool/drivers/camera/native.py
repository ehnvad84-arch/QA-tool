"""Чтение родного API камеры (Hikvision ISAPI, Dahua CGI…) сборщиками брендов (S1, S8, S9).

Сборщик бренда (`drivers/camera/<бренд>/collector.py`) знает, что читать и как разобрать ответ,
но к камере не ходит: единственный путь — `NativeReader`. Он отправляет только GET по шаблонам,
которые сборщик вернул из `reads()`; каждый запрос проходит `taboo.check_native_read` (у Dahua
`magicBox.cgi?action=reboot` — обычный GET), затем `http.request` с входом и пределом размера.
Адрес — только `camera.address:port` со стенда: из ответов камеры адреса не берутся.

Пределы — защита от враждебного ответа и от зависания процесса камеры: ≤ 80 запросов, бюджет
времени на карту, ≤ 5000 параметров, ответ ≤ 2 МБ. Отказ во входе (`LoginRefused`) не ловится:
его превращает в «ошибку стенда» проверка (`checks/api/discovery.py`), она же ставит общий признак
отказа (`checks/rtsp_refusal.py`), — драйвер модуль проверок не импортирует.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from qa_tool.core.api_map import MAX_PARAMS, MapError, NativeMap, Param, Section, Sensitivity
from qa_tool.core.models import Camera
from qa_tool.core.taboo import TabooViolation, check_native_read
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.http import HttpLogin, ResponseTooLarge, TransportError

MAX_REQUESTS = 80
MAX_RESPONSE_BYTES = 2 << 20
MAX_EXCHANGE_BODY = 16 << 10  # тела в exchanges.txt — для разбора человеком, не архив ответа
# Путь и запрос шаблона: только то, что встречается в адресах ISAPI и CGI. Ни схемы, ни хоста,
# ни «..», ни переводов строк — шаблон не может увести запрос с камеры стенда.
_SAFE_PATH = re.compile(r"^/[\w\-./]{0,255}$")
_SAFE_QUERY = re.compile(r"^[\w\-.\[\]=&,*%]{0,512}$")


@dataclass(frozen=True)
class ReadTemplate:
    """Один запрос чтения сборщика.

    `describe_path` — парный запрос описания (ISAPI `…/capabilities`): допустимые значения
    раздела; его ответ сборщик получает в `parse` вместе с основным.
    """

    path: str
    section_id: str
    cls: str
    query: str = ""
    describe_path: str = ""
    sensitivity: Sensitivity = Sensitivity.NORMAL
    method: str = "GET"

    def target(self) -> str:
        """Путь с запросом, как в строке запроса HTTP."""
        return f"{self.path}?{self.query}" if self.query else self.path


@dataclass(frozen=True)
class NativeWrite:
    """Запрос возврата параметра родным API (S5, `checks/api/restore.py`)."""

    method: str
    path: str
    query: str = ""
    body: str = ""


class ParseRefused(ValueError):
    """Ответ не разобран: в «ошибки» карты с причиной, остальные разделы читаются дальше."""


class NativeUnreachable(RuntimeError):
    """Больше половины запросов без ответа: камера пропала, карта не снята."""


class Collector(Protocol):
    """Сборщик бренда: что читать, как разобрать, что ожидать при записи и как вернуть.

    `names` — слова производителя без регистра: первое слово паспорта ONVIF («hikvision») и
    значение `camera.vendor` на месте стенда. `login` — «digest» (`HttpLogin`) или «session»
    («Рувер», свой клиент).
    """

    brand: str
    name: str
    version: int
    names: frozenset[str]
    login: str

    def reads(self, found: list[Section]) -> list[ReadTemplate]:
        """Следующие запросы по уже прочитанным разделам; номера каналов — только цифры."""
        ...

    def parse(
        self, template: ReadTemplate, body: bytes, content_type: str, describe: bytes | None
    ) -> Section:
        """Раздел из ответа; не разобран — `ParseRefused`."""
        ...

    def expected(self, kind: str) -> tuple[str, ...]:
        """Шаблоны id разделов, где ждём сдвиг при записи настройки класса `kind` (S5)."""
        ...

    def observed(self, native: NativeMap) -> list[str]:
        """id разделов для снимков автопоиска: несекретные, нечувствительные, со значениями."""
        ...

    def restore_request(self, param: Param, m0: NativeMap) -> NativeWrite | None:
        """Запрос возврата параметра картинки к значению `m0`; None — возвращать нечем."""
        ...


@dataclass(frozen=True)
class Choice:
    """Выбранный сборщик и пометка для сводки протокола, если паспорт и стенд разошлись."""

    collector: Collector | None
    note: str = ""


def maker_word(passport: dict | None) -> str:
    """Первое слово производителя из паспорта ONVIF без регистра: «HIKVISION» → «hikvision»."""
    maker = str((passport or {}).get("manufacturer", "")).strip().casefold()
    return maker.split()[0] if maker else ""


def choose_collector(collectors: list[Collector], passport: dict | None, camera: Camera) -> Choice:
    """Сборщик по паспорту ONVIF, иначе по производителю на месте стенда.

    Паспорт главнее: место стенда заполняет человек и может ошибиться, а паспорт отдаёт камера.

    Returns:
        `Choice`; сборщика нет — `collector=None` (S2: карта из трафика веб-обхода).
    """
    word, vendor = maker_word(passport), camera.vendor.strip().casefold()
    by_passport = next((c for c in collectors if word and word in c.names), None)
    by_vendor = next((c for c in collectors if vendor and vendor in c.names), None)
    if by_passport is not None:
        note = ""
        if by_vendor is not None and by_vendor.brand != by_passport.brand:
            note = (
                f"производитель на месте стенда — {camera.vendor}, паспорт ONVIF — "
                f"{passport.get('manufacturer')}: родной API читается по паспорту"
            )
        return Choice(by_passport, note)
    if by_vendor is not None:
        note = ""
        if word:
            note = (
                f"паспорт ONVIF — {passport.get('manufacturer')}, сборщика для него нет: "
                f"родной API читается как {camera.vendor} по месту стенда"
            )
        return Choice(by_vendor, note)
    return Choice(None)


@dataclass(frozen=True)
class Exchange:
    """Запрос и ответ для `exchanges.txt`; у чувствительных разделов тела нет."""

    request: str
    code: int | None
    size: int
    body: str = ""


def _reason(code: int) -> str:
    """Причина ошибки раздела по коду ответа (401 сюда не доходит: это отказ во входе)."""
    if code == 403:
        return "нет прав у пользователя"
    if code == 404:
        return "раздела нет у модели"
    if code >= 500:
        return "камера ответила ошибкой"
    return f"камера ответила кодом {code}"


@dataclass
class NativeReader:
    """Снимает карту родного API одной камеры одним сборщиком.

    Не потокобезопасен: одна камера — один процесс и одна нить. `login` — общий на прогон
    (`HttpLogin`), его отказ видят и другие проверки.
    """

    camera: Camera
    collector: Collector
    login: HttpLogin | None
    timeout_s: float = 5.0
    budget_s: float = 120.0
    clock: Callable[[], float] = time.monotonic
    exchanges: list[Exchange] = field(default_factory=list)
    sent: int = 0
    lost: int = 0

    def url(self, target: str) -> str:
        """Полный адрес: только адрес и порт камеры со стенда."""
        return f"http://{self.camera.address}:{self.camera.port}{target}"

    def _get(self, target: str) -> http.Response | None:
        """GET с одним повтором на сбой сети; None — камера не ответила."""
        url = self.url(target)
        check_native_read("GET", url)
        self.sent += 1
        for attempt in (1, 2):
            try:
                return http.request(
                    "GET",
                    url,
                    timeout=self.timeout_s,
                    login=self.login,
                    max_bytes=MAX_RESPONSE_BYTES,
                )
            except TransportError:
                if attempt == 2:
                    self.lost += 1
        return None

    def _checked(self, template: ReadTemplate) -> str:
        """Причина, по которой шаблон сборщика не отправляется; пусто — можно."""
        if template.method != "GET":
            return f"метод {template.method}: родной API только читается"
        for path in filter(None, (template.path, template.describe_path)):
            if not _SAFE_PATH.match(path) or ".." in path or "//" in path:
                return "адрес шаблона не годится"
        if not _SAFE_QUERY.match(template.query):
            return "запрос шаблона не годится"
        return ""

    def read(self, meta: NativeMap) -> NativeMap:
        """Снять карту: запросы по `reads()`, пока сборщик даёт новые, или до пределов.

        Args:
            meta: заготовка карты с брендом, паспортом, прогоном и местом; разделы и ошибки
                добавляются в неё же.

        Returns:
            Та же карта с разделами, ошибками, признаком обрезки и способом входа.

        Raises:
            LoginRefused: камера отвергла вход или его уже отвергли на прогоне (S8).
            LoginUnsupported: способ входа камеры не поддержан — пароль не ушёл.
            NativeUnreachable: больше половины запросов без ответа.
        """
        started, seen = self.clock(), set()
        while True:
            fresh = [t for t in self.collector.reads(meta.sections) if t.target() not in seen]
            if not fresh:
                break
            for template in fresh:
                seen.add(template.target())
                stop = self._limit(meta, started)
                if stop:
                    meta.cut, meta.cut_reason = True, stop
                    return self._finish(meta)
                self._read_one(meta, template)
        return self._finish(meta)

    def _limit(self, meta: NativeMap, started: float) -> str:
        """Причина остановки по пределам; пусто — читать дальше."""
        if self.sent >= MAX_REQUESTS:
            return f"запросов больше {MAX_REQUESTS} — остальные разделы не читались"
        if self.clock() - started > self.budget_s:
            return f"карта не уложилась в {self.budget_s:g} с — остальные разделы не читались"
        if meta.param_count() >= MAX_PARAMS:
            return f"параметров {MAX_PARAMS} — остальные разделы не читались"
        if self.sent >= 4 and self.lost * 2 > self.sent:
            raise NativeUnreachable(f"камера не ответила на {self.lost} запросов из {self.sent}")
        return ""

    def _read_one(self, meta: NativeMap, template: ReadTemplate) -> None:
        """Прочитать раздел (и его описание) и добавить в карту раздел или ошибку."""
        request = f"GET {template.target()}"
        problem = self._checked(template)
        if problem:
            meta.errors.append(MapError(request, None, f"не отправлен: {problem}"))
            return
        try:
            response = self._get(template.target())
            describe = None
            if response is not None and response.status_code == 200 and template.describe_path:
                extra = self._get(template.describe_path)
                if extra is not None and extra.status_code == 200:
                    describe = extra.content
        except TabooViolation as error:
            meta.errors.append(MapError(request, None, f"не отправлен: {error}"))
            return
        except ResponseTooLarge:
            meta.errors.append(MapError(request, None, "ответ больше 2 МБ — не разобран"))
            return
        if response is None:
            meta.errors.append(MapError(request, None, "камера не ответила"))
            return
        code = response.status_code
        if code != 200:
            meta.errors.append(MapError(request, code, _reason(code)))
            self.exchanges.append(Exchange(request, code, len(response.content)))
            return
        content_type = response.headers.get("content-type", "")
        try:
            section = self.collector.parse(template, response.content, content_type, describe)
        except ParseRefused as error:
            meta.errors.append(MapError(request, code, f"ответ не разобран: {error}"))
            self.exchanges.append(Exchange(request, code, len(response.content)))
            return
        section.read, section.code, section.size = request, code, len(response.content)
        if template.describe_path:
            section.describe = f"GET {template.describe_path}"
        meta.sections.append(section)
        self.exchanges.append(self._exchange(request, code, response.content, section))

    @staticmethod
    def _exchange(request: str, code: int, body: bytes, section: Section) -> Exchange:
        """Обмен для доказательств: тело — только если в разделе нет чувствительного."""
        hidden = section.sensitivity is not Sensitivity.NORMAL or any(
            p.sensitivity is not Sensitivity.NORMAL for p in section.params
        )
        text = "" if hidden else body[:MAX_EXCHANGE_BODY].decode("utf-8", "replace")
        return Exchange(request, code, len(body), text)

    def _finish(self, meta: NativeMap) -> NativeMap:
        """Способ входа и проверка «камера пропала» в конце чтения."""
        if self.sent and self.lost * 2 > self.sent:
            raise NativeUnreachable(f"камера не ответила на {self.lost} запросов из {self.sent}")
        if self.login is not None and self.login.challenge is not None:
            meta.login = "Digest MD5"
        elif not meta.login:
            meta.login = "без входа"
        return meta
