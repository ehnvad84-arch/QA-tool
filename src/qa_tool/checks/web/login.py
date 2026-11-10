"""Вход в веб-интерфейс: ровно одна попытка на камеру (AD-7).

Неверный пароль расходует счётчик попыток, поэтому любой неясный исход считается отказом и не
повторяется. Экран смены пароля и первая настройка не получают ни одного символа.
"""

import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from qa_tool.checks.rtsp_refusal import refused_password, remember_web_refusal
from qa_tool.checks.web.limits import limit
from qa_tool.core.checks import CheckContext
from qa_tool.core.taboo import is_logout_label
from qa_tool.drivers.camera.browser import PAGE_LIMIT_S, BrowserSession, ElementInfo
from qa_tool.drivers.camera.probe import tcp_open

POLL_MS = 200
HTTPS_PORT = 443
HTTP_PORT = 80
SESSIONS_FULL_CODE = 409
SESSIONS_FULL_HTTP_CODES = frozenset({409, 503})
HTTP_UNAUTHORIZED = 401
REFUSED_CODES = frozenset({HTTP_UNAUTHORIZED, 403, 423})
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
    STOPPED = "вход остановлен программой"


@dataclass(frozen=True)
class LoginResult:
    """Итог входа и причина для строки протокола."""

    entry: Entry
    reason: str = ""

    @property
    def walk_allowed(self) -> bool:
        """Можно ли начинать обход."""
        return self.entry in (Entry.OK, Entry.NOT_NEEDED)


def web_address(ctx: CheckContext) -> str | None:
    """Адрес интерфейса: первый открытый порт из порта камеры, 80 и 443; None — закрыты все."""
    for port in dict.fromkeys((ctx.camera.port, HTTP_PORT, HTTPS_PORT)):
        if tcp_open(ctx.camera.address, port):
            scheme = "https" if port == HTTPS_PORT else "http"
            return f"{scheme}://{ctx.camera.address}:{port}/"
    return None


def _wait_for_form(ctx: CheckContext, session: BrowserSession) -> list[ElementInfo]:
    """Элементы страницы, как только появилось поле пароля или кончилось время.

    Интерфейсы камер рисуют форму после загрузки скриптов, и не успевшая форма выглядела бы как
    камера без входа, поэтому ждём с запасом: лишнее ожидание стоит секунд один раз на камеру.
    """
    deadline = time.monotonic() + limit(ctx, "web.login_form_s")
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


def _http_login(ctx: CheckContext, session: BrowserSession, address: str) -> LoginResult:
    """Вход Basic или Digest: браузер отвечает камере сам, отказ обрывает все запросы."""
    session.use_http_login(ctx.camera.username, ctx.password)
    load = session.open(address, limit(ctx, "web.login_timeout_s"))
    if load.status in SESSIONS_FULL_HTTP_CODES:
        return LoginResult(Entry.SESSIONS_FULL, "камера не принимает новых подключений")
    if session.http_refused or load.status in REFUSED_CODES or load.status is None:
        return _refused(ctx, f"код ответа {load.status}")
    return LoginResult(Entry.OK)


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
    load = session.open(address, limit(ctx, "web.login_timeout_s"))
    if load.status == HTTP_UNAUTHORIZED:
        return _http_login(ctx, session, address)
    items = _wait_for_form(ctx, session)
    if not any(item.kind == "password" for item in items):
        return LoginResult(Entry.NOT_NEEDED)
    if _change_required(session, items):
        return LoginResult(Entry.CHANGE_REQUIRED, "камера просит сменить пароль или настроить её")
    button = _submit_button(items)
    if button is None:
        return LoginResult(Entry.REFUSED, "на странице входа нет кнопки входа: попытки не было")
    password_field = next(item for item in items if item.kind == "password")
    name_field = _name_field(items)
    stopped_before = len(session.stopped)
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
    stopped = session.stopped[stopped_before:]
    if stopped:
        # Табу остановило запрос страницы во время входа (Hikvision .112 — GET
        # /SDK/activateStatus, 06.11 и 10.11): пароль до камеры не дошёл, отказа нет.
        # Решение директора 06.11 — «ошибка стенда» с причиной; признак отказа не ставится,
        # иначе он выключал вход для «API камеры», «Картинки» и «Кодеков».
        first = stopped[0]
        path = urlsplit(first.url).path
        return LoginResult(Entry.STOPPED, f"табу остановило запрос входа {first.method} {path}")
    return _refused(ctx, "форма входа осталась на экране")


def login_for_restore(ctx: CheckContext, session: BrowserSession, address: str) -> LoginResult:
    """Войти второй раз только ради возврата значения; вызывать, если первый вход был принят.

    Raises:
        CameraSilent, Stopped, BrowserCrashed: как у `login`.
        RuntimeError: окно для возврата уже использовано.
    """
    session.allow_restore_login()
    return login(ctx, session, address)


def logout(session: BrowserSession, address: str, limit_s: float = PAGE_LIMIT_S) -> str:
    """Выйти одним нажатием, чтобы освободить подключение камеры.

    Кнопка выхода обычно есть только в шапке главной страницы, поэтому сначала открывается она.

    Args:
        session: открытая сессия браузера.
        address: адрес веб-интерфейса.
        limit_s: предел загрузки главной страницы, с.

    Returns:
        Пометка для сводки; пусто — выход выполнен.
    """
    session.open(address, limit_s)
    session.wait(POLL_MS * 3)
    for item in session.elements():
        if is_logout_label(item.label) and not item.disabled:
            with session.logout_window():
                session.click(item, wait_ms=POLL_MS * 3)
            return ""
    return "кнопки выхода нет: подключение освободится по тайм-ауту камеры"
