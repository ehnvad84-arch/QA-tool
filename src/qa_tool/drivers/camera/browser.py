"""Единственный выход браузера веб-обхода к камере (AD-7).

Здесь и только здесь импортируется Playwright. Наружу отдаётся `BrowserSession`: объекты `page`,
`context` и `request` не выдаются, потому что `context.request` ходит мимо перехвата. Перехват стоит
на всём контексте, а не на странице: `page.route` не видит первый запрос всплывающего окна.

Класс не потокобезопасен: одна камера — один рабочий процесс и одна сессия. Синхронный Playwright
обслуживает перехват только внутри своих вызовов, поэтому паузы — через его же ожидание.
"""

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Route, WebSocket, WebSocketRoute, sync_playwright

from qa_tool.core.taboo import is_write_request

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


class BrowserMissing(Exception):
    """Браузер не запустился: на стенде это «ошибка стенда», а не вина камеры."""


class Verdict(StrEnum):
    """Почему запрос не отправлен."""

    STOP_FOREIGN = "адрес не из списка стенда"
    STOP_WRITE = "изменяющий запрос остановлен до камеры"


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
        self._watched_sockets: set[str] = set()
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
        """Пока правил нет, к камере не идёт ни одно сообщение страницы."""
        self.stopped.append(StoppedRequest("WS", url, Verdict.STOP_WRITE))

    @property
    def sockets_unwatched(self) -> bool:
        """Открыт ли сокет мимо перехвата: нажимать на такой странице нельзя."""
        return bool(self.unwatched_sockets)

    def _decide(self, route: Route, request: Request) -> None:
        """Пропустить запрос или остановить его до камеры."""
        verdict = None
        if not is_camera_url(request.url, self.host, self.ports):
            verdict = Verdict.STOP_FOREIGN
        elif is_write_request(request.method, request.url):
            verdict = Verdict.STOP_WRITE
        # страница могла закрыться, пока обработчик ждал очереди: запроса уже нет
        with contextlib.suppress(PlaywrightError):
            if verdict is None:
                route.continue_()
            else:
                self.stopped.append(StoppedRequest(request.method, request.url, verdict))
                route.abort("blockedbyclient")

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
