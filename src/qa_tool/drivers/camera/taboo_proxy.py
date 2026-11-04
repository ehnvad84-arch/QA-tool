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

    `start()` — до первого запроса, `stop()` — после модуля. Потокобезопасен: список
    остановленных запросов `blocked` пополняется под блокировкой.
    """

    camera_address: str
    camera_port: int = 80
    timeout_s: float = DEFAULT_TIMEOUT_S
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
            return f"http://{self.camera_address}:{match.group(1)}{match.group(2) or '/'}{query}"
        return f"http://{self.camera_address}:{self.camera_port}{path}"

    def rewrite(self, text: str) -> str:
        """Заменить в ответе камеры адреса HTTP её служб адресом посредника.

        Адреса камеры (любой порт) ведут на `/_port/<порт>/…` посредника — везде в ответе:
        адрес камеры не бывает пространством имён XML. Адреса чужих хостов подменяются на
        `/_foreign/…` (там посредник отказывает) только внутри полей адресов служб — XAddr,
        XAddrs, Address, Uri: в остальных местах это пространства имён и ссылки на спецификации.
        RTSP и прочие схемы не трогаются.
        """

        def to_proxy(match: re.Match) -> str:
            scheme, host, port, path = (
                match.group(1),
                match.group(2),
                match.group(3),
                match.group(4),
            )
            if host.lower() == self.camera_address.lower():
                target = int(port) if port else (443 if scheme.lower() == "https" else 80)
                return f"{self.base_url}/_port/{target}{path or ''}"
            return match.group(0)

        def foreign_in_field(match: re.Match) -> str:
            def to_foreign(url: re.Match) -> str:
                if url.group(0).startswith(self.base_url):
                    return url.group(0)
                return f"{self.base_url}{_FOREIGN}/{url.group(2)}{url.group(4) or ''}"

            return match.group(1) + _HTTP_URL.sub(to_foreign, match.group(3)) + match.group(4)

        text = _HTTP_URL.sub(to_proxy, text)
        return _ADDRESS_FIELD.sub(foreign_in_field, text)

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
                    content = proxy.rewrite(content.decode(charset, "replace")).encode(charset)
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


# http(s)://хост[:порт][/путь] — хост без пользователя и пароля в адресе; схема в адресе
# без учёта регистра (RFC 3986 §3.1): «HTTP://камера/…» иначе ушёл бы к камере мимо табу
_HTTP_URL = re.compile(
    r"\b(https?)://([A-Za-z0-9.\-]+|\[[0-9a-fA-F:]+\])(?::(\d{1,5}))?(/[^\s<\"']*)?",
    re.IGNORECASE,
)
# поля, где ONVIF передаёт адреса служб: <tt:XAddr>, <d:XAddrs>, <wsa:Address>, <tt:Uri>
_ADDRESS_FIELD = re.compile(r"(<(?:[\w.-]+:)?(XAddrs?|Address|Uri)\b[^>]*>)([^<]*)(</)")
