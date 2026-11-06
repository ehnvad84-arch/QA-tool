"""Единственный выход браузера веб-обхода к камере (AD-7).

Здесь и только здесь импортируется Playwright. Наружу отдаётся `BrowserSession`: объекты `page`,
`context` и `request` не выдаются, потому что `context.request` ходит мимо перехвата. Перехват стоит
на всём контексте, а не на странице: `page.route` не видит первый запрос всплывающего окна.

Класс не потокобезопасен: одна камера — один рабочий процесс и одна сессия. Синхронный Playwright
обслуживает перехват только внутри своих вызовов, поэтому паузы — через его же ожидание.
"""

import contextlib
import math
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import (
    Dialog,
    Page,
    Request,
    Response,
    Route,
    WebSocket,
    WebSocketRoute,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

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
    web_danger_word,
    web_forbidden_word,
)
from qa_tool.core.tempfiles import TEMP_PREFIX
from qa_tool.drivers.camera.digest import digest_header, parse_challenge, uses_qop
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


STEP_MS = 200  # шаг ожидания: «Стоп» должен срабатывать между шагами
CALL_LIMIT_MS = 1000  # предел одного вызова Playwright, иначе «Стоп» не уложится в 5 с
PAGE_LIMIT_S = 30.0
TEXT_LIMIT = 200  # подписи и ошибки приходят от камеры: длинный текст ломает протокол
HTTP_ERROR = 400
FAVICON = "/favicon.ico"  # значок браузер просит сам, у камеры его может не быть, это не ошибка
BROWSER_LOAD_MESSAGES = ("Failed to load resource", "ERR_BLOCKED_BY_CLIENT")
# Подпись, тип и контекст элемента собираются в странице. Путь без индексов не годится: он нужен
# только для нажатия, поэтому индексы здесь есть, а сравнение «похожих» идёт по другим полям.
_SCAN_SCRIPT = """() => {
  const picks = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=treeitem],[role=switch],[role=checkbox],[role=radio],[role=slider],' +
    '[role=combobox]';
  const text = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const path = (node) => {
    const parts = [];
    for (let item = node; item && item.nodeType === 1 && item !== document.documentElement;
         item = item.parentElement) {
      let index = 1;
      for (let other = item.previousElementSibling; other; other = other.previousElementSibling) {
        if (other.tagName === item.tagName) index += 1;
      }
      parts.unshift(`${item.tagName.toLowerCase()}:nth-of-type(${index})`);
    }
    return parts.join(' > ');
  };
  const label = (node) => {
    const own = node.labels && node.labels[0] ? node.labels[0].innerText : '';
    return text(node.getAttribute('aria-label') || own || node.innerText || node.placeholder ||
                node.title || (node.type === 'submit' ? node.value : ''));
  };
  const area = (node) => {
    const legend = node.closest('fieldset')?.querySelector('legend')?.innerText || '';
    const tab = document.querySelector('[role=tab][aria-selected=true]')?.innerText || '';
    let heading = '';
    for (let item = node; item && !heading; item = item.parentElement) {
      heading = item.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4')
        ?.innerText || '';
    }
    return text([legend, heading, tab].join(' '));
  };
  const blocked = (node) => {
    if (node.disabled || node.matches('[aria-disabled=true],[inert],fieldset[disabled] *')) {
      return true;
    }
    const box = node.getBoundingClientRect();
    const top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return Boolean(top) && top !== node && !node.contains(top) && !top.contains(node);
  };
  return [...document.querySelectorAll(picks)].filter((node) => {
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(node).visibility !== 'hidden';
  }).map((node) => ({
    path: path(node), tag: node.tagName.toLowerCase(), kind: node.type || '',
    role: node.getAttribute('role') || '', label: label(node), name: node.name || '',
    id: node.id || '', href: node.getAttribute('href') || '', area: area(node),
    disabled: blocked(node),
  }));
}"""


class BrowserMissing(Exception):
    """Браузер не запустился: на стенде это «ошибка стенда», а не вина камеры."""


class BrowserCrashed(Exception):
    """Браузер упал: «ошибка стенда» только у этой камеры."""


class CameraSilent(Exception):
    """Камера не ответила в отведённый срок."""


class Stopped(Exception):
    """Нажат «Стоп» или истекло время проверки."""


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
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ElementInfo:
    """Элемент страницы; `frame` и `path` нужны только для нажатия."""

    frame: int
    path: str
    tag: str
    kind: str
    role: str
    label: str
    name: str
    id: str
    href: str
    area: str
    disabled: bool


