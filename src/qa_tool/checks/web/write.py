"""Запись настройки через веб-интерфейс: изменить, сверить, вернуть, сверить возврат (S12–S14).

Идёт только с галочкой «изменяющие». Порядок: чтение → журнал → окно записи → сохранение →
перезагрузка страницы → чтение → возврат тем же путём → сверка → журнал закрыт. Журнал пишется до
первого изменения поля: часть интерфейсов сохраняет сразу, без кнопки.

Меняются только поля с понятным обратным значением: список, флажок, ползунок, число. Свободный
текст, время, адреса и пароли не меняются: другое допустимое значение программа не знает. Кнопки
действий и с галочкой остаются остановленными: вернуть действие нечем (AD-3).

Запись на «Рувер» требует отдельного разрешения директора: галочка общая на сценарий, а эта камера
своя. Производитель берётся из паспорта ONVIF, а не из поля стенда, поэтому паспорт обязателен.
"""

import time
from collections.abc import Callable
from pathlib import Path

from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.checks.web.discover import Item, Mark, Page
from qa_tool.checks.web.limits import limit
from qa_tool.checks.web.login import Entry, login_for_restore
from qa_tool.checks.web.probe import Row
from qa_tool.core.checks import CheckContext, CheckResult, Severity, Status
from qa_tool.core.web_journal import (
    FOLDER_KEY,
    RUN_ID_KEY,
    PendingWrite,
    clear,
    load,
    save,
)
from qa_tool.drivers.camera.browser import (
    BrowserCrashed,
    BrowserSession,
    CameraSilent,
    ElementInfo,
    Reaction,
    Stopped,
    Verdict,
)
from qa_tool.drivers.camera.probe import tcp_open

CHANGEABLE_KINDS = frozenset({"checkbox", "range", "number"})
SAVE_WORDS = ("сохран", "примен", "save", "apply")
REBOOT_PHRASES = (
    "нужна перезагрузка",
    "требуется перезагрузка",
    "перезагрузите",
    "reboot required",
    "need reboot",
    "restart required",
    "need_reboot",
)
RUVER_NAMES = ("ruware", "ruver", "рувер")
SETTLE_MS = 500
RETURN_POLL_S = 2.0  # как часто спрашивать, вернулась ли камера
# Адаптер производителя читает записанный адрес через своё API: производитель → читатель.
# Рувера здесь нет: вход адаптера не сверен с прошивкой, и неверные попытки ушли бы на камеру (T8).
API_READERS: dict[str, Callable[[CheckContext, str], str | None]] = {}


def fingerprint(element: ElementInfo) -> str:
    """Отпечаток поля: по нему поле находится после перезагрузки страницы."""
    return "|".join((element.tag, element.kind, element.name, element.id, element.label))


def changeable(element: ElementInfo) -> bool:
    """Есть ли у поля понятное другое значение."""
    return element.tag == "select" or element.kind in CHANGEABLE_KINDS


def is_field(element: ElementInfo) -> bool:
    """Поле ввода, а не кнопка или ссылка."""
    return element.tag in ("input", "select", "textarea") and element.kind not in (
        "button",
        "submit",
        "reset",
    )


def _is_ruver(ctx: CheckContext, passport: dict) -> bool:
    names = (ctx.camera.vendor, str(passport.get("manufacturer", "")))
    return any(mark in name.lower() for name in names for mark in RUVER_NAMES)


