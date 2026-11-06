"""Посредник табу для сторонних клиентов камеры (AD-7): onvif-tt сейчас, браузер на этапе 5.

Сторонний клиент ходит не к камере, а на 127.0.0.1:<порт посредника>. Посредник пересылает
каждый запрос через `drivers.camera.http.request` — там проверяется табу, запрещённое не уходит
в сеть, а клиент получает SOAP Fault `ter:QAToolTaboo`. В ответах камеры адреса её служб
(XAddr в GetServices и GetCapabilities, адреса подписок SubscriptionReference, адреса снимков)
заменяются адресом посредника: иначе клиент пошёл бы по ним к камере напрямую, мимо табу.
Порт службы на камере сохраняется в пути (`/_port/<порт>/…`), поэтому службы на других
портах тоже идут через посредника. Адрес на чужом хосте — не камеры со стенда — посредник
не пересылает (security.md: «только адреса из списка стенда»).

RTSP (`rtsp://…`) не подменяется: чтение потока идёт к камере напрямую.

Посредник живёт на время модуля проверок одной камеры; потоки — по одному на запрос.
"""

import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

from qa_tool.core.taboo import TabooViolation
from qa_tool.drivers.camera import http

# PullMessages в onvif-tt ждёт события до 60 с (Timeout PT60S), поэтому ответ камеры ждём дольше
DEFAULT_TIMEOUT_S = 75.0
# заголовки, которые нельзя пересылать как есть: их выставляет транспорт
_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive"}
_PORT_PREFIX = re.compile(r"^/_port/(\d{1,5})(/.*)?$")
_FOREIGN = "/_foreign"
TABOO_FAULT_CODE = "ter:QAToolTaboo"


def soap_fault(code: str, reason: str) -> bytes:
    """SOAP 1.2 Fault для ответа стороннему клиенту."""
    reason = reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault>'
        "<s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
        f"<s:Value>{code}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="ru">{reason}</s:Text></s:Reason>'
        "</s:Fault></s:Body></s:Envelope>"
    ).encode()


@dataclass
class Blocked:
    """Запрос, который посредник не отправил камере."""

    method: str
    path: str
    reason: str


