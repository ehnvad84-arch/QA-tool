"""Вход в веб-интерфейс: ровно одна попытка на камеру (AD-7).

Неверный пароль расходует счётчик попыток, поэтому любой неясный исход считается отказом и не
повторяется. Экран смены пароля и первая настройка не получают ни одного символа.
"""

import time
from dataclasses import dataclass
from enum import Enum

from qa_tool.checks.rtsp_refusal import refused_password, remember_web_refusal
from qa_tool.checks.web.limits import limit
from qa_tool.core.checks import CheckContext
from qa_tool.drivers.camera.browser import BrowserSession, ElementInfo

FORM_APPEARS_S = 3.0  # интерфейс на Vue рисует форму после загрузки страницы
POLL_MS = 200
SESSIONS_FULL_CODE = 409
REFUSED_CODES = frozenset({401, 403, 423})
REFUSED_WORDS = ("неверн", "заблокир", "wrong", "invalid", "locked", "incorrect", "failed")
SESSIONS_WORDS = ("слишком много", "too many", "maxconnectionsallowed", "нет свободн")
CHANGE_WORDS = ("смена пароля", "новый пароль", "подтвердите пароль", "activate", "активац")
CHANGE_WORDS += ("new password", "confirm password", "change password", "первая настройка")
SUBMIT_WORDS = ("войти", "вход", "login", "log in", "sign in", "signin", "ok", "ок")


class Entry(Enum):
    """Итог входа."""

    OK = "вход выполнен"
    NOT_NEEDED = "вход не нужен"
    REFUSED = "пароль не принят"
    CHANGE_REQUIRED = "камера просит сменить пароль"
    SESSIONS_FULL = "заняты все подключения"


@dataclass(frozen=True)
class LoginResult:
    """Итог входа и причина для строки протокола."""

    entry: Entry
    reason: str = ""

    @property
    def walk_allowed(self) -> bool:
        """Можно ли начинать обход."""
        return self.entry in (Entry.OK, Entry.NOT_NEEDED)


def _wait_for_form(session: BrowserSession) -> list[ElementInfo]:
    """Элементы страницы, как только появилось поле пароля или кончилось время."""
    deadline = time.monotonic() + FORM_APPEARS_S
    while True:
        items = session.elements()
        if any(item.kind == "password" for item in items) or time.monotonic() > deadline:
            return items
        session.wait(POLL_MS)


def _change_required(session: BrowserSession, items: list[ElementInfo]) -> bool:
    """Показан ли экран смены пароля, активации или первой настройки."""
    text = session.page_text().lower()
    return sum(item.kind == "password" for item in items) > 1 or any(
        word in text for word in CHANGE_WORDS
    )


def _submit_button(items: list[ElementInfo]) -> ElementInfo | None:
    """Кнопка входа: по типу, затем по подписи."""
    usable = [item for item in items if not item.disabled]
    for item in usable:
        if item.kind == "submit":
            return item
    for item in usable:
        label = item.label.lower()
        is_button = item.tag == "button" or item.role == "button"
        if is_button and any(label.startswith(word) for word in SUBMIT_WORDS):
            return item
    return None


def _name_field(items: list[ElementInfo]) -> ElementInfo | None:
    """Поле логина: текстовое поле рядом с полем пароля."""
    for item in items:
        if item.tag == "input" and item.kind in ("text", "email", "") and not item.disabled:
            return item
    return None


def _refused(ctx: CheckContext, detail: str) -> LoginResult:
    """Отказ запоминается для всех проверок камеры: второй попытки не будет."""
    return LoginResult(Entry.REFUSED, remember_web_refusal(ctx, detail))


def _outcome(ctx: CheckContext, session: BrowserSession) -> LoginResult | None:
    """Итог нажатия кнопки входа; None — исход ещё неясен."""
    items = session.elements()
    passwords = sum(item.kind == "password" for item in items)
    text = session.page_text().lower()
    if SESSIONS_FULL_CODE in session.answers or any(word in text for word in SESSIONS_WORDS):
        return LoginResult(Entry.SESSIONS_FULL, "камера не принимает новых подключений")
    if passwords > 1 or any(word in text for word in CHANGE_WORDS):
        return LoginResult(Entry.CHANGE_REQUIRED, "после входа камера просит сменить пароль")
    if REFUSED_CODES & set(session.answers) or any(word in text for word in REFUSED_WORDS):
        return _refused(ctx, "камера ответила отказом")
    return LoginResult(Entry.OK) if items and not passwords else None


def login(ctx: CheckContext, session: BrowserSession, address: str) -> LoginResult:
    """Войти в интерфейс камеры одной попыткой.

    Args:
        ctx: контекст проверки; логин, пароль и память отказа.
        session: открытая сессия браузера.
        address: адрес веб-интерфейса.

    Returns:
        Итог входа; при отказе он уже записан в память камеры.

    Raises:
        CameraSilent: страница входа не загрузилась.
        Stopped: нажат «Стоп».
        BrowserCrashed: браузер упал.
    """
    if refused_password(ctx):
        return LoginResult(Entry.REFUSED, refused_password(ctx))
    session.open(address, limit(ctx, "web.login_timeout_s"))
    items = _wait_for_form(session)
    if not any(item.kind == "password" for item in items):
        return LoginResult(Entry.NOT_NEEDED)
    if _change_required(session, items):
        return LoginResult(Entry.CHANGE_REQUIRED, "камера просит сменить пароль или настроить её")
    button = _submit_button(items)
    if button is None:
        return LoginResult(Entry.REFUSED, "на странице входа нет кнопки входа: попытки не было")
    password_field = next(item for item in items if item.kind == "password")
    name_field = _name_field(items)
    with session.login_window(ctx.camera.username, ctx.password):
        if name_field is not None:
            session.set_field(name_field, ctx.camera.username)
        session.set_field(password_field, ctx.password)
        session.click(button, wait_ms=POLL_MS)
        deadline = time.monotonic() + limit(ctx, "web.login_timeout_s")
        while time.monotonic() < deadline:
            outcome = _outcome(ctx, session)
            if outcome is not None:
                return outcome
            session.wait(POLL_MS)
    return _refused(ctx, "форма входа осталась на экране")
