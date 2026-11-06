"""Виртуальный веб-интерфейс камеры для тестов веб-обхода.

Главное здесь — свидетель: `arrived` записывает всё, что реально дошло до сервера. Тесты судят по
нему, а не по журналу программы: журнал показывает, что программа думает, а не что получила камера.
"""

import base64
import hashlib
import json
import re
import ssl
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIGEST_NONCE = "mock-nonce"
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
        tls_dir: Path | None = None,
        settings: dict[str, str] | None = None,
    ) -> None:
        """Запускается в `__enter__`.

        Args:
            pages: страницы; адреса вне словаря отвечают 404, запросы записи — 200.
            redirects: перенаправления «путь → (код, адрес)».
            auth: вид входа: «form», «basic», «digest»; пусто — вход не нужен.
            password: верный пароль; логин всегда admin.
            sessions_limit: сколько входов камера держит одновременно.
            must_change_password: вместо входа показывать смену пароля.
            tls_dir: папка для самоподписанного сертификата: сервер работает по HTTPS.
            settings: настройки камеры: подставляются в страницы вместо `{{имя}}`, меняются
                запросом POST /api/settings с телом JSON.
        """
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.arrived: list[tuple[str, str]] = []
        self.ws_messages: list[str] = []
        self.auth = auth
        self.password = password
        self.sessions_limit = sessions_limit
        self.must_change_password = must_change_password
        self.tls_dir = tls_dir
        self.login_attempts = 0
        self.sessions = 0
        self.bodies: list[str] = []
        self.settings = dict(settings or {})
        self.saves_ignored = 0  # столько сохранений камера отвечает «ок» и не применяет
        self.ignore_all_saves_after = 0  # после стольких сохранений перестаёт применять их
        self.saves = 0
        self.http_login_seen = False
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
                if camera.auth in ("basic", "digest") and not self._http_auth_passed():
                    return
                if camera.auth == "form" and self._session_answered(path, body):
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
                if path == "/api/settings" and self.command == "POST":
                    camera.save(json.loads(body or "{}"))
                page = camera.pages.get(path)
                for name, value in camera.settings.items():
                    page = page.replace("{{" + name + "}}", value) if page else page
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

            def _http_auth_passed(self) -> bool:
                """Basic или Digest; False — отказ уже отправлен."""
                given = self.headers.get("Authorization", "")
                if given and self._authorized(given):
                    camera.login_attempts += not camera.http_login_seen
                    camera.http_login_seen = True
                    return True
                # каждый неверный ответ считается: на настоящей камере это расход счётчика попыток
                camera.login_attempts += bool(given)
                self._send(401, "нет доступа", {"WWW-Authenticate": camera.challenge()})
                return False

            def _authorized(self, given: str) -> bool:
                if camera.auth == "basic":
                    token = base64.b64encode(f"admin:{camera.password}".encode()).decode()
                    return given == f"Basic {token}"
                return camera.digest_valid(given, self.command)

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
        if self.tls_dir is not None:
            self._server.socket = self._tls().wrap_socket(self._server.socket, server_side=True)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def save(self, values: dict[str, str]) -> None:
        """Применить настройки, если камера сейчас их принимает."""
        self.saves += 1
        if self.saves_ignored > 0:
            self.saves_ignored -= 1
        elif not self.ignore_all_saves_after or self.saves <= self.ignore_all_saves_after:
            self.settings.update({k: str(v) for k, v in values.items()})

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _tls(self) -> ssl.SSLContext:
        """Самоподписанный сертификат создаётся при запуске: ключ не лежит в репозитории."""
        key, cert = self.tls_dir / "key.pem", self.tls_dir / "cert.pem"
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=camera", "-keyout", str(key), "-out", str(cert),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context

    @property
    def url(self) -> str:
        """Адрес интерфейса."""
        scheme = "https" if self.tls_dir is not None else "http"
        return f"{scheme}://127.0.0.1:{self._server.server_address[1]}"

    def challenge(self) -> str:
        """Заголовок `WWW-Authenticate` для выбранного вида входа."""
        if self.auth == "basic":
            return 'Basic realm="camera"'
        return f'Digest realm="camera", nonce="{DIGEST_NONCE}", qop="auth"'

    def digest_valid(self, header: str, method: str) -> bool:
        """Верен ли ответ Digest; считается независимо от кода программы."""
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header))

        def md5(text: str) -> str:
            return hashlib.md5(text.encode()).hexdigest()  # noqa: S324

        ha1 = md5(f"admin:camera:{self.password}")
        ha2 = md5(f"{method}:{fields.get('uri', '')}")
        expected = md5(
            f"{ha1}:{DIGEST_NONCE}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:auth:{ha2}"
        )
        return fields.get("response") == expected

    def writes(self) -> list[tuple[str, str]]:
        """Дошедшие запросы, кроме чтения."""
        return [item for item in self.arrived if item[0] not in ("GET", "HEAD", "OPTIONS")]
