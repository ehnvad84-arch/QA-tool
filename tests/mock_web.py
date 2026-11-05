"""Виртуальный веб-интерфейс камеры для тестов веб-обхода.

Главное здесь — свидетель: `arrived` записывает всё, что реально дошло до сервера. Тесты судят по
нему, а не по журналу программы: журнал показывает, что программа думает, а не что получила камера.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockWebCamera:
    """Сервер страниц на 127.0.0.1; страницы задаются словарём «путь → HTML»."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        """Запускается в `__enter__`.

        Args:
            pages: страницы; адреса вне словаря отвечают 404, запросы записи — 200.
        """
        self.pages = pages or {}
        self.arrived: list[tuple[str, str]] = []
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
                page = camera.pages.get(self.path.split("?")[0])
                if self.command == "GET" and page is None:
                    self.send_error(404)
                    return
                body = (page or "{}").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
