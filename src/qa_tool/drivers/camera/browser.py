"""Единственный выход браузера веб-обхода к камере (AD-7).

Здесь и только здесь импортируется Playwright. Наружу отдаётся `BrowserSession`: объекты `page`,
`context` и `request` не выдаются, потому что `context.request` ходит мимо перехвата. Перехват стоит
на всём контексте, а не на странице: `page.route` не видит первый запрос всплывающего окна.

Класс не потокобезопасен: одна камера — один рабочий процесс и одна сессия. Синхронный Playwright
обслуживает перехват только внутри своих вызовов, поэтому паузы — через его же ожидание.
"""

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, StrEnum
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Route, WebSocket, WebSocketRoute, sync_playwright

from qa_tool.core.taboo import (
    TabooViolation,
    WebDangerViolation,
    check_http,
    check_login_window,
    check_logout_window,
    check_onvif,
    check_web_danger,
    check_web_request,
    is_static_asset,
    is_web_action,
    is_write_message,
    is_write_request,
    web_forbidden_word,
)
from qa_tool.drivers.camera.http import is_soap

# без сетевого обмена: содержимое лежит в самой странице
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
# Закрытый порт вместо посредника: перенаправление на чужой хост перехват не видит, а так запрос
# никуда не уйдёт. Адрес камеры идёт мимо него. `<-loopback>` снимает встроенное исключение Chromium
# для localhost: иначе перенаправление на сам компьютер стенда прошло бы.
_DEAD_PROXY = "http://127.0.0.1:9"
# Запрос без кадра Playwright отпускает мимо перехвата: это keepalive и sendBeacon при уходе со
# страницы и SharedWorker. WebRTC и WebTransport перехват не видит совсем. Скрипт — не граница
# безопасности: он закрывает случайные обходы обычного интерфейса, а не намеренные.
_GUARD_SCRIPT = """(() => {
  for (const name of ['SharedWorker', 'RTCPeerConnection', 'webkitRTCPeerConnection',
                      'RTCDataChannel', 'WebTransport']) {
    try { Object.defineProperty(window, name, {value: undefined, configurable: false}); }
    catch (e) {}
  }
  const plainFetch = window.fetch;
  window.fetch = function (input, init) {
    if (init && init.keepalive) init = Object.assign({}, init, {keepalive: false});
    if (input instanceof Request && input.keepalive) {
      input = new Request(input, {keepalive: false});
    }
    return plainFetch.call(this, input, init);
  };
  navigator.sendBeacon = function (url, data) {
    window.fetch(url, {method: 'POST', body: data}).catch(() => {});
    return true;
  };
})();"""


LOGIN_WINDOW_WRITES = 4  # у «Рувер» вход занимает три запроса, один про запас
LOGOUT_WINDOW_WRITES = 2
WRITE_WINDOW_WRITES = 5
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_KEEPS_METHOD_CODES = frozenset({307, 308})


class BrowserMissing(Exception):
    """Браузер не запустился: на стенде это «ошибка стенда», а не вина камеры."""


class Verdict(StrEnum):
    """Почему запрос не отправлен."""

    STOP_FOREIGN = "адрес не из списка стенда"
    STOP_WRITE = "изменяющий запрос остановлен до камеры"
    STOP_TABOO = "запрет на сеть, учётные записи и сброс"
    STOP_DANGER = "опасное действие"


class Window(Enum):
    """Какое окно открыто: что разрешено сверх чтения."""

    NONE = 0
    LOGIN = 1
    LOGOUT = 2
    WRITE = 3


@dataclass(frozen=True)
class StoppedRequest:
    """Запрос, который программа не пустила к камере."""

    method: str
    url: str
    verdict: Verdict


def is_camera_url(url: str, host: str, ports: frozenset[int]) -> bool:
    """Идёт ли адрес на эту камеру: совпали хост и один из её веб-портов.

    Args:
        url: полный адрес запроса.
        host: адрес камеры из списка стенда.
        ports: разрешённые порты камеры.
    """
    parts = urlsplit(url)
    if parts.scheme in _LOCAL_SCHEMES:
        return True
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    return parts.hostname == host and port in ports


