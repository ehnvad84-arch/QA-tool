"""Возврат родным API (S5): параметр, который возврат по ONVIF не вернул, — одним запросом сборщика.

Условия — раздел «Безопасность» спеки («Запись родным API»): только с галочкой «изменяющие» и
только в S5 этого прогона; параметр не изменчивый и не чувствительный; запрос строит сам сборщик
(`restore_request`: только картинка, значение — из M0 этого прогона); журнал со списком `native`
записан до отправки; одна попытка, без повтора. Табу `http.py` действует как всегда.

Журнал прошлого прогона родным API здесь не возвращается: его параметры уходят в блокер для
ручного возврата (`old_native_lines`).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import defusedxml.ElementTree as SafeET

from qa_tool.core.api_map import Change, NativeMap, Sensitivity
from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera.http import LoginRefused, ResponseTooLarge, TransportError
from qa_tool.drivers.camera.native import Collector, NativeWrite, NotSent
from qa_tool.drivers.camera.ruver.client import RuverError

# Ответ камеры на запрос возврата: код HTTP. Отправку делает проверка своим входом (`HttpLogin`
# или сеанс «Рувер»): здесь только решение, что и можно ли отправить.
Send = Callable[[NativeWrite], int]
# Сбои одной попытки: запрос не ушёл или ответа нет — решает перечитанная карта, повтора нет.
SEND_ERRORS = (TransportError, NotSent, TabooViolation, LoginRefused, ResponseTooLarge, RuverError)
MANUAL = "веб-интерфейс камеры, раздел изображения"


@dataclass
class Attempt:
    """Попытка вернуть один параметр родным API."""

    change: Change
    write: NativeWrite | None = None
    sent: bool = False
    why_not: str = ""

    def how(self) -> str:
        """Как вернуть вручную, если параметр не вернулся (для строки `API-RESTORE-1`)."""
        if self.write is None:
            return f"{MANUAL} ({self.why_not})" if self.why_not else MANUAL
        query = f"?{self.write.query}" if self.write.query else ""
        if self.sent:
            tried = "программа пробовала один раз, не вернулось"
            tried += f" ({self.why_not})" if self.why_not else ""
        else:
            tried = f"не отправлено: {self.why_not}"
        return (
            f"запросом к родному API: {self.write.method} {self.write.path}{query}, "
            f"{self.change.id} = {self.change.before} — {tried}"
        )

    def journal(self) -> dict[str, str]:
        """Строка списка `native` журнала `API` (спека, «Побочный эффект, журнал и возврат S5»)."""
        write = self.write or NativeWrite("", "")
        query = f"?{write.query}" if write.query else ""
        return {
            "id": self.change.id,
            "запрос": f"{write.method} {write.path}{query}".strip(),
            "тело": write.body,
            "было": self.change.before,
            "осталось": self.change.after,
        }


class NativeRestorer:
    """Возврат сдвинувшихся параметров картинки запросами сборщика бренда.

    Args:
        collector: сборщик бренда (`restore_request`).
        m0: исходная карта M0 этого прогона — единственный источник значений и адресов.
        volatile: изменчивые параметры: их не возвращаем и не перезаписываем попутно.
        send: отправка запроса тем же входом, что и чтение карты; `None` — вход не позволяет
            писать (карта из трафика, заготовка бренда), всё — вручную.
        journal: запись списка `native` в журнал `API` (с `fsync`) до первой отправки.
    """

    def __init__(
        self,
        collector: Collector,
        m0: NativeMap,
        volatile: frozenset[str],
        send: Send | None,
        journal: Callable[[list[dict[str, str]]], None],
    ) -> None:
        """Подготовить возврат; запросы уходят только в `run`."""
        self.collector, self.m0, self.volatile = collector, m0, volatile
        self.send, self.journal = send, journal

    def _plan(self, change: Change, now: NativeMap) -> Attempt:
        """Запрос возврата одного параметра или причина, почему только вручную."""
        before = self.m0.params().get(change.id)
        if before is None or before.value is None or before.sensitivity is not Sensitivity.NORMAL:
            return Attempt(change, why_not="значения в исходной карте нет")
        if change.id in self.volatile or before.volatile:
            return Attempt(change, why_not="параметр меняется сам")
        if self.send is None:
            return Attempt(change, why_not="у этого входа записи родным API нет")
        write = self.collector.restore_request(before, self.m0)
        if write is None:
            return Attempt(change, why_not="у сборщика нет запроса возврата для этого параметра")
        problem = self._check(write, now)
        if problem:
            return Attempt(change, write, why_not=problem)
        return Attempt(change, write)

    def _check(self, write: NativeWrite, now: NativeMap) -> str:
        """Сверка запроса перед отправкой (п. 5 «Запись родным API»); пусто — можно."""
        if write.body.lstrip().startswith("<"):
            try:
                SafeET.fromstring(write.body, forbid_dtd=True)
            except Exception as error:  # тело не XML или с DTD: отправлять нельзя
                return f"тело запроса не разобрано: {type(error).__name__}"
        # Тело несёт весь блок M0: изменчивый лист в нём вернули бы к старому значению попутно.
        covered = self._covered(write, now)
        if covered & self.volatile:
            return "запрос вернул бы и меняющийся сам параметр — " + ", ".join(
                sorted(covered & self.volatile)
            )
        return ""

    def _covered(self, write: NativeWrite, now: NativeMap) -> set[str]:
        """Параметры раздела, которые тот же запрос возвращает вместе (поддерево ISAPI)."""
        params = self.m0.params()
        found: set[str] = set()
        for section in now.sections:
            for param in section.params:
                base = params.get(param.id)
                if base is not None and self.collector.restore_request(base, self.m0) == write:
                    found.add(param.id)
        return found

    def run(self, changes: list[Change], now: NativeMap) -> list[Attempt]:
        """Вернуть параметры `changes` (M3 ≠ M0) по одному запросу на запрос сборщика.

        Args:
            changes: что осталось сдвинутым после возврата по ONVIF.
            now: карта M3 после возврата по ONVIF.

        Returns:
            Попытки по параметрам; вернулось ли — решает вызывающий новым снимком.

        Raises:
            OSError: журнал не записан — запросы не отправлены.
        """
        attempts = [self._plan(change, now) for change in changes]
        ready = [a for a in attempts if a.write is not None and not a.why_not]
        if not ready:
            return attempts
        self.journal([a.journal() for a in attempts])
        sent: dict[NativeWrite, str] = {}
        for attempt in ready:
            write = attempt.write
            assert write is not None
            if write not in sent:
                sent[write] = self._send_once(write)
            attempt.sent, attempt.why_not = True, sent[write]
        return attempts

    def _send_once(self, write: NativeWrite) -> str:
        """Одна отправка; пусто — камера ответила 2xx."""
        assert self.send is not None
        try:
            code = self.send(write)
        except SEND_ERRORS as error:
            return f"{type(error).__name__}: {error}"[:200]
        return "" if 200 <= code < 300 else f"камера ответила {code}"


def old_native_lines(restore: dict[str, str], label: str) -> list[str]:
    """Параметры родного API из журнала прошлого прогона — для ручного возврата.

    Список `native` — недоверенный файл: берутся только строки-поля, длина ограничена.
    """
    try:
        items = json.loads(restore.get("native", "") or "[]")
    except ValueError:
        return ["список параметров родного API в журнале повреждён — проверьте вручную"]
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        fields = {k: str(item.get(k, ""))[:256] for k in ("id", "запрос", "было", "осталось")}
        how = f"; запрос возврата: {fields['запрос']}" if fields["запрос"] else ""
        lines.append(
            f"{label} {fields['id']}: было {fields['было']}, осталось {fields['осталось']}{how}"
        )
    return lines