@dataclass
class TabooProxy:
    """Посредник табу для одной камеры.

    Args:
        camera_address: адрес камеры со стенда (IP или имя).
        camera_port: HTTP-порт камеры по умолчанию (ONVIF Device Service).
        timeout_s: сколько ждать ответа камеры на один запрос, с.
        lan_address: адрес камеры в её сети, если стенд ходит через туннель (`Camera.lan_address`).

    `start()` — до первого запроса, `stop()` — после модуля. Потокобезопасен: список
    остановленных запросов `blocked` пополняется под блокировкой.
    """

    camera_address: str
    camera_port: int = 80
    timeout_s: float = DEFAULT_TIMEOUT_S
    lan_address: str = ""
    blocked: list[Blocked] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _server: ThreadingHTTPServer | None = field(default=None, repr=False)

    @property
    def port(self) -> int:
        """Порт посредника на 127.0.0.1; известен только после `start()`."""
        assert self._server is not None, "посредник не запущен"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """Адрес посредника, который получает сторонний клиент вместо адреса камеры."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "TabooProxy":
        """Поднять посредника на 127.0.0.1 и случайном свободном порту."""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, args=(0.1,), daemon=True).start()
        return self

    def stop(self) -> None:
        """Остановить посредника; повторный вызов ничего не делает."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # --- адреса -------------------------------------------------------------------------------

    def camera_url(self, path: str) -> str | None:
        """Адрес на камере для пути запроса к посреднику; None — адрес не камеры (чужой хост)."""
        # путь без «/» в начале склеился бы с адресом камеры в чужой хост («@10.9.9.9/x»)
        if path.startswith(_FOREIGN) or not path.startswith("/"):
            return None
        match = _PORT_PREFIX.match(path.split("?", 1)[0])
        if match:
            query = path[len(path.split("?", 1)[0]) :]
            # за туннелем порт из ответа камеры выбирал бы службу на компьютере стенда
            port = self.camera_port if self.lan_address else match.group(1)
            return f"http://{self.camera_address}:{port}{match.group(2) or '/'}{query}"
        return f"http://{self.camera_address}:{self.camera_port}{path}"

    def rewrite(self, text: str) -> str:
        """Заменить в ответе камеры адреса HTTP её служб адресом посредника.

        Адреса камеры (любой порт) ведут на `/_port/<порт>/…` посредника — везде в ответе:
        адрес камеры не бывает пространством имён XML. Адреса чужих хостов подменяются на
        `/_foreign/…` (там посредник отказывает) только внутри полей адресов служб — XAddr,
        XAddrs, Address, Uri: в остальных местах это пространства имён и ссылки на спецификации.
        RTSP и прочие схемы не трогаются.
        """

        def foreign_in_field(match: re.Match) -> str:
            return match.group(1) + _HTTP_URL.sub(self._to_foreign, match.group(3)) + match.group(4)

        def hidden_in_field(match: re.Match) -> str:
            return f"{match.group(1)}{self.base_url}{_FOREIGN}/{match.group(3)}"

        # Клиент прочтёт такой адрес после разбора XML, а подмена по тексту его не видит:
        # он ушёл бы мимо посредника с логином. У настоящих служб так не бывает, поэтому отказ.
        text = _HTTP_URL.sub(self._to_proxy, text)
        text = _HIDDEN_ADDRESS_FIELD.sub(hidden_in_field, text)
        return _ADDRESS_FIELD.sub(foreign_in_field, text)

    def rewrite_location(self, location: str) -> str:
        """Заменить адрес перенаправления: это всегда адрес, поэтому чужой хост — на отказ.

        `//хост/путь` клиент достраивает схемой своего запроса и уходит мимо посредника,
        поэтому такой адрес сначала приводится к полному. Всё абсолютное, что не стало адресом
        посредника, ведёт на отказ: лучше потерять перенаправление, чем выпустить запрос.
        """
        location = location.strip()
        if location.startswith("//"):
            location = f"http:{location}"
        try:
            rewritten = _HTTP_URL.sub(self._to_foreign, _HTTP_URL.sub(self._to_proxy, location))
            parts = urlsplit(rewritten)
        except ValueError:  # неразбираемый адрес вроде http://[1:2]/
            return f"{self.base_url}{_FOREIGN}/"
        if (parts.scheme or parts.netloc) and not self._is_proxy_url(rewritten):
            return f"{self.base_url}{_FOREIGN}/"
        return rewritten

    def _is_proxy_url(self, url: str) -> bool:
        # не startswith: «http://посредник@чужой-хост/» начинается с адреса посредника
        try:
            return urlsplit(url).netloc == urlsplit(self.base_url).netloc
        except ValueError:
            return False

    def _to_proxy(self, match: re.Match) -> str:
        scheme, host, port, path = match.group(1), match.group(2), match.group(3), match.group(4)
        if self.lan_address and host.lower() in (
            self.camera_address.lower(),
            self.lan_address.lower(),
        ):
            # за туннелем доступен один порт, а порты из ответа камеры — её внутренние
            return f"{self.base_url}{path or ''}"
        if host.lower() == self.camera_address.lower():
            target = int(port) if port else (443 if scheme.lower() == "https" else 80)
            return f"{self.base_url}/_port/{target}{path or ''}"
        return match.group(0)

    def _to_foreign(self, url: re.Match) -> str:
        if self._is_proxy_url(url.group(0)):
            return url.group(0)
        host = quote(url.group(2), safe="[]:.-_")  # заголовок Location — только ASCII
        return f"{self.base_url}{_FOREIGN}/{host}{url.group(4) or ''}"

    # --- обработка запросов -------------------------------------------------------------------

    def _block(self, method: str, path: str, reason: str) -> None:
        with self._lock:
            self.blocked.append(Blocked(method, path, reason))

    def _handler(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = proxy.camera_url(self.path)
                if url is None:
                    reason = "адрес службы указывает на чужой хост — не на камеру со стенда"
                    proxy._block(self.command, self.path, reason)
                    self._reply(502, soap_fault("ter:QAToolForeignHost", reason))
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS}
                try:
                    response = http.request(
                        self.command, url, content=body, headers=headers, timeout=proxy.timeout_s
                    )
                except TabooViolation as e:
                    proxy._block(self.command, self.path, str(e))
                    self._reply(400, soap_fault(TABOO_FAULT_CODE, str(e)))
                    return
                except http.TransportError as e:
                    self._reply(504, soap_fault("ter:QAToolNoAnswer", f"камера не ответила: {e}"))
                    return
                content = response.content
                content_type = response.headers.get("content-type", "")
                if any(t in content_type for t in ("xml", "soap", "json", "html", "text")):
                    charset = response.encoding or "utf-8"
                    # U+FFFD от битых байт однобайтовая кодировка обратно не примет
                    text = proxy.rewrite(content.decode(charset, "replace"))
                    content = text.encode(charset, "replace")
                if "location" in response.headers:
                    location = response.headers["location"]
                    response.headers["location"] = proxy.rewrite_location(location)
                self._reply(response.status_code, content, response.headers)

            def _reply(self, status: int, content: bytes, headers=None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    if key.lower() not in _HOP_HEADERS | {"content-encoding"}:
                        self.send_header(key, value)
                if headers is None:
                    self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _forward  # noqa: N815

            def log_message(self, *args):
                pass  # журнал запросов ведёт onvif-tt (last_request) и список blocked

        return Handler


# http(s)://[кто@]хост[:порт][/путь]; схема без учёта регистра (RFC 3986 §3.1): «HTTP://камера/…»
# иначе ушёл бы к камере мимо табу. Хост — любые символы, кроме разделителей текста вокруг адреса.
_HTTP_URL = re.compile(
    # пустая или любая часть до «@» — клиент её отбрасывает и идёт на хост после неё
    r"\b(https?)://(?:[^/@\s<>\"']*@)?([^/:?#@\s<>\"'\[\](),;]+|\[[0-9a-fA-F:]+\])(?::(\d{1,5}))?"
    r"(/[^\s<\"']*)?",
    re.IGNORECASE,
)
# поля, где ONVIF передаёт адреса служб: <tt:XAddr>, <d:XAddrs>, <wsa:Address>, <tt:Uri>
_ADDRESS_FIELD = re.compile(r"(<(?:[\w.-]+:)?(XAddrs?|Address|Uri)\b[^>]*>)([^<]*)(</)")
# то же поле, но адрес спрятан от подмены: CDATA, комментарий или числовая ссылка на символ
_HIDDEN_ADDRESS_FIELD = re.compile(
    r"(<(?:[\w.-]+:)?(XAddrs?|Address|Uri)\b[^>]*>)"
    r"(?:\s*<!\[CDATA\[.*?\]\]>|\s*<!--.*?-->[^<]*|[^<]*&#[^<]*)(</)",
    re.DOTALL,
)