class BrowserSession:
    """Браузер одной камеры: только чтение, каждый изменяющий запрос останавливается.

    Использовать как контекстный менеджер: при выходе браузер закрыт при любом исходе.
    """

    def __init__(self, base_url: str, ports: frozenset[int] = frozenset()) -> None:
        """Подготовить сессию; браузер запускается в `__enter__`.

        Args:
            base_url: адрес веб-интерфейса камеры, например `http://192.168.1.10:80`.
            ports: дополнительные порты камеры; порт `base_url`, 80 и 443 разрешены всегда.
        """
        parts = urlsplit(base_url)
        self.base_url = base_url
        self.host = parts.hostname or ""
        own_port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 80)
        self.ports = frozenset({own_port, 80, 443}) | ports
        self.stopped: list[StoppedRequest] = []
        self.unwatched_sockets: list[str] = []
        self.late_redirects: list[StoppedRequest] = []
        self.page_label = ""
        self._watched_sockets: set[str] = set()
        self._window = Window.NONE
        self._window_writes = 0
        self._login_used = False
        self._credentials = ("", "")
        self._pressing = False
        self._quiet_reads: set[str] = set()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "BrowserSession":
        """Запустить браузер и поставить перехват до первой страницы.

        Raises:
            BrowserMissing: браузер не установлен или не запускается.
        """
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                headless=True, proxy={"server": _DEAD_PROXY, "bypass": f"<-loopback>,{self.host}"}
            )
        except PlaywrightError as e:
            self.close()
            raise BrowserMissing(f"браузер не запустился: {e}") from e
        self._context = self._browser.new_context(
            service_workers="block",  # запросы Service Worker перехват не видит
            ignore_https_errors=True,
            accept_downloads=False,
        )
        self._context.add_init_script(_GUARD_SCRIPT)
        self._context.route("**/*", self._decide)
        self._context.route_web_socket("**/*", self._watch_socket)
        self._context.on("page", self._watch_page)
        self._context.on("request", self._watch_redirect)
        self._page = self._context.new_page()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Закрыть браузер."""
        self.close()

    def _watch_page(self, page: Page) -> None:
        """Следить за сокетами страницы, в том числе всплывающего окна."""
        page.on("websocket", self._note_socket)

    def _note_socket(self, socket: WebSocket) -> None:
        """Запомнить сокет, который обошёл перехват: так открываются сокеты из воркера."""
        if socket.url not in self._watched_sockets:
            self.unwatched_sockets.append(socket.url)

    def _watch_socket(self, socket: WebSocketRoute) -> None:
        """Сокет страницы: сообщения к камере проходят через проверку, от камеры — свободно."""
        self._watched_sockets.add(socket.url)
        if not is_camera_url(socket.url, self.host, self.ports):
            self.stopped.append(StoppedRequest("WS", socket.url, Verdict.STOP_FOREIGN))
            socket.close()
            return
        server = socket.connect_to_server()
        socket.on_message(lambda message: self._forward_message(server, socket.url, message))

    def _forward_message(self, server: WebSocketRoute, url: str, message: str | bytes) -> None:
        """Отправить сообщение страницы камере, если оно проходит те же запреты, что и запрос."""
        verdict = self._judge_message(url, message)
        if verdict is None:
            server.send(message)
        else:
            self.stopped.append(StoppedRequest("WS", url, verdict))

    def _judge_message(self, url: str, message: str | bytes) -> Verdict | None:
        """Решение по сообщению WebSocket; двоичное проверить нельзя, поэтому оно не уходит."""
        if isinstance(message, bytes):
            return Verdict.STOP_WRITE
        try:
            check_web_danger("POST", url, message)
        except WebDangerViolation:
            return Verdict.STOP_DANGER
        if web_forbidden_word(message):
            return Verdict.STOP_TABOO
        if self._window is Window.WRITE:
            return None
        return Verdict.STOP_WRITE if is_write_message(message) else None

    @property
    def presses_stopped(self) -> bool:
        """Нажимать на этой странице нельзя: что-то прошло мимо перехвата."""
        return bool(self.unwatched_sockets or self.late_redirects)

    def _watch_redirect(self, request: Request) -> None:
        """Чтение перенаправляет сам браузер, поэтому запрет виден только после факта."""
        if request.redirected_from is None:
            return
        verdict = self._judge(request.method, request.url, "")
        if verdict is not None:
            self.late_redirects.append(StoppedRequest(request.method, request.url, verdict))

    def _judge(self, method: str, url: str, body: str | None) -> Verdict | None:
        """Решение по запросу: первое сработавшее правило (спека, раздел «Решение по запросу»).

        Args:
            method: метод запроса.
            url: адрес запроса.
            body: тело; None — браузер не смог его прочитать.

        Returns:
            Причина остановки; None — запрос можно отправить.
        """
        if not is_camera_url(url, self.host, self.ports):
            return Verdict.STOP_FOREIGN
        if is_static_asset(method, url):
            return None
        write = is_write_request(method, url)
        if write and body is None:
            return Verdict.STOP_WRITE
        text = body or ""
        try:
            check_http(method, url)
            check_onvif(text)
        except TabooViolation:
            return Verdict.STOP_TABOO
        try:
            check_web_danger(method, url, text)
        except WebDangerViolation:
            return Verdict.STOP_DANGER
        if self._window in (Window.LOGIN, Window.LOGOUT):
            return self._judge_session_window(method, url, body, write)
        try:
            if not is_soap({}, text):
                check_web_request(method, url, text, self.page_label)
        except TabooViolation:
            return Verdict.STOP_TABOO
        if self._window is Window.WRITE and self._take_window_write(write):
            return None
        return self._judge_plain(method, url, write)

    def _judge_session_window(
        self, method: str, url: str, body: str | None, write: bool
    ) -> Verdict | None:
        """Правило окна входа и выхода: слова входа сняты, признаки смены пароля действуют."""
        try:
            if self._window is Window.LOGIN:
                check_login_window(method, url, body, *self._credentials)
            else:
                check_logout_window(method, url, body)
        except TabooViolation:
            return Verdict.STOP_TABOO
        return None if self._take_window_write(write) else Verdict.STOP_TABOO

    def _take_window_write(self, write: bool) -> bool:
        """Потратить разрешённый запрос окна; чтение ничего не тратит."""
        if not write:
            return True
        if self._window_writes <= 0:
            return False
        self._window_writes -= 1
        return True

    def _judge_plain(self, method: str, url: str, write: bool) -> Verdict | None:
        """Вне окон запись останавливается; при нажатии подозрителен и незнакомый GET."""
        if write:
            return Verdict.STOP_WRITE
        if self._pressing and (is_web_action(method, url) or url not in self._quiet_reads):
            return Verdict.STOP_WRITE
        if not self._pressing:
            self._quiet_reads.add(url)
        return None

    @staticmethod
    def _body(request: Request) -> str | None:
        """Тело запроса текстом; None — тело есть, но прочитать его нельзя."""
        data = request.post_data_buffer
        if data is None:
            return None if int(request.headers.get("content-length") or 0) else ""
        try:
            return data.decode()
        except UnicodeDecodeError:
            return None

    def _decide(self, route: Route, request: Request) -> None:
        """Пропустить запрос или остановить его до камеры."""
        body = self._body(request)
        verdict = self._judge(request.method, request.url, body)
        # страница могла закрыться, пока обработчик ждал очереди: запроса уже нет
        with contextlib.suppress(PlaywrightError):
            if verdict is not None:
                self._stop(route, request.method, request.url, verdict)
            elif is_write_request(request.method, request.url):
                self._send_write(route, request, body)
            else:
                route.continue_()

    def _stop(self, route: Route, method: str, url: str, verdict: Verdict) -> None:
        """Записать остановленный запрос и оборвать его."""
        self.stopped.append(StoppedRequest(method, url, verdict))
        route.abort("blockedbyclient")

    def _send_write(self, route: Route, request: Request, body: str | None) -> None:
        """Отправить разрешённую запись, проверяя каждое перенаправление.

        `route.continue_` здесь нельзя: браузер сам пошёл бы по 307 с тем же телом, а перехват
        этого не видит. Поэтому запись идёт через `route.fetch` без перенаправлений.
        """
        method, url = request.method, request.url
        for _ in range(MAX_REDIRECTS):
            answer = route.fetch(url=url, method=method, max_redirects=0)
            if answer.status not in _REDIRECT_CODES:
                self._finish_write_window()
                route.fulfill(response=answer)
                return
            target = urljoin(url, answer.headers.get("location", ""))
            if answer.status not in _KEEPS_METHOD_CODES:
                method, body = "GET", ""
            verdict = self._judge(method, target, body)
            if verdict is None and request.is_navigation_request():
                if answer.status in _KEEPS_METHOD_CODES:
                    verdict = Verdict.STOP_WRITE
                else:
                    # переход сделает новый запрос: его перехват уже увидит
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=f'<meta http-equiv="refresh" content="0;url={target}">',
                    )
                    return
            if verdict is not None:
                self._stop(route, method, target, verdict)
                return
            url = target
        self._stop(route, method, url, Verdict.STOP_WRITE)

    def _finish_write_window(self) -> None:
        """Окно записи закрывается по первому ответу камеры: одно нажатие — одно сохранение."""
        if self._window is Window.WRITE:
            self._window = Window.NONE

    @contextlib.contextmanager
    def _opened(self, window: Window, writes: int) -> Iterator[None]:
        """Открыть окно на время блока; окна не вкладываются."""
        if self._window is not Window.NONE:
            raise RuntimeError("окно уже открыто")
        self._window, self._window_writes = window, writes
        try:
            yield
        finally:
            self._window, self._window_writes = Window.NONE, 0

    def login_window(self, username: str, password: str):
        """Окно входа: один раз за сессию, только перед своим нажатием кнопки входа (AD-7).

        Raises:
            RuntimeError: окно уже открывалось.
        """
        if self._login_used:
            raise RuntimeError("окно входа открывается один раз")
        self._login_used = True
        self._credentials = (username, password)
        return self._opened(Window.LOGIN, LOGIN_WINDOW_WRITES)

    def logout_window(self):
        """Окно выхода: одно нажатие в конце обхода (AD-7)."""
        return self._opened(Window.LOGOUT, LOGOUT_WINDOW_WRITES)

    def write_window(self):
        """Окно записи: только при галочке «изменяющие», с журналом и возвратом (AD-7)."""
        return self._opened(Window.WRITE, WRITE_WINDOW_WRITES)

    @contextlib.contextmanager
    def pressing(self) -> Iterator[None]:
        """На время нажатия незнакомый GET считается действием, а не чтением."""
        self._pressing = True
        try:
            yield
        finally:
            self._pressing = False

    def open(self, url: str) -> int | None:
        """Открыть страницу и дождаться загрузки.

        Args:
            url: адрес страницы на камере.

        Returns:
            Код ответа; None — ответа нет, например запрос остановлен.
        """
        try:
            response = self._page.goto(url, wait_until="load")
        except PlaywrightError:
            return None
        return response.status if response else None

    def wait(self, ms: int) -> None:
        """Подождать, не останавливая перехват: `time.sleep` повесил бы обработчики."""
        self._page.wait_for_timeout(ms)

    def close(self) -> None:
        """Закрыть браузер и драйвер; повторный вызов безопасен."""
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):
                    closer.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = self._browser = self._page = self._playwright = None