class Writer:
    """Записи одной камеры за обход. Работает в потоке обхода, сессия браузера общая.

    Args:
        ctx: контекст проверки.
        session: сессия, страница поля открыта.
        reopen: открыть страницу заново и вернуть её элементы.
    """

    def __init__(
        self,
        ctx: CheckContext,
        session: BrowserSession,
        reopen: Callable[[Page], list[ElementInfo]],
    ) -> None:
        self.ctx = ctx
        self.session = session
        self.reopen = reopen
        self.folder = Path(ctx.extra[FOLDER_KEY]) if FOLDER_KEY in ctx.extra else None
        self.done = 0
        self.restored = 0
        self.halted = ""

    def refusal(self, item: Item) -> str:
        """Почему поле не меняется; пусто — менять можно."""
        element = item.element
        if item.mark is not Mark.ORDINARY:
            return item.reason
        if not changeable(element):
            return "поле не меняется: другое допустимое значение неизвестно"
        if self.halted:
            return self.halted
        if self.folder is None:
            return "запись запрещена: нет папки журнала"
        passport = self.ctx.extra.get(PASSPORT_KEY)
        if passport is None:
            return "запись запрещена: паспорт камеры не прочитан"
        if _is_ruver(self.ctx, passport) and not limit(self.ctx, "web.ruver_writes"):
            return "запись на «Рувер» только с отдельного разрешения: web.ruver_writes"
        if self.done >= limit(self.ctx, "web.writes_limit"):
            return "предел записей web.writes_limit"
        camera = self.ctx.camera
        if load(self.folder, camera.address, camera.port) is not None:
            return "запись запрещена: есть незавершённая запись этой камеры"
        return ""

    def _other_value(self, element: ElementInfo, current: str) -> str | None:
        """Другое допустимое значение: соседний вариант, обратное состояние, шаг в пределах."""
        if element.kind == "checkbox":
            return "false" if current == "true" else "true"
        if element.tag == "select":
            options = self.session.field_options(element)
            return next((value for value in options if value != current), None)
        low, high, step = self.session.field_limits(element)
        try:
            value = float(current)
        except ValueError:
            return None
        for candidate in (value + step, value - step):
            if low <= candidate <= high:
                return str(int(candidate) if candidate.is_integer() else candidate)
        return None

    def _apply(self, element: ElementInfo, value: str, elements: list[ElementInfo]) -> Reaction:
        """Записать значение и сохранить. Без кнопки считается, что поле сохраняется само."""
        wait_ms = int(limit(self.ctx, "web.click_response_s") * 1000)
        button = next(
            (
                e
                for e in elements
                if not e.disabled and any(word in e.label.lower() for word in SAVE_WORDS)
            ),
            None,
        )
        with self.session.write_window():
            self.session.set_field(element, value)
            if button is None:
                self.session.wait(wait_ms)
                return Reaction()
            return self.session.click(button, wait_ms=wait_ms)

    def _find(self, page: Page, mark: str) -> tuple[ElementInfo | None, list[ElementInfo]]:
        elements = self.reopen(page)
        self.session.wait(SETTLE_MS)
        return next((e for e in elements if fingerprint(e) == mark), None), elements

    def _reboot_asked(self, reaction: Reaction) -> bool:
        text = f"{reaction.dialog} {self.session.page_text()} {' '.join(reaction.requests)}"
        return any(phrase in text.lower() for phrase in REBOOT_PHRASES)

    def _api_value(self, reaction: Reaction) -> str | None:
        reader = API_READERS.get(self.ctx.camera.vendor)
        if reader is None:
            return None
        for request in reaction.requests:
            value = reader(self.ctx, request)
            if value is not None:
                return value
        return None

    def _entry(self, page: Page, element: ElementInfo, old: str, new: str) -> PendingWrite:
        camera = self.ctx.camera
        return PendingWrite(
            self.ctx.extra.get(RUN_ID_KEY, ""),
            camera.slot,
            camera.address,
            camera.port,
            camera.vendor,
            page.url,
            element.label or element.name,
            element.kind or element.tag,
            old,
            new,
            path=[page.tab] if page.tab else [],
            fingerprint=fingerprint(element),
        )

    def _restore(
        self, page: Page, entry: PendingWrite, element, elements, subject: str
    ) -> Row | None:
        """Вернуть значение и сверить; строка — только если вернуть не вышло (S14)."""
        entry.stage = "restoring"
        save(self.folder, entry)
        self._apply(element, entry.old, elements)
        field, _ = self._find(page, entry.fingerprint)
        back = self.session.read_field(field) if field is not None else "поле не найдено"
        if back == entry.old:
            clear(self.folder, entry.address, entry.port)
            self.restored += 1
            return None
        return self._not_returned(entry, back)

    def write(self, page: Page, item: Item, elements: list[ElementInfo]) -> Row:
        """Изменить поле, сверить и вернуть.

        Args:
            page: страница поля, открыта.
            item: поле; `refusal` уже вернул пусто.
            elements: элементы страницы.

        Returns:
            Строка записи.

        Raises:
            CameraSilent, Stopped, BrowserCrashed: журнал остаётся открытым.
            OSError: журнал не записан, поле не менялось.
        """
        element = item.element
        subject = element.label or element.name
        old = self.session.read_field(element)
        new = self._other_value(element, old)
        if new is None:
            return Row(subject, CheckResult.skipped("другого допустимого значения нет"))
        entry = self._entry(page, element, old, new)
        save(self.folder, entry)
        reaction = self._apply(element, new, elements)
        refused = [s for s in reaction.stopped if s.verdict is not Verdict.STOP_WRITE]
        if refused:
            clear(self.folder, entry.address, entry.port)
            addresses = ", ".join(f"{s.method} {s.url}" for s in refused)
            return Row(subject, CheckResult.stand_error(f"запрос не отправлен: {addresses}"))
        self.done += 1
        entry.stage = "sent"
        save(self.folder, entry)
        field, fresh = self._find(page, entry.fingerprint)
        seen = self.session.read_field(field) if field is not None else "поле не найдено"
        reboot = self._reboot_asked(reaction)
        api = self._api_value(reaction)
        declared = f"заявлено {new}, прочитано {seen}"
        if seen == old:
            clear(self.folder, entry.address, entry.port)
            self.restored += 1
            if reboot:
                return Row(subject, self._reboot_result())
            return Row(subject, self._not_saved(declared), Severity.MAJOR)
        entry.stage = "verified"
        save(self.folder, entry)
        blocker = self._restore(page, entry, field or element, fresh, subject)
        if blocker is not None:
            return blocker
        if reboot:
            return Row(subject, self._reboot_result())
        if seen != new:
            return Row(subject, self._not_saved(declared), Severity.MAJOR)
        if api is not None and api != new:
            return Row(subject, self._not_saved(f"заявлено {new}, по API {api}"), Severity.MAJOR)
        actual = f"записано {new}, прочитано {seen}, возвращено {old}"
        return Row(subject, CheckResult(Status.PASSED, actual=actual))

    def _not_returned(self, entry: PendingWrite, left: str) -> Row:
        """Блокер S14: журнал остаётся, запись на этой камере прекращена."""
        self.halted = f"запись прекращена: настройка «{entry.label}» не вернулась"
        entry.reported = True
        save(self.folder, entry)
        result = CheckResult(
            Status.FAILED,
            reason=f"настройка «{entry.label}» не вернулась",
            expected=f"значение возвращено: {entry.old}",
            actual=f"было {entry.old}, записывалось {entry.new}, осталось {left}",
        )
        return Row(entry.label, result, Severity.BLOCKER)

    def _wait_for_camera(self) -> bool:
        """Дождаться камеру, но не дольше `web.camera_return_s`."""
        camera = self.ctx.camera
        deadline = time.monotonic() + limit(self.ctx, "web.camera_return_s")
        while time.monotonic() < deadline:
            if tcp_open(camera.address, camera.port):
                return True
            time.sleep(RETURN_POLL_S)
        return tcp_open(camera.address, camera.port)

    def _field_after_login(
        self, page: Page, entry: PendingWrite, address: str, login_accepted: bool
    ) -> tuple[ElementInfo | None, list[ElementInfo], str]:
        """Найти поле; пропала сессия и первый вход был принят — один вход для возврата."""
        field, elements = self._find(page, entry.fingerprint)
        if field is not None or not self.session.password_fields():
            return field, elements, "поле не найдено"
        if not login_accepted:
            return None, elements, "сессия потеряна, а первого принятого входа не было"
        entered = login_for_restore(self.ctx, self.session, address)
        if entered.entry is not Entry.OK:
            return None, elements, f"вход для возврата не удался: {entered.entry.value}"
        field, elements = self._find(page, entry.fingerprint)
        return field, elements, "поле не найдено"

    def rescue(self, entry: PendingWrite, address: str, login_accepted: bool) -> Row | None:
        """Одна попытка вернуть значение из журнала (S15, S20, следующий обход).

        Значение возвращается, только если поле найдено по отпечатку и показывает записанное:
        за тем же адресом могла оказаться другая камера.

        Args:
            entry: открытый журнал.
            address: адрес веб-интерфейса.
            login_accepted: первый вход принят; без него второго входа нет.

        Returns:
            Строка блокера; None — значение на месте, журнал закрыт.
        """
        page = Page(entry.page, entry.label, tab=entry.path[0] if entry.path else "")
        try:
            if not self._wait_for_camera():
                return self._not_returned(entry, f"неизвестно: камера не вернулась, {entry.new}?")
            field, elements, problem = self._field_after_login(page, entry, address, login_accepted)
            if field is None:
                return self._not_returned(entry, f"неизвестно: {problem}")
            seen = self.session.read_field(field)
            if seen == entry.old:
                clear(self.folder, entry.address, entry.port)
                self.restored += 1
                return None
            if seen != entry.new:
                return self._not_returned(entry, f"{seen}: поле показывает чужое значение")
            return self._restore(page, entry, field, elements, entry.label)
        except (BrowserCrashed, CameraSilent, Stopped) as e:
            return self._not_returned(entry, f"неизвестно: {e}")

    @staticmethod
    def _reboot_result() -> CheckResult:
        return CheckResult.skipped(
            "настройка требует перезагрузки: без перезагрузки эффект не виден"
        )

    @staticmethod
    def _not_saved(actual: str) -> CheckResult:
        return CheckResult(
            Status.FAILED,
            reason="настройка не сохранилась",
            expected="страница показывает записанное значение",
            actual=actual,
        )
