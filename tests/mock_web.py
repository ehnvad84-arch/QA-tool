"""Виртуальный веб-интерфейс камеры для тестов веб-обхода.

Главное здесь — свидетель: `arrived` записывает всё, что реально дошло до сервера. Тесты судят по
нему, а не по журналу программы: журнал показывает, что программа думает, а не что получила камера.
"""

import base64
import hashlib
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MockWebCamera:
    """Сервер страниц на 127.0.0.1; страницы задаются словарём «путь → HTML»."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        redirects: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        """Запускается в `__enter__`.

        Args:
            pages: страницы; адреса вне словаря отвечают 404, запросы записи — 200.
            redirects: перенаправления «путь → (код, адрес)».
        """
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.arrived: list[tuple[str, str]] = []
        self.ws_messages: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "MockWebCamera":
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                camera.arrived.append((self.command, self.path))
                path = self.path.split("?")[0]
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self._websocket()
                    return
                if path in camera.redirects:
                    code, target = camera.redirects[path]
                    self.send_response(code)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                page = camera.pages.get(path)
                if self.command == "GET" and page is None:
                    self.send_error(404)
                    return
                body = (page or "{}").encode()
                self.send_response(200)
                kind = "text/javascript" if path.endswith(".js") else "text/html; charset=utf-8"
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _websocket(self) -> None:
                """Минимальный WebSocket: принять рукопожатие, записывать текст от страницы."""
                key = self.headers["Sec-WebSocket-Key"] + WS_GUID
                accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()  # noqa: S324
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.close_connection = True
                while True:
                    head = self.rfile.read(2)
                    if len(head) < 2 or head[0] & 0x0F == 8:
                        return
                    size = head[1] & 0x7F
                    if size == 126:
                        size = struct.unpack(">H", self.rfile.read(2))[0]
                    mask = self.rfile.read(4)
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(size)))
                    camera.ws_messages.append(data.decode(errors="replace"))

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        """Адрес интерфейса."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def writes(self) -> list[tuple[str, str]]:
        """Дошедшие запросы, кроме чтения."""
        return [item for item in self.arrived if item[0] not in ("GET", "HEAD", "OPTIONS")]