@dataclass(frozen=True)
class PageLoad:
    """Итог открытия страницы."""

    status: int | None
    elapsed_s: float
    url: str


@dataclass
class Reaction:
    """Что случилось после нажатия."""

    requests: list[str] = field(default_factory=list)
    stopped: list[StoppedRequest] = field(default_factory=list)
    navigated: bool = False
    dialog: str = ""
    elapsed_s: float = 0.0


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

    def __init__(
        self,
        base_url: str,
        ports: frozenset[int] = frozenset(),
        stop_requested: Callable[[], bool] = lambda: False,
        temp_tag: str = "0",
    ) -> None:
        """Подготовить сессию; браузер запускается в `__enter__`.

        Args:
            base_url: адрес веб-интерфейса камеры, например `http://192.168.1.10:80`.
            ports: дополнительные порты камеры; порт `base_url`, 80 и 443 разрешены всегда.
            stop_requested: опрашивается между шагами ожидания.
            temp_tag: метка папки временных файлов, обычно место камеры.
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
        self.page_errors: list[str] = []
        # парами «имя=значение»: короткое значение вроде «ok» вырезало бы обычные слова
        self.cookies_seen: set[str] = set()
        self.bad_answers: list[str] = []
        self._error_times: list[float] = []
        self.dialogs: list[str] = []
        self.popups: list[str] = []
        self.answers: list[int] = []
        self.http_refused = False
        self._http_login: tuple[str, str] | None = None
        self._digest_count = 0
        self.sandboxed = False
        self.screens_allowed = True
        self._stop_requested = stop_requested
        self._temp_tag = temp_tag
        self._temp_dir = ""
        self._crashed = False
        self._seen_requests: list[str] = []
        self._document_status: int | None = None
        self._watched_sockets: set[str] = set()
        self._window = Window.NONE
        self._window_writes = 0
        self._login_used = False
        self._restore_login_used = False
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
        self._temp_dir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{self._temp_tag}-")
        self._start_driver()
        try:
            self._browser = self._launch()
        except PlaywrightError as e:
            self.close()
            raise BrowserMissing(f"браузер не запустился: {e}") from e
        self._context = self._new_context()
        self._prepare_context()
        self._page = self._context.new_page()
        self._prepare_main_page()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Закрыть браузер."""
        self.close()

    def _start_driver(self) -> None:
        """Запустить драйвер с временной папкой этой сессии.

        Профиль браузера драйвер кладёт в системную временную папку. Своя папка нужна, чтобы её
        можно было удалить целиком и найти остатки после снятого процесса.
        """
        names = ("TMPDIR", "TEMP", "TMP")
        saved = {name: os.environ.get(name) for name in names}
        os.environ.update(dict.fromkeys(names, self._temp_dir))
        try:
            self._playwright = sync_playwright().start()
        finally:
            for name, value in saved.items():
                if value is None:
                    del os.environ[name]
                else:
                    os.environ[name] = value

    def _launch(self):
        """Запустить браузер, по возможности в песочнице: он исполняет код камеры.

        Ubuntu 24.04 может запретить песочнице пространства имён, поэтому при отказе браузер
        запускается без неё, а `sandboxed` остаётся False для протокола.
        """
        options = {
            "headless": True,
            "proxy": {"server": _DEAD_PROXY, "bypass": f"<-loopback>,{self.host}"},
        }
        try:
            browser = self._playwright.chromium.launch(chromium_sandbox=True, **options)
        except PlaywrightError:
            return self._playwright.chromium.launch(**options)
        self.sandboxed = True
        return browser

    def _new_context(self, credentials: dict | None = None):
        """Новый контекст: Service Worker закрыт, потому что его запросы перехват не видит."""
        return self._browser.new_context(
            service_workers="block",
            ignore_https_errors=True,
            accept_downloads=False,
            http_credentials=credentials,
        )

    def use_http_login(self, username: str, password: str) -> None:
        """Перейти на вход Basic или Digest: новый контекст с логином только для этой камеры.

        Вызывается один раз и только после ответа 401. Браузер отвечает на запрос пароля сам и на
        каждый запрос страницы, поэтому первый отказ обрывает все следующие запросы: иначе каждая
        картинка потратила бы попытку на счётчике камеры.
        """
        if self._http_login is not None:
            raise RuntimeError("вход по HTTP выполняется один раз")
        self._http_login = (username, password)
        self._context.close()
        parts = urlsplit(self.base_url)
        self._context = self._new_context(
            {
                "username": username,
                "password": password,
                "origin": f"{parts.scheme}://{parts.netloc}",
            }
        )
        self._prepare_context()
        self._page = None  # иначе новая страница сочтётся всплывающим окном и закроется
        self._page = self._context.new_page()
        self._prepare_main_page()

    def _prepare_context(self) -> None:
        """Поставить перехват до первой страницы."""
        self._context.add_init_script(_GUARD_SCRIPT)
        self._context.route("**/*", self._decide)
        self._context.route_web_socket("**/*", self._watch_socket)
        self._context.on("page", self._watch_page)
        self._context.on("request", self._watch_redirect)

    def _prepare_main_page(self) -> None:
        """Слушатели главной страницы."""
        page = self._page
        page.on("dialog", self._answer_dialog)
        page.on("crash", self._note_crash)
        page.on("pageerror", lambda error: self._note_error(str(error)))
        page.on("console", self._note_console)
        page.on("response", self._note_response)
        page.on("request", lambda request: self._seen_requests.append(request.url))

    def _note_crash(self, _page: Page) -> None:
        self._crashed = True

    def _note_error(self, text: str) -> None:
        self.page_errors.append(text[:TEXT_LIMIT])
        self._error_times.append(time.monotonic())

    def _note_console(self, message) -> None:
        if message.type == "error":
            self._note_error(message.text)

    def _note_response(self, response: Response) -> None:
        """Код ответа самой страницы и ошибки камеры на её ресурсы."""
        request = response.request
        if self._http_login is not None and response.status == 401:
            self.http_refused = True
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self._document_status = response.status
        elif response.status >= HTTP_ERROR and not request.url.endswith(FAVICON):
            self.bad_answers.append(f"{response.status} {request.method} {request.url}")

    def _answer_dialog(self, dialog: Dialog) -> None:
        """Ответить на диалог: без ответа нажатие зависнет.

        `confirm` принимается только в окне записи и без запретных и опасных слов.
        """
        text = dialog.message[:TEXT_LIMIT]
        self.dialogs.append(text)
        accept = dialog.type in ("alert", "beforeunload") or (
            dialog.type == "confirm"
            and self._window is Window.WRITE
            and not web_danger_word(text)
            and not web_forbidden_word(text)
        )
        with contextlib.suppress(PlaywrightError):
            if accept:
                dialog.accept()
            else:
                dialog.dismiss()

    def _watch_page(self, page: Page) -> None:
        """Следить за сокетами страницы; всплывающее окно записать и закрыть."""
        page.on("websocket", self._note_socket)
        if self._page is not None and page is not self._page:
            self.popups.append(page.url[:TEXT_LIMIT])
            with contextlib.suppress(PlaywrightError):
                page.close()

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
        in_session_window = self._window in (Window.LOGIN, Window.LOGOUT)
        try:
            check_web_danger(method, url, text, self._credentials if in_session_window else None)
        except WebDangerViolation:
            return Verdict.STOP_DANGER
        if in_session_window:
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
        if self.http_refused:
            with contextlib.suppress(PlaywrightError):
                route.abort("blockedbyclient")
            return
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
            answer = self._answer_digest(route, request, answer, url, method)
            self.answers.append(answer.status)
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

    def _answer_digest(self, route: Route, request: Request, answer, url: str, method: str):
        """Повторить запись с Digest: `route.fetch` умеет только Basic.

        Первый 401 здесь не отказ, а вопрос камеры: браузер за нас на него не ответит.
        """
        offer = answer.headers.get("www-authenticate", "")
        digest_asked = answer.status == 401 and offer.lower().startswith("digest")
        if not digest_asked or self._http_login is None:
            return answer
        challenge = parse_challenge(offer)
        if uses_qop(challenge):
            self._digest_count += 1
        parts = urlsplit(url)
        uri = parts.path + (f"?{parts.query}" if parts.query else "")
        header = digest_header(*self._http_login, challenge, method, uri, self._digest_count)
        headers = {**request.headers, "authorization": header}
        return route.fetch(url=url, method=method, headers=headers, max_redirects=0)

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

    def allow_restore_login(self) -> None:
        """Разрешить одно окно входа ради возврата значения (решение директора 05.11).

        Raises:
            RuntimeError: такое окно уже было.
        """
        if self._restore_login_used:
            raise RuntimeError("окно входа для возврата открывается один раз")
        self._restore_login_used = True
        self._login_used = False

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

    def _check(self) -> None:
        """Прервать работу, если браузер упал или нажат «Стоп».

        Raises:
            BrowserCrashed: браузер упал.
            Stopped: нажат «Стоп».
        """
        if self._crashed:
            raise BrowserCrashed("браузер упал")
        if self._stop_requested():
            raise Stopped("нажат «Стоп»")

    def open(self, url: str, limit_s: float = PAGE_LIMIT_S) -> PageLoad:
        """Открыть страницу и дождаться загрузки короткими шагами.

        Args:
            url: адрес страницы на камере.
            limit_s: предел ожидания, с.

        Returns:
            Код ответа (None — ответа нет или запрос остановлен), время и итоговый адрес.

        Raises:
            CameraSilent: страница не загрузилась за `limit_s`.
            Stopped: нажат «Стоп».
            BrowserCrashed: браузер упал.
        """
        self._check()
        self._document_status = None
        started = time.monotonic()
        # Истечение этого срока навигацию не отменяет: она идёт дальше, а ждём мы шагами ниже.
        try:
            self._page.goto(url, wait_until="commit", timeout=CALL_LIMIT_MS)
        except PlaywrightTimeout:
            pass
        except PlaywrightError:
            return PageLoad(None, time.monotonic() - started, url)
        while True:
            self._check()
            try:
                self._page.wait_for_load_state("load", timeout=STEP_MS)
                break
            except PlaywrightTimeout:
                if time.monotonic() - started > limit_s:
                    raise CameraSilent(f"страница не загрузилась за {limit_s:.0f} с") from None
        self._remember_cookies()
        return PageLoad(self._document_status, time.monotonic() - started, self._page.url)

    def _remember_cookies(self) -> None:
        """Запомнить куки: после выхода их значений уже не спросить, а вычистить их надо."""
        with contextlib.suppress(PlaywrightError):
            for cookie in self._context.cookies():
                if cookie["value"]:
                    self.cookies_seen.add(f"{cookie['name']}={cookie['value']}")

    def wait(self, ms: int) -> None:
        """Подождать шагами, не останавливая перехват: `time.sleep` повесил бы обработчики.

        Raises:
            Stopped: нажат «Стоп».
            BrowserCrashed: браузер упал.
        """
        left = ms
        while left > 0:
            self._check()
            step = min(left, STEP_MS)
            self._page.wait_for_timeout(step)
            left -= step

    def _frame(self, number: int):
        """Кадр по номеру из `elements`."""
        return self._page.frames[number]

    def elements(self) -> list[ElementInfo]:
        """Видимые элементы всех кадров страницы.

        Зависший скрипт камеры не вешает программу: чтение идёт с пределом и даёт пустой список.
        """
        self._check()
        found: list[ElementInfo] = []
        for number, frame in enumerate(self._page.frames):
            with contextlib.suppress(PlaywrightError):
                items = frame.locator("html").evaluate(_SCAN_SCRIPT, timeout=CALL_LIMIT_MS)
                for item in items:
                    trimmed = {
                        key: value[:TEXT_LIMIT]
                        if isinstance(value, str) and key != "path"
                        else value
                        for key, value in item.items()
                    }
                    found.append(ElementInfo(frame=number, **trimmed))
        return found

    def activity(self) -> tuple[int, int, int, str]:
        """Отметка активности страницы: изменилась — значит, что-то произошло."""
        return len(self._seen_requests), len(self.stopped), len(self.dialogs), self._page.url

    def errors_since(self, mark: int, quiet_s: float) -> list[str]:
        """Ошибки страницы после отметки, кроме вызванных нашей остановкой запроса.

        Страница падает, когда запрос оборвали, поэтому ошибка в течение `quiet_s` после остановки
        не считается дефектом камеры. Сообщения браузера о загрузке ресурсов не считаются тоже:
        настоящие ответы камеры с ошибкой лежат в `bad_answers`.

        Args:
            mark: длина `page_errors` до шага.
            quiet_s: сколько секунд после остановки ошибки не считаются.

        Returns:
            Тексты ошибок.
        """
        found = []
        for text, moment in zip(self.page_errors[mark:], self._error_times[mark:], strict=True):
            if any(part in text for part in BROWSER_LOAD_MESSAGES):
                continue
            if not any(0 <= moment - stop.at <= quiet_s for stop in self.stopped):
                found.append(text)
        return found

    def page_text(self) -> str:
        """Текст страницы: сообщения об отказе есть только в нём."""
        self._check()
        try:
            return self._page.locator("body").inner_text(timeout=CALL_LIMIT_MS)
        except PlaywrightError:
            return ""

    def password_fields(self) -> int:
        """Сколько видимых полей пароля на странице: вход и его смена различаются этим числом."""
        return sum(item.kind == "password" for item in self.elements())

    def _locator(self, element: ElementInfo):
        return self._frame(element.frame).locator(element.path).first

    def click(self, element: ElementInfo, wait_ms: int = 1000) -> Reaction:
        """Нажать элемент и собрать реакцию за `wait_ms`.

        Args:
            element: элемент из `elements`.
            wait_ms: сколько ждать реакцию, мс.

        Returns:
            Запросы, остановленные запросы, смена адреса, текст диалога, время.
        """
        self._check()
        address = self._page.url
        seen, stopped, dialogs = (
            len(self._seen_requests),
            len(self.stopped),
            len(self.dialogs),
        )
        started = time.monotonic()
        with contextlib.suppress(PlaywrightError):
            self._locator(element).click(timeout=CALL_LIMIT_MS)
        self.wait(wait_ms)
        return Reaction(
            requests=self._seen_requests[seen:],
            stopped=self.stopped[stopped:],
            navigated=self._page.url != address,
            dialog=" ".join(self.dialogs[dialogs:]),
            elapsed_s=time.monotonic() - started,
        )

    def set_field(self, element: ElementInfo, value: str) -> None:
        """Записать значение в поле: только в браузере, до камеры оно идёт нажатием."""
        self._check()
        locator = self._locator(element)
        if element.tag == "select":
            locator.select_option(value, timeout=CALL_LIMIT_MS)
        elif element.kind in ("checkbox", "radio"):
            locator.set_checked(value == "true", timeout=CALL_LIMIT_MS)
        elif element.kind == "range":
            # fill у ползунка не шлёт input и change так, как их ждут страницы
            locator.evaluate(
                "(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true}));"
                " e.dispatchEvent(new Event('change', {bubbles: true})); }",
                value,
                timeout=CALL_LIMIT_MS,
            )
        else:
            locator.fill(value, timeout=CALL_LIMIT_MS)

    def field_options(self, element: ElementInfo) -> list[str]:
        """Значения доступных вариантов списка."""
        self._check()
        return self._locator(element).evaluate(
            "e => [...e.options].filter(o => !o.disabled).map(o => o.value)",
            timeout=CALL_LIMIT_MS,
        )

    def field_limits(self, element: ElementInfo) -> tuple[float, float, float]:
        """Пределы и шаг числового поля; нет атрибута — без предела и шаг 1."""
        self._check()
        low, high, step = self._locator(element).evaluate(
            "e => [e.min, e.max, e.step]", timeout=CALL_LIMIT_MS
        )

        def number(text: str, default: float) -> float:
            try:
                return float(text)
            except ValueError:
                return default

        return number(low, -math.inf), number(high, math.inf), number(step, 1.0) or 1.0

    def read_field(self, element: ElementInfo) -> str:
        """Значение поля текстом."""
        self._check()
        locator = self._locator(element)
        if element.kind in ("checkbox", "radio"):
            return "true" if locator.is_checked(timeout=CALL_LIMIT_MS) else "false"
        return locator.input_value(timeout=CALL_LIMIT_MS)

    def screenshot(self) -> bytes | None:
        """Снимок с закрытыми полями пароля; во время входа и на запретных страницах снимка нет."""
        self._check()
        if self._window is Window.LOGIN or not self.screens_allowed:
            return None
        try:
            mask = [self._page.locator("input[type=password]")]
            return self._page.screenshot(mask=mask, timeout=CALL_LIMIT_MS * 3)
        except PlaywrightError:
            return None

    def session_state(self) -> dict:
        """Куки сессии только в памяти: для новой сессии после падения браузера, на диск нельзя."""
        return self._context.storage_state()

    def finishing(self) -> None:
        """Дать завершить вход после «Стоп»: дальше шаги ожидания «Стоп» не проверяют."""
        self._stop_requested = lambda: False

    def close(self) -> None:
        """Закрыть браузер и драйвер; повторный вызов безопасен."""
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):
                    closer.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = self._browser = self._page = self._playwright = None
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = ""
