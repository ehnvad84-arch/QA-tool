"""Виртуальный веб-интерфейс камеры для тестов веб-обхода.

Главное здесь — свидетель: `arrived` записывает всё, что реально дошло до сервера. Тесты судят по
нему, а не по журналу программы: журнал показывает, что программа думает, а не что получила камера.
"""

import base64
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


LOGIN_PAGE = """<h1>Вход</h1><form onsubmit="event.preventDefault(); send()">
<input id="u" name="username" placeholder="Логин"><input id="p" type="password" name="password">
<button type="submit">Войти</button></form><div id="m"></div><script>
function send() {
  fetch('/api/auth/login', {method: 'POST', body: JSON.stringify({username: u.value, password: p.value})})
    .then((r) => { if (r.ok) location.reload(); else m.innerText = r.status === 409 ?
      'Слишком много подключений' : 'Неверный логин или пароль'; });
}
</script>"""
CHANGE_PAGE = """<h1>Смена пароля</h1><form><input id="u" name="username">
<input type="password" name="password"><input type="password" name="confirm">
<button type="submit">Сохранить</button></form>"""


class MockWebCamera:
    """Сервер страниц на 127.0.0.1; страницы задаются словарём «путь → HTML»."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        redirects: dict[str, tuple[int, str]] | None = None,
        auth: str = "",
        password: str = "secret",
        sessions_limit: int = 3,
        must_change_password: bool = False,
    ) -> None:
        """Запускается в `__enter__`.

        Args:
            pages: страницы; адреса вне словаря отвечают 404, запросы записи — 200.
            redirects: перенаправления «путь → (код, адрес)».
            auth: вид входа: «form»; пусто — вход не нужен.
            password: верный пароль; логин всегда admin.
            sessions_limit: сколько входов камера держит одновременно.
            must_change_password: вместо входа показывать смену пароля.
        """
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.arrived: list[tuple[str, str]] = []
        self.ws_messages: list[str] = []
        self.auth = auth
        self.password = password
        self.sessions_limit = sessions_limit
        self.must_change_password = must_change_password
        self.login_attempts = 0
        self.sessions = 0
        self.bodies: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "MockWebCamera":
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode(errors="replace") if length else ""
                if body:
                    camera.bodies.append(body)
                camera.arrived.append((self.command, self.path))
                path = self.path.split("?")[0]
                if camera.auth and self._session_answered(path, body):
                    return
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

            def _send(self, code: int, text: str, headers: dict[str, str] | None = None) -> None:
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def _logged_in(self) -> bool:
                return "sid=ok" in self.headers.get("Cookie", "")

            def _session_answered(self, path: str, body: str) -> bool:
                """Вход, выход и страница входа; True — ответ уже отправлен."""
                if path == "/api/auth/login" and self.command == "POST":
                    camera.login_attempts += 1
                    if camera.sessions >= camera.sessions_limit:
                        self._send(409, '{"maxConnectionsAllowed": 3}')
                    elif json.loads(body or "{}").get("password") == camera.password:
                        camera.sessions += 1
                        self._send(200, "{}", {"Set-Cookie": "sid=ok; Path=/"})
                    else:
                        self._send(401, "{}")
                    return True
                if path == "/api/auth/logout" and self.command == "POST":
                    camera.sessions = max(0, camera.sessions - 1)
                    self._send(200, "{}", {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
                    return True
                if camera.must_change_password and path == "/":
                    self._send(200, CHANGE_PAGE)
                    return True
                if not self._logged_in() and path == "/":
                    self._send(200, LOGIN_PAGE)
                    return True
                return False

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
